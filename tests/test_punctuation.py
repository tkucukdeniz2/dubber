import math
import re

import pytest

from dubber.captions import Word
from dubber.punctuation import (
    FIRST_STRETCH,
    LEFT_CONTEXT,
    RIGHT_CONTEXT,
    STRETCH,
    OpenTrack,
    PunctuationError,
    restore,
    restore_stretch,
    settled,
)


def words(text, step=0.5, start=0.0):
    return [Word(round(start + i * step, 2), round(start + (i + 1) * step, 2), t) for i, t in enumerate(text.split())]


def texts(ws):
    return [w.text for w in ws]


# restore: the caption's words, the reply's marks.

def test_restore_takes_marks_and_capitals_from_the_reply():
    got = restore(words("the ship is huge it appears briefly"), "The ship is huge. It appears briefly.")
    assert texts(got) == ["The", "ship", "is", "huge.", "It", "appears", "briefly."]


def test_restore_keeps_the_times():
    ws = words("so it goes")
    assert [(w.start, w.end) for w in restore(ws, "So it goes.")] == [(w.start, w.end) for w in ws]


def test_restore_never_takes_a_word_from_the_reply():
    ws = words("the heighliner is a big ship that folds space for the guild")
    # a word changed and a word added
    got = restore(ws, "The Highliner is a very big ship that folds space, for the Guild.")
    assert texts(got) == ["The", "heighliner", "is", "a", "big", "ship", "that", "folds", "space,", "for", "the", "Guild."]
    # a word dropped
    got = restore(ws, "The heighliner is a ship that folds space for the guild.")
    assert texts(got) == ["The", "heighliner", "is", "a", "big", "ship", "that", "folds", "space", "for", "the", "guild."]


def test_restore_compares_words_without_case_and_punctuation():
    assert texts(restore(words("it's dune 2 isn't it"), "It's Dune 2, isn't it?")) == ["It's", "Dune", "2,", "isn't", "it?"]
    assert texts(restore(words("it's here"), "It’s here.")) == ["It’s", "here."]


def test_restore_handles_a_word_of_several_tokens():
    ws = [Word(0.0, 1.0, "in the"), Word(1.0, 2.0, "end"), Word(2.0, 3.0, "we won")]
    assert restore(ws, "In the end, we won.") == [Word(0.0, 1.0, "In the"), Word(1.0, 2.0, "end,"), Word(2.0, 3.0, "we won.")]


def test_restore_raises_when_the_reply_is_not_the_text():
    with pytest.raises(PunctuationError):
        restore(words("one two three four five six seven eight nine ten"), "I cannot help with that.")
    with pytest.raises(PunctuationError):
        restore(words("one two three"), "")
    # nine words of ten came back: that is the floor
    assert restore(words("a b c d e f g h i j"), "A b c d e f g h i.")[-1].text == "j"
    with pytest.raises(PunctuationError):
        restore(words("a b c d e f g h i j"), "A b c d e f g h.")


def test_restore_of_no_words_is_empty():
    assert restore([], "Anything.") == []


# settled: how far a stretch can be trusted.

def test_settled_is_everything_on_the_last_stretch():
    assert settled(words("And so it ends"), final=True) == 4


def test_settled_runs_through_the_last_sentence_end_with_words_after_it():
    assert settled(words("One two three. Four five six. Seven eight nine ten eleven twelve"), final=False) == 6


def test_a_sentence_end_near_the_end_of_the_stretch_is_not_trusted():
    assert settled(words("One two three. Four five six seven eight nine. Ten eleven"), final=False) == 3
    ws = words("One two three. Four five six. " + " ".join("abcde"[:RIGHT_CONTEXT]))
    assert settled(ws, final=False) == 6       # exactly RIGHT_CONTEXT words after it: trusted
    assert settled(ws[:-1], final=False) == 3  # one fewer: not


def test_without_a_sentence_end_the_last_clause_mark_settles():
    assert settled(words("one two three, four five six, seven eight nine ten eleven twelve"), final=False) == 6


def test_without_any_mark_nothing_is_settled():
    assert settled(words("one two three four five six seven eight nine ten"), final=False) == 0
    assert settled(words("one two three four five six."), final=False) == 0  # the last word proves nothing
    assert settled([], final=False) == 0 and settled([], final=True) == 0


# OpenTrack: the words, by time and by stretch.

def test_open_track_finds_words_by_time():
    track = OpenTrack(words(" ".join(f"w{i}" for i in range(100)), step=1.0))  # word i runs from i to i + 1
    assert len(track) == 100
    assert track.word_at(0.0) == 0 and track.word_at(41.5) == 41 and track.word_at(42.0) == 42
    assert track.word_at(500.0) == 100
    assert track.first_word(41.5) == 41 - LEFT_CONTEXT and track.first_word(3.0) == 0
    assert track.starts_at(7) == 7.0 and track.starts_at(100) == math.inf


def test_open_track_hands_out_a_short_first_stretch():
    track = OpenTrack(words(" ".join(f"w{i}" for i in range(400))))
    assert len(track.stretch(0, first=True)) == FIRST_STRETCH
    assert len(track.stretch(10, first=False)) == STRETCH
    assert texts(track.stretch(390, first=False)) == [f"w{i}" for i in range(390, 400)]


# restore_stretch: one step along the track.

LENGTHS = [7, 12, 9, 30, 8, 11, 6, 14] * 6
SENTENCES = [" ".join(f"s{k}w{j}" for j in range(n)) for k, n in enumerate(LENGTHS)]


def perfect(text):
    """A model that knows these sentences: a capital on the first word, a full stop on the last."""
    out = []
    for token in text.split():
        k, j = (int(x) for x in re.fullmatch(r"s(\d+)w(\d+)", token).groups())
        out.append((token.capitalize() if j == 0 else token) + ("." if j == LENGTHS[k] - 1 else ""))
    return " ".join(out)


def test_restore_stretch_gives_the_settled_words_and_where_the_next_begins():
    track = OpenTrack(words(" ".join(SENTENCES)))
    got, after = restore_stretch(track, 0, True, perfect)
    # Of the first 60 words the sentences of 7, 12 and 9 are whole. The fourth ends two words
    # before the stretch does, which is too near to trust.
    assert after == 28 and texts(got)[0] == "S0w0" and texts(got)[-1] == "s2w8."
    got, after = restore_stretch(track, 28, False, perfect)
    assert texts(got)[0] == "S3w0" and after > 28 + 100


def test_restore_stretch_settles_the_last_stretch_whole():
    track = OpenTrack(words("s0w0 s0w1 s0w2 s0w3"))
    got, after = restore_stretch(track, 0, True, perfect)
    assert after == 4 and texts(got) == ["S0w0", "s0w1", "s0w2", "s0w3"]


def test_restore_stretch_raises_when_nothing_is_settled():
    track = OpenTrack(words(" ".join(f"w{i}" for i in range(300))))
    with pytest.raises(PunctuationError):
        restore_stretch(track, 0, True, lambda text: text)


def test_restore_stretch_sends_the_words_of_the_stretch():
    track = OpenTrack(words(" ".join(SENTENCES)))
    seen = []
    restore_stretch(track, 28, False, lambda text: seen.append(text) or perfect(text))
    assert seen[0].split()[0] == "s3w0" and len(seen[0].split()) == STRETCH


def test_walking_a_track_gives_back_its_sentences():
    track = OpenTrack(words(" ".join(SENTENCES)))
    i, first, out, begins = 0, True, [], []
    while i < len(track):
        got, after = restore_stretch(track, i, first, perfect)
        assert after > i
        begins.append(got[0].text)
        out += got
        i, first = after, False
    assert " ".join(texts(out)) == perfect(" ".join(SENTENCES))
    assert all(re.fullmatch(r"S\d+w0", b) for b in begins)  # every stretch begins a sentence


# What the final review found.

def test_restore_takes_only_punctuation_and_capitals_never_a_symbol_or_an_abbreviation():
    ws = words("it cost 50 dollars and 20 percent of us left the us")
    got = restore(ws, "It cost $50 dollars, and 20% percent of us left the U.S.")
    assert texts(got) == ["It", "cost", "50", "dollars,", "and", "20", "percent", "of", "us", "left", "the", "us"]
    assert texts(restore(words("he said hello there"), 'He said, "Hello there!"')) == ["He", "said,", '"Hello', 'there!"']
    assert texts(restore(words("a mysterious-looking ship"), "A mysterious-looking ship.")) == ["A", "mysterious-looking", "ship."]


def test_an_open_track_counts_tokens_not_caption_lines():
    lines = [Word(0.0, 4.0, "aa bb cc dd"), Word(4.0, 6.0, "ee"), Word(6.0, 10.0, "ff gggg")]
    track = OpenTrack(lines)
    assert len(track) == 7 and texts(track.words) == ["aa", "bb", "cc", "dd", "ee", "ff", "gggg"]
    # a line's time is shared out by letters, and its ends are the line's own
    assert [(w.start, w.end) for w in track.words[:4]] == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]
    assert [(w.start, w.end) for w in track.words[5:]] == [(6.0, 7.33), (7.33, 10.0)]
    assert track.word_at(2.5) == 2 and track.first_word(2.5) == 0
