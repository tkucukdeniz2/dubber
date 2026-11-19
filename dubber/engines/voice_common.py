"""What the voice adapters share: the checks on a request and the speed rule."""

import math

import numpy as np

from dubber.engines.base import Clip, VoiceError


def check_request(text: str, speed: float) -> None:
    """Refuse a request no voice can answer, before anything is loaded or run.

    Blank text is a VoiceError. A speed that is not a positive number is the
    caller's bug and a ValueError.
    """
    if not (math.isfinite(speed) and speed > 0):
        raise ValueError(f"speed must be a positive number, got {speed!r}")
    if not text.strip():
        raise VoiceError("empty text")


def retime(clip: Clip, speed: float) -> Clip:
    """The clip played at speed times the pace, by resampling its length.

    The speed rule for an engine with no speed parameter of its own, which is VoxCPM2
    (Supertonic-3 has one and gets the speed itself). Output sample j is the input at
    position j * speed (numpy.interp, linear), so the pitch moves with the speed. The
    clip is returned as it is when speed is 1.0.

    This is a Phase 0 stand-in that shifts the pitch; Phase 1 replaces it with a
    time-stretch that keeps the pitch.
    """
    if speed == 1.0:
        return clip
    count = len(clip.samples)
    positions = np.arange(max(1, round(count / speed))) * speed
    samples = np.interp(positions, np.arange(count), clip.samples)
    return Clip(samples.astype(np.float32), clip.sample_rate)
