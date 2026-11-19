"""Cut the words of a caption track into sentence-sized segments for translation."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import accumulate, pairwise

from dubber.captions import Cue, Word

# A word may start this much before the latest kept start without being a replay of it.
_JITTER = 0.05
# Times are rounded to 10 ms, so a difference meant to equal a limit can land a hair off
# it in floating point (5.72 - 5.12 is 0.5999999999999996). Limits and equal gaps get
# this much give.
_SLACK = 1e-6
_SENTENCE_END = (".", "?", "!")
# Closing quotes and brackets that may follow a sentence's last mark: `them."`, `(see above.)`.
_CLOSERS = "\"')]\u201d\u2019\u00bb"
_OPENERS = "\"'([\u201c\u2018\u00ab"
# Full stops that do not end a sentence: a title before a name, a dotted abbreviation (U.S.,
# e.g., a.m.) and an initial (R. in George R. R. Martin, but not "I.", which is a word). Missing
# a real end here only joins two sentences, which translate well together; cutting at a false
# one leaves two halves that do not.
_TITLES = frozenset("Mr. Mrs. Ms. Mx. Dr. Prof. St. Mt. Gen. Sen. Rep. Gov. Capt. Col. Lt. Sgt. Rev. Hon. Pres. vs. Vs. v.".split())
_DOTTED = re.compile(r"(?:[A-Za-z]\.){2,}")
_INITIAL = re.compile(r"[A-HJ-Z]\.")
# Marks that end a clause: a long sentence is split after one of them before anywhere else.
_CLAUSE_END = (",", ";", ":", "-", "\u2013", "\u2014")
# A track where fewer than one word in this many ends a sentence has lost its punctuation.
_WORDS_PER_SENTENCE_END = 40
# English words a clause does not end on: articles, conjunctions, prepositions, auxiliaries,
# possessives and their contractions. A forced cut right after one leaves it hanging ("... and").
_NEVER_LAST = frozenset(
    """a an the and or but nor so because if although though unless until while whereas than that which who whose
    of to in on at for with from by into onto about between through during against among
    is are was were be been being am will would can could should may might must shall do does did have has had
    my your his her its our their this these those every no not very
    i'm you're he's she's it's we're they're that's there's i've you've we've they've
    i'll you'll he'll she'll we'll they'll
    don't doesn't didn't can't won't wouldn't couldn't shouldn't isn't aren't wasn't weren't""".split()
)
# Words a forced cut is put before, in this order of trust. On the Dune essay's auto-captions
# "so", "now" and "well" start a sentence in three uses out of four and "but" in two out of
# five; "they" and "he" in half; "and" in one out of four, but it always joins two parts.
_STARTS_SENTENCE = frozenset("so now well but however okay ok alright anyway because although".split())
_STARTS_CLAUSE = frozenset(
    "i we he she they i'm we're he's she's they're i've we've they've i'll we'll he'll she'll they'll there's".split()
)
_JOINS = frozenset("and or then also if when while".split())
_NOT_OF_A_WORD = re.compile(r"[^\w']")


@dataclass
class SegmenterConfig:
    """How `segment` cuts a track; the defaults keep nearly every sentence whole.

    On the Dune essay's auto-captions, 114 of 182 sentences run past 15 words or 10 s, and a
    half sentence translates badly into Turkish, which puts the verb last; its longest
    sentence has 59 words and its slowest lasts 16.3 s, so none runs past these limits.
    A pause cuts at `pause` seconds on a track without sentence punctuation, and only at
    `long_pause` on a punctuated one, where sentence marks say where sentences end.
    """

    pause: float = 0.6
    long_pause: float = 1.5
    min_words: int = 6
    max_words: int = 60
    max_seconds: float = 18.0


@dataclass(frozen=True)
class Segment:
    """One clause of the track.

    It runs from the start of its first word to the end of its last, and `words` is the
    number of whitespace-separated tokens in `text`.
    """

    idx: int
    start: float
    end: float
    text: str
    words: int


def ends_sentence(text: str, following: str = "") -> bool:
    """Whether the last token of `text` ends a sentence, given the text that follows it.

    It must end with ".", "?" or "!", before any closing quotes or brackets, and not be an
    ellipsis, a title, a dotted abbreviation or an initial. "No." before a number does not
    end one, and neither does a quote or "etc." before a lowercase word: "Why?" he asked.
    """
    tokens = text.split()
    if not tokens:
        return False
    token = tokens[-1].rstrip(_CLOSERS)
    if not token.endswith(_SENTENCE_END) or token.endswith("..."):
        return False
    if token in _TITLES or _DOTTED.fullmatch(token) or _INITIAL.fullmatch(token):
        return False
    next_char = following.lstrip().lstrip(_OPENERS)[:1]
    if token == "No." and next_char.isdigit():
        return False
    return not ((token != tokens[-1] or token == "etc.") and next_char.islower())


def ends_clause(text: str) -> bool:
    """Whether text ends with a comma, semicolon, colon or dash, before any closing quotes."""
    return text.rstrip().rstrip(_CLOSERS).endswith(_CLAUSE_END)


def _size(word: Word) -> int:
    # A Word from a seg without its own timing can hold several tokens.
    return len(word.text.split())


def _fits(words: Sequence[Word], max_words: int, max_seconds: float) -> bool:
    seconds = words[-1].end - words[0].start
    return sum(map(_size, words)) <= max_words and seconds <= max_seconds + _SLACK


def _replays(word: Word, last: Word) -> bool:
    """Whether word re-delivers what the last kept word already covered.

    Kept starts never go back, so the last kept word holds the latest start. A replay
    starts earlier than that by more than the jitter, or repeats that word's text at the
    same time.
    """
    shift = word.start - last.start
    jitter = _JITTER + _SLACK
    return shift < -jitter or (abs(shift) <= jitter and word.text == last.text)


def _flatten(cues: Sequence[Cue]) -> list[Word]:
    """List the words of all cues in cue order, leaving out rolling duplicates.

    Only the cues are sorted. Sorting the words as well would interleave a replay with
    the words it repeats, and the drop rule could no longer see it. A word kept a little
    before the latest start is moved up to it, so starts never go back.
    """
    kept: list[Word] = []
    for cue in sorted(cues, key=lambda cue: cue.start):
        for word in cue.words:
            if kept:
                if _replays(word, kept[-1]):
                    continue
                latest = kept[-1].start
                if word.start < latest:
                    word = replace(word, start=latest, end=max(word.end, latest))
            kept.append(word)
    return kept


def _split_sentence_ends(words: Sequence[Word]) -> list[Word]:
    """Cut each Word after every token but its last that ends a sentence.

    A human caption arrives as one Word per cue, so without this a sentence that ends inside
    a cue would never be cut there. Each piece gets a share of the Word's time in proportion
    to its non-space characters, rounded to 10 ms like the track's own times. Run it on
    trimmed Words: a caption's display window runs past the speech, and sharing that out
    would put a sentence after the cue that follows.
    """
    out: list[Word] = []
    for word in words:
        tokens = word.text.split()
        cuts = [i + 1 for i, (token, after) in enumerate(pairwise(tokens)) if ends_sentence(token, after)]
        if not cuts:
            out.append(word)
            continue
        pieces = [tokens[a:b] for a, b in pairwise([0, *cuts, len(tokens)])]
        sizes = [sum(map(len, piece)) for piece in pieces]
        total, done, start = sum(sizes), 0, word.start
        for piece, size in zip(pieces, sizes, strict=True):
            done += size
            end = word.end if done == total else round(word.start + (word.end - word.start) * done / total, 2)
            out.append(Word(start, end, " ".join(piece)))
            start = end
    return out


def _trim_ends(words: Sequence[Word]) -> list[Word]:
    """End each word no later than the next one starts, and never before its own start.

    A caption stays on screen after its word is said, so its end often runs into the
    next cue. Trimmed, no gap is negative, a segment ends before the next one starts,
    and a duration measures speech rather than the display window.
    """
    trimmed = [
        replace(word, end=max(word.start, min(word.end, after.start)))
        for word, after in pairwise(words)
    ]
    return trimmed + list(words[-1:])


def _is_punctuated(words: Sequence[Word]) -> bool:
    """Whether at least one token in 40 ends a sentence."""
    ends = sum(ends_sentence(word.text, after.text) for word, after in pairwise(words))
    ends += bool(words) and ends_sentence(words[-1].text)
    return ends * _WORDS_PER_SENTENCE_END >= sum(map(_size, words))


def _is_boundary(last: Word, word: Word, pause: float) -> bool:
    """Whether a cut falls between last and the word after it."""
    if ends_sentence(last.text, word.text):
        return True
    return word.start - last.end >= pause - _SLACK


def _cut_into_runs(words: Sequence[Word], pause: float) -> list[list[Word]]:
    runs: list[list[Word]] = []
    for word in words:
        if runs and not _is_boundary(runs[-1][-1], word, pause):
            runs[-1].append(word)
        else:
            runs.append([word])
    return runs


def bare(token: str) -> str:
    """An English caption token without its punctuation and case, to look it up in the word lists."""
    return _NOT_OF_A_WORD.sub("", token.replace("\u2019", "'")).casefold()


def _between_clauses(part: Sequence[Word], candidates: Sequence[int], tokens_up_to: Sequence[int]) -> list[int]:
    """Of the equally good cuts `candidates`, those where a clause may end; none if no cut is one.

    A cut counts when it leaves each side at least a quarter of the tokens and does not
    follow a word a clause never ends on. Of these, the ones before a word that starts a
    sentence are taken if there are any, else the ones before a subject pronoun, else the
    ones before a word that joins two parts, else all of them. Without punctuation nothing
    says where a sentence ends: on the Dune essay's auto-captions this puts half the forced
    cuts on a sentence end and none after "and", where the middle put 3 % and 43 %.
    """
    total = tokens_up_to[-1]
    kept = [
        i
        for i in candidates
        if min(tokens_up_to[i], total - tokens_up_to[i]) >= total / 4
        and bare((part[i].text.split() or [""])[-1]) not in _NEVER_LAST
    ]
    for starters in (_STARTS_SENTENCE, _STARTS_CLAUSE, _JOINS):
        before = [i for i in kept if bare((part[i + 1].text.split() or [""])[0]) in starters]
        if before:
            return before
    return kept


def _best_cut(part: Sequence[Word]) -> int:
    """Return how many Words stay on the left of the cut.

    The cut falls after a word that ends a clause (a comma, semicolon, colon or dash), so
    that each side can be translated on its own; only such a cut that leaves each side at
    least a quarter of the tokens counts, so a lone "So," is never a segment. Without one,
    it falls at the largest gap, and gaps within the slack of the largest tie; when several
    tie, as all do on an auto-caption track, whose words have no gaps, the words decide
    (see _between_clauses). Among the candidates, the cut that leaves the token counts of
    the two sides nearest equal wins, the earlier one if two are equally near.
    """
    # tokens_up_to[i] counts the tokens up to and including word i
    tokens_up_to = list(accumulate(map(_size, part)))
    total = tokens_up_to[-1]
    half = total / 2
    candidates = [
        i
        for i, word in enumerate(part[:-1])
        if ends_clause(word.text) and min(tokens_up_to[i], total - tokens_up_to[i]) >= total / 4
    ]
    if not candidates:
        gaps = [after.start - before.end for before, after in pairwise(part)]
        top = max(gaps)
        candidates = [i for i, gap in enumerate(gaps) if gap >= top - _SLACK]
        candidates = _between_clauses(part, candidates, tokens_up_to) or candidates
    return min(candidates, key=lambda i: abs(tokens_up_to[i] - half)) + 1


def _split(run: list[Word], max_words: int, max_seconds: float) -> list[list[Word]]:
    """Split a run at its largest gap, again and again, until every part fits.

    A gap lies between two Words, so a part holding a single Word is final however
    long it is.
    """
    parts: list[list[Word]] = []
    pending = [run]
    while pending:
        part = pending.pop()
        if len(part) == 1 or _fits(part, max_words, max_seconds):
            parts.append(part)
            continue
        cut = _best_cut(part)
        pending += [part[cut:], part[:cut]]  # the left part is popped first
    return parts


def _build(idx: int, words: Sequence[Word]) -> Segment:
    tokens = [token for word in words for token in word.text.split()]
    return Segment(idx, words[0].start, words[-1].end, " ".join(tokens), len(tokens))


def looks_unpunctuated(segments: Sequence[Segment]) -> bool:
    """Whether fewer than one word in 40 ends a sentence: a track that has lost its punctuation.

    Every sentence end cuts, so the segments that end with a sentence mark count them.
    """
    words = sum(seg.words for seg in segments)
    ends = sum(1 for seg in segments if ends_sentence(seg.text))
    return ends * _WORDS_PER_SENTENCE_END < words


def track_words(cues: Sequence[Cue]) -> list[Word]:
    """The words of the cues in time order, rolling duplicates left out and ends trimmed.

    Word times only move forward: a start never precedes the one before it, and an end
    never passes the next start.
    """
    return _trim_ends(_flatten(cues))


def segment_words(
    words: Sequence[Word],
    *,
    first_idx: int = 0,
    punctuated: bool | None = None,
    pause: float = 0.6,
    long_pause: float = 1.5,
    min_words: int = 6,
    max_words: int = 60,
    max_seconds: float = 18.0,
) -> list[Segment]:
    """Cut words that `track_words` gave into segments numbered from `first_idx`.

    A Word holding several tokens (a human caption gives one per cue) is cut after each of
    its tokens but the last that ends a sentence, its time shared out by characters. The
    words are cut into runs after a word that ends a sentence (see ends_sentence) and
    before a gap of at least `pause` seconds, or of `long_pause` when the words are
    `punctuated`; None asks the words themselves, which are when at least one token in 40
    ends a sentence. A run over `max_words` words or `max_seconds` seconds is split again
    and again until every part fits (see _best_cut). The last segment joins the one before
    it when it has fewer than `min_words` words and the result still fits. Words are
    whitespace-separated tokens, so one Word can count as several.
    """
    words = _split_sentence_ends(words)
    if punctuated is None:
        punctuated = _is_punctuated(words)
    parts = [
        part
        for run in _cut_into_runs(words, long_pause if punctuated else pause)
        for part in _split(run, max_words, max_seconds)
    ]
    # A short tail is too little to translate on its own: it joins the part before it
    # when the result still fits.
    if len(parts) >= 2 and sum(map(_size, parts[-1])) < min_words:
        merged = parts[-2] + parts[-1]
        if _fits(merged, max_words, max_seconds):
            parts[-2:] = [merged]
    return [_build(first_idx + n, part) for n, part in enumerate(parts)]


def segment(
    cues: Sequence[Cue],
    *,
    pause: float = 0.6,
    long_pause: float = 1.5,
    min_words: int = 6,
    max_words: int = 60,
    max_seconds: float = 18.0,
) -> list[Segment]:
    """Cut the words of the cues into segments: `segment_words` of their `track_words`.

    The cues are sorted by start and their words read in that order, dropping rolling
    duplicates.
    """
    return segment_words(
        track_words(cues),
        pause=pause,
        long_pause=long_pause,
        min_words=min_words,
        max_words=max_words,
        max_seconds=max_seconds,
    )
