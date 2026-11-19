import numpy as np
import pytest
from dubber.engines.base import Clip
from dubber.engines.voice_common import retime

def ramp(n):
    return Clip(np.arange(n, dtype=np.float32), 1000)

def test_retime_at_normal_speed_returns_the_clip_itself():
    clip = ramp(100)
    assert retime(clip, 1.0) is clip

def test_retime_changes_the_length_by_the_speed_factor_and_keeps_the_sample_rate():
    faster, slower = retime(ramp(1000), 2.0), retime(ramp(1000), 0.5)
    assert len(faster.samples) == 500 and len(slower.samples) == 2000
    assert faster.sample_rate == slower.sample_rate == 1000
    assert faster.samples.dtype == slower.samples.dtype == np.float32

def test_retime_plays_the_input_at_speed_times_the_pace():
    # output sample j is the input at position j * speed, interpolated linearly
    assert np.allclose(retime(ramp(1000), 2.0).samples[:4], [0, 2, 4, 6])
    assert np.allclose(retime(ramp(1000), 0.5).samples[:4], [0, 0.5, 1, 1.5])

def test_retime_holds_the_last_sample_instead_of_running_past_the_end():
    out = retime(ramp(6), 0.7).samples  # nine samples; the last would sit at input position 5.6 of 5
    assert len(out) == 9 and out[-1] == 5

def test_retime_keeps_at_least_one_sample():
    assert len(retime(ramp(1), 4.0).samples) == 1

# Ruling 12: Clip holds an ndarray, so == and hash() must not look inside it.

def test_clip_compares_and_hashes_by_identity():
    a, b = ramp(4), ramp(4)
    assert a == a and a != b
    assert len({a, b}) == 2
