"""The `dubber` command: `dubber serve` runs the orchestrator, `dubber stats` summarizes a run,
`dubber voice` shows or changes the voice."""

import argparse
import asyncio
import dataclasses
import errno
import logging
import logging.handlers
import pathlib
import sys
from collections.abc import Callable

from dubber.config import AUTO_VOICE, VOICE_CHOICES, Config, load_config, set_voice
from dubber.engines.base import (
    IdentityTranslator,
    Translator,
    TranslatorError,
    Voice,
    VoiceError,
    failure_reason,
)
from dubber.engines.translate_llamacpp import LlamaCppTranslator, translator_from_config
from dubber.engines.voice_by_presenter import PresenterVoices
from dubber.engines.voice_say import SayVoice
from dubber.engines.voice_pc import PcVoice
from dubber.engines.voices import voice_from_name
from dubber.metrics import MetricsLog, format_stats, stats
from dubber.playback import AudioOut
from dubber.presenter import Presenters, YouTube
from dubber.server import Send, SessionFactory, serve
from dubber.session import Session
from dubber.warmup import Warmer


log = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# The log file starts anew at this size, and this many old files are kept beside it.
_LOG_BYTES, _LOG_BACKUPS = 2_000_000, 3


def main(argv: list[str] | None = None) -> int:
    """Run the command `argv` names (default: the command line); return the exit status."""
    args = _parser().parse_args(argv)
    return args.run(args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dubber", description="Dub English YouTube videos into Turkish."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve_parser = commands.add_parser("serve", help="listen for the browser extension")
    serve_parser.add_argument(
        "--config", type=pathlib.Path, help="a TOML settings file (default: the built-in settings)"
    )
    serve_parser.add_argument(
        "--verbose", action="store_true", help="log every message, not only the main ones"
    )
    serve_parser.set_defaults(run=_serve)

    default_metrics = Config().metrics_path
    stats_parser = commands.add_parser("stats", help="summarize one session of the metrics file")
    stats_parser.add_argument(
        "--metrics", type=pathlib.Path, default=default_metrics,
        help=f"the metrics file (default: {default_metrics})",
    )
    stats_parser.add_argument(
        "--session", help="the session to summarize (default: the last one in the file)"
    )
    stats_parser.set_defaults(run=_stats)

    voice_parser = commands.add_parser("voice", help="show the voice, or change it in the settings file")
    voice_parser.add_argument(
        "name", nargs="?", choices=sorted(VOICE_CHOICES), help="the voice to use (default: show the current one)"
    )
    voice_parser.add_argument(
        "--config", type=pathlib.Path, default=pathlib.Path("config.toml"),
        help="the TOML settings file (default: config.toml)",
    )
    voice_parser.set_defaults(run=_voice)
    return parser


def _serve(args: argparse.Namespace) -> int:
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt="%H:%M:%S")
    # The server logs the connections itself; the library's own lines would repeat them.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # httpx logs every request at INFO, and the PC translator makes two for each sentence.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as exc:
        return _fail("config", exc)
    # The engines are built once, here, so that a setting that cannot work stops the
    # server before it listens and not on the first sentence of the first video.
    try:
        translator = _build_translator(cfg)
        voice = _build_voice(cfg, translator)
        _check_pc(translator, cfg.pc.base_url)
    except ValueError as exc:
        return _fail("config", exc)
    except _EngineError as exc:
        return _fail("engine", exc)
    handler = None
    if cfg.log_path:
        try:
            handler = _log_file(cfg.log_path, level)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            return _fail("config", OSError(f"cannot write the log file {cfg.log_path}: {reason}"))
    # The package's own level, so that its lines reach the file whatever the root logger was left at.
    root, package = logging.getLogger(), logging.getLogger("dubber")
    before = package.level
    package.setLevel(level)
    if handler is not None:
        root.addHandler(handler)
    try:
        return asyncio.run(_run(cfg, translator, voice, _to_file_only(handler)))
    except KeyboardInterrupt:
        return 0  # Ctrl-C: asyncio.run has closed the server and its connections first
    finally:
        package.setLevel(before)
        if handler is not None:
            root.removeHandler(handler)
            handler.close()


def _log_file(path: str, level: int) -> logging.Handler:
    """The handler that writes the log to `path`, each line with its date.

    The file's directory is made when it is missing. At 2 MB the file is put aside and a new
    one begun, and three old ones are kept. Raises OSError when the file cannot be opened.
    """
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    return handler


def _to_file_only(handler: logging.Handler | None) -> Callable[[str], None]:
    """A function that writes a line to the log file alone: what `serve` prints is not logged twice on the screen."""

    def note(line: str) -> None:
        if handler is not None:
            handler.handle(log.makeRecord(log.name, logging.INFO, __file__, 0, line, (), None))

    return note


def _fail(kind: str, exc: Exception) -> int:
    """Print `exc` as the one stderr line `<kind> error: ...`; return the exit status 2."""
    print(f"{kind} error: {' '.join(str(exc).split())}", file=sys.stderr)
    return 2


class _EngineError(Exception):
    """An engine that the settings name cannot start; the message says why."""


def _build_translator(cfg: Config) -> Translator:
    """The translator `translator.engine` names: "identity" (the English stand-in) or "pc".

    The PC translator asks the PC which model is loaded before every sentence and
    requests that one, so it does not make the PC swap models. When none is loaded it
    requests `pc.preferred_model`, which the PC then loads. Nothing is sent to the PC
    here (see _check_pc). The key file is read, and the file is named in an error but
    its contents never are. Any other engine name is a ValueError, and a key file that
    cannot be read, or does not hold the key on one printable line, an _EngineError.
    """
    engine = cfg.translator.engine
    if engine == "identity":
        return IdentityTranslator()
    if engine != "pc":
        raise ValueError(f'translator.engine must be "identity" or "pc", not {engine!r}')
    _check_key_file(pathlib.Path(cfg.pc.api_key_file).expanduser())
    return translator_from_config({"pc": dataclasses.asdict(cfg.pc)})


def _check_key_file(path: pathlib.Path) -> None:
    """Refuse a PC key file that cannot be read, or that is not the key on one printable line.

    The key goes into an HTTP header. A key with a line break, a control character or a
    character beyond ASCII ends up in an error text that quotes it (httpx: "Illegal header
    value b'Bearer ...'"), which the session logs and shows on the YouTube page. So such
    a file is refused here. Only the path is ever named: no message holds any of the file.
    Blanks around the key and a line end after it are fine.
    """
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise _EngineError(
            f"cannot read the PC key file {path}: {exc.strerror or type(exc).__name__}"
        ) from None
    except UnicodeDecodeError:
        raise _EngineError(f"the PC key file {path} is not text") from None
    if not key or not all(" " <= char <= "~" for char in key):
        raise _EngineError(f"the PC key file {path} must hold the key on one line")


def _check_pc(translator: Translator, base_url: str) -> None:
    """Ask the PC for its list of models, once, so that `serve` does not start against a PC it cannot reach.

    That is one read-only request, and never a translation. A PC that is off, or a
    Tailscale that is down on either machine, is found now. Left to the first sentence,
    every sentence would wait for the PC and the video would play on undubbed. Does
    nothing for the stand-in translator. Raises _EngineError when the list cannot be had.
    """
    if not isinstance(translator, LlamaCppTranslator):
        return
    try:
        translator.list_models()
    except TranslatorError as exc:
        raise _EngineError(f"cannot reach the PC at {base_url}: {failure_reason(exc)}") from None


def _build_voice(cfg: Config, translator: Translator | None = None) -> Voice | PresenterVoices:
    """The voice for `voice.engine` and `voice.voice`, which `voice_from_name` reads as
    "<engine>:<voice>" (just "<engine>" when `voice.voice` is empty). The engine "pc" is
    the voice service on the PC instead (see _build_pc_voices), and the voice "auto" its two
    voices for a man and a woman, picked for each video by a look of `translator`'s model
    (see _build_presenter_voices).

    A spec that names no voice is a ValueError. A `say` voice is looked up now: `say`
    speaks a name it does not know in its default voice, without a word, so a typo has to
    be found before the first sentence. A voice that is not installed, or whose name
    fits several, is an _EngineError.
    """
    engine, name = cfg.voice.engine, cfg.voice.voice
    if name == AUTO_VOICE:
        return _build_presenter_voices(cfg, translator)
    if engine == "pc":
        return _build_pc_voices(cfg, [name or "owner"])[0]
    try:
        voice = voice_from_name(f"{engine}:{name}" if name else engine)
    except ValueError as exc:
        raise ValueError(f"[voice] {exc}") from None
    if isinstance(voice, SayVoice):
        try:
            voice._resolve_voice()  # the lookup its first synthesize would make, and keeps
        except VoiceError as exc:
            raise _EngineError(str(exc)) from None
    return voice


def _build_presenter_voices(cfg: Config, translator: Translator | None) -> PresenterVoices:
    """The voices `voice.male` and `voice.female` of the PC, and the look that picks one for each video.

    The look is `translator`'s: the PC translator's model is shown what YouTube shows of the
    video (see dubber.presenter). So "auto" with another voice engine, with another
    translator, or without a name for either voice is a ValueError. Nothing is looked at here.
    """
    if cfg.voice.engine != "pc":
        raise ValueError('voice.voice = "auto" needs voice.engine = "pc": its two voices are on the PC')
    if not isinstance(translator, LlamaCppTranslator):
        raise ValueError(
            'voice.voice = "auto" needs translator.engine = "pc": that model looks who presents the video'
        )
    if not (cfg.voice.male and cfg.voice.female):
        raise ValueError('with voice.voice = "auto", voice.male and voice.female must each name a voice')
    male, female = _build_pc_voices(cfg, [cfg.voice.male, cfg.voice.female])
    return PresenterVoices(male, female, Presenters(YouTube().glimpse, translator.look).who)


def _build_pc_voices(cfg: Config, names: list[str]) -> list[PcVoice]:
    """The voices `names` of the voice service on the PC (`pc.tts_url`), asked once whether it can speak.

    The service answers with the PC key, read as for the PC translator. It must be reachable,
    have loaded its model (it compiles for about 3 minutes after it starts) and hold every
    name; otherwise the reason is an _EngineError, so that `serve` stops before it listens.
    """
    key_path = pathlib.Path(cfg.pc.api_key_file).expanduser()
    _check_key_file(key_path)
    key = key_path.read_text(encoding="utf-8").strip()
    built = [PcVoice(cfg.pc.tts_url, key, voice=name) for name in names]
    try:
        status = built[0].status()
    except VoiceError as exc:
        raise _EngineError(str(exc)) from None
    if not status.get("ready"):
        if status.get("error"):
            raise _EngineError(f"the PC voice service could not load its model: {status['error']}")
        raise _EngineError(
            f"the PC voice service at {cfg.pc.tts_url} is still loading its model "
            "(about 3 minutes after it starts); try again shortly"
        )
    voices = status.get("voices") or []
    for name in names:
        if name not in voices:
            raise _EngineError(f"the PC voice service has no voice {name!r}; it has: {', '.join(voices) or 'none'}")
    return built


def _session_factory(
    translator: Translator,
    voice: Voice | PresenterVoices,
    metrics: MetricsLog,
    on_connect: Callable[[], object] | None = None,
) -> SessionFactory:
    """The session factory of the server: every connection shares the engines and the
    metrics log, and gets a mixer of its own. A voice that goes by the presenter is each
    session's own too, since each has its own video. `on_connect` is called for each connection."""

    def make(cfg: Config, send: Send) -> Session:
        if on_connect is not None:
            on_connect()
        speaks = voice.for_session() if isinstance(voice, PresenterVoices) else voice
        return Session(translator, speaks, AudioOut(), metrics, cfg, send)

    return make


def _warmer(translator: Translator, voice: Voice | PresenterVoices) -> Warmer | None:
    """What warms the engines, when both are the PC's: a ready PC voice says the PC is in
    dubbing mode, the one mode where loading its translator is what the owner wants. Of the
    two voices that go by the presenter the man's is spoken through; they are one service."""
    speaks = voice.male if isinstance(voice, PresenterVoices) else voice
    if isinstance(translator, LlamaCppTranslator) and isinstance(speaks, PcVoice):
        return Warmer(translator, speaks)
    return None


async def _run(
    cfg: Config,
    translator: Translator,
    voice: Voice | PresenterVoices,
    note: Callable[[str], None] = lambda line: None,
) -> int:
    """Listen until cancelled, which is what Ctrl-C does; return 1 if it cannot listen.

    `note` gets the lines printed once it listens, for the log file.
    """
    host, port = cfg.server.host, cfg.server.port
    warmer = _warmer(translator, voice)
    factory = _session_factory(
        translator, voice, MetricsLog(cfg.metrics_path), warmer.kick if warmer is not None else None
    )
    try:
        server = await serve(cfg, factory)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"port {port} is in use: is another dubber serve running?", file=sys.stderr)
        else:
            print(f"cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 1
    # Printed once the port is bound, so that a port in use gives its one error line and nothing else.
    for line in (f"engines: {translator.name} + {voice.name}", f"listening on ws://{host}:{port}"):
        print(line, flush=True)
        note(line)
    if warmer is not None:
        warmer.kick()  # in the background: the model loads while the owner reaches for the button
    await server.serve_forever()  # when cancelled it closes the server and every connection
    return 0


def _voice(args: argparse.Namespace) -> int:
    """Print the voice of the settings file, after changing it when `args.name` names one."""
    try:
        if args.name:
            set_voice(args.config, *VOICE_CHOICES[args.name])
        voice = load_config(args.config).voice
    except (ValueError, OSError) as exc:
        return _fail("config", exc)
    spec = (voice.engine, voice.voice)
    name = next((name for name, choice in VOICE_CHOICES.items() if choice == spec), None)
    print(f"voice: {name} ({voice.engine}:{voice.voice})" if name else f"voice: {voice.engine}:{voice.voice}")
    if voice.voice == AUTO_VOICE:
        print(f"A video that a woman presents is read in {voice.female}, every other video in {voice.male}.")
    if voice.engine == "pc":
        print("It speaks on the PC: run pc-dublaj before dubber serve.")
    elif voice.engine == "say":
        print("It speaks on this Mac: pc-dublaj is not needed. The model loaded on the PC translates.")
    if args.name:
        print("Restart dubber serve to use it.")
    return 0


def _stats(args: argparse.Namespace) -> int:
    try:
        print(format_stats(stats(args.metrics, args.session)))
    except (ValueError, TypeError, OSError) as exc:
        print(f"stats error: {exc}", file=sys.stderr)
        return 1
    return 0
