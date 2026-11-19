"""Dub a caption file without Chrome and without sound, and say how the engines kept up.

    uv run python -m tools.dry_run CAPTIONS.json3 [--config config.toml] [--start SECONDS] [--seconds SECONDS] [--video ID]

The session, the translator and the voice are the ones `dubber serve` would use with that
config, so with the PC's engines the PC must be in dubbing mode. The player is a stand-in
that plays in real time and obeys the session's pause and play. The mixer opens no audio
device, so nothing is heard, and no metrics file is written. The run takes as long as the
video it plays, and the start-up hold before it. A caption file does not say which video it is
of: with `voice = "auto"` give the video's YouTube id with `--video`, or nothing is looked at and
the man's voice reads.

At the end it prints how many sentences were dubbed and dropped, what a second of speech
cost the translator and the voice, how far ahead of the video the dub was when the run
ended, and every hold. Since the translator and the voice work at the same time, the dub
keeps up when the larger of the two figures is under 1, with room to spare well under it.

The logic is in `run` and `Summary`, which tests/test_dry_run.py drives.
"""

import argparse
import json
import pathlib
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dubber.cli import _build_translator, _build_voice, _check_pc, _EngineError
from dubber.config import Config, load_config
from dubber.engines.base import Translator, Voice
from dubber.engines.voice_by_presenter import PresenterVoices
from dubber.metrics import MetricsLog, SegmentRecord
from dubber.playback import AudioOut
from dubber.protocol import Captions, Control, Duck, Message, Start, Status, Stop
from dubber.session import Session
from tools.fake_extension import REPORT_EVERY, FakePlayer

VIDEO_ID = "dry-run"


@dataclass
class Summary:
    """What a dry run saw. Times are seconds; the `per_second` figures are for each second of dubbed speech."""

    engines: str
    sentences: int = 0
    dubbed: int = 0
    dropped: int = 0
    shortened: int = 0
    speech: float = 0.0
    translation_per_second: float = 0.0
    voice_per_second: float = 0.0
    ahead_at_the_end: float = 0.0
    latest: float = 0.0  # the most a clip started after its sentence
    startup: float = 0.0  # the hold before the video first played
    holds: list[tuple[float, float]] = field(default_factory=list)  # (video time, seconds) of each later hold

    def lines(self, start: float, seconds: float) -> list[str]:
        held = ", ".join(f"{length:.1f} s at {at:.0f} s" for at, length in self.holds) or "none"
        return [
            f"dry run: {seconds:.0f} s of video from {start:.0f} s, engines {self.engines}",
            f"sentences {self.sentences}: dubbed {self.dubbed}, dropped {self.dropped}, shorter renderings {self.shortened}",
            f"a second of speech took the translator {self.translation_per_second:.2f} s and the voice "
            f"{self.voice_per_second:.2f} s (they work at the same time)",
            f"the dub was {self.ahead_at_the_end:.1f} s ahead at the end; the latest clip started "
            f"{self.latest:.1f} s after its sentence",
            f"start-up hold {self.startup:.1f} s; later holds: {held}",
        ]


class _Recording(MetricsLog):
    """Keeps the records and writes no file."""

    def __init__(self) -> None:
        super().__init__(pathlib.Path("unused.jsonl"))
        self.records: list[SegmentRecord] = []

    def write(self, rec: SegmentRecord) -> None:
        self.records.append(rec)


def run(
    track: dict,
    translator: Translator,
    voice: Voice,
    cfg: Config,
    start: float = 0.0,
    seconds: float = 240.0,
    out: Callable[[str], None] = print,
    video_id: str = VIDEO_ID,
) -> Summary:
    """Dub `seconds` of video from `start` on the json3 `track`, in real time; print and return the summary.

    `video_id` is the video the session is told it plays, for a voice that goes by who presents it.
    """
    player = FakePlayer(start)
    lock = threading.Lock()  # the player and the states are touched by this thread and the producer's
    states: list[tuple[float, float, str]] = []  # (wall time, video time, the session's state)

    def send(msg: Message) -> None:
        with lock:
            if isinstance(msg, (Duck, Control)):
                player.apply(msg)
            elif isinstance(msg, Status):
                states.append((time.monotonic(), player.current_time, msg.state))

    def report() -> None:
        with lock:
            state = player.state(video_id)
        session.handle(state)

    metrics = _Recording()
    session = Session(translator, voice, AudioOut(stream_factory=lambda callback: None), metrics, cfg, send)
    stop = threading.Event()
    producer = threading.Thread(target=session.run_producer, args=(stop,), name="dubber-producer", daemon=True)
    end, give_up = start + seconds, time.monotonic() + 3 * seconds + 120  # a video that never plays ends the run too
    session.handle(Start(mode=None))
    producer.start()
    try:
        report()
        session.handle(Captions(video_id, "en", "asr", track))
        while time.monotonic() < give_up:
            with lock:
                if player.current_time >= end:
                    break
            time.sleep(REPORT_EVERY)
            report()
    finally:
        stop.set()
        session.handle(Stop())
        producer.join(timeout=5.0)
    summary = _summarize(f"{translator.name} + {voice.name}", metrics.records, states, start, end)
    for line in summary.lines(start, seconds):
        out(line)
    return summary


def _summarize(
    engines: str, records: list[SegmentRecord], states: list[tuple[float, float, str]], start: float, end: float
) -> Summary:
    summary = Summary(engines)
    records = [r for r in records if start <= r.src_start < end]
    dubbed = [r for r in records if not r.dropped]
    summary.sentences, summary.dubbed, summary.dropped = len(records), len(dubbed), len(records) - len(dubbed)
    summary.shortened = sum(r.shortened for r in dubbed)
    summary.speech = sum(r.src_end - r.src_start for r in dubbed)
    if summary.speech > 0:
        summary.translation_per_second = sum(r.mt_ms for r in dubbed) / 1000 / summary.speech
        summary.voice_per_second = sum(r.tts_ms for r in dubbed) / 1000 / summary.speech
    if dubbed:
        summary.ahead_at_the_end = dubbed[-1].ahead or 0.0
        summary.latest = max(r.lag for r in dubbed)
    held: tuple[float, float] | None = None
    first = True
    for wall, at, state in states:
        if state in ("preparing", "holding") and held is None:
            held = (wall, at)
        elif state == "playing" and held is not None:
            if first:
                summary.startup, first = wall - held[0], False
            else:
                summary.holds.append((held[1], wall - held[0]))
            held = None
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dry_run", description="Dub a caption file without Chrome and without sound, and say how the engines kept up."
    )
    parser.add_argument("captions", type=pathlib.Path, help="a YouTube caption file (json3)")
    parser.add_argument("--config", type=pathlib.Path, help="a TOML settings file (default: the built-in settings)")
    parser.add_argument("--start", type=float, default=0.0, help="where in the video to start, in seconds (default: 0)")
    parser.add_argument("--seconds", type=float, default=240.0, help="how much of the video to play (default: 240)")
    parser.add_argument(
        "--video", default=VIDEO_ID, metavar="ID",
        help='the video\'s YouTube id, for voice = "auto" (default: none, so the man\'s voice reads)',
    )
    args = parser.parse_args(argv)
    try:
        track = json.loads(args.captions.read_text(encoding="utf-8"))
        cfg = load_config(args.config)
        translator = _build_translator(cfg)
        voice = _build_voice(cfg, translator)
        _check_pc(translator, cfg.pc.base_url)
    except (OSError, ValueError, _EngineError) as exc:
        print(f"dry run error: {' '.join(str(exc).split())}", file=sys.stderr)
        return 2
    if isinstance(voice, PresenterVoices):
        voice = voice.for_session()
    run(track, translator, voice, cfg, args.start, args.seconds, video_id=args.video)
    return 0


if __name__ == "__main__":
    sys.exit(main())
