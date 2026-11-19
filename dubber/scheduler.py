"""Fit dubbed clips to their slots, place them on the video timeline, gate playback.

Pure arithmetic on media seconds. Nothing here touches audio or threads: the session
calls it with measured clip lengths and acts on the answers.
"""

import math
from dataclasses import dataclass

# Times are decimals of a few places, so a sum or a quotient meant to land on a limit
# can miss it by a hair in floating point (2.3 + 0.8 is 3.0999999999999996). Limits get
# this much give.
_SLACK = 1e-9
# The Timeline says "slow" when the shift passes this many times overrun_max.
_SLOW_OVERRUNS = 3


@dataclass
class SchedulerConfig:
    """Limits of the scheduler; times are seconds of video time.

    The producer works up to `lead_max` ahead of the playhead, and `lead_min` is the
    least it should keep ahead: with less ready, the session does not ask for a shorter
    rendering of a clip that does not fit, because making one costs time the dub does not
    have. Playback starts once `startup_ready` is produced, a hold
    lasts at most `hold_cap`, and dubbing follows the player's rate up to `rate_clamp`.
    A clip plays at up to `speed_max` and may end `overrun_max` after its slot.

    `slow_player` lets the session answer the action "slow" by slowing the player to 0.9.
    It stays off until the mixer follows the player's rate: clips sit at fixed places in
    video time, so today a slowed player makes up nothing and the dub only stutters.

    This module reads none of `lead_min`, `speed_min` and `silence_reset`. A clip is
    never slowed below 1.0, and a silence in the source ends a backlog without a reset.
    """

    lead_min: float = 30.0
    lead_max: float = 120.0
    startup_ready: float = 12.0
    hold_cap: float = 20.0
    rate_clamp: float = 1.25
    speed_min: float = 0.95
    speed_max: float = 1.2
    overrun_max: float = 0.8
    silence_reset: float = 1.5
    slow_player: bool = False


@dataclass(frozen=True)
class Fit:
    """How `fit` plays a clip in its slot.

    `speed` is the playback speed and `overrun` how many seconds the clip then ends
    after its slot does. `action` is "fit", or "shorten" when no allowed speed is
    enough.
    """

    speed: float
    overrun: float
    action: str


def _speeds(speed_max: float) -> list[float]:
    """The speeds `fit` tries, slowest first: 1.00, 1.01, ... and `speed_max` last.

    The steps are counted as whole hundredths, so none carries float error. `speed_max`
    is tried as it is, whether or not it is a multiple of 0.01.
    """
    below = math.ceil(speed_max * 100 - _SLACK)
    return [hundredths / 100 for hundredths in range(100, below)] + [speed_max]


def fit(slot: float, audio: float, cfg: SchedulerConfig) -> Fit:
    """Choose the speed at which a clip of `audio` seconds plays in `slot` seconds.

    A clip may end up to `overrun_max` after its slot does, into the silence that
    follows. It plays at 1.0 when it ends in time that way, and otherwise at the
    smallest speed, in steps of 0.01 up to `speed_max`, that gets it there. It is never
    slowed down to fill a gap. When even `speed_max` is not enough the action is
    "shorten" and the speed is `speed_max`: the caller asks the translator for a
    shorter phrasing.

    The slot can be zero or negative: segment starts can be equal, and a clip held back
    by the one before it can start after the next segment does. The same rule applies,
    so only a very short clip fits such a slot.
    """
    # The latest the clip may end, counted from its start.
    limit = slot + cfg.overrun_max + _SLACK
    for speed in _speeds(cfg.speed_max):
        if audio / speed <= limit:
            return Fit(speed, max(0.0, audio / speed - slot), "fit")
    return Fit(cfg.speed_max, audio / cfg.speed_max - slot, "shorten")


@dataclass(frozen=True)
class Placement:
    """Where and how fast a clip plays; `target_start` is a time on the video timeline.

    `action` is "fit" or "shorten" as in Fit, "overrun" when a shortened clip still
    runs past its slot, or "slow" when the backlog has grown too large (see
    Timeline.place). For "shorten", `shrink` is the share of the clip that would fit:
    its slot plus `overrun_max`, at `speed_max`, over the clip's seconds; else 1.0.
    """

    target_start: float
    speed: float
    action: str
    shrink: float = 1.0


class Timeline:
    """Puts clips on the video timeline, none over another.

    A clip starts at its segment's source start, or later when the previous clip is
    still playing or the caller asks for a later start. `busy_until` is the time at
    which the last placed clip ends, minus infinity before the first. `shift` is how
    many seconds that clip ends after the next segment starts: the seconds the dub runs
    behind the source. A shift is made up as soon as the source leaves room, so a
    silence between segments absorbs it. A nanosecond or less is no shift.
    """

    busy_until: float
    shift: float

    def __init__(self, cfg: SchedulerConfig) -> None:
        self._cfg = cfg
        self.reset()

    @property
    def slowing(self) -> bool:
        """Whether "slow" was reported and the shift has not yet returned to 0."""
        return self._slowing

    def reset(self) -> None:
        """Forget every placed clip, as the session does on a seek."""
        self.busy_until = -math.inf
        self.shift = 0.0
        self._slowing = False

    def place(
        self,
        seg_start: float,
        next_start: float | None,
        audio: float,
        shortened: bool = False,
        not_before: float = -math.inf,
    ) -> Placement:
        """Place the clip of a segment and return where and how fast it plays.

        `audio` is the clip's measured length at speed 1.0 and `next_start` the source
        start of the segment after this one, None for the last. `not_before` is the
        earliest time the clip may start; the session passes the playhead plus a margin
        so that a late clip never starts in the past.

        A clip that does not fit even at `speed_max` is answered with the action
        "shorten" the first time, and the timeline is left unchanged: the caller asks
        the translator for a shorter phrasing and places the segment again with
        `shortened=True`. If it still does not fit, it plays at `speed_max` and runs
        long, action "overrun". A clip that fits has the action "fit".

        The first placement that leaves the shift above three times `overrun_max` has
        the action "slow" instead, once: `slowing` stays True until the shift is back
        to 0.
        """
        target_start = max(seg_start, self.busy_until, not_before)
        slot = audio if next_start is None else next_start - target_start
        fitted = fit(slot, audio, self._cfg)
        if fitted.action == "shorten" and not shortened:
            room = max(0.0, slot + self._cfg.overrun_max) * self._cfg.speed_max
            return Placement(target_start, fitted.speed, "shorten", room / audio if audio > 0 else 0.0)
        action = "overrun" if fitted.action == "shorten" else "fit"
        self.busy_until = target_start + audio / fitted.speed
        if next_start is None:
            self.shift = 0.0
        else:
            # A clip meant to end on the next start can overshoot it by float residue
            # (0.1 + 0.2 is 0.30000000000000004). That is not a backlog.
            overhang = self.busy_until - next_start
            self.shift = overhang if overhang > _SLACK else 0.0
        if self.shift == 0.0:
            self._slowing = False
        elif self.shift > _SLOW_OVERRUNS * self._cfg.overrun_max and not self._slowing:
            self._slowing = True
            action = "slow"
        return Placement(target_start, fitted.speed, action)


def clamp_rate(rate: float, cfg: SchedulerConfig) -> float:
    """The playback rate dubbing follows: a faster player is held to `rate_clamp`."""
    return min(rate, cfg.rate_clamp)


class ReadyGate:
    """The start-up, look-ahead and hold rules of the session.

    All times are on the video timeline, in seconds. `produced_until` is the source end
    of the last produced segment.
    """

    def __init__(self, cfg: SchedulerConfig) -> None:
        self._cfg = cfg

    def ready(self, produced_until: float, current_time: float, started: bool) -> bool:
        """Whether playback may run at `current_time`.

        Before the start, `startup_ready` seconds must be produced ahead of the
        playhead. Once started, it is enough that the playhead is not past
        `produced_until`, which only a seek forward can cause. The gate does not know
        where the video ends: for a video shorter than `startup_ready` it never says
        ready, so the session handles that case itself.
        """
        if started:
            return current_time <= produced_until + _SLACK
        return produced_until - current_time >= self._cfg.startup_ready

    def should_produce(self, produced_until: float, current_time: float) -> bool:
        """Whether the producer is less than `lead_max` ahead of the playhead."""
        return produced_until - current_time < self._cfg.lead_max

    def hold_expired(self, hold_started_at: float, now: float) -> bool:
        """Whether a hold has lasted longer than `hold_cap`."""
        return now - hold_started_at > self._cfg.hold_cap
