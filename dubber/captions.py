"""Parse YouTube json3 captions into cues, words and sentences."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Sentence:
    start: float
    end: float
    text: str


_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+")


def _seconds(ms: int) -> float:
    return round(ms / 1000, 2)


def _collapse(text: str) -> str:
    """Strip the text and turn each whitespace run, newlines included, into a space."""
    return " ".join(text.split())


def _non_space_len(text: str) -> int:
    return len("".join(text.split()))


def parse_json3(data: dict) -> list[Cue]:
    """Parse a json3 caption document into one cue per event that carries text.

    Each seg with text becomes a word. Events without segs, events whose text is
    empty (the newline-only fillers) and events repeating the previous kept
    event's text (rolling duplicates) are skipped.
    """
    cues: list[Cue] = []
    for event in data["events"]:
        segs = event.get("segs") or []
        # Join the raw segs: word segs carry their own leading space.
        text = _collapse("".join(seg["utf8"] for seg in segs))
        if not text or (cues and text == cues[-1].text):
            continue
        begin = event["tStartMs"]
        end = _seconds(begin + event["dDurationMs"])
        spoken = [seg for seg in segs if seg["utf8"].strip()]
        starts = [_seconds(begin + seg.get("tOffsetMs", 0)) for seg in spoken]
        # A word ends where the next one starts; the last one ends with the cue.
        ends = [*starts[1:], end]
        words = tuple(
            Word(start, stop, _collapse(seg["utf8"]))
            for seg, start, stop in zip(spoken, starts, ends, strict=True)
        )
        cues.append(Cue(_seconds(begin), end, text, words))
    return cues


def cues_to_sentences(cues: list[Cue]) -> list[Sentence]:
    """Join the cue texts and split them into sentences with word-aligned times.

    A sentence starts when the word holding its first character starts and ends
    when the word holding its last character ends. A word that holds a sentence
    boundary inside its own text is shared by both neighbours.
    """
    # A seg can hold several words, or sit directly against the previous seg, so a
    # cue's text and its words disagree on spacing. Align them on non-space
    # characters instead: word_by_char[i] is the word holding the i-th non-space
    # character of the joined text.
    word_by_char = [
        word
        for cue in cues
        for word in cue.words
        for _ in range(_non_space_len(word.text))
    ]
    sentences: list[Sentence] = []
    used = 0
    for piece in _SENTENCE_BREAK.split(" ".join(cue.text for cue in cues)):
        size = _non_space_len(piece)
        if size == 0:
            continue
        first, last = word_by_char[used], word_by_char[used + size - 1]
        sentences.append(Sentence(first.start, last.end, piece))
        used += size
    return sentences
