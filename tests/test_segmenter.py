import random
from itertools import pairwise

import pytest

from dubber.captions import Cue, Word
from dubber.segmenter import Segment, segment

# The caps of Phase 1 (15 words, 10 s). The tests of how an overlong run is split keep them, so
# that their small examples still need splitting now that the defaults keep sentences whole.
def segment15(cues, **kwargs):
    return segment(cues, **{"max_words": 15, "max_seconds": 10.0, **kwargs})


def cue(start, texts, step=0.3, end=None):
    words = tuple(Word(start=round(start + i * step, 2), end=round(start + (i + 1) * step, 2), text=t) for i, t in enumerate(texts))
    return Cue(start=start, end=end or words[-1].end, text=" ".join(texts), words=words)

def test_splits_on_terminal_punctuation():
    segs = segment([cue(0.0, "one two three four five six seven.".split()), cue(2.5, "eight nine ten eleven twelve thirteen?".split())])
    assert [s.text for s in segs] == ["one two three four five six seven.", "eight nine ten eleven twelve thirteen?"]
    assert segs[0].start == 0.0 and segs[1].start == 2.5

def test_splits_on_pause_without_punctuation():
    c = cue(0.0, "a b c d e f g".split())
    later = cue(c.end + 0.7, "h i j k l m n".split())
    segs = segment([c, later])
    assert len(segs) == 2 and segs[1].text == "h i j k l m n"

def test_long_run_splits_at_largest_gap_and_respects_max_words():
    words = [f"w{i}" for i in range(20)]
    c = cue(0.0, words, step=0.2)
    segs = segment15([c], pause=0.6)
    assert all(s.words <= 15 for s in segs) and sum(s.words for s in segs) == 20

def test_short_tail_merges_into_previous():
    segs = segment([cue(0.0, "a b c d e f g h.".split()), cue(3.0, "ok.".split())])
    assert len(segs) == 1 and segs[0].text.endswith("h. ok.")

def test_out_of_order_rolling_words_are_dropped_and_segments_increase():
    c1 = cue(0.0, "a b c d e f.".split())
    dup = Cue(start=0.3, end=1.8, text="b c d e f.", words=tuple(Word(0.3 + i * 0.3, 0.6 + i * 0.3, t) for i, t in enumerate("b c d e f.".split())))
    segs = segment([c1, dup, cue(2.5, "g h i j k l.".split())])
    assert [s.text for s in segs][0].startswith("a b c d e f.")
    assert all(segs[i].start < segs[i + 1].start for i in range(len(segs) - 1))

# Rules the brief leaves implicit: input handling, boundaries, limits, the tail, replays.

def test_no_words_give_no_segments():
    assert segment([]) == []
    assert segment([Cue(start=0.0, end=1.0, text="", words=())]) == []

def test_segment_times_come_from_its_first_and_last_word():
    c = Cue(start=0.0, end=9.0, text="hello world.", words=(Word(0.5, 1.0, "hello"), Word(1.0, 2.0, "world.")))
    assert segment([c]) == [Segment(idx=0, start=0.5, end=2.0, text="hello world.", words=2)]

def test_cues_out_of_order_come_out_in_time_order_with_idx_from_zero():
    early = cue(0.0, "one two three four five six.".split())
    late = cue(5.0, "seven eight nine ten eleven twelve.".split())
    segs = segment([late, early])
    assert [(s.idx, s.start) for s in segs] == [(0, 0.0), (1, 5.0)]

def test_multi_token_word_counts_as_several_words_and_loses_doubled_spaces():
    # parse_json3 turns a seg without its own timing into one Word holding several tokens
    c = Cue(start=61.0, end=64.0, text="", words=(Word(61.0, 62.0, "The  ship"), Word(62.0, 64.0, "is called a Heighliner and imaginative.")))
    [seg] = segment([c])
    assert (seg.text, seg.words) == ("The ship is called a Heighliner and imaginative.", 8)

def test_multi_token_word_is_never_split_and_cuts_fall_between_words():
    big = Word(0.0, 5.0, " ".join(f"w{i}" for i in range(20)))
    c = Cue(start=0.0, end=5.5, text="", words=(big, Word(5.0, 5.5, "end.")))
    assert [s.words for s in segment15([c])] == [20, 1]

@pytest.mark.parametrize("mark, expected", [(".", 2), ("?", 2), ("!", 2), (",", 1)])
def test_only_sentence_ending_punctuation_cuts_without_a_pause(mark, expected):
    first = cue(0.0, f"one two three four five six{mark}".split())
    second = cue(first.end, "seven eight nine ten eleven twelve".split())
    assert len(segment([first, second])) == expected

def test_multi_token_word_ends_a_sentence_when_its_last_token_does():
    # a stray space after the full stop does not hide it
    words = (Word(0.0, 3.0, "think it's really quite imaginative. "), Word(3.0, 6.0, "but think again please everyone now"))
    assert [s.words for s in segment([Cue(start=0.0, end=6.0, text="", words=words)])] == [5, 6]

@pytest.mark.parametrize("pause, expected", [(0.6, 2), (0.61, 1)])
def test_a_gap_of_exactly_pause_is_a_boundary(pause, expected):
    first = cue(3.02, "a b c d e f g".split())  # ends at 5.12
    second = cue(5.72, "h i j k l m n".split())  # 0.6 s later, though 5.72 - 5.12 is 0.5999999999999996
    assert len(segment([first, second], pause=pause)) == expected

def test_run_over_max_words_splits_at_its_largest_gap():
    first = cue(0.0, [f"a{i}" for i in range(12)], step=0.25)
    second = cue(3.5, [f"b{i}" for i in range(6)], step=0.25)  # 0.5 s after the first, under pause
    assert [s.words for s in segment15([first, second])] == [12, 6]

def test_run_over_max_seconds_splits_at_its_largest_gap():
    # twelve words but 12.75 s; the 0.5 s gap is larger than the 0.25 s one
    parts = [cue(0.0, ["a1", "a2", "a3", "a4"], step=1.0), cue(4.25, ["b1", "b2", "b3", "b4"], step=1.0), cue(8.75, ["c1", "c2", "c3", "c4"], step=1.0)]
    assert [s.words for s in segment15(parts)] == [8, 4]

def test_run_without_a_gap_splits_into_even_halves():
    # the brief's 20 words with no gap anywhere: among equal gaps, the cut nearest the middle
    assert [s.words for s in segment15([cue(0.0, [f"w{i}" for i in range(20)], step=0.2)])] == [10, 10]

def test_equally_near_cuts_take_the_earlier():
    # 17 words: cutting after the 8th or the 9th is equally near the middle
    assert [s.words for s in segment15([cue(0.0, [f"w{i}" for i in range(17)])])] == [8, 9]

def test_even_halves_count_tokens_not_words():
    # a 10-token Word and eight single words: the cut nearest 9 of 18 tokens falls right
    # after the big Word, where a cut by Word count would fall after the fourth Word
    big = Word(0.0, 3.0, " ".join(f"t{i}" for i in range(10)))
    rest = tuple(Word(round(3.0 + i * 0.3, 2), round(3.3 + i * 0.3, 2), f"w{i}") for i in range(8))
    assert [s.words for s in segment15([Cue(start=0.0, end=5.4, text="", words=(big, *rest))])] == [10, 8]

def test_unique_largest_gap_off_centre_beats_the_middle():
    short = cue(0.0, ["a0", "a1"], step=0.25)
    long = cue(1.0, [f"b{i}" for i in range(16)], step=0.25)  # 0.5 s after the short one
    assert [s.words for s in segment15([short, long])] == [2, 8, 8]

def test_gaps_equal_to_the_written_precision_tie_despite_float_noise():
    # both gaps are 0.4 s, but 2.2 - 1.8 and 4.4 - 4.0 differ in the last bits
    parts = [cue(0.0, [f"a{i}" for i in range(6)]), cue(2.2, [f"b{i}" for i in range(6)]), cue(4.4, [f"c{i}" for i in range(6)])]
    assert [s.words for s in segment15(parts)] == [6, 12]

def test_a_run_exactly_at_the_limits_is_not_split():
    assert [s.words for s in segment([cue(0.0, [f"w{i}" for i in range(15)])])] == [15]
    # 6.01 to 16.01 is 10 s, though 16.01 - 6.01 is 10.000000000000002
    assert [s.words for s in segment([cue(6.01, [f"w{i}" for i in range(5)], step=2.0)])] == [5]

def test_limits_can_be_changed():
    run = [cue(0.0, ["a", "b", "c", "d"]), cue(1.7, ["e", "f", "g", "h"])]  # 8 words over 2.9 s, one 0.5 s gap
    assert [s.words for s in segment(run)] == [8]
    assert [s.words for s in segment(run, max_words=6)] == [4, 4]
    assert [s.words for s in segment(run, max_seconds=2.5)] == [4, 4]
    tail = [cue(0.0, "a b c d e f g h.".split()), cue(3.0, ["ok", "now."])]
    assert [s.words for s in segment(tail)] == [10]
    assert [s.words for s in segment(tail, min_words=2)] == [8, 2]

def test_short_sentence_in_the_middle_is_kept_apart():
    segs = segment([
        cue(0.0, "one two three four five six seven eight.".split()),
        cue(3.0, ["Yes."]),
        cue(4.0, "nine ten eleven twelve thirteen fourteen fifteen sixteen.".split()),
    ])
    assert [s.words for s in segs] == [8, 1, 8]

def test_short_tail_stays_apart_when_merging_would_break_max_words():
    segs = segment15([cue(0.0, [f"w{i}" for i in range(13)] + ["end."], step=0.1), cue(2.0, ["ok", "now."])])
    assert [s.words for s in segs] == [14, 2]

def test_short_tail_stays_apart_when_merging_would_break_max_seconds():
    segs = segment15([cue(0.0, "a b c d e f g.".split(), step=1.0), cue(9.0, ["ok."], step=2.0)])
    assert [s.words for s in segs] == [7, 1]

def test_replayed_cue_leaves_no_trace_not_even_its_last_word():
    first = cue(0.0, "a b c d e f.".split())
    replay = Cue(start=0.3, end=1.8, text="b c d e f.", words=tuple(Word(0.3 + i * 0.3, 0.6 + i * 0.3, t) for i, t in enumerate("b c d e f.".split())))
    segs = segment([first, replay, cue(2.5, "g h i j k l.".split())])
    assert [s.text for s in segs] == ["a b c d e f.", "g h i j k l."]

def test_word_up_to_0_05_s_before_the_latest_start_is_kept_and_an_earlier_one_is_not():
    words = (Word(0.0, 1.0, "a"), Word(1.5, 2.0, "b"), Word(1.5, 2.0, "c"), Word(1.45, 2.0, "d"), Word(1.38, 2.0, "e"))
    [seg] = segment([Cue(start=0.0, end=2.0, text="", words=words)])
    assert seg.text == "a b c d"  # c shares b's start, d is 0.05 s before it, e is 0.12 s before it

def test_starts_never_go_back_along_a_chain_of_words_0_04_s_apart():
    words = (Word(1.0, 1.5, "a."), Word(0.96, 0.98, "b."), Word(0.92, 1.4, "c."))
    segs = segment([Cue(start=0.92, end=1.5, text="", words=words)], min_words=1)
    # b is kept with its start (and end) raised to 1.0; c is 0.08 s before that and dropped
    assert [(s.text, s.start, s.end) for s in segs] == [("a.", 1.0, 1.0), ("b.", 1.0, 1.0)]

def test_overlapping_cues_end_where_the_next_one_starts():
    # the first cue's last word stays on screen until 3.5 s, 1.0 s after the second cue starts
    first = Cue(start=0.0, end=3.5, text="", words=(*cue(0.0, "one two three four five".split()).words, Word(1.5, 3.5, "six.")))
    segs = segment([first, cue(2.5, "seven eight nine ten eleven twelve.".split())])
    assert segs[0].end == segs[1].start == 2.5

def test_a_word_never_ends_before_it_starts():
    words = (Word(2.0, 1.0, "a."), Word(3.0, 4.0, "b."))  # the first one is malformed
    segs = segment([Cue(start=2.0, end=4.0, text="", words=words)], min_words=1)
    assert segs[0].end == 2.0

def test_overlap_leaves_no_negative_gap_so_the_even_cut_can_fall_between_cues():
    # a8 stays on screen until 5.0 s, past the next cue's start at 3.0 s
    first = Cue(start=0.0, end=5.0, text="", words=(*cue(0.0, [f"a{i}" for i in range(8)]).words, Word(2.4, 5.0, "a8")))
    second = cue(3.0, [f"b{i}" for i in range(9)])
    assert [s.words for s in segment15([first, second])] == [9, 9]


def test_a_human_cue_holding_two_sentences_splits_at_the_period():
    text = "It was late. The ship left at dawn."
    segs = segment([Cue(10.0, 14.0, text, (Word(10.0, 14.0, text),))], min_words=1)
    assert [s.text for s in segs] == ["It was late.", "The ship left at dawn."]
    # 10 of the 28 non-space characters belong to the first sentence
    assert segs[1].start == pytest.approx(10.0 + 4.0 * 10 / 28, abs=0.01)
    assert segs[0].start == 10.0 and segs[1].end == 14.0


def test_the_tail_of_a_cue_joins_the_words_of_the_next_cue():
    a = Cue(0.0, 3.0, "We left. The night", (Word(0.0, 3.0, "We left. The night"),))
    b = Cue(3.0, 5.0, "was cold.", (Word(3.0, 5.0, "was cold."),))
    assert [s.text for s in segment([a, b], min_words=1)] == ["We left.", "The night was cold."]


@pytest.mark.parametrize("end", ['them."', "them.)", "them?”"])
def test_a_sentence_end_before_a_closing_quote_or_bracket_cuts(end):
    segs = segment([cue(0.0, ["They", "said", end]), cue(1.0, ["Then", "silence."])], min_words=1)
    assert len(segs) == 2


def test_a_sentence_of_thirty_words_stays_one_segment():
    words = [f"w{i}" for i in range(29)] + ["end."]
    assert [s.words for s in segment([cue(0.0, words, step=0.4)])] == [30]


def test_a_sentence_over_the_cap_splits_at_the_clause_comma_nearest_its_middle():
    words = [f"w{i}" for i in range(50)]
    words[9] += ","
    words[23] += ","
    words[49] += "."
    assert [s.words for s in segment([cue(0.0, words, step=0.3)], max_words=40)] == [24, 26]


def test_a_clause_comma_is_preferred_to_a_longer_pause():
    first = [f"w{i}" for i in range(35)]
    first[19] += ","  # a comma after the 20th word, the only pause (0.5 s) after the 35th
    rest = [f"w{i}" for i in range(35, 45)]
    rest[-1] += "."
    head = cue(0.0, first, step=0.3)
    assert [s.words for s in segment([head, cue(head.end + 0.5, rest, step=0.3)], max_words=40)] == [20, 25]


def test_a_comma_before_a_closing_quote_is_a_clause_cut():
    words = [f"w{i}" for i in range(50)]
    words[23] += ',"'
    words[49] += "."
    assert [s.words for s in segment([cue(0.0, words, step=0.3)], max_words=40)] == [24, 26]


def test_without_a_comma_an_overlong_sentence_splits_at_its_largest_gap():
    head = cue(0.0, [f"w{i}" for i in range(31)], step=0.3)
    rest = [f"w{i}" for i in range(31, 50)]
    rest[-1] += "."
    assert [s.words for s in segment([head, cue(head.end + 0.5, rest, step=0.3)], max_words=40)] == [31, 19]


def test_the_longest_sentence_of_the_dune_track_stays_whole():
    words = [f"w{i}" for i in range(58)] + ["end."]  # 59 words in 16.3 s
    assert [s.words for s in segment([cue(0.0, words, step=0.28)])] == [59]


def test_a_sentence_over_eighteen_seconds_is_split():
    words = [f"w{i}" for i in range(39)] + ["end."]  # 40 words in 20 s
    assert [s.words for s in segment([cue(0.0, words, step=0.5)])] == [20, 20]


FULL_STOPS = [
    ("We met Dr. Smith and Mr. Jones in the U.S. last year on a very long trip.", 1),
    ("George R. R. Martin wrote it in the U.K. around 1991.", 1),
    ("I was... I was thinking that maybe... you know what I mean.", 1),
    ("See No. 5 on the list for details about this.", 1),
    ('"Why?" he asked me on the way back home.', 1),
    ("Apples, pears, etc. and other fruit were there.", 1),
    ("No. I do not think so at all today.", 2),
    ("It cost 3.5 dollars. Then the price went up again.", 2),
    ("So did I. Then we all went back home again.", 2),
]


@pytest.mark.parametrize("text, pieces", FULL_STOPS)
def test_only_a_real_sentence_end_cuts_a_human_cue(text, pieces):
    assert len(segment([Cue(0.0, 10.0, text, (Word(0.0, 10.0, text),))], min_words=1)) == pieces


@pytest.mark.parametrize("text, pieces", FULL_STOPS)
def test_only_a_real_sentence_end_cuts_timed_words(text, pieces):
    assert len(segment([cue(0.0, text.split())], min_words=1)) == pieces


def one_word_cue(start, end, text):
    """A human caption: one Word holds the whole cue."""
    return Cue(start, end, text, (Word(start, end, text),))


def test_a_pause_inside_a_punctuated_sentence_does_not_cut_it():
    cues = [one_word_cue(0.0, 3.0, "We left. The ship"), one_word_cue(3.7, 5.0, "left at dawn.")]
    assert [s.text for s in segment(cues, min_words=1)] == ["We left.", "The ship left at dawn."]


@pytest.mark.parametrize("gap, pieces", [(1.4, 1), (1.5, 2)])
def test_only_a_long_pause_cuts_a_punctuated_sentence(gap, pieces):
    cues = [one_word_cue(0.0, 2.0, "We left"), one_word_cue(2.0 + gap, 5.0, "at dawn.")]
    assert len(segment(cues, min_words=1)) == pieces


def test_a_track_without_sentence_ends_still_cuts_at_the_short_pause():
    cues = [one_word_cue(0.0, 2.0, "we left the city"), one_word_cue(2.7, 5.0, "and the night was cold")]
    assert len(segment(cues, min_words=1)) == 2


def test_a_cue_shown_past_the_next_one_shares_only_the_time_before_it():
    text = "One two three. Four five six. Seven eight nine."
    segs = segment([one_word_cue(0.0, 10.0, text), one_word_cue(5.0, 5.5, "Ten.")], min_words=1)
    assert [s.text for s in segs] == ["One two three.", "Four five six.", "Seven eight nine.", "Ten."]
    assert [s.start for s in segs] == sorted(s.start for s in segs) and segs[-1].start == 5.0


def test_segments_stay_in_time_order_on_overlapping_human_cues():
    rng = random.Random(7)
    for _ in range(300):
        cues, start = [], 0.0
        for _ in range(rng.randint(2, 8)):
            words = [f"w{i}." if rng.random() < 0.5 else f"w{i}" for i in range(rng.randint(1, 6))]
            end = round(start + rng.uniform(0.5, 6.0), 2)
            cues.append(one_word_cue(round(start, 2), end, " ".join(words)))
            start = max(start + 0.1, end - rng.uniform(0.0, 1.0))  # the next cue may start before this one ends
        segs = segment(cues, min_words=1)
        assert all(a.start <= b.start and a.end <= b.start for a, b in pairwise(segs))


# The words of a track, cut without its cues: a stretch of an open track is cut this way.

from dubber.segmenter import segment_words, track_words


def test_segment_words_gives_what_segment_gives():
    cues = [
        cue(0.0, "one two three four five six seven.".split()),
        cue(2.5, "eight nine ten eleven twelve thirteen?".split()),
        cue(9.0, "a b c d e f g".split()),
    ]
    assert segment_words(track_words(cues)) == segment(cues)
    assert segment_words(track_words(cues), max_words=4, max_seconds=10.0) == segment(cues, max_words=4, max_seconds=10.0)


def test_track_words_drops_replays_and_trims_ends():
    first = Cue(0.0, 5.0, "a b", (Word(0.0, 1.0, "a"), Word(1.0, 5.0, "b")))
    replay = Cue(1.0, 6.0, "b c", (Word(1.0, 2.0, "b"), Word(2.0, 6.0, "c")))
    assert track_words([first, replay]) == [Word(0.0, 1.0, "a"), Word(1.0, 2.0, "b"), Word(2.0, 6.0, "c")]


def test_segment_words_numbers_from_first_idx():
    words = track_words([cue(0.0, "one two three four five six seven.".split()), cue(2.5, "eight nine ten eleven twelve thirteen?".split())])
    assert [s.idx for s in segment_words(words, first_idx=7)] == [7, 8]


def test_segment_words_punctuated_is_forced():
    # 0.8 s between the cues: a pause on a track without punctuation, not on one with it
    words = track_words([cue(0.0, "a b c d e f g".split()), cue(2.9, "h i j k l m n".split())])
    assert len(segment_words(words, punctuated=False)) == 2
    assert len(segment_words(words, punctuated=True)) == 1
    assert len(segment_words(words)) == 2  # asked of the words themselves, as segment does


def test_segment_words_of_nothing_is_empty():
    assert segment_words([]) == []


# A forced cut where every gap is the same, as on an auto-caption track: the words decide.

def one_cut(text, **kwargs):
    tokens = text.split()
    return [s.text for s in segment([cue(0.0, tokens)], max_words=len(tokens) - 1, max_seconds=1000.0, **kwargs)]


def test_a_forced_cut_never_follows_a_function_word():
    # the middle of these 16 words is right after "the"
    assert one_cut("one two three four five six seven the eight nine ten eleven twelve thirteen fourteen fifteen") == [
        "one two three four five six seven",
        "the eight nine ten eleven twelve thirteen fourteen fifteen",
    ]
    for word in ("and", "of", "is", "it's", "And", "because"):
        left, _ = one_cut(f"one two three four five six seven {word} eight nine ten eleven twelve thirteen fourteen fifteen")
        assert left == "one two three four five six seven", word


def test_a_forced_cut_falls_before_a_word_that_starts_a_sentence():
    left, right = one_cut("w0 w1 w2 w3 w4 w5 w6 w7 w8 w9 so w11 w12 w13 w14 w15")
    assert left.endswith("w9") and right.startswith("so w11")
    # but not when that leaves a side under a quarter of the words
    left, right = one_cut("w0 w1 so w3 w4 w5 w6 w7 w8 w9 w10 w11 w12 w13 w14 w15")
    assert left.endswith("w7") and right.startswith("w8")


def test_a_forced_cut_prefers_so_to_they_and_they_to_and():
    words = [f"w{i}" for i in range(24)]
    def cut_of(**at):
        tokens = list(words)
        for word, i in at.items():
            tokens[i] = word
        return one_cut(" ".join(tokens))[1].split()[0]
    assert cut_of(**{"and": 12, "they": 9, "so": 7}) == "so"
    assert cut_of(**{"and": 12, "they": 9}) == "they"
    assert cut_of(**{"and": 12}) == "and"
    assert cut_of(**{"and": 15, "but": 14}) == "but"   # "but" starts a sentence, "and" only joins
    assert cut_of() == "w12"                            # nothing to go by: the middle, as before


def test_a_forced_cut_with_one_largest_gap_is_as_before():
    # a real gap says more than the words do: the cut stays at it, even after "the"
    first = cue(0.0, "a b c d e f the".split())
    second = cue(first.end + 0.4, "h i j k l m n o p".split())
    assert [s.text for s in segment15([first, second], pause=0.6)] == ["a b c d e f the", "h i j k l m n o p"]
