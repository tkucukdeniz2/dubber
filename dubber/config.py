"""The orchestrator's settings: built-in defaults, overridden by a TOML file.

Each table of the file sets some keys of one section, and `metrics_path` and `log_path`
sit at the top level. The `[glossary]` table is taken whole: English terms and the Turkish the
translator must use for them. A name the file does not know, or a value of the wrong type, is an error
rather than a setting that is silently ignored.
"""

import dataclasses
import pathlib
import re
import tomllib
from dataclasses import dataclass, field

from dubber.scheduler import SchedulerConfig
from dubber.segmenter import SegmenterConfig

# The ducked video volume: never muted, and never so loud that it covers the dub.
DUCK_RANGE = (0.15, 0.25)

# `voice.voice` when the voice goes by who presents the video (see VoiceConfig).
AUTO_VOICE = "auto"

# The voices `dubber voice` switches between: a name, and its voice.engine and voice.voice.
# "kalin" is a deep, steady male voice and "ipek" a calm female one, both designed with VoxCPM2
# from a description and nobody's own voice; their reference clips and transcripts are
# pc/voices/kalin.wav, kalin.txt, ipek.wav and ipek.txt. "auto" picks one of the two for each video.
VOICE_CHOICES = {
    "auto": ("pc", AUTO_VOICE),
    "ipek": ("pc", "ipek"),
    "kalin": ("pc", "kalin"),
    "owner": ("pc", "owner"),
    "yelda": ("say", "Yelda"),
}

_TABLE_LINE = re.compile(r"\s*\[([^\[\]]+)\]\s*(#.*)?$")


@dataclass
class ServerConfig:
    """Where the loopback WebSocket listens."""

    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class DuckConfig:
    """The video's volume while it is dubbed, between 0.15 and 0.25."""

    volume: float = 0.2


@dataclass
class VoiceConfig:
    """The voice engine and the voice it speaks with.

    With the engine "pc", `voice = "auto"` goes by who presents the video: the PC's model looks
    at the video's title, thumbnail and three frames, and a video that a woman presents is read
    in the voice `female`, every other video in `male`. Any other `voice` reads every video,
    and `male` and `female` are then not used.
    """

    engine: str = "say"
    voice: str = "Samantha"
    male: str = "kalin"
    female: str = "ipek"


@dataclass
class TranslatorConfig:
    """The translation engine: "identity" (the English stand-in) or "pc".

    `context_sentences` is how many segments before each one, in track order, go with it
    as context (never translated themselves); 0 sends none. With `restore_punctuation` the
    translator's model puts the punctuation back into a caption track that has none, a
    stretch at a time, so that the dub is cut at sentence ends; false cuts such a track
    at pauses and by length.
    """

    engine: str = "identity"
    context_sentences: int = 3
    restore_punctuation: bool = True


@dataclass
class PcConfig:
    """The PC's llama.cpp server; `dataclasses.asdict` gives the [pc] table it reads."""

    base_url: str = "http://my-pc.example.ts.net:8080"
    api_key_file: str = "~/.qwen-pc/api-key"
    preferred_model: str = "qwen3.8-27b"
    tts_url: str = "http://my-pc.example.ts.net:8191"  # dubber-tts, the voice on the PC


@dataclass
class MacLlmConfig:
    """The Mac's own OpenAI-compatible model server, the fallback translator."""

    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3.8:27b-mlx"


@dataclass
class Config:
    """Every setting of the orchestrator; each section is its own instance.

    `log_path` is the file `dubber serve` writes its log to, beside what it prints; an
    empty value writes no file.
    """

    server: ServerConfig = field(default_factory=ServerConfig)
    duck: DuckConfig = field(default_factory=DuckConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    translator: TranslatorConfig = field(default_factory=TranslatorConfig)
    pc: PcConfig = field(default_factory=PcConfig)
    mac_llm: MacLlmConfig = field(default_factory=MacLlmConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    glossary: dict[str, str] = field(default_factory=dict)
    metrics_path: str = "metrics.jsonl"
    log_path: str = "dubber.log"


def load_config(path: pathlib.Path | None) -> Config:
    """The defaults, with whatever the TOML file at `path` sets; None means no file.

    Raises ValueError naming every unknown table or key, for a value whose type differs
    from the default's (an integer is accepted for a float), and for a duck volume
    outside DUCK_RANGE.
    """
    cfg = Config()
    if path is not None:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f), str(path))
    low, high = DUCK_RANGE
    if not low <= cfg.duck.volume <= high:
        raise ValueError(f"duck.volume must be between {low} and {high}, not {cfg.duck.volume}")
    if cfg.translator.context_sentences < 0:
        raise ValueError(f"translator.context_sentences must be 0 or more, not {cfg.translator.context_sentences}")
    for term, turkish in cfg.glossary.items():
        if not isinstance(turkish, str):
            raise ValueError(f"glossary.{term} must be a string, not {turkish!r}")
    return cfg


def set_voice(path: pathlib.Path, engine: str, voice: str) -> None:
    """Set `engine` and `voice` in the [voice] table of the TOML file at `path`.

    Every other line stays as it was, and so does a comment after either value. The new text
    is parsed before it is written: a file that is not TOML, or one whose [voice] table this
    cannot rewrite, is a ValueError and stays untouched.
    """
    text = path.read_bytes().decode("utf-8")
    before = tomllib.loads(text)
    wanted = {**before, "voice": {**before.get("voice", {}), "engine": engine, "voice": voice}}
    new_text = _with_voice(text, {"engine": engine, "voice": voice})
    if tomllib.loads(new_text) != wanted:
        raise ValueError(f"{path}: cannot rewrite the [voice] table; change it by hand")
    path.write_bytes(new_text.encode("utf-8"))


def _with_voice(text: str, values: dict[str, str]) -> str:
    """`text` with `values` set in its [voice] table, which is added when there is none."""
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if _table_of(line) == "voice"), None)
    if header is None:
        block = "[voice]\n" + "".join(f'{key} = "{value}"\n' for key, value in values.items())
        return text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + block
    missing = dict(values)
    for i in range(header + 1, len(lines)):
        if _table_of(lines[i]) is not None:
            break
        for key, value in values.items():
            found = re.match(rf"""(\s*{key}\s*=\s*)(?:"[^"]*"|'[^']*')(.*)$""", lines[i], re.DOTALL)
            if found:
                lines[i] = f'{found.group(1)}"{value}"{found.group(2)}'
                missing.pop(key)
    for key, value in reversed(missing.items()):
        lines.insert(header + 1, f'{key} = "{value}"\n')
    return "".join(lines)


def _table_of(line: str) -> str | None:
    """The name of the table that `line` opens, or None."""
    found = _TABLE_LINE.match(line.rstrip("\r\n"))
    return found.group(1).strip() if found else None


def _apply(cfg: Config, data: dict, source: str) -> None:
    """Set on `cfg` every value `data` gives, after checking all of the names."""
    names = {f.name for f in dataclasses.fields(cfg)}
    unknown = [name for name in data if name not in names]
    for name, value in data.items():
        section = getattr(cfg, name, None)
        if dataclasses.is_dataclass(section) and isinstance(value, dict):
            keys = {f.name for f in dataclasses.fields(section)}
            unknown += [f"{name}.{key}" for key in value if key not in keys]
    if unknown:
        raise ValueError(f"{source}: unknown settings: {', '.join(unknown)}")
    for name, value in data.items():
        current = getattr(cfg, name)
        if not dataclasses.is_dataclass(current):
            setattr(cfg, name, _checked(name, current, value))
        elif not isinstance(value, dict):
            raise ValueError(f"{source}: {name} must be a table, [{name}]")
        else:
            for key, item in value.items():
                setattr(current, key, _checked(f"{name}.{key}", getattr(current, key), item))


def _checked(name: str, default: object, value: object) -> object:
    """`value`, if it has the type of `default`; an integer becomes a float where needed."""
    if isinstance(default, float) and type(value) is int:
        return float(value)
    if type(value) is not type(default):
        raise ValueError(f"{name} must be a {type(default).__name__}, not {value!r}")
    return value
