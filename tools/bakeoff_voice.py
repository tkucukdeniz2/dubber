"""Run the blind Turkish voice test and write its judging sheet, key and timings."""

import argparse
import json
import pathlib
import random
import time
from dataclasses import dataclass, field

import numpy as np
import soundfile as sf

from dubber.engines.base import Clip, Voice, VoiceError
from dubber.engines.voices import voice_from_name
from tools.voice_sentences import SEAM_IDS, SENTENCES

WARM_UP_TEXT = "Merhaba."
MIN_CHARS_PER_SECOND = 6
MAX_CHARS_PER_SECOND = 25
SHEET_FILE = "voice_sheet.md"
KEY_FILE = "voice_key.json"
TIMING_FILE = "voice_timing.json"


def prefilter(text: str, seconds: float) -> str:
    """"suspect" when the text over the clip runs outside 6 to 25 characters a second.

    Both limits are inclusive. An empty clip is suspect.
    """
    if seconds <= 0:
        return "suspect"
    rate = len(text) / seconds
    return "ok" if MIN_CHARS_PER_SECOND <= rate <= MAX_CHARS_PER_SECOND else "suspect"


def _blocks() -> list[tuple[str, list[tuple[str, str]]]]:
    """The sheet's blocks as (block id, [(sentence id, text), ...]).

    The three seam sentences make one block, under the id of the first.
    """
    texts = dict(SENTENCES)
    blocks = []
    for sentence_id, text in SENTENCES:
        if sentence_id == SEAM_IDS[0]:
            blocks.append((sentence_id, [(i, texts[i]) for i in SEAM_IDS]))
        elif sentence_id not in SEAM_IDS:
            blocks.append((sentence_id, [(sentence_id, text)]))
    return blocks


BLOCKS = _blocks()


def _joined_text(requests: list[tuple[str, str]]) -> str:
    """The text of a block: its sentences joined by a space.

    The sheet shows it under the block's heading, and the pre-filter measures it.
    """
    return " ".join(text for _, text in requests)


@dataclass
class _EngineRun:
    """What one voice did: its clip for each block, and the numbers about its requests.

    clips holds the clip of every block the voice answered. kept_off_sheet holds the ids of
    the blocks the pre-filter flagged: their clips are written and keyed but not listed on
    the sheet. suspect lists those block ids (the seam is "s1"); errors lists the sentence
    id of each failed request.
    """

    first_call_seconds: float
    clips: dict[str, Clip] = field(default_factory=dict)
    kept_off_sheet: set[str] = field(default_factory=set)
    suspect: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    synthesis_seconds: float = 0.0
    audio_seconds: float = 0.0

    def answer_block(
        self, voice: Voice, block_id: str, requests: list[tuple[str, str]]
    ) -> None:
        """Ask the voice for each request of the block, in order, and keep the joined clip.

        A VoiceError drops the block; the failing request's sentence id goes to errors and
        the later requests of the block are not made. The pre-filter then judges the block as
        a whole, its joined text over its joined clip, so one odd sentence of the seam does
        not cost the voice the seam. A flagged block's id goes to suspect and the block is
        kept off the sheet.
        """
        clips = []
        for sentence_id, text in requests:
            began = time.perf_counter()
            try:
                clip = voice.synthesize(text)
            except VoiceError:
                self.errors.append(sentence_id)
                return
            self.synthesis_seconds += time.perf_counter() - began
            self.audio_seconds += clip.seconds
            clips.append(clip)
        joined = Clip(np.concatenate([clip.samples for clip in clips]), clips[0].sample_rate)
        self.clips[block_id] = joined
        if prefilter(_joined_text(requests), joined.seconds) == "suspect":
            self.suspect.append(block_id)
            self.kept_off_sheet.add(block_id)

    def timing(self) -> dict:
        speed = self.synthesis_seconds / self.audio_seconds if self.audio_seconds else None
        return {
            "seconds_per_audio_second": speed,
            "synthesis_seconds_total": self.synthesis_seconds,
            "first_call_seconds": self.first_call_seconds,
            "suspect": self.suspect,
            "errors": self.errors,
        }


def _measure(voice: Voice) -> _EngineRun:
    """Warm the voice up with one request, then ask it for every block.

    The first request of a voice includes loading its model, so it is left out of the
    timing and kept apart as first_call_seconds. A VoiceError from it passes on: the voice
    is unavailable.
    """
    began = time.perf_counter()
    voice.synthesize(WARM_UP_TEXT)
    result = _EngineRun(first_call_seconds=time.perf_counter() - began)
    for block_id, requests in BLOCKS:
        result.answer_block(voice, block_id, requests)
    return result


def _measure_all(engines: dict[str, Voice]) -> tuple[dict[str, _EngineRun], list[str]]:
    """Each available voice's run, and the "engine unavailable" line of each other voice."""
    runs: dict[str, _EngineRun] = {}
    unavailable = []
    for name, voice in engines.items():
        try:
            runs[name] = _measure(voice)
        except VoiceError as exc:
            reason = " ".join(str(exc).split())  # one line, so it cannot pass for sheet content
            unavailable.append(f"engine unavailable: {name}: {reason}")
    return runs, unavailable


def _file_names(rng: random.Random, engines: dict[str, Voice]) -> dict[tuple[str, str], str]:
    """A distinct name v_<12 hex>.wav for each (block id, voice name) pair."""
    slots = [(block_id, name) for block_id, _ in BLOCKS for name in engines]
    draws = rng.sample(range(16**12), len(slots))
    return {slot: f"v_{draw:012x}.wav" for slot, draw in zip(slots, draws)}


def _orders(rng: random.Random, engines: dict[str, Voice]) -> dict[str, list[str]]:
    """The order of the voices in each block, shuffled per block."""
    orders = {}
    for block_id, _ in BLOCKS:
        orders[block_id] = list(engines)
        rng.shuffle(orders[block_id])
    return orders


def _write_wavs(
    runs: dict[str, _EngineRun],
    unavailable: list[str],
    names: dict[tuple[str, str], str],
    orders: dict[str, list[str]],
    out_dir: pathlib.Path,
) -> tuple[str, dict]:
    """Write every clip as a wav and return the sheet and key that describe them.

    The sheet lists the clips the pre-filter let through. The key names every wav
    written, so a clip kept off the sheet is still in it.
    """
    sheet_blocks = ["\n".join(unavailable)] if unavailable else []
    key: dict[str, dict[str, str]] = {}
    for block_id, requests in BLOCKS:
        lines = [f"### {block_id}", _joined_text(requests)]
        key[block_id] = {}
        for name in orders[block_id]:
            if name not in runs or block_id not in runs[name].clips:
                continue
            filename = names[block_id, name]
            clip = runs[name].clips[block_id]
            sf.write(out_dir / filename, clip.samples, clip.sample_rate)
            key[block_id][filename] = name
            if block_id not in runs[name].kept_off_sheet:
                lines.append(f"- `{filename}` → mark: ")
        sheet_blocks.append("\n".join(lines))
    return "\n\n".join(sheet_blocks) + "\n", key


def run(engines: dict[str, Voice], out_dir: pathlib.Path, seed: int) -> tuple[str, dict]:
    """Synthesize every block with every voice and write the wavs and the timing file.

    Returns the sheet (markdown) and the key {sentence_id: {file name: voice name}}; the
    seam is under "s1" only. A voice that fails its warm-up request is listed at the top of
    the sheet as unavailable and left out.

    The file names and the order of the voices come from random.Random(seed) and are drawn
    before any voice is asked, so they depend on the seed and the voices given, not on
    what the voices produce.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    names = _file_names(rng, engines)
    orders = _orders(rng, engines)
    runs, unavailable = _measure_all(engines)
    sheet, key = _write_wavs(runs, unavailable, names, orders, out_dir)
    timing = {name: result.timing() for name, result in runs.items()}
    (out_dir / TIMING_FILE).write_text(
        json.dumps(timing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return sheet, key


class _Unbuildable:
    """Stands in for a voice that could not be built: every request raises its error."""

    def __init__(self, name: str, error: VoiceError):
        self.name = name
        self.error = error

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        raise self.error


def _build(spec: str) -> Voice:
    """The voice for the spec. One that cannot be built is listed as unavailable by the run."""
    try:
        return voice_from_name(spec)
    except VoiceError as exc:
        return _Unbuildable(spec, exc)


def _summary_line(name: str, entry: dict | None) -> str:
    """What happened to one voice, from its entry in the timing file (None: unavailable)."""
    if entry is None:
        return f"{name}: unavailable (listed at the top of the sheet)"
    suspect = ", ".join(entry["suspect"]) or "none"
    errors = ", ".join(entry["errors"]) or "none"
    return (
        f"{name}: flagged by the pre-filter and kept off the sheet: {suspect}; "
        f"failed requests: {errors}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Synthesize the 16 fixed Turkish sentences with each voice "
        "and write a blind judging sheet."
    )
    parser.add_argument(
        "--engines",
        required=True,
        help="comma-separated voices: say:<Voice>, supertonic:<preset>, voxcpm "
        "or voxcpm:<reference.wav>",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="seed of the shuffle and the file names (default: 7)",
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=pathlib.Path("bakeoff/voice"),
        help="folder for the wavs, the sheet, the key and the timing "
        "(default: bakeoff/voice)",
    )
    args = parser.parse_args(argv)

    specs = [spec.strip() for spec in args.engines.split(",")]
    try:
        engines = {spec: _build(spec) for spec in specs}
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    sheet, key = run(engines, args.out_dir, args.seed)
    sheet_path = args.out_dir / SHEET_FILE
    key_path = args.out_dir / KEY_FILE
    timing_path = args.out_dir / TIMING_FILE
    sheet_path.write_text(sheet, encoding="utf-8")
    key_text = json.dumps(key, ensure_ascii=False, indent=2)
    key_path.write_text(key_text + "\n", encoding="utf-8")

    print(f"wrote {sheet_path}, {key_path}, {timing_path} and the wavs beside them")
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    for name in engines:
        print(_summary_line(name, timing.get(name)))
    print(
        'Listen to each wav and write ok or defect after "mark:" on its line of the '
        "sheet, then run:\n"
        f"  uv run python -m tools.bakeoff_score voice --sheet {sheet_path} "
        f"--key {key_path} --timing {timing_path}"
    )


if __name__ == "__main__":
    main()
