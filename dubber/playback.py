"""Mix the dubbed clips onto the video's timeline and play them on the default output.

All times are media time: seconds into the video. The browser extension reports the
video's state every 250 ms, and `AudioOut.set_clock` turns each report into a clock that
keeps running between reports. A clip is scheduled at the media time where it must be
heard and is resampled to the mixer's rate once, when it is scheduled. The output stream
asks `render` for its next block, so a clip is heard when the video reaches it.

Three threads meet here. The producer calls `schedule`, the event loop calls
`set_clock`, `cancel_after` and `flush`, and the audio thread calls `render` and reads
the clock. Clips stay on the timeline after they have played, because a seek back must
play them again: only `cancel_after` and `flush` remove them.
"""

import bisect
import itertools
import logging
import threading
import time
from collections.abc import Callable
from operator import attrgetter
from typing import NamedTuple, Protocol

import numpy as np

from dubber.engines.base import Clip

log = logging.getLogger(__name__)

# The audio callback keeps its own place in the video and follows the media clock only
# when the two are further apart than this many seconds. The clock is re-anchored every
# 250 ms, and following its jitter block by block would click.
_RESYNC_AFTER = 0.05

# The longest output delay, in seconds, taken from PortAudio's timestamps. Outside 0 to
# this they are not a latency (a host API may leave them unset), and the delay is 0.
_MAX_OUTPUT_DELAY = 0.5

# Frames per callback. At low latency CoreAudio asks for 64 frames here, 1.3 ms, and a
# Python callback has to take the GIL first: a stretch of pure Python on another thread
# would make it late. The dub is scheduled ahead against a media clock and the cursor
# allows for the output delay, so latency costs nothing, and 1024 frames give the
# callback 21 ms.
_BLOCKSIZE = 1024

StreamCallback = Callable[[np.ndarray, int, object, object], None]


class Stream(Protocol):
    """What AudioOut needs of an output stream; sounddevice.OutputStream provides it."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


# Gets the stream callback and returns the stream, or None for no audio output.
StreamFactory = Callable[[StreamCallback], Stream | None]


class _Clock(NamedTuple):
    """The player's last report: the video was at `media_time` at `wall_time`."""

    media_time: float
    playing: bool
    rate: float
    wall_time: float

    def at(self, wall_time: float) -> float:
        """The media time at `wall_time`; the clock runs only while the video plays."""
        if not self.playing:
            return self.media_time
        return self.media_time + (wall_time - self.wall_time) * self.rate


class _Placed(NamedTuple):
    """A clip on the timeline: `samples` at the mixer's rate, starting at `start`."""

    start: float
    samples: np.ndarray


_START = attrgetter("start")


def _output_delay(time_info: object) -> float:
    """Seconds from this callback to the moment its block is heard; 0 if unknown.

    `time_info` is the callback's `time` argument. Its two timestamps are on the same
    clock, so their difference is the delay.
    """
    current = getattr(time_info, "currentTime", None)
    heard = getattr(time_info, "outputBufferDacTime", None)
    if current is None or heard is None:
        return 0.0
    delay = heard - current
    return delay if 0.0 <= delay <= _MAX_OUTPUT_DELAY else 0.0


class AudioOut:
    """The playback mixer: scheduled clips, a media clock and an output stream.

    `stream_factory` makes the output stream when `start` is first called. By default
    that is a sounddevice stereo stream on the system default output device. A factory
    receives the stream callback and returns the stream, or None for no audio output.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        crossfade_ms: int = 20,
        stream_factory: StreamFactory | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._fade = round(crossfade_ms / 1000 * sample_rate)
        self._stream_factory = (
            self._open_output_stream if stream_factory is None else stream_factory
        )
        self._stream: Stream | None = None
        # `_clips` is sorted by start. `_longest` is an upper bound, in seconds, on the
        # length of any clip in it. Both are guarded by `_lock`.
        self._lock = threading.Lock()
        self._clips: list[_Placed] = []
        self._longest = 0.0
        self._ids = itertools.count(1)
        # The clock is replaced as a whole, never changed, so a reader sees one report.
        self._clock = _Clock(0.0, False, 1.0, 0.0)
        # Used by the audio thread only: where the callback is in the video, and the
        # kinds of error it has logged.
        self._cursor: float | None = None
        self._failures: set[type[Exception]] = set()

    def set_clock(
        self,
        media_time: float,
        playing: bool,
        rate: float,
        wall_time: float | None = None,
    ) -> None:
        """Take a state report: the video was at `media_time` at `wall_time`.

        `wall_time` is a time.monotonic() value and defaults to now.
        """
        if wall_time is None:
            wall_time = time.monotonic()
        self._clock = _Clock(media_time, playing, rate, wall_time)

    def media_now(self, wall_time: float | None = None) -> float:
        """The media time at `wall_time` (default now), from the last report.

        Before any report the clock is paused at 0.
        """
        clock = self._clock
        if wall_time is None:
            wall_time = time.monotonic()
        return clock.at(wall_time)

    def schedule(self, clip: Clip, at_media_time: float, speed: float = 1.0) -> int:
        """Put `clip` on the timeline from `at_media_time`, played `speed` times faster.

        The clip is resampled to the mixer's rate here and given a linear fade-in and
        fade-out of `crossfade_ms` each (shorter for a clip shorter than two fades), so
        that `render` only has to add. A speed above 1 shortens the clip by stepping
        through it faster, which also raises its pitch. Returns the clip's id, counting
        up from 1. Raises ValueError for an empty clip or a speed that is not positive.
        """
        if len(clip.samples) == 0:
            raise ValueError("cannot schedule an empty clip")
        if speed <= 0:
            raise ValueError(f"speed must be positive, not {speed}")
        placed = _Placed(float(at_media_time), self._prepare(clip, speed))
        with self._lock:
            bisect.insort(self._clips, placed, key=_START)
            self._longest = max(self._longest, len(placed.samples) / self._sample_rate)
            return next(self._ids)

    def cancel_after(self, media_time: float) -> int:
        """Drop the clips that start at or after `media_time`; return how many.

        A clip that started earlier stays, even if it is still playing at that time.
        """
        with self._lock:
            first = bisect.bisect_left(self._clips, media_time, key=_START)
            dropped = len(self._clips) - first
            del self._clips[first:]
        return dropped

    def flush(self) -> None:
        """Drop every clip."""
        with self._lock:
            self._clips.clear()
            self._longest = 0.0

    def played_until(self) -> float:
        """The latest end of the scheduled clips, in media time; 0.0 with no clips."""
        with self._lock:
            clips = list(self._clips)
        ends = (c.start + len(c.samples) / self._sample_rate for c in clips)
        return max(ends, default=0.0)

    def render(self, frames: int, media_now: float) -> np.ndarray:
        """Mix `frames` samples of the timeline, starting at media time `media_now`.

        Every clip that overlaps that window is cut to it and added in. The result is
        float32 mono, clipped to [-1, 1].
        """
        rate = self._sample_rate
        with self._lock:
            # No clip is longer than `_longest`, so one that started more than that
            # before the window is over by the time the window begins.
            earliest = media_now - self._longest
            latest = media_now + frames / rate
            first = bisect.bisect_left(self._clips, earliest, key=_START)
            last = bisect.bisect_left(self._clips, latest, key=_START)
            overlapping = self._clips[first:last]
        mix = np.zeros(frames, dtype=np.float32)
        for start, samples in overlapping:
            # Window index of the clip's first sample; negative if it started earlier.
            offset = round((start - media_now) * rate)
            lo, hi = max(offset, 0), min(offset + len(samples), frames)
            if lo < hi:
                mix[lo:hi] += samples[lo - offset : hi - offset]
        return np.clip(mix, -1.0, 1.0, out=mix)

    def start(self) -> None:
        """Make the output stream and start it; does nothing while it is running.

        A stream that cannot be started is closed and the error is raised.
        """
        if self._stream is not None:
            return
        stream = self._stream_factory(self._fill)
        if stream is None:
            return
        self._cursor = None
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def stop(self) -> None:
        """Stop and close the output stream; does nothing when there is none."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def _prepare(self, clip: Clip, speed: float) -> np.ndarray:
        """The clip at the mixer's rate, `speed` times faster, with its fades."""
        n_in = len(clip.samples)
        n_out = round(n_in * self._sample_rate / (clip.sample_rate * speed))
        # Output sample j is the clip at input position j * step. A larger speed takes
        # bigger steps, which shortens the clip and raises its pitch.
        step = clip.sample_rate * speed / self._sample_rate
        positions = np.arange(n_out) * step
        samples = np.interp(positions, np.arange(n_in), clip.samples).astype(np.float32)
        fade = min(self._fade, n_out // 2)
        if fade > 0:
            ramp = np.arange(fade, dtype=np.float32) / fade
            samples[:fade] *= ramp
            samples[n_out - fade :] *= ramp[::-1]
        return samples

    def _open_output_stream(self, callback: StreamCallback) -> Stream:
        """The default stream factory: the system default output device."""
        # Imported here, so that nothing loads PortAudio until audio actually starts.
        import sounddevice

        # The stream stays on the device that is the default now, also when the system's
        # output moves later, so the log names it.
        try:
            device = sounddevice.query_devices(kind="output")["name"]
        except Exception:  # PortAudio's errors have no common base, and a name is not worth a failure
            device = "unknown device"
        log.info("audio output: %s", device)
        # Two channels with the mix on both: a one-channel CoreAudio stream can come
        # out of the left speaker only. High latency and big blocks: see _BLOCKSIZE.
        return sounddevice.OutputStream(
            samplerate=self._sample_rate,
            channels=2,
            dtype="float32",
            latency="high",
            blocksize=_BLOCKSIZE,
            callback=callback,
        )

    def _fill(
        self, outdata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        """The stream callback: write the next `frames` of the mix to every channel.

        It must not raise, because an exception in a PortAudio callback aborts the
        stream. A failure is logged, once for each kind of error, and the block is
        silence.
        """
        if status:
            log.debug("output stream status: %s", status)
        try:
            outdata[:] = self._next_block(frames, time_info)[:, np.newaxis]
        except Exception as exc:
            if type(exc) not in self._failures:
                self._failures.add(type(exc))
                log.exception(
                    "audio callback failed (%s), writing silence", type(exc).__name__
                )
            outdata.fill(0.0)

    def _next_block(self, frames: int, time_info: object) -> np.ndarray:
        """The next `frames` of the mix: silence while paused, else from the cursor.

        The cursor is the media time of the block's first sample at the moment the
        speaker plays it, which is `_output_delay` after the callback. It starts at the
        clock's reading for that moment and moves on by one block at a time. It snaps
        to the clock when the two are further apart than `_RESYNC_AFTER`, and when
        playback restarts. Both uses of the clock take the delay into account, so a
        steady delay never makes it snap. Playback rates other than 1.0 are not
        followed.
        """
        clock = self._clock
        if not clock.playing:
            self._cursor = None
            return np.zeros(frames, dtype=np.float32)
        heard_at = clock.at(time.monotonic() + _output_delay(time_info))
        if self._cursor is None or abs(self._cursor - heard_at) > _RESYNC_AFTER:
            self._cursor = heard_at
        block = self.render(frames, self._cursor)
        self._cursor += frames / self._sample_rate
        return block
