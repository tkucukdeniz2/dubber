import math
from dataclasses import FrozenInstanceError

import pytest

from dubber.scheduler import Fit, Placement, ReadyGate, SchedulerConfig, Timeline, clamp_rate, fit

cfg = SchedulerConfig()

def test_fit_speed_one_when_audio_fits():
    assert fit(slot=4.0, audio=3.5, cfg=cfg) == Fit(speed=1.0, overrun=0.0, action="fit")

def test_fit_speeds_up_within_limits_and_allows_overrun():
    f = fit(slot=4.0, audio=5.2, cfg=cfg)
    assert f.action == "fit" and 1.0 < f.speed <= 1.2 and 5.2 / f.speed <= 4.0 + 0.8

def test_fit_asks_to_shorten_when_too_long():
    f = fit(slot=3.0, audio=5.0, cfg=cfg)
    assert f.action == "shorten" and f.speed == 1.2

def test_timeline_accumulates_shift_and_resets_on_silence():
    tl = Timeline(cfg)
    p1 = tl.place(seg_start=10.0, next_start=13.0, audio=5.0, shortened=True)
    assert p1.action == "overrun" and p1.target_start == 10.0
    assert tl.shift == pytest.approx(5.0 / 1.2 - 3.0, abs=0.01)      # 1.17 s behind after the overrun
    p2 = tl.place(seg_start=13.0, next_start=20.0, audio=2.0)
    assert p2.target_start == pytest.approx(13.0 + 5.0 / 1.2 - 3.0, abs=0.01)   # 14.17: shifted by the backlog
    assert tl.shift == 0.0   # audio ends at 16.17; 20.0 - 16.17 >= 1.5 s of silence resets the shift

def test_clamp_rate():
    assert clamp_rate(2.0, cfg) == 1.25 and clamp_rate(1.0, cfg) == 1.0

def test_ready_gate_startup_and_seek():
    g = ReadyGate(cfg)
    assert g.ready(produced_until=11.0, current_time=0.0, started=False) is False
    assert g.ready(produced_until=12.0, current_time=0.0, started=False) is True
    assert g.ready(produced_until=50.0, current_time=40.0, started=True) is True
    assert g.ready(produced_until=50.0, current_time=200.0, started=True) is False
    assert g.should_produce(produced_until=100.0, current_time=0.0) is True
    assert g.should_produce(produced_until=121.0, current_time=0.0) is False
    assert g.hold_expired(hold_started_at=0.0, now=20.1) is True

# Rules the brief leaves implicit: the limits of fit, edge cases, and the timeline's
# catch-up and slow rules.

def test_config_defaults_and_mutability():
    assert SchedulerConfig() == SchedulerConfig(
        lead_min=30.0, lead_max=120.0, startup_ready=12.0, hold_cap=20.0, rate_clamp=1.25,
        speed_min=0.95, speed_max=1.2, overrun_max=0.8, silence_reset=1.5,
    )
    own = SchedulerConfig()
    own.startup_ready = 1.0      # the session tests shorten the start-up wait this way
    assert ReadyGate(own).ready(produced_until=1.0, current_time=0.0, started=False) is True

def test_fit_and_placement_are_immutable():
    with pytest.raises(FrozenInstanceError):
        Fit(speed=1.0, overrun=0.0, action="fit").speed = 1.1
    with pytest.raises(FrozenInstanceError):
        Placement(target_start=1.0, speed=1.0, action="fit").target_start = 2.0

def test_fit_picks_the_smallest_hundredth_that_fits():
    f = fit(slot=4.0, audio=5.2, cfg=cfg)
    # 1.08 would end 4.81 s in, past the 4.8 s limit
    assert f.speed == 1.09 and f.action == "fit"
    assert f.overrun == pytest.approx(5.2 / 1.09 - 4.0)

def test_fit_never_slows_a_clip_below_normal_speed():
    # speed_min is not used by fit: 4.5 s in a 4 s slot is within the overrun allowance
    # as it is, so it plays at 1.0, not at 0.95 or lower.
    assert fit(slot=4.0, audio=4.5, cfg=cfg) == Fit(speed=1.0, overrun=0.5, action="fit")
    low = SchedulerConfig(speed_min=0.5)
    for audio in (0.0, 0.5, 2.0, 3.9, 4.0, 4.5):
        assert fit(slot=4.0, audio=audio, cfg=low).speed == 1.0

def test_fit_limits_are_not_decided_by_float_noise():
    # In floats 2.3 + 0.8 is 3.0999999999999996 and 2.16 / 1.2 is 1.8000000000000003.
    f = fit(slot=4.0, audio=4.8, cfg=cfg)
    assert f.speed == 1.0 and f.action == "fit" and f.overrun == pytest.approx(0.8)
    f = fit(slot=2.3, audio=3.1, cfg=cfg)
    assert f.speed == 1.0 and f.action == "fit" and f.overrun == pytest.approx(0.8)
    f = fit(slot=1.0, audio=2.16, cfg=cfg)
    assert f.speed == 1.2 and f.action == "fit" and f.overrun == pytest.approx(0.8)

def test_fit_handles_empty_slots_and_empty_clips():
    # Segment starts can be equal, so a slot can be zero, and a clip held back by the one
    # before it can start after the next segment does (negative slot).
    assert fit(slot=0.0, audio=0.5, cfg=cfg) == Fit(speed=1.0, overrun=0.5, action="fit")
    f = fit(slot=-0.5, audio=2.0, cfg=cfg)
    assert f.action == "shorten" and f.speed == 1.2 and f.overrun == pytest.approx(2.0 / 1.2 + 0.5)
    assert fit(slot=3.0, audio=0.0, cfg=cfg) == Fit(speed=1.0, overrun=0.0, action="fit")
    assert fit(slot=0.0, audio=0.0, cfg=cfg) == Fit(speed=1.0, overrun=0.0, action="fit")

@pytest.mark.parametrize("slot", [-3.0, -0.5, 0.0, 0.5, 4.0], ids=lambda v: f"slot={v}")
@pytest.mark.parametrize("audio", [0.0, 0.5, 4.0, 9.0], ids=lambda v: f"audio={v}")
def test_fit_gives_a_valid_fit_for_any_slot_and_clip(slot, audio):
    f = fit(slot=slot, audio=audio, cfg=cfg)
    assert 1.0 <= f.speed <= 1.2 and f.overrun >= 0.0
    if f.action == "fit":
        assert audio / f.speed <= slot + 0.8 + 1e-9
    else:
        assert f.action == "shorten" and f.speed == 1.2 and audio / 1.2 > slot + 0.8

def test_consecutive_overruns_leave_no_gap_and_no_overlap():
    tl = Timeline(cfg)
    # 5 s in a 3 s slot: shortened, played at 1.2, and it still runs 1.17 s long
    clip = 5.0 / 1.2
    p1 = tl.place(seg_start=10.0, next_start=13.0, audio=5.0, shortened=True)
    p2 = tl.place(seg_start=13.0, next_start=16.0, audio=5.0, shortened=True)
    p3 = tl.place(seg_start=16.0, next_start=19.0, audio=5.0, shortened=True)
    assert (p1.action, p2.action) == ("overrun", "overrun")
    assert p1.target_start == 10.0
    assert p2.target_start == pytest.approx(p1.target_start + clip)     # where the first clip ends
    assert p3.target_start == pytest.approx(p2.target_start + clip)     # where the second clip ends

def test_fit_that_uses_the_overrun_allowance_pushes_the_next_clip_back():
    tl = Timeline(cfg)
    p1 = tl.place(seg_start=10.0, next_start=14.0, audio=4.5)
    assert p1 == Placement(target_start=10.0, speed=1.0, action="fit")
    assert tl.shift == pytest.approx(0.5)      # the clip ends 0.5 s after the next segment starts
    p2 = tl.place(seg_start=14.0, next_start=20.0, audio=2.0)
    assert p2.target_start == pytest.approx(14.5)      # at the end of the first clip, not over it
    assert tl.shift == 0.0

def test_shorten_placement_commits_nothing_and_the_retry_overruns():
    tl = Timeline(cfg)
    tl.place(seg_start=10.0, next_start=13.0, audio=5.0, shortened=True)
    state = (tl.busy_until, tl.shift, tl.slowing)
    ask = tl.place(seg_start=13.0, next_start=16.0, audio=5.0)
    assert ask.action == "shorten" and ask.speed == 1.2
    assert ask.target_start == pytest.approx(10.0 + 5.0 / 1.2)      # where the first clip ends
    assert (tl.busy_until, tl.shift, tl.slowing) == state
    retry = tl.place(seg_start=13.0, next_start=16.0, audio=5.0, shortened=True)
    assert retry.action == "overrun" and retry.target_start == ask.target_start
    assert tl.busy_until == pytest.approx(retry.target_start + 5.0 / 1.2)
    assert tl.shift == pytest.approx(retry.target_start + 5.0 / 1.2 - 16.0)

def test_shortened_clip_that_fits_is_a_plain_fit():
    p = Timeline(cfg).place(seg_start=10.0, next_start=14.0, audio=3.5, shortened=True)
    assert p == Placement(target_start=10.0, speed=1.0, action="fit")

def test_not_before_moves_a_placement_later_and_the_shift_follows():
    tl = Timeline(cfg)
    p = tl.place(seg_start=10.0, next_start=14.0, audio=3.0, not_before=11.5)
    assert p == Placement(target_start=11.5, speed=1.0, action="fit")
    assert tl.shift == pytest.approx(0.5)      # from 10.0 the clip would have ended 1 s early
    early = Timeline(cfg).place(seg_start=10.0, next_start=14.0, audio=3.0, not_before=9.0)
    assert early.target_start == 10.0      # not_before never moves a clip earlier

def test_segments_with_equal_starts_play_one_after_the_other():
    tl = Timeline(cfg)
    first = tl.place(seg_start=10.0, next_start=10.0, audio=2.0, shortened=True)     # slot 0
    second = tl.place(seg_start=10.0, next_start=14.0, audio=1.0)
    assert first.action == "overrun" and first.target_start == 10.0
    assert second.target_start == pytest.approx(10.0 + 2.0 / 1.2)

def test_last_segment_fits_its_own_length_and_ends_the_backlog():
    tl = Timeline(cfg)
    tl.place(seg_start=10.0, next_start=11.0, audio=5.0, shortened=True)     # 3.17 s behind
    last = tl.place(seg_start=11.0, next_start=None, audio=6.0)
    assert last.target_start == pytest.approx(10.0 + 5.0 / 1.2)
    assert last.speed == 1.0 and last.action == "fit"
    assert tl.shift == 0.0 and tl.slowing is False

def test_slow_is_reported_once_per_backlog():
    tl = Timeline(cfg)
    # A clip that is only asked to shorten changes nothing, slowing included.
    assert tl.place(seg_start=10.0, next_start=11.0, audio=5.0).action == "shorten"
    assert tl.slowing is False
    # The shift grows 1.17 s, 2.33 s (both under 3 * 0.8 s), 3.5 s, 4.67 s, 5.83 s.
    actions = [
        tl.place(seg_start=10.0 + 3 * i, next_start=13.0 + 3 * i, audio=5.0, shortened=True).action
        for i in range(5)
    ]
    assert actions == ["overrun", "overrun", "slow", "overrun", "overrun"]
    assert tl.slowing is True
    # A long silence absorbs the backlog; the shift is 0 again and so is slowing.
    assert tl.place(seg_start=25.0, next_start=100.0, audio=2.0).action == "fit"
    assert tl.shift == 0.0 and tl.slowing is False
    # A new backlog is reported again.
    assert tl.place(seg_start=100.0, next_start=101.0, audio=5.0, shortened=True).action == "slow"
    assert tl.slowing is True

def test_float_residue_past_the_next_start_is_not_a_shift():
    # 0.1 + 0.2 is 0.30000000000000004 in floats: the clip ends on the next start, not after it
    tl = Timeline(cfg)
    tl.place(seg_start=0.1, next_start=0.3, audio=0.2)
    assert tl.shift == 0.0
    # a nanosecond or less past the next start counts as on it, anything more is a real shift
    tl = Timeline(cfg)
    tl.place(seg_start=10.0, next_start=14.0, audio=4.0 + 5e-10)
    assert tl.shift == 0.0
    tl = Timeline(cfg)
    tl.place(seg_start=10.0, next_start=14.0, audio=4.0 + 2e-9)
    assert tl.shift == pytest.approx(2e-9, abs=1e-12)

def test_catching_up_exactly_on_the_next_start_ends_slowing():
    tl = Timeline(cfg)
    # a 6 s clip in a 1 s slot plays from 7.0 to 12.0 at 1.2: 4 s behind
    assert tl.place(seg_start=7.0, next_start=8.0, audio=6.0, shortened=True).action == "slow"
    assert tl.slowing is True
    # the catch-up clip runs from 12.0 up to its next start; in floats 12.0 + 1.12 is
    # 13.120000000000001, a hair past 13.12
    p = tl.place(seg_start=8.0, next_start=13.12, audio=1.12)
    assert p == Placement(target_start=12.0, speed=1.0, action="fit")
    assert tl.shift == 0.0 and tl.slowing is False

def test_slowing_is_read_only():
    with pytest.raises(AttributeError):
        Timeline(cfg).slowing = True

def test_reset_returns_the_timeline_to_its_initial_state():
    initial = (-math.inf, 0.0, False)
    tl = Timeline(cfg)
    assert (tl.busy_until, tl.shift, tl.slowing) == initial
    tl.place(seg_start=10.0, next_start=11.0, audio=5.0, shortened=True)     # behind and slowing
    assert (tl.busy_until, tl.shift, tl.slowing) != initial
    tl.reset()
    assert (tl.busy_until, tl.shift, tl.slowing) == initial
    # After a seek back the old backlog is gone, and slow can be reported again.
    assert tl.place(seg_start=2.0, next_start=6.0, audio=3.0) == Placement(2.0, 1.0, "fit")
    assert tl.place(seg_start=6.0, next_start=7.0, audio=5.0, shortened=True).action == "slow"

def test_ready_gate_boundaries():
    g = ReadyGate(cfg)
    # before the start the lead is counted from the playhead, not from 0
    assert g.ready(produced_until=62.0, current_time=50.0, started=False) is True
    # once started, the playhead may sit at the end of what is produced, float noise included
    assert g.ready(produced_until=50.0, current_time=50.0, started=True) is True
    assert g.ready(produced_until=50.0, current_time=50.0 + 1e-12, started=True) is True
    assert g.ready(produced_until=50.0, current_time=50.01, started=True) is False
    # lead_max and hold_cap are not yet reached or passed at exactly their value
    assert g.should_produce(produced_until=120.0, current_time=0.0) is False
    assert g.hold_expired(hold_started_at=5.0, now=25.0) is False

def test_clamp_rate_uses_the_configured_limit():
    assert clamp_rate(2.0, SchedulerConfig(rate_clamp=1.5)) == 1.5
    assert clamp_rate(0.75, cfg) == 0.75


def test_a_clip_that_cannot_fit_is_placed_with_its_shrink_factor():
    cfg = SchedulerConfig()
    tl = Timeline(cfg)
    placed = tl.place(0.0, 4.0, 10.0)
    assert placed.action == "shorten"
    assert placed.shrink == pytest.approx((4.0 + cfg.overrun_max) * cfg.speed_max / 10.0)
    assert tl.place(0.0, 4.0, 3.0).shrink == 1.0  # a clip that fits has nothing to shrink
