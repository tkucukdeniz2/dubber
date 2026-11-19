import numpy as np
from dubber.engines.base import Clip
from dubber.playback import AudioOut

def tone(seconds, sr=22050):
    return Clip(samples=np.ones(int(sr * seconds), dtype=np.float32) * 0.5, sample_rate=sr)

def test_clock_advances_only_while_playing():
    out = AudioOut(stream_factory=lambda cb: None)
    out.set_clock(10.0, playing=True, rate=1.0, wall_time=100.0)
    assert out.media_now(wall_time=101.5) == 11.5
    out.set_clock(11.5, playing=False, rate=1.0, wall_time=101.5)
    assert out.media_now(wall_time=105.0) == 11.5

def test_schedule_resamples_speed_and_renders_at_media_time():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(tone(1.0), at_media_time=20.0, speed=1.25)
    silent = out.render(4800, media_now=19.5)      # 100 ms before the clip
    assert np.all(silent == 0)
    block = out.render(4800, media_now=20.05)      # inside the clip, after the fade-in
    assert block.max() > 0.4
    assert out.played_until() == 20.8              # 1.0 s / 1.25

def test_crossfade_applied_at_edges():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(tone(0.5), at_media_time=0.0)
    head = out.render(96, media_now=0.0)            # first 2 ms of a 20 ms fade
    assert head[0] == 0 and head[-1] < 0.1

def test_cancel_after_drops_future_clips():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(tone(1.0), at_media_time=5.0)
    out.schedule(tone(1.0), at_media_time=30.0)
    assert out.cancel_after(10.0) == 1
    assert out.played_until() == 6.0

# What the four tests above leave open: the stream callback, the stream's life cycle,
# the mix, and the edges of scheduling. No test opens an audio device: the stream is a
# stand-in and the clock is set by hand.
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from dubber import playback

REPO = Path(__file__).resolve().parent.parent
BLOCK = 480      # frames in 10 ms at 48 kHz: the size of block the callback is given here

def level_clip(level, seconds, sr=48000):
    """A constant clip already at the mixer's rate: scheduling it adds only the fades."""
    return Clip(samples=np.full(int(sr * seconds), level, dtype=np.float32), sample_rate=sr)

def ramp_clip(seconds=1.0, sr=48000):
    """Samples rising from 0 to 0.9, so that no two windows of the clip are equal."""
    return Clip(samples=np.linspace(0.0, 0.9, int(sr * seconds), dtype=np.float32), sample_rate=sr)

class FakeTime:
    """Stands in for the `time` module inside dubber.playback; `now` moves when told."""

    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now

@pytest.fixture
def fake_time(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(playback, "time", fake)
    return fake

class FakeStream:
    """Stands in for sounddevice.OutputStream: remembers what AudioOut did to it."""

    def __init__(self, callback, start_error=None):
        self.callback = callback
        self.start_error = start_error
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def stop(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")

class StreamFactory:
    """A stream factory that keeps the streams it made."""

    def __init__(self, start_error=None):
        self.made = []
        self.start_error = start_error

    def __call__(self, callback):
        self.made.append(FakeStream(callback, self.start_error))
        return self.made[-1]

def started_mixer():
    """An AudioOut running on a fake stream: returns it and the stream."""
    factory = StreamFactory()
    out = AudioOut(stream_factory=factory)
    out.start()
    return out, factory.made[0]

def pull(stream, frames=BLOCK, status=None, fill=0.0, time_info=None):
    """Call the stream's callback as PortAudio does and return the stereo block it filled.

    PortAudio does not clear the buffer it hands over, so tests of silence pass a `fill`.
    `time_info` is its `time` argument, which says when the block will be heard.
    """
    outdata = np.zeros((frames, 2), dtype=np.float32)
    outdata[:] = fill
    stream.callback(outdata, frames, time_info, status)
    return outdata

def dac_info(delay, current=8.0):
    """PortAudio's `time` argument for a block that will be heard `delay` seconds from now."""
    return types.SimpleNamespace(currentTime=current, outputBufferDacTime=current + delay)

# The stream callback

def test_callback_writes_silence_while_the_clock_is_paused(fake_time):
    out, stream = started_mixer()
    out.schedule(level_clip(0.5, 1.0), at_media_time=0.0)
    assert not pull(stream, fill=9.0).any()             # no clock report yet: paused
    out.set_clock(0.5, playing=False, rate=1.0, wall_time=fake_time.now)
    assert not pull(stream, fill=9.0).any()
    assert out.render(BLOCK, media_now=0.5).any()       # the clip is there to be heard

def test_callback_plays_the_mix_on_both_channels(fake_time):
    out, stream = started_mixer()
    out.schedule(level_clip(0.5, 1.0), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    block = pull(stream, fill=9.0)
    assert block.shape == (BLOCK, 2)
    assert np.array_equal(block[:, 0], block[:, 1])
    assert np.array_equal(block[:, 0], out.render(BLOCK, media_now=0.5))
    assert block.max() == 0.5

def test_callback_advances_block_by_block_without_rendering_one_window_twice(fake_time):
    out, stream = started_mixer()
    out.schedule(ramp_clip(), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    blocks = [pull(stream)[:, 0] for _ in range(3)]      # the clock stands still, the audio does not
    assert np.array_equal(np.concatenate(blocks), out.render(3 * BLOCK, media_now=0.5))
    assert not np.array_equal(blocks[0], blocks[1])

@pytest.mark.parametrize("report, follows", [
    (0.47, False), (0.55, False),       # 40 ms from the callback's place: re-anchoring jitter
    (0.45, True), (0.57, True),         # 60 ms: a real difference
    (0.80, True),                       # a seek
])
def test_callback_follows_the_clock_only_when_it_is_more_than_50_ms_away(fake_time, report, follows):
    out, stream = started_mixer()
    out.schedule(ramp_clip(), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    pull(stream)                                         # the callback is now at 0.51
    out.set_clock(report, playing=True, rate=1.0, wall_time=fake_time.now)
    expected = out.render(BLOCK, media_now=report if follows else 0.51)
    assert np.array_equal(pull(stream)[:, 0], expected)

def test_callback_starts_again_from_the_clock_when_playback_resumes(fake_time):
    out, stream = started_mixer()
    out.schedule(ramp_clip(), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    pull(stream)
    pull(stream)                                         # the callback is now at 0.52
    out.set_clock(0.5, playing=False, rate=1.0, wall_time=fake_time.now)
    assert not pull(stream, fill=9.0).any()
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    assert np.array_equal(pull(stream)[:, 0], out.render(BLOCK, media_now=0.5))

def test_callback_renders_the_window_that_is_heard_when_the_block_reaches_the_speaker(fake_time):
    out, stream = started_mixer()
    out.schedule(ramp_clip(), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    block = pull(stream, time_info=dac_info(0.1))        # the speaker plays it 0.1 s from now
    assert np.array_equal(block[:, 0], out.render(BLOCK, media_now=0.6))

@pytest.mark.parametrize("time_info, heard_at", [
    (None, 0.5),                                         # no timestamps at all
    (types.SimpleNamespace(), 0.5),                      # a time argument without the fields
    (types.SimpleNamespace(currentTime=8.0), 0.5),       # only one of the two fields
    (types.SimpleNamespace(outputBufferDacTime=8.1), 0.5),
    (dac_info(-0.1), 0.5),                               # a DAC time in the past is not a latency
    (dac_info(0.6), 0.5),                                # nor is one more than half a second ahead
    (dac_info(0.0), 0.5),
    (dac_info(0.5), 1.0),                                # half a second is still a latency
])
def test_callback_takes_the_output_delay_only_when_it_is_from_0_to_half_a_second(fake_time, time_info, heard_at):
    out, stream = started_mixer()
    out.schedule(ramp_clip(2.0), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    assert np.array_equal(pull(stream, time_info=time_info)[:, 0], out.render(BLOCK, media_now=heard_at))

def test_callback_does_not_snap_from_block_to_block_while_the_output_delay_is_steady(fake_time):
    out, stream = started_mixer()
    out.schedule(ramp_clip(), at_media_time=0.0)
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    blocks = []
    for gap in (0.009, 0.011, 0.009, 0.011):             # the device calls back a little early or late
        blocks.append(pull(stream, time_info=dac_info(0.1))[:, 0])
        fake_time.now += gap
    # The cursor sits 0.1 s ahead of the raw clock, more than the 50 ms it tolerates, so
    # the four blocks join up only if the delay counts in the drift check as well as in
    # the snap target.
    assert np.array_equal(np.concatenate(blocks), out.render(4 * BLOCK, media_now=0.6))

def test_callback_never_raises_and_logs_each_kind_of_error_once(fake_time, monkeypatch, caplog):
    out, stream = started_mixer()
    out.set_clock(0.5, playing=True, rate=1.0, wall_time=fake_time.now)
    errors = iter([RuntimeError("boom"), RuntimeError("boom again"), ValueError("other")])

    def failing_render(frames, media_now):
        raise next(errors)

    monkeypatch.setattr(out, "render", failing_render)
    with caplog.at_level(logging.DEBUG, logger="dubber.playback"):
        for _ in range(3):
            assert not pull(stream, fill=9.0).any()      # silence, and nothing escaped
    problems = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(problems) == 2
    assert "RuntimeError" in problems[0] and "ValueError" in problems[1]

def test_callback_logs_stream_status_at_debug_level_and_does_not_print(caplog, capsys):
    _, stream = started_mixer()
    with caplog.at_level(logging.DEBUG, logger="dubber.playback"):
        pull(stream, status="output underflow")
    records = [r for r in caplog.records if r.name == "dubber.playback"]
    assert [r.levelno for r in records] == [logging.DEBUG]
    assert "output underflow" in records[0].getMessage()
    assert capsys.readouterr().out == ""

# The stream's life cycle

def test_start_and_stop_are_idempotent():
    factory = StreamFactory()
    out = AudioOut(stream_factory=factory)
    out.stop()                                          # nothing to stop yet
    assert factory.made == []                           # and no stream is made before start()
    out.start()
    out.start()
    assert len(factory.made) == 1 and factory.made[0].calls == ["start"]
    out.stop()
    out.stop()
    assert factory.made[0].calls == ["start", "stop", "close"]
    out.start()                                         # a closed stream cannot run again: a new one
    assert [s.calls for s in factory.made] == [["start", "stop", "close"], ["start"]]

def test_start_and_stop_do_nothing_when_the_factory_makes_no_stream():
    out = AudioOut(stream_factory=lambda cb: None)
    out.start()
    out.start()
    out.stop()
    out.stop()

def test_a_stream_that_fails_to_start_is_closed_and_the_next_start_tries_again():
    factory = StreamFactory(start_error=RuntimeError("no output device"))
    out = AudioOut(stream_factory=factory)
    with pytest.raises(RuntimeError, match="no output device"):
        out.start()
    assert factory.made[0].calls == ["start", "close"]
    factory.start_error = None
    out.start()
    assert [s.calls for s in factory.made] == [["start", "close"], ["start"]]

def test_default_stream_is_stereo_float32_in_1024_frame_blocks_at_high_latency(monkeypatch):
    opened = []

    class FakeOutputStream(FakeStream):
        def __init__(self, **kwargs):
            opened.append(kwargs)
            super().__init__(kwargs["callback"])

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=FakeOutputStream))
    out = AudioOut()
    out.start()
    out.stop()
    assert len(opened) == 1
    options = {**opened[0]}
    assert callable(options.pop("callback"))
    assert options == {
        "samplerate": 48000, "channels": 2, "dtype": "float32", "latency": "high", "blocksize": 1024,
    }

def test_importing_and_creating_the_mixer_does_not_load_sounddevice():
    code = (
        "import sys\n"
        "from dubber.playback import AudioOut\n"
        "AudioOut()\n"
        "assert 'sounddevice' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr

# The clock

def test_clock_follows_the_rate_and_defaults_to_the_monotonic_clock(fake_time):
    out = AudioOut(stream_factory=lambda cb: None)
    assert out.media_now() == 0.0                       # no report yet: paused at the start
    out.set_clock(10.0, playing=True, rate=2.0)         # wall time is fake_time.now, 100.0
    fake_time.now = 101.5
    assert out.media_now() == 13.0
    out.set_clock(13.0, playing=False, rate=2.0)
    fake_time.now = 200.0
    assert out.media_now() == 13.0

# Scheduling and the mix

def test_resampling_keeps_the_pitch_and_speed_shortens_the_time_axis():
    sr, freq = 22050, 440.0
    wave = 0.5 * np.sin(2 * np.pi * freq * np.arange(sr) / sr)       # one second at 22.05 kHz
    clip = Clip(samples=wave.astype(np.float32), sample_rate=sr)
    for speed in (1.0, 1.25):
        out = AudioOut(stream_factory=lambda cb: None)
        out.schedule(clip, at_media_time=0.0, speed=speed)
        block = out.render(24000, media_now=0.1)         # 0.1 s to 0.6 s, clear of both fades
        t = 0.1 + np.arange(24000) / 48000
        expected = 0.5 * np.sin(2 * np.pi * freq * speed * t)
        assert np.abs(block - expected).max() < 0.01

def test_clip_starts_on_the_sample_that_matches_its_media_time():
    out = AudioOut(crossfade_ms=0, stream_factory=lambda cb: None)
    out.schedule(level_clip(0.5, 0.1), at_media_time=2.0)
    block = out.render(1000, media_now=1.99)            # the clip begins 10 ms in: sample 480
    assert np.all(block[:480] == 0) and np.all(block[480:] == 0.5)

def test_fades_are_linear_and_20_ms_long():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(level_clip(1.0, 0.5), at_media_time=1.0)            # 1.0 s to 1.5 s
    ramp = np.arange(960) / 960
    head = out.render(1920, media_now=1.0)
    assert np.allclose(head[:960], ramp, atol=1e-6) and np.all(head[960:] == 1.0)
    tail = out.render(1920, media_now=1.46)                          # the last 40 ms
    assert np.all(tail[:960] == 1.0) and np.allclose(tail[960:], ramp[::-1], atol=1e-6)
    assert tail[-1] == 0

def test_a_clip_shorter_than_two_fades_gets_shorter_fades():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(level_clip(1.0, 0.01), at_media_time=0.0)           # 480 samples: two 20 ms fades do not fit
    block = out.render(BLOCK, media_now=0.0)
    assert block[0] == 0 and block[-1] == 0
    assert np.array_equal(block, block[::-1])           # the same fade at both ends
    assert block.max() > 0.99

def test_clips_scheduled_out_of_order_are_heard_at_their_own_times():
    out = AudioOut(stream_factory=lambda cb: None)
    for at, level in ((3.0, 0.3), (1.0, 0.1), (2.0, 0.2)):
        out.schedule(level_clip(level, 0.5), at_media_time=at)
    for at, level in ((1.0, 0.1), (2.0, 0.2), (3.0, 0.3)):
        assert np.allclose(out.render(BLOCK, media_now=at + 0.1), level)
        assert not out.render(BLOCK, media_now=at + 0.6).any()

def test_a_long_clip_is_still_heard_after_shorter_clips_that_started_later_have_ended():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(level_clip(0.2, 5.0), at_media_time=0.0)
    for at in (1.0, 1.5, 2.0):
        out.schedule(level_clip(0.1, 0.1), at_media_time=at)
    assert np.allclose(out.render(BLOCK, media_now=4.0), 0.2)

def test_overlapping_clips_are_summed():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(level_clip(0.3, 1.0), at_media_time=0.0)
    out.schedule(level_clip(0.2, 1.0), at_media_time=0.5)
    assert np.allclose(out.render(BLOCK, media_now=0.3), 0.3)        # the first alone
    assert np.allclose(out.render(BLOCK, media_now=0.7), 0.5)        # both

def test_the_mix_is_clipped_to_the_range_minus_one_to_one():
    out = AudioOut(stream_factory=lambda cb: None)
    for _ in range(2):
        out.schedule(level_clip(0.8, 1.0), at_media_time=0.0)
        out.schedule(level_clip(-0.8, 1.0), at_media_time=2.0)
    loud = out.render(BLOCK, media_now=0.5)
    assert loud.dtype == np.float32 and np.all(loud == 1.0)
    assert np.all(out.render(BLOCK, media_now=2.5) == -1.0)

def test_a_clip_is_rendered_again_when_the_window_moves_back():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(ramp_clip(), at_media_time=0.0)
    first = out.render(BLOCK, media_now=0.5)
    out.render(BLOCK, media_now=0.9)                    # play on past it, then seek back
    assert first.any() and np.array_equal(out.render(BLOCK, media_now=0.5), first)

def test_cancel_after_drops_clips_starting_at_or_after_the_time_and_keeps_the_rest():
    out = AudioOut(stream_factory=lambda cb: None)
    for at in (5.0, 9.5, 10.0, 12.0):
        out.schedule(tone(1.0), at_media_time=at)
    assert out.cancel_after(10.0) == 2                  # 10.0 and 12.0
    assert out.played_until() == 10.5                   # the clip from 9.5 is still playing at 10.0
    assert out.cancel_after(10.0) == 0

def test_flush_drops_every_clip():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(tone(1.0), at_media_time=0.0)
    out.schedule(tone(1.0), at_media_time=5.0)
    out.flush()
    assert out.played_until() == 0.0
    assert not out.render(4800, media_now=0.0).any()

def test_played_until_is_zero_when_nothing_is_scheduled():
    out = AudioOut(stream_factory=lambda cb: None)
    assert out.played_until() == 0.0
    out.schedule(tone(1.0), at_media_time=5.0)
    out.cancel_after(0.0)
    assert out.played_until() == 0.0

def test_played_until_is_the_latest_end_not_the_end_of_the_latest_start():
    out = AudioOut(stream_factory=lambda cb: None)
    out.schedule(tone(3.0), at_media_time=5.0)
    out.schedule(tone(0.5), at_media_time=6.0)
    assert out.played_until() == 8.0

def test_clip_ids_count_up_from_one_and_are_not_reused():
    out = AudioOut(stream_factory=lambda cb: None)
    assert [out.schedule(tone(0.1), at_media_time=at) for at in (3.0, 1.0, 2.0)] == [1, 2, 3]
    out.flush()
    assert out.schedule(tone(0.1), at_media_time=0.0) == 4

@pytest.mark.parametrize("clip, speed", [
    (Clip(samples=np.zeros(0, dtype=np.float32), sample_rate=22050), 1.0),
    (tone(1.0), 0.0),
    (tone(1.0), -1.25),
])
def test_schedule_rejects_an_empty_clip_and_a_speed_that_is_not_positive(clip, speed):
    out = AudioOut(stream_factory=lambda cb: None)
    with pytest.raises(ValueError):
        out.schedule(clip, at_media_time=0.0, speed=speed)
    assert out.played_until() == 0.0


# The run log names the device the dub is played on: the stream stays on the device it was
# opened on, also when the Mac's output moves to headphones later.
def test_the_mixer_names_the_device_it_opens(monkeypatch, caplog):
    fake = types.SimpleNamespace(
        OutputStream=lambda **kwargs: FakeStream(kwargs["callback"]),
        query_devices=lambda kind=None: {"name": "MacBook Pro Hoparlörü", "default_samplerate": 48000.0},
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    out = AudioOut()
    with caplog.at_level(logging.INFO, logger="dubber.playback"):
        out.start()
    out.stop()
    assert [rec.getMessage() for rec in caplog.records] == ["audio output: MacBook Pro Hoparlörü"]

def test_a_device_that_cannot_be_named_still_plays(monkeypatch, caplog):
    def refuse(kind=None):
        raise RuntimeError("no default output device")
    fake = types.SimpleNamespace(OutputStream=lambda **kwargs: FakeStream(kwargs["callback"]), query_devices=refuse)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    out = AudioOut()
    with caplog.at_level(logging.INFO, logger="dubber.playback"):
        out.start()
    assert out._stream.calls == ["start"]
    assert [rec.getMessage() for rec in caplog.records] == ["audio output: unknown device"]
