import json, logging, pathlib, numpy as np
from dubber.config import Config
from dubber.engines.base import Clip, IdentityTranslator
from dubber.metrics import MetricsLog
from dubber.playback import AudioOut
from dubber.protocol import Start, Captions, State, Seek, Stop, Control, Duck, Status
from dubber.session import Session

FIX = json.loads((pathlib.Path(__file__).parent / "fixtures" / "captions_asr.json").read_text())

class Fake:
    name = "fake"
    def synthesize(self, text, speed=1.0):
        return Clip(samples=np.zeros(22050, dtype=np.float32), sample_rate=22050)   # 1 s each

def make(sent, t=[0.0]):
    cfg = Config()
    cfg.scheduler.startup_ready = 1.0
    audio = AudioOut(stream_factory=lambda cb: None)
    return Session(IdentityTranslator(), Fake(), audio, MetricsLog(pathlib.Path("/tmp/m.jsonl")), cfg, send=sent.append, clock=lambda: t[0]), t

def test_start_then_captions_then_state_releases_when_ready():
    sent = []; s, t = make(sent)
    s.handle(Start(mode=None))
    assert s.state == "preparing" and Duck(volume=0.2) in sent and Control(action="pause", value=None) in sent
    s.handle(State(video_id="v", current_time=61.0, rate=1.0, paused=True, live=False, volume=0.8))   # before captions: no crash
    s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIX))
    while s.produce_next(): pass
    s.handle(State(video_id="v", current_time=61.0, rate=1.0, paused=True, live=False, volume=0.8))
    assert s.state == "playing" and Control(action="play", value=None) in sent

def test_captions_twice_replace_segments():
    sent = []; s, t = make(sent)
    s.handle(Start(mode=None)); s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIX))
    n = len(s.segments); assert n > 0
    s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIX))
    assert len(s.segments) == n and s.produced_count == 0

def test_seek_forward_holds_and_backward_does_not():
    sent = []; s, t = make(sent)
    s.handle(Start(mode=None)); s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIX))
    while s.produce_next(): pass
    s.handle(State(video_id="v", current_time=61.0, rate=1.0, paused=False, live=False, volume=0.8))
    s.handle(Seek(current_time=500.0))
    assert s.state == "holding"
    s.handle(Seek(current_time=62.0))
    assert s.state == "playing"

def test_rate_above_clamp_is_pushed_back_and_stop_restores():
    sent = []; s, t = make(sent)
    s.handle(Start(mode=None)); s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIX))
    s.handle(State(video_id="v", current_time=0.0, rate=2.0, paused=False, live=False, volume=0.8))
    assert Control(action="rate", value=1.25) in sent
    s.handle(Stop())
    assert s.state == "idle" and Duck(volume=None) in sent

# What the six tests above leave open (task 6 resolutions): message order, releasing the
# player, seeks and jumps, the end of a video, holds, drops, engine failures, results
# made stale by a seek, slowing, the Status throttle, metrics and the producer thread.
# Each rig has its own fake clock and metrics file; no audio device is opened.
import dataclasses
import logging
import threading
import time

import httpx
import pytest

from dubber.engines.base import TranslatorError, VoiceError
from dubber.engines.translate_llamacpp import LlamaCppTranslator
from dubber.protocol import Hello, Subtitle


def track(*spans):
    """json3 with one six-word sentence per (start, end) span: one segment each."""
    return {"events": [
        {"tStartMs": round(a * 1000), "dDurationMs": round((b - a) * 1000),
         "segs": [{"utf8": f"This is sentence number {i} here."}]}
        for i, (a, b) in enumerate(spans)
    ]}

TEXT = "This is sentence number {} here.".format
FIVE = track((10, 12), (20, 22), (30, 32), (40, 42), (50, 52))

class SecondsVoice:
    """1 s clips, or the length `seconds` gives the sentence number; counts its calls."""
    name = "seconds"
    def __init__(self, seconds=None, rate=22050):
        self.seconds = seconds or {}
        self.rate = rate
        self.calls = []
    def synthesize(self, text, speed=1.0):
        self.calls.append(text)
        n = int(text.split()[4]) if text.startswith("This is sentence number") else -1
        return Clip(samples=np.zeros(round(self.seconds.get(n, 1.0) * 22050), dtype=np.float32), sample_rate=self.rate)

class Recording(MetricsLog):
    """A MetricsLog that also keeps the records it was given."""
    def __init__(self, path):
        super().__init__(path)
        self.records = []
    def write(self, rec):
        self.records.append(rec)
        super().write(rec)

class Lazy:
    """A translation made ahead that is made only when it is asked for, so the engines are
    called in series, in the order every test written before the look-ahead expects."""
    def __init__(self, call):
        self._call, self._asked, self._value = call, False, None
    def result(self):
        if not self._asked:
            self._asked, self._value = True, self._call()
        return self._value

class Eager:
    """A translation made ahead at once, before the current sentence is voiced."""
    def __init__(self, call):
        self._value = call()
    def result(self):
        return self._value

THREADS = None   # the session's own way: a thread for each translation made ahead

class Rig:
    def __init__(self, tmp_path, voice=None, translator=None, startup_ready=1.0, stream_factory=None, metrics_path=None, cfg=None, spawn=Lazy, **sched):
        self.t = [0.0]
        self.sent = []
        self.voice = voice or SecondsVoice()
        self.audio = AudioOut(stream_factory=stream_factory or (lambda cb: None))
        self.metrics = Recording(metrics_path or tmp_path / "m.jsonl")
        cfg = cfg or Config()
        cfg.scheduler.startup_ready = startup_ready
        for k, v in sched.items():
            setattr(cfg.scheduler, k, v)
        extra = {} if spawn is THREADS else {"spawn": spawn}
        self.s = Session(translator or IdentityTranslator(), self.voice, self.audio, self.metrics, cfg, send=self.sent.append, clock=lambda: self.t[0], **extra)

    def state(self, at, paused=False, rate=1.0, video="v"):
        self.s.handle(State(video_id=video, current_time=at, rate=rate, paused=paused, live=False, volume=0.8))

    def start(self, json3=FIVE, at=None, paused=True):
        self.s.handle(Start(mode=None))
        if at is not None:
            self.state(at, paused=paused)
        self.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=json3))

    def produce_all(self):
        n = 0
        while self.s.produce_next():
            n += 1
        return n

    def playing_at(self, at, json3=FIVE):
        """Started at `at`, everything produced, released, the clock running."""
        self.start(json3, at=at)
        self.produce_all()
        self.state(at)
        assert self.s.state == "playing"
        self.sent.clear()

    def advance(self, seconds):
        self.t[0] += seconds

    def of(self, kind):
        return [m for m in self.sent if isinstance(m, kind)]

    def messages(self):
        return [m for m in self.of(Status) if m.message]

def test_start_sends_duck_pause_then_preparing_status_in_that_order(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Start(mode=None))
    assert r.sent[:2] == [Duck(volume=0.2), Control(action="pause", value=None)]
    assert len(r.sent) == 3
    assert r.sent[2] == Status(mode="captions", state="preparing", buffered_ahead=0.0, lag=0.0, engine="identity+seconds", message="")

def test_start_gives_each_run_a_new_session_id(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Start(mode=None)); first = r.s.session_id
    r.s.handle(Start(mode=None)); second = r.s.session_id
    assert first and second and first != second
    assert len(first.split("-")[0]) == 8 and len(first.split("-")[1]) == 6

def test_start_in_live_mode_releases_the_player(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Start(mode="live"))
    assert r.s.state == "idle"
    assert Duck(volume=None) in r.sent and Control(action="play", value=None) in r.sent
    assert Duck(volume=0.2) not in r.sent and Control(action="pause", value=None) not in r.sent
    assert r.of(Status)[-1].state == "idle" and r.of(Status)[-1].message == "live mode is not available yet"

@pytest.mark.parametrize("json3", [{"events": []}, {"events": [{"tStartMs": 0, "dDurationMs": 10, "segs": [{"utf8": "\n"}]}]}, {"no": "events"}, {"events": [{"segs": [{"utf8": "hi"}]}]}])
def test_captions_without_usable_segments_release_the_player(tmp_path, json3):
    r = Rig(tmp_path)
    r.s.handle(Start(mode=None)); r.sent.clear()
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=json3))
    assert r.s.state == "idle" and r.s.segments == []
    assert Duck(volume=None) in r.sent and Control(action="play", value=None) in r.sent
    assert r.sent[-1].state == "idle" and r.sent[-1].message == "no usable captions"

def test_captions_while_idle_are_ignored_with_a_message(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    assert r.s.segments == [] and r.s.state == "idle" and r.s.produce_next() is False
    assert len(r.sent) == 1 and r.sent[0].state == "idle" and r.sent[0].message

def test_hello_is_answered_with_the_current_status(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Hello(client="test", version="0"))
    assert r.sent == [Status(mode="captions", state="idle", buffered_ahead=0.0, lag=0.0, engine="identity+seconds", message="")]

def test_backward_seek_before_the_produced_range_holds_and_reproduces_from_there(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)                       # produced 30..52: segments 2, 3, 4
    assert r.s.produced_count == 3 and r.audio.played_until() == 51.0
    r.s.handle(Seek(current_time=5.0))
    assert r.s.state == "holding"
    assert Control(action="pause", value=None) in r.sent and r.of(Status)[-1].state == "holding"
    assert r.s.produced_count == 0 and r.audio.played_until() == 0.0   # every clip after 5 s cancelled
    r.sent.clear()
    assert r.s.produce_next() is True
    assert r.of(Subtitle) == [Subtitle(text=TEXT(0), start=10.0, end=11.0)]
    r.produce_all()
    r.state(5.0, paused=True)
    assert r.s.state == "playing" and Control(action="play", value=None) in r.sent

def test_forward_seek_inside_the_produced_range_only_reanchors(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.s.handle(Seek(current_time=41.0))
    assert r.s.state == "playing" and r.sent == []
    assert r.s.produced_count == 3 and r.audio.played_until() == 51.0
    assert r.audio.media_now(0.0) == 41.0 and r.s.produced_until == 52.0

def test_seek_inside_the_produced_range_releases_a_hold(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.s.handle(Seek(current_time=500.0))      # past the last segment: nothing to produce there
    assert r.s.state == "holding"
    r.sent.clear()
    r.s.handle(Seek(current_time=31.0))
    assert r.s.state == "playing" and r.sent[0] == Control(action="play", value=None)
    assert r.sent[1].state == "playing"

def test_hold_with_nothing_left_to_produce_is_released_by_the_next_state(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.s.handle(Seek(current_time=500.0))
    r.state(500.0, paused=True)
    assert r.s.state == "playing"

def test_state_far_from_the_running_clock_acts_as_a_seek(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.advance(0.25); r.state(25.75)          # 0.5 s off the clock: not a seek
    assert r.s.state == "playing" and r.sent == []
    r.advance(0.25); r.state(5.0)            # 21 s off: a jump back before the produced range
    assert r.s.state == "holding" and Control(action="pause", value=None) in r.sent
    assert r.s.produced_count == 0

def test_the_first_report_after_start_is_not_a_jump(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Start(mode=None))
    r.state(300.0, paused=True)              # far from the clock, which start left at 0
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    assert r.s.state == "preparing" and r.of(Status)[-1].state == "preparing"

def test_a_scrub_while_paused_is_caught_as_a_seek_and_keeps_the_pause(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)               # the owner pauses
    r.state(5.0, paused=True)                # and drags the playhead back to 5.0
    assert r.s.state == "holding" and r.s.produced_count == 0
    r.produce_all(); r.sent.clear()
    r.state(5.0, paused=True)
    assert r.s.state == "paused" and Control(action="play", value=None) not in r.sent

def test_a_seek_while_the_owner_has_paused_keeps_the_pause(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)               # the owner pauses
    r.s.handle(Seek(current_time=5.0))       # outside the produced range
    assert r.s.state == "holding"
    r.produce_all(); r.sent.clear()
    r.state(5.0, paused=True)
    assert r.s.state == "paused" and r.sent[-1].state == "paused"
    assert Control(action="play", value=None) not in r.sent
    r.state(5.0, paused=False)               # the owner plays
    assert r.s.state == "playing" and Control(action="play", value=None) not in r.sent

def test_an_owner_paused_hold_past_its_cap_stays_paused(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)
    r.s.handle(Seek(current_time=5.0)); r.sent.clear()
    r.advance(20.5); r.state(5.0, paused=True)
    assert r.s.state == "paused" and r.sent[-1].message == "hold exceeded"
    assert Control(action="play", value=None) not in r.sent

def test_stop_during_an_owner_paused_hold_leaves_the_video_paused(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)
    r.s.handle(Seek(current_time=5.0)); r.sent.clear()
    r.s.handle(Stop())
    assert r.s.state == "idle" and Duck(volume=None) in r.sent
    assert Control(action="play", value=None) not in r.sent

def test_the_owner_playing_during_a_paused_seek_hold_ends_it_as_usual(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)
    r.s.handle(Seek(current_time=5.0))
    r.state(5.0, paused=False)               # the owner plays before anything is ready
    assert r.s.state == "holding"
    r.produce_all(); r.sent.clear()
    r.state(5.0, paused=False)
    assert r.s.state == "playing" and r.sent[0] == Control(action="play", value=None)

def test_a_seek_within_a_quarter_second_of_the_playhead_does_nothing(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.s.handle(Seek(current_time=25.2))
    assert r.sent == [] and r.audio.media_now(0.0) == 25.0
    r.s.handle(Seek(current_time=24.7))      # 0.3 s away: a seek, inside the produced range
    assert r.audio.media_now(0.0) == 24.7

def test_state_for_another_video_stops_the_session(tmp_path):
    r = Rig(tmp_path)
    r.start(at=25.0)
    r.produce_all(); r.sent.clear()
    r.state(3.0, video="w")
    assert r.s.state == "idle" and r.s.produce_next() is False
    assert Duck(volume=None) in r.sent and r.sent[-1].state == "idle" and r.sent[-1].message == "video changed"
    assert Control(action="pause", value=None) not in r.sent and Control(action="play", value=None) not in r.sent
    assert r.audio.played_until() == 0.0

def test_ready_near_the_end_when_every_remaining_segment_is_produced(tmp_path):
    r = Rig(tmp_path, startup_ready=12.0)
    r.start(at=49.0)
    r.produce_all()                           # only the last segment, 50..52: 3 s ahead, under 12
    assert r.s.produced_until == 52.0
    r.state(49.0, paused=True)
    assert r.s.state == "playing"

def test_not_ready_until_startup_ready_is_produced_ahead(tmp_path):
    r = Rig(tmp_path, startup_ready=12.0)
    r.start(at=9.0)
    r.s.produce_next()                        # 10..12 only: 3 s ahead
    r.state(9.0, paused=True)
    assert r.s.state == "preparing"

def test_hold_past_the_cap_plays_with_hold_exceeded_and_does_not_hold_again(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)                           # nothing produced: the producer never ran
    r.advance(20.0); r.state(9.0, paused=True)
    assert r.s.state == "preparing"           # 20 s is the cap itself, not past it
    r.advance(0.5); r.state(9.0, paused=True)
    assert r.s.state == "playing" and Control(action="play", value=None) in r.sent
    assert r.of(Status)[-1].state == "playing" and r.of(Status)[-1].message == "hold exceeded"
    r.sent.clear()
    r.advance(0.25); r.state(9.0)
    r.advance(0.25); r.state(9.25)
    assert r.s.state == "playing" and Control(action="pause", value=None) not in r.sent

def test_hold_past_the_cap_with_no_captions_ends_the_run_and_gives_the_video_back(tmp_path):
    r = Rig(tmp_path)
    r.s.handle(Start(mode=None))              # the captions never arrive
    r.state(9.0, paused=True)
    r.advance(20.0)
    r.state(9.0, paused=True)
    assert r.s.state == "preparing"           # 20 s is the cap itself, not past it
    r.sent.clear()
    r.advance(0.5)
    r.state(9.0, paused=True)
    assert r.s.state == "idle"
    assert r.sent == [Duck(volume=None), Control(action="play", value=None),
                      Status(mode="captions", state="idle", buffered_ahead=0.0, lag=0.0, engine="identity+seconds", message="no captions arrived")]

def test_hold_cap_counts_from_the_seek_that_started_the_hold(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.advance(15.0); r.state(40.0)
    r.s.handle(Seek(current_time=5.0)); r.state(5.0, paused=True)
    r.advance(15.0); r.state(5.0, paused=True)
    assert r.s.state == "holding"             # 15 s into this hold, 30 s after start
    r.advance(5.5); r.state(5.0, paused=True)
    assert r.s.state == "playing" and r.of(Status)[-1].message == "hold exceeded"

def test_playing_past_the_produced_coverage_holds(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.s.produce_next()                        # 10..12 only
    r.state(9.0); r.sent.clear()              # released, clock running
    r.advance(4.0); r.state(13.0)             # the next segment, 20..22, is not produced
    assert r.s.state == "holding"
    assert r.sent[0] == Control(action="pause", value=None) and r.sent[1].state == "holding"

def test_pause_and_resume_follow_the_player_while_playing(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)
    assert r.s.state == "paused" and r.of(Status)[-1].state == "paused"
    r.state(25.0, paused=True)
    assert len(r.of(Status)) == 1
    r.state(25.0, paused=False)
    assert r.s.state == "playing" and r.of(Status)[-1].state == "playing"

def test_paused_reports_do_not_count_as_a_user_pause_while_preparing(tmp_path):
    r = Rig(tmp_path, startup_ready=12.0)
    r.start(at=25.0)
    r.state(25.0, paused=True)
    assert r.s.state == "preparing"

def test_producer_drops_a_segment_the_playhead_passed_while_playing(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.s.produce_next()                        # 10..12
    r.state(9.0)                              # released; the clock runs from 9.0
    r.advance(14.0)                           # media time 23.0: 20..22 went by unproduced
    assert r.s.produce_next() is True
    first, dropped, made = r.metrics.records
    assert (first.idx, first.dropped) == (0, False)
    assert (dropped.idx, dropped.dropped, dropped.ahead) == (1, True, None)
    assert (made.idx, made.dropped, made.scheduled_start) == (2, False, 30.0)
    assert r.s.produce_next() is True and len(r.metrics.records) == 4   # dropped once only
    assert not any(isinstance(m, Subtitle) and m.start == 20.0 for m in r.sent)

def test_segments_skipped_before_start_or_by_a_seek_get_no_record(tmp_path):
    r = Rig(tmp_path)
    r.start(at=25.0)                          # 10..12 and 20..22 lie before the start
    r.s.produce_next()                        # 30..32
    r.state(25.0)
    r.s.handle(Seek(current_time=45.0))       # holds: 50..52 is not produced
    r.produce_all()                           # 50..52 only: 40..42 lies behind 45
    r.s.handle(Seek(current_time=31.0))       # back into 30..32: the hold ends at once
    # 40..42 is wanted again now, so 50..52, which lay after it, is to be made again in its
    # turn (2026-11-14): its clip is gone when the next seek lands inside it.
    r.s.handle(Seek(current_time=51.0))       # forward over the unproduced 40..42, into 50..52
    assert r.s.state == "holding"
    assert r.s.produce_next() is False        # a sentence that began a second ago is not started late
    assert [rec.idx for rec in r.metrics.records] == [2, 4]
    r.s.tick()
    assert r.s.state == "playing"             # nothing is left to make: the hold ends

def test_after_a_seek_back_a_segment_the_playhead_passes_unproduced_is_dropped(tmp_path):
    r = Rig(tmp_path)
    r.start(at=25.0)
    r.s.produce_next()                        # 30..32
    r.state(25.0)
    r.s.handle(Seek(current_time=45.0))       # holds: 50..52 is not produced
    r.produce_all()                           # 50..52 only
    r.s.handle(Seek(current_time=31.0))       # back into 30..32: playing at once
    r.advance(12.0)                           # media 43.0: 40..42 went by unproduced
    r.s.produce_next()                        # and 50..52, which lay after it, is made again in its turn
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(2, False), (4, False), (3, True), (4, False)]

def test_segments_the_playhead_passes_while_the_player_is_held_get_no_record(tmp_path):
    r = Rig(tmp_path, startup_ready=12.0)
    r.start(at=9.0)
    r.state(9.0)                              # the pause has not reached the player: the clock runs
    r.advance(14.0)                           # media 23.0: 10..12 and 20..22 went by while preparing
    r.produce_all()
    assert r.s.state == "preparing"
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(2, False), (3, False), (4, False)]

def test_segments_passed_during_a_hold_are_skipped_when_it_ends(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.state(9.0)                              # the pause has not reached the player: the clock runs
    r.advance(20.5); r.state(29.5)            # 10..12 and 20..22 went by; the cap ends the hold
    assert r.s.state == "playing"
    r.produce_all()
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(2, False), (3, False), (4, False)]

def test_captions_replaced_mid_run_skip_what_lies_behind_the_playhead(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    assert r.s.produced_count == 0 and r.audio.played_until() == 0.0
    r.produce_all()
    assert [rec.idx for rec in r.metrics.records] == [2, 3, 4, 2, 3, 4]
    assert not any(rec.dropped for rec in r.metrics.records)

def test_a_new_start_does_not_take_the_first_report_for_a_seek(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)                        # the clock runs
    r.s.handle(Stop())
    r.advance(60.0)
    r.start(at=200.0)                         # started again with the player elsewhere
    assert r.s.state == "preparing"

def test_a_seek_unmarks_a_clip_that_was_pushed_past_the_seek_point(tmp_path):
    r = Rig(tmp_path, voice=SecondsVoice({0: 4.0}))
    r.start(json3=track((10, 12), (12, 12.5), (20, 22)), at=9.0)
    r.s.produce_next(); r.s.produce_next()    # 10..12 runs to 13.33, so 12..12.5 plays from 13.33
    assert r.of(Subtitle)[1].start == pytest.approx(13.333, abs=0.001)
    r.s.handle(Seek(current_time=13.0))       # 20..22 is not produced: hold, cancelling the 13.33 clip
    assert r.s.produced_count == 1
    r.s.handle(Seek(current_time=12.2))       # inside 12..12.5, whose clip is gone
    assert r.s.state == "holding" and r.s.produced_count == 1

class FailingVoice(SecondsVoice):
    def __init__(self, failure, bad="number 0 "):
        super().__init__()
        self.failure, self.bad = failure, bad
    def synthesize(self, text, speed=1.0):
        if self.bad in text:
            if self.failure == "empty":
                return Clip(samples=np.zeros(0, dtype=np.float32), sample_rate=22050)
            raise self.failure
        return super().synthesize(text, speed)

class FailingTranslator:
    name = "failing"
    def translate(self, text, context=(), glossary=None):
        if "number 0 " in text:
            raise TranslatorError("server down")
        return text.upper()

@pytest.mark.parametrize("voice, translator, shown", [
    (FailingVoice(VoiceError("say failed")), None, TEXT(0)),
    (FailingVoice(RuntimeError("model crashed")), None, TEXT(0)),
    (FailingVoice("empty"), None, TEXT(0)),
    (SecondsVoice(), FailingTranslator(), TEXT(0)),
])
def test_an_engine_failure_shows_the_subtitle_logs_a_drop_and_production_goes_on(tmp_path, voice, translator, shown):
    r = Rig(tmp_path, voice=voice, translator=translator)
    r.start(at=9.0); r.sent.clear()
    assert r.s.produce_next() is True
    assert r.of(Subtitle) == [Subtitle(text=shown, start=10.0, end=12.0)]
    assert any("segment 0" in m.message for m in r.messages())
    rec, = r.metrics.records
    assert (rec.idx, rec.dropped, rec.ahead) == (0, True, None)
    assert r.audio.played_until() == 0.0      # nothing scheduled
    assert r.s.produce_next() is True
    assert r.of(Subtitle)[-1].start == 20.0 and r.audio.played_until() == 21.0
    assert r.metrics.records[-1].dropped is False

def test_a_failed_translation_is_not_voiced(tmp_path):
    r = Rig(tmp_path, translator=FailingTranslator())
    r.start(at=9.0)
    r.produce_all()
    assert r.voice.calls == [TEXT(i).upper() for i in range(1, 5)]

def test_a_timeout_with_no_words_still_says_what_it_was_on_the_bar(tmp_path):
    """httpx leaves the message of a timeout empty, and the PC translator's error then has none either."""
    def timed_out(request):
        raise httpx.ReadTimeout("")
    pc = LlamaCppTranslator("http://pc:8080", None, model="m", transport=httpx.MockTransport(timed_out))
    r = Rig(tmp_path, translator=pc)
    r.start(at=9.0)
    r.sent.clear()
    assert r.s.produce_next() is True
    assert [m.message for m in r.messages()] == ["segment 0 at 10.0 s not translated (TranslatorError: ReadTimeout)"]

@pytest.mark.parametrize("failure, shown", [
    (VoiceError(""), "not voiced (VoiceError)"),                                  # no words and no cause: the type alone
    (RuntimeError("model crashed"), "not voiced (RuntimeError: model crashed)"),  # an error with a message reads as before
])
def test_a_voice_error_is_described_by_its_type_and_its_message_when_it_has_one(tmp_path, failure, shown):
    r = Rig(tmp_path, voice=FailingVoice(failure))
    r.start(at=9.0)
    r.sent.clear()
    assert r.s.produce_next() is True
    assert [m.message for m in r.messages()] == [f"segment 0 at 10.0 s {shown}"]

class SeekingVoice(SecondsVoice):
    """Delivers a message to the session while a clip whose text holds `when` is made."""
    def __init__(self, msg, when=""):
        super().__init__()
        self.msg, self.when, self.session = msg, when, None
    def synthesize(self, text, speed=1.0):
        if self.msg is not None and self.when in text:
            msg, self.msg = self.msg, None
            self.session.handle(msg)
        return super().synthesize(text, speed)

@pytest.mark.parametrize("msg", [
    Seek(current_time=5.0),
    State(video_id="v", current_time=5.0, rate=1.0, paused=True, live=False, volume=0.8),   # a jump while paused
    Captions(video_id="v", lang="en", kind="asr", json3=FIVE),
    Stop(),
])
def test_a_result_made_stale_during_synthesis_is_discarded(tmp_path, msg):
    voice = SeekingVoice(msg)
    r = Rig(tmp_path, voice=voice)
    voice.session = r.s
    r.start(at=35.0); r.sent.clear()          # 40..42 is being made when the message comes
    assert r.s.produce_next() is True
    assert r.of(Subtitle) == [] and r.metrics.records == [] and r.audio.played_until() == 0.0
    assert r.s.produced_count == 0

def test_after_a_stale_result_the_producer_starts_from_the_seek(tmp_path):
    voice = SeekingVoice(Seek(current_time=5.0))
    r = Rig(tmp_path, voice=voice)
    voice.session = r.s
    r.start(at=35.0)
    r.s.produce_next()
    r.sent.clear()
    r.s.produce_next()
    assert r.of(Subtitle) == [Subtitle(text=TEXT(0), start=10.0, end=11.0)]

def test_a_seek_to_where_a_detected_jump_went_changes_nothing(tmp_path):
    voice = SeekingVoice(Seek(current_time=5.0), when="number 0 ")   # "seeked" arrives while 10..12 is made
    r = Rig(tmp_path, voice=voice, startup_ready=30.0)
    voice.session = r.s
    r.playing_at(25.0)
    r.advance(0.25); r.state(5.0, paused=True)          # "seeking": a jump back to 5.0 holds
    assert r.s.state == "holding" and r.s.produced_count == 0
    r.advance(1.0)
    r.s.produce_next()                                   # 10..12, with Seek(5.0) arriving meanwhile
    assert r.of(Subtitle)[-1] == Subtitle(text=TEXT(0), start=10.0, end=11.0)   # kept: no second generation
    r.s.handle(Seek(current_time=5.1))                   # within 0.25 s of the playhead
    assert r.s.state == "holding" and r.s.produced_count == 1 and r.audio.played_until() == 11.0
    assert r.of(Control).count(Control(action="pause", value=None)) == 1
    assert [m.state for m in r.of(Status)].count("holding") == 1
    r.advance(18.75); r.state(5.0, paused=True)         # 20.0 s after the jump: not past the cap
    assert r.s.state == "holding"
    r.advance(0.5); r.state(5.0, paused=True)
    assert r.s.state == "playing" and r.of(Status)[-1].message == "hold exceeded"

def test_a_clip_finished_after_its_segment_went_by_is_not_played(tmp_path):
    class SlowVoice(SecondsVoice):
        def synthesize(self, text, speed=1.0):
            if "number 1 " in text:
                r.advance(13.0)               # the playhead moves on while this runs
            return super().synthesize(text, speed)
    r = Rig(tmp_path, voice=SlowVoice())
    r.start(at=9.0)
    r.s.produce_next()                        # 10..12
    r.state(9.0)                              # released: the clock runs from 9.0
    r.sent.clear()
    assert r.s.produce_next() is True         # 20..22 is done at media 22.0: too late
    assert r.of(Subtitle) == [] and r.audio.played_until() == 11.0
    r.s.produce_next()
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(0, False), (1, True), (2, False)]

def test_late_clip_starts_after_the_playhead_not_in_the_past(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.s.produce_next()
    r.state(9.0)
    r.advance(11.5)                           # media 20.5: inside 20..22, not produced
    r.s.produce_next()
    assert r.of(Subtitle)[-1] == Subtitle(text=TEXT(1), start=20.65, end=21.65)

def test_slow_sends_rate_0_9_and_restores_the_rate_once_caught_up(tmp_path):
    voice = SecondsVoice({0: 4.0, 1: 4.0})
    r = Rig(tmp_path, voice=voice, slow_player=True)
    r.start(json3=track((10, 12), (12, 14), (14, 16), (40, 42)), at=9.0)
    r.sent.clear()
    r.s.produce_next()                        # overruns by 1.33 s
    assert not any(isinstance(m, Control) for m in r.sent)
    r.s.produce_next()                        # 2.67 s behind: more than 3 x 0.8
    assert Control(action="rate", value=0.9) in r.sent
    assert "slowing to catch up" in [m.message for m in r.messages()]
    r.sent.clear()
    r.s.produce_next()                        # ends long before 40 s: caught up
    assert r.of(Control) == [Control(action="rate", value=1.0)]

def test_a_seek_while_slowing_restores_the_rate(tmp_path):
    r = Rig(tmp_path, voice=SecondsVoice({0: 4.0, 1: 4.0}), slow_player=True)
    r.start(json3=track((10, 12), (12, 14), (14, 16), (40, 42)), at=9.0)
    r.s.produce_next(); r.s.produce_next()
    r.sent.clear()
    r.s.handle(Seek(current_time=41.0))
    assert Control(action="rate", value=1.0) in r.sent

def test_stop_restores_the_rate_the_session_changed(tmp_path):
    r = Rig(tmp_path, voice=SecondsVoice({0: 4.0, 1: 4.0}), slow_player=True)
    r.start(json3=track((10, 12), (12, 14), (14, 16), (40, 42)), at=9.0)
    r.s.produce_next(); r.s.produce_next()
    r.sent.clear()
    r.s.handle(Stop())
    assert r.of(Control)[0] == Control(action="rate", value=1.0)

def test_stop_gives_back_the_rate_that_was_clamped(tmp_path):
    r = Rig(tmp_path)
    r.start(at=25.0)
    r.state(25.0, rate=2.0, paused=True)
    r.state(25.0, rate=2.0, paused=True)      # not yet applied: pushed back once only
    assert r.of(Control).count(Control(action="rate", value=1.25)) == 1
    assert any("1.25" in m.message for m in r.messages())
    r.state(25.0, rate=1.25, paused=True)
    r.state(25.0, rate=1.75, paused=True)     # another offending rate is pushed back too
    assert r.of(Control).count(Control(action="rate", value=1.25)) == 2
    r.sent.clear()
    r.s.handle(Stop())
    assert r.sent[0] == Control(action="rate", value=1.75)

def test_stop_leaves_a_rate_the_owner_chose_after_the_clamp(tmp_path):
    r = Rig(tmp_path)
    r.start(at=25.0)
    r.state(25.0, rate=2.0, paused=True)
    r.state(25.0, rate=1.25, paused=True)
    r.state(25.0, rate=1.0, paused=True)      # chosen by the owner
    r.sent.clear()
    r.s.handle(Stop())
    assert r.of(Control) == [Control(action="play", value=None)]

def test_no_rate_control_while_idle(tmp_path):
    r = Rig(tmp_path)
    r.state(25.0, rate=2.0)
    assert r.sent == []

def test_stop_while_holding_the_player_plays_it_again_and_stop_after_release_does_not(tmp_path):
    r = Rig(tmp_path)
    r.start(at=25.0); r.sent.clear()
    r.s.handle(Stop())
    assert r.sent == [Duck(volume=None), Control(action="play", value=None),
                      Status(mode="captions", state="idle", buffered_ahead=0.0, lag=0.0, engine="identity+seconds", message="")]
    r2 = Rig(tmp_path)
    r2.playing_at(25.0)
    r2.s.handle(Stop())
    assert Control(action="play", value=None) not in r2.sent and Duck(volume=None) in r2.sent
    assert r2.s.state == "idle" and r2.s.produced_count == 0 and r2.s.segments == []

def test_status_while_playing_is_sent_at_most_once_a_second(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)                        # the release Status went out at wall 0.0
    for i in range(1, 9):                     # a report every 0.25 s for 2 s
        r.advance(0.25); r.state(25.0 + 0.25 * i)
    statuses = r.of(Status)
    assert [(m.state, m.buffered_ahead, m.message) for m in statuses] == [("playing", 26.0, ""), ("playing", 25.0, "")]

def test_status_says_the_look_ahead_is_low_while_playing_with_work_left(tmp_path):
    r = Rig(tmp_path, lead_max=10.0)
    r.start(at=9.0)
    assert r.produce_all() == 2               # stops once 10 s or more is ready
    r.state(9.0)
    status = r.of(Status)[-1]
    assert (status.state, status.buffered_ahead, status.message) == ("playing", 13.0, "look-ahead low")

def test_metrics_records_carry_the_session_lag_ahead_and_plain_numbers(tmp_path):
    r = Rig(tmp_path, voice=SecondsVoice(rate=np.int64(22050)))   # numpy rate: numpy seconds
    r.t[0] = 5.0
    r.s.handle(Start(mode=None))
    r.state(9.0, paused=True)
    r.advance(1.5)
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    r.s.produce_next()                        # 10..12 at 10.0, the playhead paused at 9.0
    r.state(9.0)                              # released: the clock runs from 9.0 at wall 6.5
    r.advance(11.5)                           # media 20.5, inside 20..22
    r.s.produce_next()                        # scheduled at 20.65
    first, second = r.metrics.records
    assert first.session == second.session == r.s.session_id != ""
    assert (first.scheduled_start, first.lag, first.ahead) == (10.0, 0.0, 1.0)
    assert (second.video_id, second.idx, second.src_start, second.src_end) == ("v", 1, 20.0, 22.0)
    assert second.scheduled_start == pytest.approx(20.65) and second.actual_start is None
    assert second.lag == pytest.approx(0.65) and second.ahead == pytest.approx(0.15)
    assert (second.text_ready_at, second.audio_seconds, second.speed) == (1.5, 1.0, 1.0)
    assert (second.mt_engine, second.tts_engine, second.dropped) == ("identity", "seconds", False)
    assert second.mt_ms >= 0.0 and second.tts_ms >= 0.0
    for rec in r.metrics.records:
        for name, value in dataclasses.asdict(rec).items():
            assert type(value) in (str, int, float, bool, type(None)), (name, type(value))
    lines = (tmp_path / "m.jsonl").read_text().splitlines()
    assert [json.loads(line)["idx"] for line in lines] == [0, 1]

def test_a_metrics_file_that_cannot_be_written_is_logged_once_and_production_goes_on(tmp_path, caplog):
    blocked = tmp_path / "blocked"; blocked.mkdir()
    r = Rig(tmp_path, metrics_path=blocked)   # a directory: every write raises OSError
    r.start(at=9.0)
    with caplog.at_level(logging.WARNING, logger="dubber.session"):
        assert r.produce_all() == 5
    assert len([rec for rec in caplog.records if rec.name == "dubber.session"]) == 1
    assert len(r.of(Subtitle)) == 5

class Stream:
    def __init__(self, fail=False):
        self.fail, self.started, self.stopped = fail, 0, 0
    def start(self):
        if self.fail:
            raise OSError("no output device")
        self.started += 1
    def stop(self):
        self.stopped += 1
    def close(self):
        pass

def test_the_output_stream_opens_with_the_first_clip_and_closes_on_stop(tmp_path):
    streams = []
    def factory(cb):
        streams.append(Stream())
        return streams[-1]
    r = Rig(tmp_path, stream_factory=factory)
    r.start(at=9.0)
    r.state(9.0, paused=True)
    assert streams == []                      # nothing is opened before there is a clip
    r.produce_all()
    assert len(streams) == 1 and streams[0].started == 1
    r.s.handle(Stop())
    assert streams[0].stopped == 1
    r.start(at=9.0); r.s.produce_next()
    assert len(streams) == 2 and streams[1].started == 1

def test_an_output_stream_that_cannot_start_is_reported_once_and_production_goes_on(tmp_path, caplog):
    r = Rig(tmp_path, stream_factory=lambda cb: Stream(fail=True))
    r.start(at=9.0)
    with caplog.at_level(logging.WARNING, logger="dubber.session"):
        assert r.produce_all() == 5
    assert r.audio.played_until() == 51.0     # the clips are on the timeline all the same
    assert len([m for m in r.messages() if "audio" in m.message]) == 1
    assert len([rec for rec in caplog.records if rec.name == "dubber.session"]) == 1

def test_run_producer_produces_in_its_thread_and_stops_when_told(tmp_path):
    made_all = threading.Event()
    class Voice(SecondsVoice):
        def synthesize(self, text, speed=1.0):
            clip = super().synthesize(text, speed)
            if len(self.calls) == 5:
                made_all.set()
            return clip
    r = Rig(tmp_path, voice=Voice())
    r.start(at=9.0)
    stop = threading.Event()
    worker = threading.Thread(target=r.s.run_producer, args=(stop,))
    worker.start()
    try:
        assert made_all.wait(timeout=2.0)
    finally:
        stop.set()
        worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert r.s.produced_count == 5

def test_run_producer_logs_an_unexpected_error_and_carries_on(tmp_path, caplog, monkeypatch):
    r = Rig(tmp_path)
    def broken(rec):
        raise ValueError("cannot record")
    monkeypatch.setattr(r.metrics, "write", broken)
    r.start(at=9.0)
    stop = threading.Event()
    worker = threading.Thread(target=r.s.run_producer, args=(stop,))
    with caplog.at_level(logging.ERROR, logger="dubber.session"):
        worker.start()
        try:
            for _ in range(200):
                if r.s.produced_count == 5:
                    break
                stop.wait(0.01)
        finally:
            stop.set()
            worker.join(timeout=1.0)
    assert not worker.is_alive() and r.s.produced_count == 5
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)



def word_track(words, step=0.3):
    """json3 with one event whose segs are timed words, as YouTube's auto-captions give them."""
    return {"events": [{"tStartMs": 0, "dDurationMs": round(len(words) * step * 1000),
                        "segs": [{"utf8": (" " if i else "") + w, "tOffsetMs": round(i * step * 1000)}
                                 for i, w in enumerate(words)]}]}


def test_the_session_cuts_with_the_configured_segmenter(tmp_path):
    cfg = Config()
    cfg.segmenter.max_words = 5
    r = Rig(tmp_path, cfg=cfg)
    r.start(word_track("one two three four five six seven eight nine ten.".split()))
    assert [s.words for s in r.s.segments] == [5, 5]


def test_a_track_without_sentence_ends_is_warned_about_once(tmp_path, caplog):
    r = Rig(tmp_path)
    with caplog.at_level(logging.WARNING, logger="dubber.session"):
        r.start(FIVE)
        assert not [rec for rec in caplog.records if "punctuation" in rec.getMessage()]
        r.start(word_track([f"word{i}" for i in range(80)]))
    warnings = [rec.getMessage() for rec in caplog.records if "punctuation" in rec.getMessage()]
    assert len(warnings) == 1 and warnings[0].startswith("the captions of v have almost no sentence punctuation")
    assert [s.words for s in r.s.segments] == [40, 40]


class ContextRecorder:
    """Gives the English back unchanged and keeps what each call was given."""
    name = "recorder"

    def __init__(self):
        self.seen = []

    def translate(self, text, context=(), glossary=None):
        self.seen.append((text, tuple(context), glossary))
        return text


def test_each_segment_is_translated_with_the_three_before_it_as_context(tmp_path):
    rec = ContextRecorder()
    r = Rig(tmp_path, translator=rec)
    r.start(FIVE)
    r.produce_all()
    assert [seen[:2] for seen in rec.seen] == [
        (TEXT(0), ()),
        (TEXT(1), (TEXT(0),)),
        (TEXT(2), (TEXT(0), TEXT(1))),
        (TEXT(3), (TEXT(0), TEXT(1), TEXT(2))),
        (TEXT(4), (TEXT(1), TEXT(2), TEXT(3))),
    ]


def test_context_comes_from_the_track_not_from_what_was_produced(tmp_path):
    rec = ContextRecorder()
    r = Rig(tmp_path, translator=rec)
    r.playing_at(25.0)  # the segments at 10 s and 20 s are behind the playhead and never produced
    assert rec.seen[0][:2] == (TEXT(2), (TEXT(0), TEXT(1)))
    r.s.handle(Seek(current_time=5.0))
    r.produce_all()
    assert (TEXT(1), (TEXT(0),)) in [seen[:2] for seen in rec.seen]


def test_context_sentences_zero_sends_no_context(tmp_path):
    cfg = Config()
    cfg.translator.context_sentences = 0
    rec = ContextRecorder()
    r = Rig(tmp_path, translator=rec, cfg=cfg)
    r.start(FIVE)
    r.produce_all()
    assert len(rec.seen) == 5 and {context for _, context, _ in rec.seen} == {()}


def test_the_configured_glossary_reaches_the_translator_and_an_empty_one_is_none(tmp_path):
    cfg = Config()
    cfg.glossary = {"Heighliner": "Heighliner", "spice": "baharat"}
    rec = ContextRecorder()
    r = Rig(tmp_path, translator=rec, cfg=cfg)
    r.start(FIVE)
    r.produce_all()
    assert [glossary for _, _, glossary in rec.seen] == [{"Heighliner": "Heighliner", "spice": "baharat"}] * 5
    plain = ContextRecorder()
    r = Rig(tmp_path, translator=plain)
    r.start(FIVE)
    r.produce_all()
    assert {glossary for _, _, glossary in plain.seen} == {None}


TIGHT = track((10, 12), (13, 15), (16, 18))  # 3 s from one start to the next


class WordsVoice:
    """One second of audio per word; keeps the texts it spoke."""
    name = "words"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, speed=1.0):
        self.calls.append(text)
        return Clip(samples=np.zeros(len(text.split()) * 22050, dtype=np.float32), sample_rate=22050)


class Shortener:
    """Gives the English back; asked to shorten, answers `shorter`, the same text, or fails."""
    name = "shortener"

    def __init__(self, shorter=None, fail=False):
        self.shorter, self.fail = shorter, fail
        self.asked = []

    def translate(self, text, context=(), glossary=None):
        return text

    def shorten(self, text, turkish, max_words, context=(), glossary=None):
        self.asked.append((text, turkish, max_words))
        if self.fail:
            raise RuntimeError("the PC went away")
        return turkish if self.shorter is None else self.shorter


def test_a_clip_that_cannot_fit_is_made_again_shorter_and_the_shorter_one_plays(tmp_path):
    shortener, voice = Shortener(shorter="Kısa bir cümle bu."), WordsVoice()
    r = Rig(tmp_path, voice=voice, translator=shortener, lead_min=0.0)
    r.start(TIGHT)
    assert r.s.produce_next() is True
    # six words take 6 s; 3 s of slot and 0.8 s of overrun at 1.2 hold 4.56 s, so at most 4 words
    assert shortener.asked == [(TEXT(0), TEXT(0), 4)]
    assert voice.calls == [TEXT(0), "Kısa bir cümle bu."]
    assert r.of(Subtitle)[0].text == "Kısa bir cümle bu."
    [record] = r.metrics.records
    assert record.shortened is True and record.audio_seconds == 4.0 and record.speed < 1.2


def test_a_shorter_rendering_that_is_not_shorter_keeps_the_first_clip_as_an_overrun(tmp_path):
    voice = WordsVoice()
    r = Rig(tmp_path, voice=voice, translator=Shortener(), lead_min=0.0)  # answers the same text
    r.start(TIGHT)
    r.s.produce_next()
    assert voice.calls == [TEXT(0)]  # the same text is not voiced again
    [record] = r.metrics.records
    assert r.of(Subtitle)[0].text == TEXT(0) and record.shortened is False and record.speed == 1.2


def test_a_failed_shorten_keeps_the_first_clip(tmp_path):
    r = Rig(tmp_path, voice=WordsVoice(), translator=Shortener(fail=True), lead_min=0.0)
    r.start(TIGHT)
    r.s.produce_next()
    [record] = r.metrics.records
    assert r.of(Subtitle)[0].text == TEXT(0) and record.shortened is False and not record.dropped


def test_shorten_is_asked_at_most_once_per_segment(tmp_path):
    shortener = Shortener(shorter="bir iki üç dört beş")  # 5 s: still too long, so it overruns
    r = Rig(tmp_path, voice=WordsVoice(), translator=shortener, lead_min=0.0)
    r.start(TIGHT)
    r.s.produce_next()
    [record] = r.metrics.records
    assert len(shortener.asked) == 1 and record.shortened is True and record.speed == 1.2


def test_a_start_in_the_middle_of_a_sentence_dubs_from_the_next_one(tmp_path):
    r = Rig(tmp_path)
    r.start(track((0, 6), (6, 10), (10, 14)), at=3.0)
    r.produce_all()
    assert [subtitle.text for subtitle in r.of(Subtitle)] == [TEXT(1), TEXT(2)]
    assert r.of(Subtitle)[0].start == 6.0
    r.state(3.0)
    assert r.s.state == "playing"
    r.advance(1.0)
    r.state(4.0)                                     # inside the skipped sentence: nothing to hold for
    assert r.s.state == "playing"
    r.advance(3.0)
    r.state(7.0)
    r.s.produce_next()
    assert not [record for record in r.metrics.records if record.dropped]


def test_a_first_report_after_the_captions_still_skips_the_partly_passed_sentence(tmp_path):
    r = Rig(tmp_path)
    r.start(track((0, 6), (6, 10)))                  # the captions come before the player's first report
    r.state(3.0, paused=True)
    r.produce_all()
    assert [subtitle.text for subtitle in r.of(Subtitle)] == [TEXT(1)]


def test_a_start_just_after_a_sentence_begins_keeps_it(tmp_path):
    r = Rig(tmp_path)
    r.start(track((0, 6), (6, 10)), at=0.4)
    r.produce_all()
    assert [subtitle.text for subtitle in r.of(Subtitle)] == [TEXT(0), TEXT(1)]


def test_a_seek_into_a_sentence_skips_to_the_next_one(tmp_path):
    r = Rig(tmp_path, lead_max=10.0)
    r.playing_at(0.0, json3=track((10, 16), (16, 22), (30, 36), (36, 42)))
    r.s.handle(Seek(current_time=33.0))              # inside a sentence not yet produced
    assert r.s.state == "holding"
    r.sent.clear()
    r.produce_all()
    assert [subtitle.text for subtitle in r.of(Subtitle)] == [TEXT(3)]
    assert r.of(Subtitle)[0].start == 36.0
    assert not [record for record in r.metrics.records if record.dropped]


def test_a_segment_that_falls_behind_while_playing_is_still_placed_late(tmp_path):
    r = Rig(tmp_path, lead_max=2.0)
    r.start(track((1, 7), (7, 13), (13, 19)), at=0.0)
    r.produce_all()                                  # only the first: the look-ahead is full
    r.state(0.0)
    assert r.s.state == "playing"
    r.sent.clear()
    r.advance(8.0)
    r.state(8.0)                                     # the producer fell behind: 1 s into the second sentence
    assert r.s.state == "holding"
    assert r.s.produce_next() is True
    assert [subtitle.text for subtitle in r.of(Subtitle)] == [TEXT(1)]


def test_the_player_is_not_slowed_while_the_mixer_cannot_follow_its_rate(tmp_path):
    r = Rig(tmp_path, voice=SecondsVoice({0: 4.0, 1: 4.0}))
    r.start(json3=track((10, 12), (12, 14), (14, 16), (40, 42)), at=9.0)
    r.sent.clear()
    r.produce_all()                           # 2.67 s behind at the second clip
    assert not r.of(Control) and "slowing to catch up" not in [m.message for m in r.messages()]


# The run log (design of 2026-11-14, 4.1): a clip that came too late keeps what it cost, and the
# session says what it does, so that a run can be read afterwards.
class LateVoice(SecondsVoice):
    """Takes `took` seconds of the rig's clock to make the clip of sentence `number`."""
    def __init__(self, number, took):
        super().__init__()
        self.number, self.took, self.rig = number, took, None
    def synthesize(self, text, speed=1.0):
        if f"number {self.number} " in text:
            self.rig.advance(self.took)
        return super().synthesize(text, speed)

def late_rig(tmp_path):
    voice = LateVoice(1, 13.0)
    r = Rig(tmp_path, voice=voice)
    voice.rig = r
    return r

def test_a_clip_that_comes_too_late_keeps_its_times_in_the_metrics(tmp_path):
    r = late_rig(tmp_path)
    r.start(at=9.0)
    r.s.produce_next()                        # 10..12
    r.state(9.0)                              # released: the clock runs from 9.0
    r.s.produce_next()                        # 20..22 is ready at media 22.0: too late
    late = [rec for rec in r.metrics.records if rec.idx == 1]
    assert len(late) == 1 and late[0].dropped  # logged at once, not by the next pick
    assert late[0].audio_seconds == 1.0 and late[0].tts_ms > 0 and late[0].mt_ms >= 0
    r.s.produce_next()                        # and the next pick does not log it again
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(0, False), (1, True), (2, False)]

def test_the_session_logs_what_it_does_and_what_each_sentence_cost(tmp_path, caplog):
    r = late_rig(tmp_path)
    with caplog.at_level(logging.INFO, logger="dubber.session"):
        r.start(at=9.0)
        r.s.produce_next()                    # 10..12 is placed
        r.advance(2.0); r.state(9.0)          # released after 2 s
        r.s.produce_next()                    # 20..22 comes too late, at media 22.0
        r.state(22.0)                         # nothing is ready at 30..32: a hold
        r.state(22.0, paused=True)            # the player reports the pause
        r.advance(20.5); r.state(22.0, paused=True)   # and the hold passes its cap
        r.s.handle(Stop())
    said = [rec.getMessage() for rec in caplog.records if rec.name == "dubber.session"]
    def line(*parts):
        found = [m for m in said if all(part in m for part in parts)]
        assert len(found) == 1, (parts, said)
        return found[0]
    assert line("session", "started").split()[1] == r.s.session_id != ""
    line("v:", "5 sentences")
    line("segment 0 at 10.0 s", "placed at 10.0 s", "1.0 s ahead", "clip 1.0 s")
    line("playing from 9.0 s", "after 2.0 s")
    line("segment 1 at 20.0 s", "lost", "clip 1.0 s")
    line("holding at 22.0 s")
    line("hold", "22.0 s", "passed its cap of 20 s")
    line("session", "ended")
    assert [rec.levelname for rec in caplog.records if "passed its cap" in rec.getMessage()] == ["WARNING"]

def test_a_jump_is_logged_with_what_it_found(tmp_path, caplog):
    r = Rig(tmp_path)
    r.playing_at(9.0)
    with caplog.at_level(logging.INFO, logger="dubber.session"):
        r.s.handle(Seek(current_time=20.5))   # produced there
        r.s.handle(Seek(current_time=500.0))  # nothing there
    said = [rec.getMessage() for rec in caplog.records if rec.name == "dubber.session"]
    assert [m for m in said if m.startswith("jump to 20.5 s") and "holding" not in m]
    assert [m for m in said if m.startswith("jump to 500.0 s") and "holding" in m]

def test_a_stop_while_idle_logs_nothing(tmp_path, caplog):
    r = Rig(tmp_path)
    with caplog.at_level(logging.INFO, logger="dubber.session"):
        r.s.handle(Stop())
    assert [rec for rec in caplog.records if rec.name == "dubber.session"] == []


# Two engines at once (design of 2026-11-14, 4.2): the next sentence is translated while the
# current one is voiced.
class NotingTranslator(IdentityTranslator):
    """Writes "mt <sentence number>" into a list it shares with a NotingVoice."""
    def __init__(self, events):
        self.events = events
    def translate(self, text, context=(), glossary=None):
        self.events.append("mt " + text.split()[4])
        return text

class NotingVoice(SecondsVoice):
    def __init__(self, events):
        super().__init__()
        self.events = events
    def synthesize(self, text, speed=1.0):
        self.events.append("tts " + text.split()[4])
        return super().synthesize(text, speed)

def noting_rig(tmp_path, **kwargs):
    events = []
    return Rig(tmp_path, translator=NotingTranslator(events), voice=NotingVoice(events), **kwargs), events

def test_the_next_sentence_is_translated_before_this_one_is_voiced(tmp_path):
    r, events = noting_rig(tmp_path, spawn=Eager)
    r.start(at=9.0)
    assert r.produce_all() == 5
    assert events == ["mt 0", "mt 1", "tts 0", "mt 2", "tts 1", "mt 3", "tts 2", "mt 4", "tts 3", "tts 4"]
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(i, False) for i in range(5)]

def test_a_translation_asked_for_lazily_keeps_the_serial_order(tmp_path):
    r, events = noting_rig(tmp_path)          # the Rig's own spawn
    r.start(at=9.0)
    r.produce_all()
    assert events == ["mt 0", "tts 0", "mt 1", "tts 1", "mt 2", "tts 2", "mt 3", "tts 3", "mt 4", "tts 4"]

def test_nothing_is_translated_ahead_when_the_look_ahead_is_full(tmp_path):
    r, events = noting_rig(tmp_path, spawn=Eager, lead_max=15.0)
    r.start(at=9.0)
    assert r.produce_all() == 3               # 10..12, 20..22, 30..32: then 23 s are ready ahead
    assert events == ["mt 0", "mt 1", "tts 0", "mt 2", "tts 1", "tts 2"]

OTHER = {"events": [
    {"tStartMs": 10000, "dDurationMs": 2000, "segs": [{"utf8": "A new first line of six words."}]},
    {"tStartMs": 20000, "dDurationMs": 2000, "segs": [{"utf8": "A new second line of six words."}]},
]}

def test_a_translation_made_ahead_for_another_track_is_not_used(tmp_path):
    r, events = noting_rig(tmp_path, spawn=Eager)
    r.start(at=9.0)
    r.s.produce_next()                        # voices 0, with 1 translated ahead
    assert events == ["mt 0", "mt 1", "tts 0"]
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=OTHER))
    r.produce_all()
    assert r.voice.calls == [TEXT(0), "A new first line of six words.", "A new second line of six words."]
    assert [m.text for m in r.of(Subtitle)][1:] == ["A new first line of six words.", "A new second line of six words."]

def test_a_translation_made_ahead_survives_a_seek_on_the_same_track(tmp_path):
    r, events = noting_rig(tmp_path, spawn=Eager)
    r.start(at=9.0)
    r.s.produce_next()                        # voices 0, with 1 translated ahead
    r.state(9.0)
    r.s.handle(Seek(current_time=19.0))       # nothing is ready at 20..22: a new generation
    r.s.produce_next()
    assert events.count("mt 1") == 1 and events[-1] == "tts 1"

def test_a_translation_made_ahead_that_failed_is_shown_as_not_translated(tmp_path):
    class Failing(IdentityTranslator):
        def translate(self, text, context=(), glossary=None):
            if "number 1 " in text:
                raise TranslatorError("boom")
            return text
    r = Rig(tmp_path, translator=Failing(), spawn=Eager)
    r.start(at=9.0)
    r.produce_all()
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(0, False), (1, True), (2, False), (3, False), (4, False)]
    assert TEXT(1) not in r.voice.calls
    assert [m.message for m in r.messages() if "segment 1" in m.message] == ["segment 1 at 20.0 s not translated (TranslatorError: boom)"]

class OneAtATime(IdentityTranslator):
    """Notes when a second call comes in while one is still running."""
    def __init__(self):
        self.inside, self.overlapped, self.shortened = 0, False, 0
    def _work(self):
        self.inside += 1
        self.overlapped = self.overlapped or self.inside > 1
        time.sleep(0.02)
        self.inside -= 1
    def translate(self, text, context=(), glossary=None):
        self._work()
        return text
    def shorten(self, text, turkish, max_words, context=(), glossary=None):
        self.shortened += 1
        self._work()
        return " ".join(turkish.split()[:max_words])

def test_the_translator_is_never_inside_two_calls_at_once(tmp_path):
    translator = OneAtATime()
    # 14 s do not fit the 10 s slot even at 1.2 times: a shorter rendering is asked for while
    # the next sentence is being translated on its own thread.
    r = Rig(tmp_path, translator=translator, voice=SecondsVoice({0: 14.0, 2: 14.0}), spawn=THREADS, lead_min=0.0)
    r.start(at=9.0)
    r.produce_all()
    assert translator.shortened == 2 and not translator.overlapped

def test_with_threads_the_dub_is_the_one_made_in_series(tmp_path):
    def run(spawn, where):
        where.mkdir()
        r, events = noting_rig(where, spawn=spawn)
        r.start(at=9.0)
        r.produce_all()
        return sorted(events), [(rec.idx, rec.dropped, rec.scheduled_start, rec.speed) for rec in r.metrics.records]
    assert run(THREADS, tmp_path / "threads") == run(Lazy, tmp_path / "series")


# Economy while the look-ahead is thin (design of 2026-11-14, 4.3): a shorter rendering costs a
# second translation and a second clip, so it is asked for only when there is time for it.
ROOMY_THEN_TIGHT = track((10, 12), (20, 22), (30, 32), (40, 42), (50, 52), (53, 55), (56, 58))

def test_no_shorter_rendering_is_asked_while_less_than_lead_min_is_ready(tmp_path):
    shortener, voice = Shortener(shorter="Kısa bir cümle bu."), WordsVoice()
    r = Rig(tmp_path, voice=voice, translator=shortener)   # lead_min is 30 s, and nothing is ready yet
    r.start(TIGHT)
    r.s.produce_next()
    assert shortener.asked == [] and voice.calls == [TEXT(0)]
    [record] = r.metrics.records
    # It plays as it is: at the top speed and past its slot.
    assert record.shortened is False and record.speed == 1.2 and not record.dropped
    assert r.of(Subtitle)[0].text == TEXT(0)

def test_a_shorter_rendering_is_asked_once_lead_min_is_ready(tmp_path):
    shortener, voice = Shortener(shorter="Kısa bir cümle bu."), WordsVoice()
    r = Rig(tmp_path, voice=voice, translator=shortener)
    r.start(ROOMY_THEN_TIGHT, at=9.0)
    for _ in range(4):
        r.s.produce_next()                    # 10..42: 33 s are ready ahead of 9.0
    assert shortener.asked == []
    r.s.produce_next()                        # 50..52: six words in a 3 s slot
    assert shortener.asked == [(TEXT(4), TEXT(4), 4)]
    assert r.metrics.records[-1].shortened is True

def test_a_late_clip_is_never_asked_to_be_shorter(tmp_path):
    shortener = Shortener(shorter="Kısa bir cümle bu.")
    r = Rig(tmp_path, voice=WordsVoice(), translator=shortener, lead_min=0.0)   # the look-ahead never forbids it
    r.start(TIGHT, at=9.0)
    r.advance(20.5); r.state(9.0, paused=True)   # the hold passes its cap: the video plays on
    r.state(9.0); r.advance(1.5)              # and is at 10.5, half a second into 10..12
    r.s.produce_next()
    [record] = r.metrics.records
    assert shortener.asked == [] and record.shortened is False
    assert record.scheduled_start == pytest.approx(10.65) and record.speed == 1.2


# Coming back after falling behind (design of 2026-11-14, 4.4). After the hold cap the video
# plays on. A producer slower than the sentences must not chase the playhead: each sentence
# would be over before its clip is ready, and the dub would stay silent to the end of the video
# (seen on 2026-11-14 over a long-distance link: 41 of 44 dropped while the PC made 25 clips for nothing).
DENSE = track(*[(10 + 3 * i, 13 + 3 * i) for i in range(30)])   # 3 s sentences, back to back, 10..100 s

class SlowVoice(SecondsVoice):
    """A voice whose every clip takes `took` seconds of the rig's clock to make."""
    def __init__(self, took):
        super().__init__()
        self.took, self.clock = took, None
    def synthesize(self, text, speed=1.0):
        self.clock[0] += self.took
        return super().synthesize(text, speed)

def behind(tmp_path, took, **kwargs):
    """A rig on DENSE whose hold at 9.0 has passed its cap: it plays, with nothing produced."""
    voice = SlowVoice(took)
    r = Rig(tmp_path, voice=voice, **kwargs)
    voice.clock = r.t
    r.start(json3=DENSE, at=9.0)
    r.advance(20.5); r.state(9.0, paused=True)
    assert r.s.state == "playing" and r.of(Status)[-1].message == "hold exceeded"
    r.state(9.0)                              # the player reports that it plays: the playhead runs
    return r

def test_after_the_hold_cap_a_slow_producer_aims_ahead_and_the_dub_comes_back(tmp_path):
    r = behind(tmp_path, took=4.0)            # 4 s for each 3 s sentence
    for _ in range(8):                        # 32 s of the video go by while the producer works
        r.s.produce_next()
    placed = [rec for rec in r.metrics.records if not rec.dropped]
    assert len(placed) >= 6, "the dub never came back"
    assert all(rec.lag <= 0.5 for rec in placed)   # and every clip it made starts on time

def test_what_the_aim_jumps_over_is_dropped_once_and_never_made(tmp_path):
    r = behind(tmp_path, took=4.0)
    for _ in range(4):
        r.s.produce_next()
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [
        (0, True),                            # made at the playhead, before any make was timed: too late
        (1, True), (2, True),                 # jumped over: a clip takes 4 s, and they begin sooner
        (3, False), (4, False), (5, False),
    ]
    assert [text.split()[4] for text in r.voice.calls] == ["0", "3", "4", "5"]
    assert [m.message for m in r.messages() if "catching up" in m.message] == ["catching up: 2 sentences skipped"]

def test_after_the_hold_cap_a_fast_producer_makes_every_sentence(tmp_path):
    r = behind(tmp_path, took=0.25)
    for _ in range(4):
        r.s.produce_next()
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(0, False), (1, False), (2, False), (3, False)]

@pytest.mark.parametrize("lead_min, holds_again", [(6.0, True), (30.0, False)])
def test_catching_up_ends_once_a_clip_is_ready_lead_min_ahead(tmp_path, lead_min, holds_again):
    r = behind(tmp_path, took=0.25, lead_min=lead_min)
    for _ in range(3):
        r.s.produce_next()                    # the third, 16..19, is ready 6.25 s ahead of 9.75
    r.sent.clear()
    r.advance(9.75); r.state(19.5)            # the playhead reaches 19..22, which is not made
    assert (r.s.state == "holding") == holds_again
    assert (Control(action="pause", value=None) in r.sent) == holds_again

def test_a_seek_back_brings_the_sentences_that_were_jumped_over_back(tmp_path):
    r = behind(tmp_path, took=4.0)
    r.s.produce_next(); r.s.produce_next()    # 10..13 came too late, 13..19 was jumped over, 19..22 is placed
    r.voice.took = 0.0
    r.s.handle(Seek(current_time=13.5))       # back into what was given up
    assert r.s.state == "holding"
    r.s.produce_next()
    assert r.voice.calls[-1] == TEXT(1) and r.of(Subtitle)[-1].text == TEXT(1)


# An engine that is down (design of 2026-11-14, 4.6). On 2026-11-02 the PC had left dubbing
# mode: 23 sentences in a row failed at the voice within seconds, each after a translation
# that was thrown away. After two failures in a row the producer waits between tries.
class DownVoice(SecondsVoice):
    """Fails while `down` is set, as a voice service that is not running does."""
    def __init__(self):
        super().__init__()
        self.down = True
    def synthesize(self, text, speed=1.0):
        if self.down:
            self.calls.append(text)
            raise VoiceError("the PC voice service answered HTTP 502")
        return super().synthesize(text, speed)

def down_rig(tmp_path):
    voice = DownVoice()
    r = Rig(tmp_path, voice=voice)
    r.start(json3=DENSE, at=9.0)
    assert r.s.produce_next() is True and r.s.produce_next() is True   # two sentences fail at once
    assert len(voice.calls) == 2
    return r

def test_after_two_failures_in_a_row_the_producer_waits_5_s_before_each_try(tmp_path):
    r = down_rig(tmp_path)
    assert r.s.produce_next() is False and len(r.voice.calls) == 2      # now it waits
    r.advance(4.9)
    assert r.s.produce_next() is False
    r.advance(0.2)
    assert r.s.produce_next() is True and len(r.voice.calls) == 3       # one try after 5 s
    assert r.s.produce_next() is False                                  # and it waits again
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [(0, True), (1, True), (2, True)]

def test_while_it_waits_the_status_says_why(tmp_path):
    r = down_rig(tmp_path)
    r.sent.clear()
    r.s.handle(Hello("test", "0"))            # any status report, as the one sent every second
    assert r.of(Status)[-1].message == (
        "an engine is down, trying again every 5 s: not voiced (VoiceError: the PC voice service answered HTTP 502)"
    )

def test_a_success_ends_the_waiting(tmp_path):
    r = down_rig(tmp_path)
    r.advance(5.0)
    r.voice.down = False
    assert r.s.produce_next() is True and r.s.produce_next() is True    # no wait after a success
    assert [rec.dropped for rec in r.metrics.records] == [True, True, False, False]
    r.sent.clear()
    r.s.handle(Hello("test", "0"))
    assert "down" not in r.of(Status)[-1].message

def test_one_failure_between_successes_does_not_make_it_wait(tmp_path):
    voice = DownVoice()
    r = Rig(tmp_path, voice=voice)
    r.start(json3=DENSE, at=9.0)
    assert r.s.produce_next() is True         # fails
    voice.down = False
    assert r.s.produce_next() is True         # succeeds: the count starts over
    voice.down = True
    assert r.s.produce_next() is True and r.s.produce_next() is True    # the next failure is the first again
    assert r.s.produce_next() is False        # and only the second in a row makes it wait

def test_a_new_start_ends_the_waiting(tmp_path):
    r = down_rig(tmp_path)
    assert r.s.produce_next() is False
    r.start(json3=DENSE, at=9.0)              # the owner stops and starts again
    assert r.s.produce_next() is True and len(r.voice.calls) == 3


# Pause and seek (Phase 1 findings R43, R44 and R46, parked on 2026-11-01 and brought in on
# 2026-11-14). The tests of the parked patch are further up; these cover what it left open.
def test_a_scrub_while_paused_to_where_the_dub_is_ready_holds_nothing_and_stays_paused(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(9.0)                         # all of 10..52 is produced
    r.state(9.0, paused=True)                 # the owner pauses
    r.sent.clear()
    r.state(35.0, paused=True)                # and drags the playhead to 35.0
    assert r.s.state == "paused" and r.of(Control) == []
    assert r.audio.media_now(r.t[0]) == 35.0 and r.s.produced_count == 5

def test_a_second_seek_during_a_hold_that_began_paused_still_ends_paused(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)                # the owner pauses
    r.s.handle(Seek(current_time=5.0))        # outside the produced range: a hold
    r.s.handle(Seek(current_time=15.0))       # and again, while holding
    assert r.s.state == "holding"
    r.produce_all(); r.sent.clear()
    r.state(15.0, paused=True)
    assert r.s.state == "paused" and Control(action="play", value=None) not in r.sent

def test_a_new_start_forgets_the_owners_pause_and_the_old_clock(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)
    r.s.handle(Seek(current_time=5.0))        # a hold that began paused
    r.s.handle(Stop())
    r.s.handle(Start(mode=None))
    r.state(300.0, paused=True)               # far from the old clock: the first report, not a jump
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=track((301, 303), (310, 312))))
    assert r.s.state == "preparing"
    r.produce_all(); r.sent.clear()
    r.state(300.0, paused=True)
    assert r.s.state == "playing" and Control(action="play", value=None) in r.sent

# R46: a seek inside the produced range resets the timeline, which then no longer knows that
# a clip pushed late by a backlog is still playing.
def test_after_a_seek_inside_the_produced_range_a_clip_waits_for_the_one_before_it(tmp_path):
    r = Rig(tmp_path, voice=WordsVoice())     # six words, 6 s: each clip runs at 1.2 and still 5 s long
    r.start(track((10, 12), (13, 15), (16, 18), (19, 21)), at=9.0)
    for _ in range(3):
        r.s.produce_next()                    # 10..15, then 15..20 and 20..25: a backlog
    assert [rec.scheduled_start for rec in r.metrics.records] == [10.0, 15.0, 20.0]
    r.state(9.0)
    r.s.handle(Seek(current_time=13.5))       # inside what is produced: nothing is made again
    assert r.s.state == "playing" and r.s.produced_count == 3
    r.s.produce_next()                        # 19..21 must not start under the clip that ends at 25
    assert r.metrics.records[-1].scheduled_start == 25.0


# The final review's findings (2026-11-14), each with the test that showed it.

# 1. A hold that began while the owner had paused sent a pause of its own. The extension takes
# a pause from the session as "the session holds the player" until a play follows, and none
# follows when the hold ends paused: a later Stop, or a server going away, then played the video.
def held_by_the_session(sent):
    """What the extension's PlayerControl keeps of these controls: whether the session holds the player."""
    held = False
    for msg in sent:
        if isinstance(msg, Control) and msg.action in ("pause", "play"):
            held = msg.action == "pause"
    return held

def test_a_hold_that_began_while_the_owner_had_paused_sends_no_pause_of_its_own(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.state(25.0, paused=True)                # the owner pauses
    r.sent.clear()
    r.state(5.0, paused=True)                 # and scrubs to where nothing is made: a hold
    assert r.s.state == "holding" and r.of(Control) == []
    r.produce_all()
    r.state(5.0, paused=True)                 # ready: it ends paused, as the owner left it
    assert r.s.state == "paused" and r.of(Control) == []
    assert not held_by_the_session(r.sent)    # so nothing is left for a Stop to undo

def test_a_hold_that_began_playing_still_pauses_and_plays(tmp_path):
    r = Rig(tmp_path)
    r.playing_at(25.0)
    r.sent.clear()
    r.s.handle(Seek(current_time=5.0))
    assert r.of(Control) == [Control(action="pause", value=None)] and held_by_the_session(r.sent)
    r.produce_all()
    r.state(5.0, paused=True)
    assert r.s.state == "playing" and not held_by_the_session(r.sent)


# 2. A seek back to a sentence that is made, with sentences ahead that the catch-up gave up:
# the seek makes those wanted again, and they were made out of order, after clips that lie
# behind them on the timeline.
def no_two_clips_at_once(audio):
    clips = sorted(audio._clips, key=lambda clip: clip.start)
    return all(b.start >= a.start + len(a.samples) / 48000 - 1e-6 for a, b in zip(clips, clips[1:]))

def with_a_hole(tmp_path, voice):
    """A rig on DENSE that caught up at 4 s a clip: 10..25 and 28..34 are made, 25..28 was given up; the playhead is at 29.5."""
    r = Rig(tmp_path, voice=voice)
    voice.clock = r.t
    r.start(json3=DENSE, at=9.0)
    r.advance(20.5); r.state(9.0, paused=True); r.state(9.0)   # the hold ran out: playing, catching up
    voice.took = 0.25
    r.s.produce_next(); r.s.produce_next()    # 10..13 and 13..16
    voice.took = 4.0
    for _ in range(5):
        r.s.produce_next()                    # 16..25 on time, then 25..28 cannot be: 28..34
    assert [(rec.idx, rec.dropped) for rec in r.metrics.records] == [
        (0, False), (1, False), (2, False), (3, False), (4, False), (5, True), (6, False), (7, False)]
    voice.took = 0.0
    return r

def test_a_seek_back_with_a_given_up_sentence_ahead_makes_the_rest_again_in_order(tmp_path):
    voice = SlowVoice(0.25)
    voice.seconds[5] = 5.0                    # the sentence that was given up has a long clip
    r = with_a_hole(tmp_path, voice)
    r.s.handle(Seek(current_time=10.5))       # back to hear it again: 10..13 is made
    assert r.s.state == "playing"
    for _ in range(4):
        r.s.produce_next()
    assert no_two_clips_at_once(r.audio)      # the long clip of 25..28 does not play over the next one
    assert voice.calls.count(TEXT(5)) == 1 and voice.calls.count(TEXT(6)) == 2   # what lay after the hole is made again
    after = [rec for rec in r.metrics.records[8:] if not rec.dropped]
    assert [rec.idx for rec in after] == [5, 6, 7, 8]
    assert [rec.scheduled_start for rec in after] == sorted(rec.scheduled_start for rec in after)

class SeekingSlowVoice(SlowVoice):
    """A SlowVoice that hands the session a message while it makes the sentence `when`."""
    def __init__(self, took):
        super().__init__(took)
        self.when, self.msg, self.session = None, None, None
    def synthesize(self, text, speed=1.0):
        if self.msg is not None and f"number {self.when} " in text:
            msg, self.msg = self.msg, None
            self.session.handle(msg)
        return super().synthesize(text, speed)

def test_a_clip_in_the_making_during_such_a_seek_is_not_placed_ahead_of_the_rest(tmp_path):
    voice = SeekingSlowVoice(0.25)
    r = with_a_hole(tmp_path, voice)
    voice.session, voice.when, voice.msg = r.s, 8, Seek(current_time=10.5)
    voice.took = 4.0
    r.s.produce_next()                        # 34..37 is in the making when the owner seeks back
    voice.took = 0.0
    for _ in range(4):
        r.s.produce_next()
    after = [rec for rec in r.metrics.records[8:] if not rec.dropped]
    assert [rec.idx for rec in after] == [5, 6, 7, 8]            # in order, from the hole
    assert all(rec.lag <= 0.5 for rec in after)                  # each at its own sentence
    assert no_two_clips_at_once(r.audio)


# 4. The aim trusted the last make alone: one reply 25 s slow sent it 27 s ahead.
def test_one_slow_make_does_not_send_the_aim_far_ahead(tmp_path):
    r = behind(tmp_path, took=2.0)
    for _ in range(3):
        r.s.produce_next()                    # three clips of 2 s each
    r.voice.took = 25.0
    r.s.produce_next()                        # one reply is 25 s slow, and its clip comes too late
    r.voice.took = 2.0
    playhead = r.s._playhead()
    r.s.produce_next()
    aimed_at = 10 + 3 * int(r.voice.calls[-1].split()[4])
    assert aimed_at - playhead <= 6.0         # a clip's usual time ahead, not 27 s

def test_two_slow_makes_in_a_row_are_believed(tmp_path):
    r = behind(tmp_path, took=2.0)
    for _ in range(3):
        r.s.produce_next()
    r.voice.took = 25.0
    r.s.produce_next(); r.s.produce_next()    # two in a row: the engines have become slow
    playhead = r.s._playhead()
    r.s.produce_next()
    aimed_at = 10 + 3 * int(r.voice.calls[-1].split()[4])
    assert aimed_at - playhead >= 20.0

def test_when_no_sentence_begins_late_enough_the_aim_takes_the_one_at_the_playhead(tmp_path):
    voice = SlowVoice(4.0)
    r = Rig(tmp_path, voice=voice)
    voice.clock = r.t
    r.start(json3=track((10, 13), (13, 16), (16, 19)), at=9.0)
    r.advance(20.5); r.state(9.0, paused=True); r.state(9.0)
    r.s.produce_next()                        # 10..13 takes 4 s and comes too late
    r.s.produce_next()                        # a clip takes 4 s, and nothing begins 3.9 s from 13.0
    assert voice.calls == [TEXT(0), TEXT(1)]  # so it makes the next one all the same


# 5. A hold ended, and began, only when the player reported. Chrome lets a hidden tab report
# once a second, and a hidden, silent one once a minute: the producer now looks itself.
def test_a_hold_ends_when_enough_is_ready_without_a_report(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.produce_all()
    assert r.s.state == "preparing"           # making a clip does not by itself play the video
    r.sent.clear()
    r.s.tick()
    assert r.s.state == "playing" and Control(action="play", value=None) in r.sent

def test_a_hold_runs_out_at_its_cap_without_a_report(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.advance(20.0); r.s.tick()
    assert r.s.state == "preparing"
    r.advance(0.5); r.s.tick()
    assert r.s.state == "playing" and r.of(Status)[-1].message == "hold exceeded"

def test_the_playhead_running_into_a_sentence_that_is_not_ready_holds_without_a_report(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    r.s.produce_next()                        # 10..12 only
    r.state(9.0)                              # playing from 9.0
    r.sent.clear()
    r.advance(11.5)                           # the playhead is at 20.5, inside 20..22
    r.s.tick()
    assert r.s.state == "holding" and Control(action="pause", value=None) in r.sent

def test_nothing_is_done_before_the_first_report_or_while_idle(tmp_path):
    r = Rig(tmp_path)
    r.s.tick()
    assert r.sent == []
    r.s.handle(Start(mode=None))
    r.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    r.produce_all(); r.sent.clear()
    r.advance(30.0); r.s.tick()               # where the player is is not known yet
    assert r.s.state == "preparing" and r.sent == []

def test_the_producer_thread_ends_a_hold_by_itself(tmp_path):
    r = Rig(tmp_path)
    r.start(at=9.0)
    stop = threading.Event()
    thread = threading.Thread(target=r.s.run_producer, args=(stop,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while r.s.state != "playing" and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set(); thread.join(timeout=5.0)
    assert r.s.state == "playing" and Control(action="play", value=None) in r.sent


# A voice that chooses by the video is told which video is on.

class Choosing(SecondsVoice):
    """A voice with `prepare`: it keeps the videos it was told."""
    def __init__(self):
        super().__init__()
        self.prepared = []
    def prepare(self, video_id):
        self.prepared.append(video_id)


def test_the_voice_is_told_the_video_by_the_first_report_after_a_start_and_only_once(tmp_path):
    rig = Rig(tmp_path, voice=Choosing())
    rig.s.handle(Start(mode=None))
    rig.state(0.0, paused=True)
    assert rig.voice.prepared == ["v"]
    rig.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    rig.state(0.0, paused=True)
    rig.produce_all()
    rig.state(0.0)
    assert rig.voice.prepared == ["v"]


def test_the_voice_is_told_the_video_by_the_captions_when_no_report_came_first(tmp_path):
    rig = Rig(tmp_path, voice=Choosing())
    rig.start()
    assert rig.voice.prepared == ["v"]


def test_captions_for_another_video_than_the_first_report_named_tell_the_voice_again(tmp_path):
    rig = Rig(tmp_path, voice=Choosing())
    rig.s.handle(Start(mode=None))
    rig.state(0.0, paused=True, video="w")
    rig.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=FIVE))
    assert rig.voice.prepared == ["w", "v"]


def test_a_new_start_tells_the_voice_the_video_again(tmp_path):
    rig = Rig(tmp_path, voice=Choosing())
    rig.start(at=0.0)
    rig.s.handle(Stop())
    rig.start(at=0.0)
    assert rig.voice.prepared == ["v", "v"]


def test_a_report_while_idle_tells_the_voice_nothing(tmp_path):
    rig = Rig(tmp_path, voice=Choosing())
    rig.state(0.0)
    assert rig.voice.prepared == []


def test_a_voice_that_cannot_prepare_does_not_stop_the_session(tmp_path, caplog):
    class Broken(Choosing):
        def prepare(self, video_id):
            raise RuntimeError("no thread left")

    rig = Rig(tmp_path, voice=Broken())
    with caplog.at_level(logging.ERROR, logger="dubber.session"):
        rig.start(at=0.0)
    assert "no thread left" in caplog.text
    assert rig.produce_all() > 0 and rig.s.state != "idle"


def test_the_status_and_the_metrics_name_the_voice_in_use(tmp_path):
    rig = Rig(tmp_path, voice=Choosing())
    rig.voice.name = "pc-voxcpm2:kalin"
    rig.start(at=0.0)
    assert rig.of(Status)[-1].engine == "identity+pc-voxcpm2:kalin"
    rig.voice.name = "pc-voxcpm2:ipek"  # the look came back: a woman presents the video
    rig.produce_all()
    rig.state(0.0)
    assert rig.of(Status)[-1].engine == "identity+pc-voxcpm2:ipek"
    assert {rec.tts_engine for rec in rig.metrics.records} == {"pc-voxcpm2:ipek"}


def test_a_video_started_again_after_a_look_that_failed_is_read_in_the_voice_the_new_look_finds(tmp_path):
    """The real voice in a session: the button off and on must be able to put a wrong voice right."""
    from dubber.engines.voice_by_presenter import PresenterVoice
    man, woman = SecondsVoice(), SecondsVoice()
    man.name, woman.name = "the man", "the woman"
    answers = iter([None, "woman"])  # the first look fails: the PC's model could not be asked
    rig = Rig(tmp_path, voice=PresenterVoice(man, woman, lambda video_id: next(answers)))
    rig.start(at=0.0)
    assert rig.produce_all() > 0
    assert {rec.tts_engine for rec in rig.metrics.records} == {"the man"}
    rig.s.handle(Stop())
    rig.metrics.records.clear()
    rig.start(at=0.0)
    assert rig.produce_all() > 0
    assert {rec.tts_engine for rec in rig.metrics.records} == {"the woman"}
