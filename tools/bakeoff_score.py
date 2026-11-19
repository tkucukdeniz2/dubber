"""Tally the owner's marks on the bake-off judging sheets."""

import argparse
import json
import math
import pathlib
import re
import sys
from collections.abc import Iterator

from tools.voice_sentences import SEAM_IDS, SENTENCES

MARKS = ("ok", "meaning", "style")
VOICE_MARKS = ("ok", "defect")

# In a candidate pattern, "raw" is everything after "mark:" and "mark" is the first run of
# letters in it.
_HEADING = re.compile(r"^### (?P<id>.+?)\s*$")
_CANDIDATE = re.compile(
    r"^- \*\*(?P<label>[A-Z]+)\*\*.*→\s*mark:(?P<raw>\s*(?P<mark>[A-Za-z]*).*)$"
)

VOICE_PASS_MARK = 14
_VOICE_CANDIDATE = re.compile(
    r"^- `(?P<file>[^`]+)`\s*→\s*mark:(?P<raw>\s*(?P<mark>[A-Za-z]*).*)$"
)

# (block id, label or file name, the text after "mark:" as it was written)
Line = tuple[str, str, str]


def _candidates(
    sheet_text: str, pattern: re.Pattern
) -> Iterator[tuple[str | None, re.Match]]:
    """Each candidate line of the sheet, matched, with the id of the heading above it.

    The id is None for a line above the first heading.
    """
    block_id = None
    for line in sheet_text.splitlines():
        if heading := _HEADING.match(line):
            block_id = heading["id"]
        elif candidate := pattern.match(line):
            yield block_id, candidate


def _unread(
    sheet_text: str, pattern: re.Pattern, who: str, words: tuple[str, ...]
) -> tuple[list[Line], list[Line]]:
    """The candidate lines whose mark is none of words: (unreadable, unmarked).

    A mark is read as the first run of letters after "mark:", compared without regard
    to case, as the scorers do. A line with nothing after "mark:" is unmarked; any other
    line that is not read ("okk", "tamam", "1") is unreadable. Every candidate line is
    checked, also one the key does not know. who names the pattern's group that holds the
    label or file name.
    """
    unreadable: list[Line] = []
    unmarked: list[Line] = []
    for block_id, candidate in _candidates(sheet_text, pattern):
        if candidate["mark"].casefold() in words:
            continue
        raw = candidate["raw"].strip()
        line = (block_id or "(no heading)", candidate[who], raw)
        (unreadable if raw else unmarked).append(line)
    return unreadable, unmarked


def unread_translate_lines(sheet_text: str) -> tuple[list[Line], list[Line]]:
    """The translation sheet's lines with no ok, meaning or style: (unreadable, unmarked)."""
    return _unread(sheet_text, _CANDIDATE, "label", MARKS)


def unread_voice_lines(sheet_text: str) -> tuple[list[Line], list[Line]]:
    """The voice sheet's lines with no ok or defect: (unreadable, unmarked)."""
    return _unread(sheet_text, _VOICE_CANDIDATE, "file", VOICE_MARKS)


def unread_report(
    unreadable: list[Line], unmarked: list[Line], words: tuple[str, ...]
) -> str:
    """What to tell the owner about the lines the scorer did not read; "" when none.

    A summary line with both counts, then the unreadable lines with the text that was
    written, then the unmarked ones, each group under its own heading. words are the
    marks the sheet reads.
    """
    if not (unreadable or unmarked):
        return ""
    noun = "line" if len(unreadable) == 1 else "lines"
    lines = [
        f"{len(unreadable)} candidate {noun} unreadable (listed below), "
        f"{len(unmarked)} unmarked"
    ]
    if unreadable:
        lines.append(f"unreadable (write one of: {', '.join(words)}):")
        lines += [f"  {block}  {who}  mark: {raw}" for block, who, raw in unreadable]
    if unmarked:
        lines.append("unmarked:")
        lines += [f"  {block}  {who}" for block, who, _ in unmarked]
    return "\n".join(lines)


def _warn_unread(
    unreadable: list[Line], unmarked: list[Line], words: tuple[str, ...]
) -> None:
    """Print the report on stderr, so the table on stdout stays what it was."""
    report = unread_report(unreadable, unmarked, words)
    if report:
        print(report, file=sys.stderr)


def score_translate(sheet_text: str, key: dict) -> dict[str, dict[str, int]]:
    """Count each engine's ok, meaning and style marks on the translation sheet.

    A candidate line reads "- **<label>** `<turkish>` → mark: <word>" under a
    "### <sentence id>" heading. The word is the first run of letters after "mark:",
    compared without regard to case, so a note after it does no harm. A line with no
    mark, with another word, or with a sentence or label the key does not know counts
    for nothing (unread_translate_lines lists the first two kinds). Every engine in the
    key is listed, marked or not.
    """
    scores = {
        engine: dict.fromkeys(MARKS, 0)
        for labels in key.values()
        for engine in labels.values()
    }
    for sentence_id, candidate in _candidates(sheet_text, _CANDIDATE):
        engine = key.get(sentence_id, {}).get(candidate["label"])
        mark = candidate["mark"].casefold()
        if engine is not None and mark in MARKS:
            scores[engine][mark] += 1
    return scores


def translate_table(scores: dict[str, dict[str, int]]) -> str:
    """The tallies as a table: the counts, the number marked, and ok over marked."""
    rows = [("engine", *MARKS, "marked", "pass rate")]
    for engine, counts in scores.items():
        marked = sum(counts.values())
        rate = f"{100 * counts['ok'] / marked:.1f}%" if marked else "n/a"
        cells = (str(counts[mark]) for mark in MARKS)
        rows.append((engine, *cells, str(marked), rate))
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(
            cell.ljust(width) if column == 0 else cell.rjust(width)
            for column, (cell, width) in enumerate(zip(row, widths))
        )
        for row in rows
    )


def _translate_command(args: argparse.Namespace) -> None:
    key = json.loads(args.key.read_text(encoding="utf-8"))
    sheet = args.sheet.read_text(encoding="utf-8")
    print(translate_table(score_translate(sheet, key)))
    _warn_unread(*unread_translate_lines(sheet), MARKS)


def _sentences_in(block_id: str) -> int:
    """How many of the 16 sentences a block of the voice sheet stands for.

    One, except the seam block, which is its three sentences.
    """
    return len(SEAM_IDS) if block_id == SEAM_IDS[0] else 1


def score_voice(sheet_text: str, key: dict) -> dict[str, dict]:
    """Count each voice's ok sentences on the voice sheet, and say whether it passes.

    A candidate line reads "- `<file>` → mark: <word>" under a "### <sentence id>"
    heading. A line marked ok counts for the sentences its block stands for, which is
    three for the seam block, so a flawless voice scores 16. The word is the first run of
    letters after "mark:", compared without regard to case, so a note after it does no
    harm. A line with no mark or with another word (unread_voice_lines lists those), a
    file or sentence the key does not know, and a clip that is in the key but not on the
    sheet (the pre-filter kept it off) count for nothing. Every voice in the key is
    listed, marked or not. A voice passes with at least VOICE_PASS_MARK sentences ok.
    """
    oks = dict.fromkeys((engine for files in key.values() for engine in files.values()), 0)
    for block_id, candidate in _candidates(sheet_text, _VOICE_CANDIDATE):
        engine = key.get(block_id, {}).get(candidate["file"])
        if engine is not None and candidate["mark"].casefold() == "ok":
            oks[engine] += _sentences_in(block_id)
    return {engine: {"ok": ok, "pass": ok >= VOICE_PASS_MARK} for engine, ok in oks.items()}


def _slowness(speed: float | None) -> float:
    """A voice with no measured speed ranks after every voice that has one."""
    return math.inf if speed is None else speed


def voice_report(scores: dict[str, dict], timing: dict[str, dict]) -> str:
    """A line per voice (ok count, pass, seconds per audio second), then the winner.

    The winner is the passing voice with the lowest seconds_per_audio_second. A voice
    with no speed in the timing shows n/a and counts as slower than any with one.
    """
    speeds = {
        engine: timing.get(engine, {}).get("seconds_per_audio_second") for engine in scores
    }
    width = max(map(len, scores), default=0)
    digits = len(str(len(SENTENCES)))
    lines = []
    for engine, score in scores.items():
        speed = "n/a" if speeds[engine] is None else f"{speeds[engine]:.3f}"
        verdict = "yes" if score["pass"] else "no"
        lines.append(
            f"{engine.ljust(width)}  ok: {score['ok']:>{digits}}/{len(SENTENCES)}  "
            f"pass: {verdict:<3}  seconds_per_audio_second: {speed}"
        )
    passing = [engine for engine, score in scores.items() if score["pass"]]
    if passing:
        winner = min(passing, key=lambda engine: _slowness(speeds[engine]))
        lines.append(f"winner: {winner} (the fastest voice that passes)")
    else:
        lines.append("winner: no engine passed")
    return "\n".join(lines)


def _voice_command(args: argparse.Namespace) -> None:
    key = json.loads(args.key.read_text(encoding="utf-8"))
    sheet = args.sheet.read_text(encoding="utf-8")
    timing = json.loads(args.timing.read_text(encoding="utf-8"))
    print(voice_report(score_voice(sheet, key), timing))
    _warn_unread(*unread_voice_lines(sheet), VOICE_MARKS)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Tally the marks on a bake-off sheet.")
    commands = parser.add_subparsers(dest="command", required=True)
    translate = commands.add_parser("translate", help="score the translation sheet")
    translate.add_argument(
        "--sheet", type=pathlib.Path, required=True, help="the sheet with your marks"
    )
    translate.add_argument(
        "--key", type=pathlib.Path, required=True, help="the key written beside it"
    )
    translate.set_defaults(handler=_translate_command)
    voice = commands.add_parser("voice", help="score the voice sheet")
    voice.add_argument(
        "--sheet", type=pathlib.Path, required=True, help="the sheet with your marks"
    )
    voice.add_argument(
        "--key", type=pathlib.Path, required=True, help="the key written beside it"
    )
    voice.add_argument(
        "--timing",
        type=pathlib.Path,
        required=True,
        help="voice_timing.json written beside the sheet",
    )
    voice.set_defaults(handler=_voice_command)
    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
