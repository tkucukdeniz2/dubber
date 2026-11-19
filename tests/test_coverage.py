import pytest

from dubber.coverage import Coverage
from dubber.segmenter import Segment


def seg(idx, start, end):
    return Segment(idx, start, end, f"segment {idx}", 2)


# 10..12, 20..22, 30..32, 40..42
FOUR = [seg(0, 10.0, 12.0), seg(1, 20.0, 22.0), seg(2, 30.0, 32.0), seg(3, 40.0, 42.0)]


def made(*produced, segments=FOUR):
    """A Coverage of `segments` with the given indexes produced, each output at its start."""
    c = Coverage(segments)
    for i in produced:
        c.mark(i, segments[i].start)
    return c


def test_an_empty_track_covers_nothing_and_has_nothing_left():
    c = Coverage([])
    assert c.segments == [] and c.produced_count == 0
    assert c.index_at(5.0) == 0
    assert c.covered_until(5.0) == 5.0
    assert c.first_unproduced(5.0) is None and c.all_produced_from(5.0)
    assert not c.produced_at(5.0) and not c.missing_at(5.0)
    assert c.passed(100.0) == []


@pytest.mark.parametrize("t, index", [
    (0.0, 0), (10.0, 0), (11.9, 0),
    (12.0, 1),            # a segment ending exactly at t is behind it
    (15.0, 1),            # in a gap: the next segment
    (41.0, 3), (42.0, 4), (99.0, 4),
])
def test_the_segment_at_t_is_the_first_that_ends_after_it(t, index):
    assert Coverage(FOUR).index_at(t) == index

def test_covered_until_walks_the_produced_run_from_the_segment_at_t():
    c = made(1, 2)
    assert c.covered_until(15.0) == 32.0      # from 20..22 through 30..32
    assert c.covered_until(21.0) == 32.0
    assert c.covered_until(5.0) == 5.0        # 10..12, the segment at 5, is not produced
    assert c.covered_until(35.0) == 35.0      # 40..42 is not produced

def test_a_gap_in_the_produced_set_ends_the_run():
    c = made(0, 2, 3)
    assert c.covered_until(9.0) == 12.0
    assert c.first_unproduced(9.0) == 1
    assert c.covered_until(25.0) == 42.0      # past the gap, the run goes to the end
    assert c.first_unproduced(25.0) is None and c.all_produced_from(25.0)
    assert not c.all_produced_from(9.0)

def test_first_unproduced_skips_what_lies_behind_t():
    c = made(1)
    assert c.first_unproduced(5.0) == 0
    assert c.first_unproduced(13.0) == 2      # 10..12 is behind, 20..22 produced

def test_the_vacuous_case_past_the_last_segment():
    c = made()
    assert c.all_produced_from(42.0) and c.first_unproduced(42.0) is None
    assert c.covered_until(50.0) == 50.0
    assert not c.produced_at(50.0) and not c.missing_at(50.0)

def test_produced_at_and_missing_at_look_at_the_segment_at_t():
    c = made(1)
    assert c.produced_at(15.0) and c.produced_at(21.0) and not c.missing_at(21.0)
    assert c.missing_at(11.0) and not c.produced_at(11.0)
    assert c.missing_at(25.0)                 # in a gap before an unproduced segment

def test_equal_starts_and_a_zero_length_segment():
    segments = [seg(0, 10.0, 12.0), seg(1, 12.0, 12.0), seg(2, 12.0, 14.0), seg(3, 20.0, 22.0)]
    c = made(0, 1, 2, segments=segments)
    assert c.index_at(12.0) == 2              # both segments ending at 12.0 are behind
    assert c.index_at(11.0) == 0 and c.covered_until(11.0) == 14.0
    assert c.first_unproduced(11.0) == 3
    c = made(0, 2, segments=segments)
    assert c.covered_until(11.0) == 12.0      # the zero-length 12..12 is a gap

def test_mark_counts_and_unmark_from_goes_by_where_the_output_starts():
    c = Coverage(FOUR)
    c.mark(0, 10.0); c.mark(1, 23.5); c.mark(2, 30.0)    # 20..22 was pushed back to 23.5
    assert c.produced_count == 3
    c.unmark_from(23.0)
    assert c.produced_count == 1 and c.produced_at(11.0) and c.missing_at(21.0)
    c.unmark_from(10.0)
    assert c.produced_count == 0

def test_passed_reports_each_unproduced_segment_behind_t_once():
    c = made(0)
    assert c.passed(5.0) == []
    assert c.passed(33.0) == [1, 2]           # 10..12 was produced
    assert c.passed(33.0) == [] and c.passed(43.0) == [3]
    assert c.passed(15.0) == []               # the playhead going back changes nothing,
    assert c.passed(43.0) == []               # so nothing is reported twice

def test_skip_to_accounts_for_what_lies_behind_without_reporting_it():
    c = made()
    c.skip_to(25.0)
    assert c.passed(33.0) == [2]              # 10..12 and 20..22 were skipped
    c.skip_to(15.0)                           # a seek back: 20..22 and 30..32 are ahead again
    assert c.passed(33.0) == [1, 2]

def test_a_new_coverage_starts_with_nothing_produced_or_accounted_for():
    c = Coverage(FOUR)
    assert c.produced_count == 0 and c.covered_until(0.0) == 0.0
    assert c.passed(25.0) == [0, 1]


TWO = [seg(0, 0.0, 6.0), seg(1, 6.0, 10.0)]


def test_skip_to_inside_a_segment_skips_it_and_the_next_one_comes_first():
    c = Coverage(TWO)
    c.skip_to(3.0)
    assert c.first_unproduced(3.0) == 1 and c.passed(7.0) == []
    assert not c.missing_at(3.0) and c.covered_until(3.0) == 6.0 and c.produced_count == 0


def test_skip_to_within_half_a_second_of_a_segment_start_keeps_it():
    c = Coverage(TWO)
    c.skip_to(0.5)
    assert c.first_unproduced(0.5) == 0 and c.missing_at(0.5)


def test_skip_to_in_a_gap_skips_only_what_lies_behind():
    c = Coverage(FOUR)
    c.skip_to(15.0)
    assert c.first_unproduced(15.0) == 1 and c.missing_at(15.0)


def test_a_later_skip_to_forgets_the_earlier_skip():
    c = Coverage(TWO)
    c.skip_to(3.0)
    c.skip_to(0.0)
    assert c.first_unproduced(0.0) == 0 and c.missing_at(0.0)


def test_skip_to_leaves_a_produced_segment_alone():
    c = Coverage(TWO)
    c.mark(0, 0.0)
    c.skip_to(3.0)
    assert c.produced_at(3.0) and c.produced_count == 1 and c.first_unproduced(3.0) == 1


def test_next_unproduced_is_the_first_after_a_segment_that_is_neither_produced_nor_skipped():
    c = Coverage(FOUR)
    assert c.next_unproduced(0) == 1
    c.mark(1, 20.0)
    assert c.next_unproduced(0) == 2 and c.next_unproduced(1) == 2
    c.skip_to(31.0)                           # inside 30..32: that one is skipped
    assert c.next_unproduced(1) == 3
    assert c.next_unproduced(3) is None       # nothing after the last


# What the producer gives up while it catches up (design of 2026-11-14, 4.4).
def test_what_is_given_up_is_accounted_for_and_never_reported_or_picked():
    c = Coverage(FOUR)
    c.give_up([1, 2])
    assert c.first_unproduced(9.0) == 0 and c.next_unproduced(0) == 3
    c.mark(0, 10.0)
    assert c.first_unproduced(9.0) == 3
    assert not c.missing_at(21.0) and not c.produced_at(21.0)   # nothing to hold for, nothing to hear
    assert c.passed(35.0) == []               # 20..22 and 30..32 went by: given up, not missed
    assert c.produced_count == 1

def test_a_skip_to_brings_back_what_was_given_up():
    c = Coverage(FOUR)
    c.give_up([1, 2])
    c.skip_to(15.0)                           # a seek: what lies ahead is wanted again
    assert c.first_unproduced(15.0) == 1 and c.missing_at(21.0)


# R46: where a produced segment's clip ends, so that the next clip can wait for it after a seek.
def test_the_output_end_of_a_produced_segment_is_kept_and_forgotten_with_it():
    c = Coverage(FOUR)
    c.mark(0, 10.0, 15.0)
    c.mark(1, 20.0)                           # an engine failed: a subtitle, no clip
    assert c.output_end(0) == 15.0 and c.output_end(1) is None and c.output_end(2) is None
    assert c.end_of_clip_before(2) == 15.0    # the nearest clip before 30..32 is the first one's
    assert c.end_of_clip_before(0) is None
    c.unmark_from(5.0)
    assert c.output_end(0) is None and c.end_of_clip_before(2) is None


# An open coverage: its segments are appended a stretch at a time (a track without punctuation).

def segs(*spans, first=0):
    return [seg(first + n, float(a), float(b)) for n, (a, b) in enumerate(spans)]


def test_an_open_coverage_without_segments_is_missing_everywhere_and_not_all_produced():
    cov = Coverage(open=True)
    assert cov.open and cov.segments == []
    assert cov.missing_at(0.0) and cov.missing_at(99.0)
    assert not cov.all_produced_from(0.0)
    assert cov.first_unproduced(0.0) is None and cov.covered_until(5.0) == 5.0


def test_a_closed_coverage_without_segments_is_as_before():
    cov = Coverage()
    assert not cov.open and not cov.missing_at(0.0) and cov.all_produced_from(0.0)


def test_extend_appends_and_keeps_what_was_produced():
    cov = Coverage(segs((0, 2), (2, 4)), open=True)
    cov.mark(0, 0.0, 1.5)
    cov.mark(1, 2.0, 3.5)
    assert cov.missing_at(4.0)            # past the last segment, and more is to come
    assert not cov.all_produced_from(0.0)
    cov.extend(segs((4, 6), (6, 8), first=2), open=True)
    assert [s.idx for s in cov.segments] == [0, 1, 2, 3]
    assert cov.is_produced(0) and cov.is_produced(1) and not cov.is_produced(2)
    assert cov.index_at(5.0) == 2 and cov.first_unproduced(0.0) == 2 and cov.covered_until(0.0) == 4.0
    assert cov.missing_at(5.0) and cov.missing_at(8.0)


def test_extend_can_close_the_coverage():
    cov = Coverage(open=True)
    cov.extend(segs((0, 2)), open=False)
    assert not cov.open
    assert cov.missing_at(1.0) and not cov.missing_at(2.0)   # nothing past the last segment now
    cov.mark(0, 0.0, 1.5)
    assert cov.all_produced_from(0.0)
