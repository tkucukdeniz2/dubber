# The dub comes back after falling behind (design of 2026-11-14, 4.4): whole runs of the real
# session in the simulation (tests/sim.py), on the timing of the video dubbed over a long-distance link that
# day and on a talker who never stops. Before the aim, the slow scenarios dubbed 5 of 91 and 44 of
# 92 sentences and made up to 33 clips for nothing.
import pathlib

from tests import sim

REMOTE_TRACK = pathlib.Path(__file__).parent / "fixtures" / "lULieGSxVrU-timing.json3"   # 63 sentences, 4.8 to 400 s
LENGTHS = [2.1, 1.5, 4.2, 2.6, 2.6, 3.7, 2.9, 5.2, 9.4, 2.8, 12.5, 5.5, 1.5, 9.6]


def talker(sentences=110):
    """A track without a silence: sentences of the lengths above, each beginning where the last ended, from 10 s."""
    events, start = [], 10.0
    for i in range(sentences):
        seconds = LENGTHS[i % len(LENGTHS)]
        words = max(6, round(seconds * 2.5))
        events.append({
            "tStartMs": round(start * 1000), "dDurationMs": round(seconds * 1000),
            "segs": [{"utf8": f"S{i} " + "la " * (words - 2) + "end."}],
        })
        start += seconds
    return {"events": events}


WARM = sim.warm_remote()


def unsteady(n):
    """From 0.7 to 1.3, the same for the n-th clip in every run."""
    return 0.7 + 0.6 * ((n * 2654435761) % 1000) / 1000


def model(mt=WARM.mt, tts=WARM.tts):
    return sim.Model(mt=mt, tts=tts, talk=sim.uneven)


def cold(n, d, now):
    """The first translation waits 11.5 s for the model to load."""
    return WARM.mt(n, d, now) + (11.5 if n == 1 else 0.0)


def made_for_nothing(run):
    return [r for r in run.records if r.dropped and r.audio_seconds > 0]


def latest(run):
    return max(r.lag for r in run.dubbed())


def test_engines_that_keep_up_drop_nothing_and_never_hold():
    for track, start, seconds in ((REMOTE_TRACK, 58.9, 330.0), (talker(), 9.0, 420.0)):
        run = sim.simulate(track, start, seconds, model())
        assert run.dropped() == [] and run.holds == [] and run.exceeded == 0
        assert run.startup < 12.0


def test_a_cold_start_costs_a_few_sentences_and_no_more():
    slow_first_clips = lambda n, d, now: WARM.tts(n, d, now) + ([3.0, 6.0, 1.8][n - 1] if n <= 3 else 0.0)
    run = sim.simulate(REMOTE_TRACK, 58.9, 330.0, model(mt=cold, tts=slow_first_clips))
    assert run.exceeded == 1                  # the start-up hold ran into its cap
    assert len(run.dropped()) <= 4 and made_for_nothing(run) == []
    assert run.holds == [] and latest(run) <= 2.0


def test_clips_slower_than_the_sentences_still_dub_what_they_can_on_time():
    run = sim.simulate(talker(), 9.0, 420.0, model(tts=lambda n, d, now: 2.0 + 1.5 * d))
    assert len(run.dubbed()) >= 50            # of 92 (56 when this was written); chasing the playhead dubbed 5
    assert len(made_for_nothing(run)) <= 2    # and made 33 clips for nothing
    assert latest(run) <= 2.0 and run.holds == []   # no further hold after the one that ran out


def test_four_slow_minutes_are_recovered_from():
    slow_at_first = lambda n, d, now: WARM.tts(n, d, now) + (4.0 if now < 240.0 else 0.0)
    run = sim.simulate(talker(), 9.0, 420.0, model(mt=cold, tts=slow_at_first))
    assert len(run.dubbed()) >= 70            # of 92; chasing the playhead dubbed 44
    assert all(not r.dropped for r in run.records[-20:])   # and it is whole again at the end
    assert made_for_nothing(run) == [] and latest(run) <= 2.0


def test_uneven_slow_clips_are_never_far_behind_their_sentence():
    run = sim.simulate(talker(), 9.0, 420.0, model(tts=lambda n, d, now: (2.0 + 1.5 * d) * unsteady(n)))
    assert len(run.dubbed()) >= 50 and latest(run) <= 3.0
    assert len(made_for_nothing(run)) <= 3
