"""Run the translation bake-off and write the blind judging sheet and its key."""

import argparse
import json
import pathlib
import random
import statistics
import string
import time
import tomllib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from dubber.captions import parse_json3
from dubber.engines.base import TranslatorError
from dubber.engines.translate_llamacpp import LlamaCppTranslator

# The bake-off pins the PC models: each request names its model. _swap_refusal keeps a
# request from forcing a model swap on the PC.
PC_MODELS = {"pc-qwen": "qwen3.8-27b", "pc-gemma": "gemma4-12b"}
ENGINE_NAMES = (*PC_MODELS, "mac-ollama", "youtube-tr")
REQUEST_TIMEOUT = 120.0
PROBE_TIMEOUT = 10.0
WINDOW_SECONDS = 0.5
CONTEXT_SENTENCES = 2
MAX_CONSECUTIVE_ERRORS = 3
NO_TRACK = "[no Turkish track for this video]"
NO_CUES = "[no Turkish cues in window]"


def _video_id(name: str) -> str:
    """The text before the first dot: the video id of a file name or a sentence id."""
    return name.split(".")[0]


class YouTubeTurkishAligner:
    """The Turkish captions of one video, looked up by the time span of a sentence."""

    name = "youtube-tr"

    def __init__(self, json3_path: pathlib.Path):
        data = json.loads(pathlib.Path(json3_path).read_text(encoding="utf-8"))
        self._cues = parse_json3(data)

    def for_sentence(self, start: float, end: float) -> str:
        """Join the Turkish cues that start within half a second of the span."""
        return " ".join(
            cue.text
            for cue in self._cues
            if start - WINDOW_SECONDS <= cue.start < end + WINDOW_SECONDS
        )


class YouTubeTurkishTracks:
    """The youtube-tr engine: one Turkish file per video, each serving its own video.

    A file belongs to the video named at the start of its file name, as in
    XP5k0bHmhqc.tr.translated.json3, and a sentence to the video named at the start
    of its id, as in XP5k0bHmhqc.en.asr:12. A sentence whose video has no file, or
    whose time span holds no cue, gets a fixed note and not an error.
    """

    name = "youtube-tr"

    def __init__(self, paths: Sequence[pathlib.Path]):
        by_video: dict[str, pathlib.Path] = {}
        for path in map(pathlib.Path, paths):
            video_id = _video_id(path.name)
            if not video_id:
                raise ValueError(f"no video id at the start of the file name: {path}")
            if video_id in by_video:
                raise ValueError(
                    f"two Turkish files for video {video_id}: "
                    f"{by_video[video_id]} and {path}"
                )
            by_video[video_id] = path
        self._aligners = {
            video_id: YouTubeTurkishAligner(path) for video_id, path in by_video.items()
        }

    def lookup(self, sentence: dict) -> str:
        """The Turkish captions at the sentence's time span in its own video's file."""
        aligner = self._aligners.get(_video_id(sentence["id"]))
        if aligner is None:
            return NO_TRACK
        return aligner.for_sentence(sentence["start"], sentence["end"]) or NO_CUES


def _pc_key(cfg: dict) -> str:
    key_file = pathlib.Path(cfg["pc"]["api_key_file"]).expanduser()
    return key_file.read_text(encoding="utf-8").strip()


def build_engines(
    cfg: dict,
    names: list[str],
    youtube_tr: Sequence[pathlib.Path] | None,
    timeout: float = REQUEST_TIMEOUT,
) -> dict[str, object]:
    """Build the named engines from the config, in the order given.

    youtube_tr lists the Turkish json3 files, one per video, for the youtube-tr engine.
    Raises ValueError for an unknown name, for youtube-tr without files, and for a
    file name that does not start with a video id or repeats another file's video id.
    The PC key is read only when a PC engine is asked for, and is never shown.
    """
    unknown = [name for name in names if name not in ENGINE_NAMES]
    if unknown:
        choices = ", ".join(ENGINE_NAMES)
        raise ValueError(f"unknown engine {unknown[0]!r}; choose from {choices}")
    engines: dict[str, object] = {}
    for name in names:
        if name in PC_MODELS:
            engines[name] = LlamaCppTranslator(
                cfg["pc"]["base_url"],
                _pc_key(cfg),
                model=PC_MODELS[name],
                timeout=timeout,
            )
        elif name == "mac-ollama":
            mac = cfg["mac_llm"]
            engines[name] = LlamaCppTranslator(
                mac["base_url"], api_key=None, model=mac["model"], timeout=timeout
            )
        else:  # youtube-tr
            if not youtube_tr:
                raise ValueError(
                    "the youtube-tr engine needs --youtube-tr, its json3 files"
                )
            engines[name] = YouTubeTurkishTracks(youtube_tr)
    return engines


def _translate(engine: object, sentence: dict, context: list[str]) -> str:
    """The engine's Turkish: a caption lookup by the sentence, or a translation call."""
    if hasattr(engine, "lookup"):
        return engine.lookup(sentence)
    return engine.translate(sentence["text"], context=context)


def _unavailable(reason: str) -> str:
    return f"[engine unavailable: {reason}]"


@dataclass
class _EngineRun:
    """One engine's state during a run.

    seconds collects the duration of each successful call. down holds the reason an
    engine is no longer called: its probe failed, or it failed on the last
    MAX_CONSECUTIVE_ERRORS calls in a row (strikes counts the current streak).
    """

    engine: object
    seconds: list[float]
    down: str | None = None
    strikes: int = 0

    def candidate(self, sentence: dict, context: list[str]) -> str:
        """The engine's Turkish for the sentence, or the unavailable note."""
        if self.down is not None:
            return _unavailable(self.down)
        began = time.perf_counter()
        try:
            text = _translate(self.engine, sentence, context)
        except TranslatorError as exc:
            self.strikes += 1
            if self.strikes >= MAX_CONSECUTIVE_ERRORS:
                self.down = str(exc)
            return _unavailable(str(exc))
        self.strikes = 0
        self.seconds.append(time.perf_counter() - began)
        return text


def _swap_refusal(probe: object, models: list) -> str | None:
    """Why the probe's pinned model must not be requested now, or None when it may be.

    A model is pinned when the probe has a model and does not follow the loaded one
    (prefer_loaded). A request for a pinned model that is not loaded, while the server
    has another loaded, would make the PC swap models: the owner may be using it, and
    only the owner swaps. When no model is loaded the request loads one without
    evicting anything, and a listing with no status (Ollama's) shows nothing loaded.
    """
    pinned = getattr(probe, "model", None)
    if not pinned or getattr(probe, "prefer_loaded", False):
        return None
    loaded = [model.id for model in models if model.loaded]
    if not loaded or pinned in loaded:
        return None
    verb = "is" if len(loaded) == 1 else "are"
    return f"{pinned} is not loaded ({', '.join(loaded)} {verb}); load it on the PC first"


def _probe(runs: dict[str, _EngineRun], probes: dict[str, object]) -> None:
    """Ask each probe that can list models, once; a problem marks its engine down.

    The problem is a failure to list the models, or a pinned model that could only be
    requested by forcing a swap on the PC (see _swap_refusal).
    """
    for name, engine_run in runs.items():
        probe = probes.get(name)
        if hasattr(probe, "list_models"):
            try:
                models = probe.list_models()
            except TranslatorError as exc:
                engine_run.down = str(exc)
            else:
                engine_run.down = _swap_refusal(probe, models)


def _candidate_line(label: str, text: str) -> str:
    """One candidate as a single sheet line: no newline, no backtick in its text."""
    clean = " ".join(text.split()).replace("`", "'")
    return f"- **{label}** `{clean}` → mark: "


def _check_unique_ids(sentences: list[dict]) -> None:
    """The key is indexed by sentence id, so a repeated id would corrupt the scoring."""
    counts = Counter(sentence["id"] for sentence in sentences)
    repeated = [sentence_id for sentence_id, n in counts.items() if n > 1]
    if repeated:
        raise ValueError(f"duplicate sentence ids: {', '.join(repeated)}")


def run(
    sentences: list[dict],
    engines: dict[str, object],
    seed: int,
    probes: dict[str, object] | None = None,
    timings: dict[str, list[float]] | None = None,
) -> tuple[str, dict]:
    """Translate every sentence with every engine and build the blind sheet.

    Returns the sheet (markdown) and the key {sentence_id: {label: engine_name}}. The
    labels A, B, ... are shuffled per sentence with random.Random(seed); the sheet
    shows no engine name.

    An engine that raises TranslatorError gets "[engine unavailable: <message>]" for
    that sentence and the run goes on. After MAX_CONSECUTIVE_ERRORS (three) such errors
    in a row the engine is not called again: its remaining sentences get the last
    message.

    Before translating, list_models() is called once on every engine that has it. When
    that raises TranslatorError, every sentence of the engine is marked unavailable and
    the engine is never called, so a server that is off costs one wait and not one
    timeout per sentence. probes maps an engine name to the object to ask in the
    engine's place, such as a copy built with a short timeout.

    An engine pinned to a model (its model attribute) is treated the same way when the
    listing shows some other model loaded and not the pinned one: every sentence gets
    "[engine unavailable: <model> is not loaded (<loaded model> is); load it on the PC
    first]" and no translation call is made, so the run never forces a model swap on
    the PC. With nothing loaded, or a listing with no status, the engine is called.

    timings, when given, receives each engine's seconds per successful call, appended
    to timings[engine_name].
    """
    _check_unique_ids(sentences)
    timings = {} if timings is None else timings
    runs = {
        name: _EngineRun(engine, timings.setdefault(name, []))
        for name, engine in engines.items()
    }
    _probe(runs, engines if probes is None else probes)
    rng = random.Random(seed)
    blocks: list[str] = []
    key: dict[str, dict[str, str]] = {}
    for index, sentence in enumerate(sentences):
        context = [
            earlier["text"]
            for earlier in sentences[max(0, index - CONTEXT_SENTENCES) : index]
        ]
        texts = {
            name: engine_run.candidate(sentence, context)
            for name, engine_run in runs.items()
        }
        order = list(engines)
        rng.shuffle(order)
        labels = dict(zip(string.ascii_uppercase, order))
        key[sentence["id"]] = labels
        lines = [f"### {sentence['id']}", sentence["text"]]
        lines += [_candidate_line(label, texts[name]) for label, name in labels.items()]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n", key


def summarise_timings(timings: dict[str, list[float]]) -> dict[str, dict]:
    """Per engine, the mean seconds and the number of successful calls.

    An engine with no successful call has no mean: it is None, written as null.
    """
    return {
        name: {
            "mean_seconds": statistics.fmean(seconds) if seconds else None,
            "n": len(seconds),
        }
        for name, seconds in timings.items()
    }


def _summary_line(name: str, entry: dict, total: int) -> str:
    line = f"{name}: {entry['n']} of {total} sentences translated"
    if entry["n"]:
        line += f", mean {entry['mean_seconds']:.2f} s"
    return line


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Translate the bake-off sentences with each engine "
        "and write a blind judging sheet."
    )
    parser.add_argument(
        "--sentences",
        type=pathlib.Path,
        required=True,
        help="sentences.json from tools.bakeoff_sentences",
    )
    parser.add_argument(
        "--engines",
        required=True,
        help="comma-separated, from: " + ", ".join(ENGINE_NAMES),
    )
    parser.add_argument(
        "--youtube-tr",
        type=pathlib.Path,
        nargs="+",
        help="YouTube's Turkish json3 files for the youtube-tr engine, one per video, "
        "named like XP5k0bHmhqc.tr.translated.json3",
    )
    parser.add_argument(
        "--seed", type=int, default=7, help="seed of the label shuffle (default: 7)"
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=pathlib.Path("bakeoff"),
        help="folder for the sheet, the key and the timing (default: bakeoff)",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=pathlib.Path("config.example.toml"),
        help="TOML with [pc] and [mac_llm] (default: config.example.toml)",
    )
    args = parser.parse_args(argv)

    names = [name.strip() for name in args.engines.split(",")]
    try:
        cfg = tomllib.loads(args.config.read_text(encoding="utf-8"))
        sentences = json.loads(args.sentences.read_text(encoding="utf-8"))
        engines = build_engines(cfg, names, args.youtube_tr)
        # The same engines again, with a short timeout, for run to probe.
        probes = build_engines(cfg, names, args.youtube_tr, timeout=PROBE_TIMEOUT)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    timings: dict[str, list[float]] = {}
    sheet, key = run(sentences, engines, args.seed, probes=probes, timings=timings)
    summary = summarise_timings(timings)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    sheet_path = args.out_dir / "translate_sheet.md"
    key_path = args.out_dir / "translate_key.json"
    timing_path = args.out_dir / "translate_timing.json"
    sheet_path.write_text(sheet, encoding="utf-8")
    key_text = json.dumps(key, ensure_ascii=False, indent=2)
    key_path.write_text(key_text + "\n", encoding="utf-8")
    timing_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {sheet_path}, {key_path} and {timing_path}")
    for name, entry in summary.items():
        print(_summary_line(name, entry, len(sentences)))
    print(
        'Write ok, meaning or style after "mark:" on each line of the sheet, '
        "then run:\n"
        f"  uv run python -m tools.bakeoff_score translate --sheet {sheet_path} "
        f"--key {key_path}"
    )


if __name__ == "__main__":
    main()
