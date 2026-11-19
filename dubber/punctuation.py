"""Restore the punctuation of a caption track that has none, a stretch at a time.

YouTube's auto-captions sometimes come without a single full stop. Nothing in such a track
says where a sentence ends: its words have no gaps between them, and a pause guessed from
the word times is a sentence end one time in four. A language model can say: it is given a
stretch of the words and told to copy them, adding only punctuation and capitals. Its reply
is then laid over the caption's own words, so that only its marks are taken and never a
word of its own.

A stretch is trusted up to its last sentence end that the model saw the continuation of,
and the words after that open the next stretch, so every stretch but the first begins at a
sentence start. Nothing here does I/O: `punctuate` is whatever asks the model.
"""

import bisect
import difflib
import math
from collections.abc import Callable, Sequence
from dataclasses import replace

from dubber.captions import Word
from dubber.segmenter import bare, ends_clause, ends_sentence

PROMPT = (
    "You restore the punctuation of transcripts of spoken English. Copy the user's text exactly, "
    "word for word, adding only punctuation marks and capital letters. Never add, remove, reorder "
    "or correct a word. Prefer short sentences: end a sentence wherever the speaker finishes a thought, "
    "even when the next one begins with 'and', 'but' or 'so'. The text may start or stop in the middle "
    "of a sentence. Output only the text."
)

# Words in the first stretch after a start or a jump: the first sentence waits for it, and
# the PC's Gemma copies 60 words in 0.8 s. The later ones are longer: 150 words take 2 s.
FIRST_STRETCH = 60
STRETCH = 150
# A sentence end is trusted when at least this many words follow it in the stretch: the
# model has then seen how the text goes on. The last word of a stretch proves nothing.
RIGHT_CONTEXT = 5
# The first stretch after a jump begins this many words before the playhead, so that the
# model can find where the sentence there begins.
LEFT_CONTEXT = 30
# A reply in which fewer than this share of the words is found again is not a copy.
MATCH_FLOOR = 0.9


# What a reply may put around a word: sentence and clause marks, quotes, brackets, dashes.
# Anything else it adds ("$50", "20%", "U.S." for "us") says something the speaker did not.
_MARKS = ".,;:!?\u2026\"'\u201c\u201d\u2018\u2019()[]-\u2013\u2014"


def _unmarked(token: str) -> str:
    """A token without the marks around it and without case; an apostrophe inside it is kept, as a straight one."""
    return token.strip(_MARKS).replace("\u2019", "'").casefold()


class PunctuationError(ValueError):
    """A reply could not be used to punctuate the words it was asked for."""


def restore(words: Sequence[Word], reply: str) -> list[Word]:
    """`words` with the punctuation and capitals of `reply`, their times unchanged.

    The reply is laid over the words by what the two have in common, their letters and
    digits. A token then becomes the reply's token for it when the two differ only in case
    and in the marks around them (see _MARKS); otherwise it stays as the caption has it. So
    a word the model changed, added or left out, or a symbol it put to one, changes nothing.
    Raises PunctuationError when fewer than MATCH_FLOOR of the tokens are found in the reply.
    """
    owners = [n for n, word in enumerate(words) for _ in word.text.split()]
    ours = [token for word in words for token in word.text.split()]
    theirs = reply.split()
    our_keys, their_keys = [bare(token) for token in ours], [bare(token) for token in theirs]
    found = 0
    for block in difflib.SequenceMatcher(None, our_keys, their_keys, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            if our_keys[block.a + k]:  # a token of marks alone ("--") is no word to compare
                found += 1
                if _unmarked(theirs[block.b + k]) == _unmarked(ours[block.a + k]):
                    ours[block.a + k] = theirs[block.b + k]
    known = sum(1 for key in our_keys if key)
    if found < MATCH_FLOOR * known - 1e-9:
        raise PunctuationError(f"only {found} of {known} words came back")
    texts: list[list[str]] = [[] for _ in words]
    for n, token in zip(owners, ours, strict=True):
        texts[n].append(token)
    return [replace(word, text=" ".join(parts)) for word, parts in zip(words, texts, strict=True)]


def settled(words: Sequence[Word], final: bool) -> int:
    """How many of the restored `words` of a stretch are settled, counted from its first.

    All of them when the stretch is the `final` one of its track. Otherwise through the last
    word that ends a sentence and has at least RIGHT_CONTEXT words after it; when there is
    none, through the last word that ends a clause so (a sentence longer than the stretch is
    cut at a comma anyway); and 0 when there is neither.
    """
    if final:
        return len(words)
    last = len(words) - 1 - RIGHT_CONTEXT
    for i in range(last, -1, -1):
        if ends_sentence(words[i].text, words[i + 1].text):
            return i + 1
    for i in range(last, -1, -1):
        if ends_clause(words[i].text):
            return i + 1
    return 0


def _single_tokens(words: Sequence[Word]) -> list[Word]:
    """`words` with every Word that holds several tokens cut into one Word a token.

    A caption typed by a person comes as one Word a line. Its time is shared out by
    characters, rounded to 10 ms like the track's own times; the first token starts where
    the line does and the last ends where it does.
    """
    out: list[Word] = []
    for word in words:
        tokens = word.text.split()
        if len(tokens) <= 1:
            out.append(word)
            continue
        total, done, start = sum(map(len, tokens)), 0, word.start
        for token in tokens:
            done += len(token)
            end = word.end if done == total else round(word.start + (word.end - word.start) * done / total, 2)
            out.append(Word(start, end, token))
            start = end
    return out


class OpenTrack:
    """The words of a caption track without punctuation, found by time and handed out in stretches.

    `words` are what `segmenter.track_words` gives: their ends never go back. They are
    kept a token to a Word, so that a stretch is as long in words whatever the track's
    cues hold.
    """

    def __init__(self, words: Sequence[Word]) -> None:
        self.words = _single_tokens(words)
        self._ends = [word.end for word in self.words]

    def __len__(self) -> int:
        return len(self.words)

    def word_at(self, t: float) -> int:
        """The index of the first word that ends after `t`; len(self) when none does."""
        return bisect.bisect_right(self._ends, t)

    def first_word(self, t: float) -> int:
        """The word to begin restoring at for a playhead at `t`: LEFT_CONTEXT words before the one there."""
        return max(0, self.word_at(t) - LEFT_CONTEXT)

    def starts_at(self, i: int) -> float:
        """When word `i` starts; infinity past the last word."""
        return self.words[i].start if i < len(self.words) else math.inf

    def stretch(self, i: int, first: bool) -> list[Word]:
        """The words of the stretch that begins at word `i`: FIRST_STRETCH of them if it is the `first`, else STRETCH."""
        return self.words[i : i + (FIRST_STRETCH if first else STRETCH)]


def restore_stretch(
    track: OpenTrack, i: int, first: bool, punctuate: Callable[[str], str]
) -> tuple[list[Word], int]:
    """Restore the stretch of `track` at word `i`; return its settled words and the index of the word after them.

    `punctuate` is given the words of the stretch, joined by spaces, and gives the model's
    copy of them. Whatever it raises is passed on, and PunctuationError is raised when the
    copy is none (see restore) or settles nothing (see settled).
    """
    words = track.stretch(i, first)
    final = i + len(words) >= len(track)
    restored = restore(words, punctuate(" ".join(word.text for word in words)))
    count = settled(restored, final)
    if count == 0:
        raise PunctuationError("the reply marks no sentence or clause end")
    return restored[:count], i + count
