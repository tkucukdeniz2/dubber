"""Which segments of a caption track are produced, measured from a playhead.

Pure bookkeeping for the session: no lock, no clock and no messages. Times are media
time, seconds into the video. The segment at a time t is the first one that ends after
t, so a playhead in the silence between two segments is at the next one. The segments
come from `dubber.segmenter.segment`: their starts never go back and none ends after
the next one starts, so their ends never go back either. An open coverage gets its
segments a stretch at a time, as the punctuation of its track is restored.
"""

import bisect
from collections.abc import Iterable, Sequence

from dubber.segmenter import Segment


class Coverage:
    """The produced segments of one track, and the ones the playhead went by unproduced.

    A segment is produced once its clip is scheduled, or once its subtitle is shown in
    place of a clip that could not be made; `mark` records the media time at which its
    output starts. The segments behind the playhead that the session has accounted
    for, produced or not, lie below a watermark that `passed` and `skip_to` move. A
    sentence the playhead was already inside after a jump is skipped: it is never
    produced, never reported, and counts as covered.
    """

    def __init__(self, segments: Sequence[Segment] = (), open: bool = False) -> None:
        self.segments = list(segments)
        # More segments are to come (see extend): the place after the last one is then a
        # sentence that is not ready, and not everything is produced.
        self.open = open
        self._ends = [seg.end for seg in self.segments]
        self._starts: dict[int, float] = {}  # produced index -> where its output starts
        self._clip_ends: dict[int, float] = {}  # produced index -> where its clip ends, if it has one
        self._behind = 0  # the segments below this index are accounted for
        self._skipped: set[int] = set()  # the segment `skip_to` found the playhead inside
        self._gone: set[int] = set()  # the segments the producer gave up to catch up

    @property
    def produced_count(self) -> int:
        return len(self._starts)

    def extend(self, segments: Iterable[Segment], open: bool) -> None:
        """Append `segments`, which follow the last one in time and in index; `open` says whether more are to come.

        The indexes of the segments already there do not change, and neither does what is
        known of them.
        """
        for seg in segments:
            self.segments.append(seg)
            self._ends.append(seg.end)
        self.open = open

    def index_at(self, t: float) -> int:
        """The index of the segment at `t`; len(segments) when none ends after `t`."""
        return bisect.bisect_right(self._ends, t)

    def is_produced(self, idx: int) -> bool:
        return idx in self._starts

    def produced_at(self, t: float) -> bool:
        """Whether there is a segment at `t` and it is produced."""
        return self.index_at(t) in self._starts

    def missing_at(self, t: float) -> bool:
        """Whether there is a segment at `t` and it is not produced.

        Past the last segment of an open coverage there is one: it is still to come.
        """
        i = self.index_at(t)
        if i == len(self.segments):
            return self.open
        return not self._accounted(i)

    def first_unproduced(self, t: float) -> int | None:
        """The index of the first unproduced segment at or after `t`; None if none."""
        i = self._run_end(self.index_at(t))
        return i if i < len(self.segments) else None

    def next_unproduced(self, idx: int) -> int | None:
        """The index of the first segment after `idx` that is neither produced nor skipped; None if none."""
        i = self._run_end(idx + 1)
        return i if i < len(self.segments) else None

    def all_produced_from(self, t: float) -> bool:
        """Whether every segment that ends after `t` is produced; True when none does. Never while more are to come."""
        return not self.open and self.first_unproduced(t) is None

    def covered_until(self, t: float) -> float:
        """The end of the run of produced segments from the one at `t`; `t` if none."""
        i = self.index_at(t)
        end = self._run_end(i)
        return self.segments[end - 1].end if end > i else t

    def mark(self, idx: int, output_start: float, output_end: float | None = None) -> None:
        """Record segment `idx` as produced, its output starting at `output_start`.

        `output_end` is where its clip ends on the timeline; None for a segment without a
        clip (an engine failed and only its subtitle is shown).
        """
        self._starts[idx] = output_start
        if output_end is None:
            self._clip_ends.pop(idx, None)
        else:
            self._clip_ends[idx] = output_end

    def output_end(self, idx: int) -> float | None:
        """Where the clip of segment `idx` ends; None when it is not produced or has no clip."""
        return self._clip_ends.get(idx)

    def produced_after(self, idx: int) -> bool:
        """Whether any segment after `idx` is produced."""
        return any(i > idx for i in self._starts)

    def end_of_clip_before(self, idx: int) -> float | None:
        """Where the nearest clip before segment `idx` ends; None when no produced segment before it has one."""
        earlier = [i for i in self._clip_ends if i < idx]
        return self._clip_ends[max(earlier)] if earlier else None

    def unmark_from(self, t: float) -> None:
        """Forget every produced segment whose output starts at or after `t`.

        That is every one whose clip a mixer `cancel_after(t)` removes: a segment that
        starts before `t` but whose clip a backlog pushed past it goes too.
        """
        self._starts = {i: start for i, start in self._starts.items() if start < t}
        self._clip_ends = {i: end for i, end in self._clip_ends.items() if i in self._starts}

    def passed(self, t: float) -> list[int]:
        """The unproduced segments a playhead at `t` has gone by since the last call.

        The watermark moves up to `t` and never back, so each is reported once.
        """
        i = self.index_at(t)
        gone = [k for k in range(self._behind, i) if not self._accounted(k)]
        self._behind = max(self._behind, i)
        return gone

    def give_up(self, indexes: Iterable[int]) -> None:
        """Count the segments `indexes` as dealt with, though they will not be heard.

        The producer does this with the sentences it jumps over to catch up with the video
        (the caller logs them as dropped). They are not picked again, not reported by
        `passed`, and nothing holds the video for them. `skip_to` forgets them: after a
        seek what lies ahead is wanted again.
        """
        self._gone.update(indexes)

    def skip_to(self, t: float, early: float = 0.5) -> None:
        """Account for every segment behind `t` without reporting it, and for none after.

        A seek or a new track does this: the segments it jumps over were skipped, not
        missed, and those after `t` are reported by `passed` once the playhead goes by
        them unproduced. An unproduced segment that began more than `early` seconds before
        `t` is skipped too: its Turkish would start late and run over the next sentence,
        and half of it would make no sense, so the dub begins at the next sentence. A later
        call forgets this skip.
        """
        i = self._behind = self.index_at(t)
        inside = i < len(self.segments) and i not in self._starts and self.segments[i].start < t - early
        self._skipped = {i} if inside else set()
        self._gone = set()

    def _accounted(self, i: int) -> bool:
        """Whether segment `i` is produced, skipped or given up: nothing more is to be done with it."""
        return i in self._starts or i in self._skipped or i in self._gone

    def _run_end(self, i: int) -> int:
        """The index of the first segment from `i` on that is neither produced, skipped nor given up."""
        while i < len(self.segments) and self._accounted(i):
            i += 1
        return i
