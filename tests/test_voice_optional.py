import pytest
from dubber.engines.base import VoiceError

def test_supertonic_missing_package_raises_voice_error(monkeypatch):
    import builtins
    real = builtins.__import__
    def fake(name, *a, **k):
        if name.startswith("supertonic"): raise ImportError("no supertonic")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    from dubber.engines.voice_supertonic import SupertonicVoice
    with pytest.raises(VoiceError):
        SupertonicVoice().synthesize("Merhaba.")

# What the test above leaves open: the call into each model package, lazy loading, the
# error mapping and the speed rule, against stand-in packages. The two slow tests at the
# end load the real models.
import sys, types
import numpy as np
from dubber.engines.voice_supertonic import SupertonicVoice
from dubber.engines.voice_voxcpm_mlx import VoxCpmMlxVoice

def fake_supertonic(monkeypatch, load_error=None, style_error=None, synth_error=None):
    """Install a stand-in `supertonic` package and return what was called on it."""
    seen = types.SimpleNamespace(loads=0, styles=[], synth=[])
    class TTS:
        sample_rate = 44100
        def __init__(self):
            seen.loads += 1
            if load_error: raise load_error
        def get_voice_style(self, voice_name):
            seen.styles.append(voice_name)
            if style_error: raise style_error
            return f"style-{voice_name}"
        def synthesize(self, text, voice_style, lang=None, speed=1.05):  # takes no other argument
            seen.synth.append((text, voice_style, lang, speed))
            if synth_error: raise synth_error
            # a second of audio at speed 1.0; a faster speed gives a shorter clip, as in the engine
            return np.linspace(-1, 1, round(44100 / speed), dtype=np.float32).reshape(1, -1), np.array([1.0])
    monkeypatch.setitem(sys.modules, "supertonic", types.SimpleNamespace(TTS=TTS))
    return seen

def test_supertonic_loads_once_on_first_use_and_speaks_turkish(monkeypatch):
    seen = fake_supertonic(monkeypatch)
    assert SupertonicVoice().name == "supertonic3:M1"  # M1 is the default preset
    voice = SupertonicVoice("F2")
    assert voice.name == "supertonic3:F2" and seen.loads == 0
    clip = voice.synthesize("Merhaba.")
    voice.synthesize("Hoşça kal.")
    assert seen.loads == 1 and seen.styles == ["F2"]
    assert seen.synth == [("Merhaba.", "style-F2", "tr", 1.0), ("Hoşça kal.", "style-F2", "tr", 1.0)]
    assert clip.sample_rate == 44100 and clip.samples.shape == (44100,) and clip.samples.dtype == np.float32

@pytest.mark.parametrize("speed, engine_speed", [
    (1.0, 1.0), (0.9, 0.9), (1.2, 1.2), (1.5, 1.5),  # our speed is the engine's speed
    (0.7, 0.7), (2.0, 2.0),                          # the ends of the engine's range are allowed
    (0.5, 0.7), (3.0, 2.0),                          # beyond them it is clamped
])
def test_supertonic_gives_the_engine_its_speed_clamped_and_leaves_the_audio_alone(monkeypatch, speed, engine_speed):
    seen = fake_supertonic(monkeypatch)
    clip = SupertonicVoice().synthesize("Merhaba.", speed=speed)
    assert [call[3] for call in seen.synth] == [engine_speed]
    assert len(clip.samples) == round(44100 / engine_speed)  # what the engine returned: not resampled again
    assert clip.sample_rate == 44100

@pytest.mark.parametrize("failing", [
    {"load_error": RuntimeError("download failed")},
    {"style_error": FileNotFoundError("no style Q9")},
    {"synth_error": ValueError("unsupported character")},
])
def test_supertonic_errors_become_voice_errors_with_the_underlying_message(monkeypatch, failing):
    fake_supertonic(monkeypatch, **failing)
    (message,) = [str(e) for e in failing.values()]
    with pytest.raises(VoiceError, match=message):
        SupertonicVoice().synthesize("Merhaba.")

def fake_mlx_audio(monkeypatch, load_error=None, results=1):
    """Install a stand-in `mlx_audio.tts.utils` and return what was called on it."""
    seen = types.SimpleNamespace(loads=[], generate=[])
    class Model:
        def generate(self, text, **kwargs):
            seen.generate.append((text, kwargs))
            for _ in range(results):
                yield types.SimpleNamespace(audio=np.linspace(-1, 1, 48000, dtype=np.float32), sample_rate=48000)
    def load_model(path, **kwargs):
        seen.loads.append(path)
        if load_error: raise load_error
        return Model()
    for name in ("mlx_audio", "mlx_audio.tts", "mlx_audio.tts.utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["mlx_audio.tts.utils"].load_model = load_model
    return seen

def test_voxcpm_loads_once_on_first_use_and_speaks_turkish(monkeypatch):
    seen = fake_mlx_audio(monkeypatch)
    voice = VoxCpmMlxVoice()
    assert voice.name == "voxcpm2-mlx" and seen.loads == []
    clip = voice.synthesize("Merhaba.")
    voice.synthesize("Hoşça kal.")
    assert seen.loads == ["mlx-community/VoxCPM2-8bit"]
    assert seen.generate == [("Merhaba.", {"lang_code": "tr"}), ("Hoşça kal.", {"lang_code": "tr"})]
    assert clip.sample_rate == 48000 and clip.samples.shape == (48000,) and clip.samples.dtype == np.float32

def test_voxcpm_loads_the_model_it_was_given(monkeypatch):
    seen = fake_mlx_audio(monkeypatch)
    VoxCpmMlxVoice(model="mlx-community/VoxCPM2-4bit").synthesize("Merhaba.")
    assert seen.loads == ["mlx-community/VoxCPM2-4bit"]

def test_voxcpm_passes_the_reference_clip_and_transcript_only_when_given(monkeypatch, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"")  # only has to exist; the stand-in model never reads it
    seen = fake_mlx_audio(monkeypatch)
    VoxCpmMlxVoice(ref_audio=ref).synthesize("Merhaba.")
    VoxCpmMlxVoice(ref_audio=ref, ref_text="Selam dünya.").synthesize("Merhaba.")
    assert seen.generate[0][1] == {"lang_code": "tr", "ref_audio": str(ref)}
    assert seen.generate[1][1] == {"lang_code": "tr", "ref_audio": str(ref), "ref_text": "Selam dünya."}

def test_voxcpm_missing_reference_clip_raises_before_the_model_loads(monkeypatch, tmp_path):
    seen = fake_mlx_audio(monkeypatch)
    with pytest.raises(VoiceError, match="gone.wav"):
        VoxCpmMlxVoice(ref_audio=tmp_path / "gone.wav").synthesize("Merhaba.")
    assert seen.loads == []

@pytest.mark.parametrize("speed, length", [(1.0, 48000), (2.0, 24000), (0.5, 96000)])
def test_voxcpm_applies_speed_by_resampling_the_clip(monkeypatch, speed, length):
    fake_mlx_audio(monkeypatch)
    assert len(VoxCpmMlxVoice().synthesize("Merhaba.", speed=speed).samples) == length

def test_voxcpm_joins_the_audio_when_the_model_yields_several_results(monkeypatch):
    fake_mlx_audio(monkeypatch, results=2)
    assert len(VoxCpmMlxVoice().synthesize("Merhaba.").samples) == 96000

def test_voxcpm_model_that_yields_nothing_raises_voice_error(monkeypatch):
    fake_mlx_audio(monkeypatch, results=0)
    with pytest.raises(VoiceError, match="no audio"):
        VoxCpmMlxVoice().synthesize("Merhaba.")

def test_voxcpm_load_error_becomes_voice_error_with_the_underlying_message(monkeypatch):
    fake_mlx_audio(monkeypatch, load_error=OSError("weights missing"))
    with pytest.raises(VoiceError, match="weights missing"):
        VoxCpmMlxVoice().synthesize("Merhaba.")

def test_voxcpm_missing_package_raises_voice_error(monkeypatch):
    import builtins
    real = builtins.__import__
    def fake(name, *a, **k):
        if name.startswith("mlx_audio"): raise ImportError("no mlx_audio")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(VoiceError, match="no mlx_audio"):
        VoxCpmMlxVoice().synthesize("Merhaba.")

# The real models. Excluded by default (see addopts); run with `uv run pytest -m slow`.
# The sample rates are the ones each engine produces.

@pytest.mark.slow
def test_supertonic_speaks_turkish_with_the_real_model():
    pytest.importorskip("supertonic")
    voice = SupertonicVoice()
    clip = voice.synthesize("Merhaba, bu bir deneme.")
    assert clip.sample_rate == 44100 and clip.samples.ndim == 1 and clip.samples.dtype == np.float32
    assert 0.5 < clip.seconds < 6
    assert voice.synthesize("Merhaba, bu bir deneme.", speed=1.5).seconds < clip.seconds

@pytest.mark.slow
def test_voxcpm_speaks_turkish_with_the_real_model():
    pytest.importorskip("mlx_audio")
    clip = VoxCpmMlxVoice().synthesize("Merhaba, bu bir deneme.")
    assert clip.sample_rate == 48000 and clip.samples.ndim == 1 and clip.samples.dtype == np.float32
    assert 0.5 < clip.seconds < 6
