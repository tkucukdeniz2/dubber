import pathlib, subprocess, sys
import pytest
from dubber.engines.base import VoiceError
from dubber.engines.voice_say import SayVoice
from dubber.engines.voice_supertonic import SupertonicVoice
from dubber.engines.voice_voxcpm_mlx import VoxCpmMlxVoice
from dubber.engines.voices import voice_from_name

def test_voice_from_name_builds_each_voice_without_loading_anything():
    say = voice_from_name("say:Yelda")
    assert isinstance(say, SayVoice) and say.name == "say:Yelda"
    supertonic = voice_from_name("supertonic:F1")
    assert isinstance(supertonic, SupertonicVoice) and supertonic.name == "supertonic3:F1"
    voxcpm = voice_from_name("voxcpm")
    assert isinstance(voxcpm, VoxCpmMlxVoice) and voxcpm.name == "voxcpm2-mlx"
    assert voxcpm.ref_audio is None and voxcpm.ref_text is None

def test_voice_from_name_voxcpm_reference_clip_takes_its_transcript_from_the_sidecar_file(tmp_path):
    ref = tmp_path / "ref.wav"
    (tmp_path / "ref.txt").write_text(" Merhaba dünya.\n", encoding="utf-8")
    voice = voice_from_name(f"voxcpm:{ref}")
    assert isinstance(voice, VoxCpmMlxVoice)
    assert voice.ref_audio == ref and voice.ref_text == "Merhaba dünya."

def test_voice_from_name_voxcpm_reference_clip_without_a_sidecar_has_no_transcript(tmp_path):
    voice = voice_from_name(f"voxcpm:{tmp_path / 'ref.wav'}")
    assert voice.ref_audio == tmp_path / "ref.wav" and voice.ref_text is None

def test_voice_from_name_expands_a_home_directory_in_the_reference_path():
    assert voice_from_name("voxcpm:~/ref.wav").ref_audio == pathlib.Path.home() / "ref.wav"

@pytest.mark.parametrize("spec", ["", "say", "say:", "supertonic", "supertonic:", "voxcpm:", "voxcpm2", "piper:x", "SAY:Yelda"])
def test_voice_from_name_rejects_any_other_spec(spec):
    with pytest.raises(ValueError, match="voice"):
        voice_from_name(spec)

def test_importing_the_voices_needs_neither_model_package():
    # in a fresh interpreter, where `import supertonic` and `import mlx_audio` raise ImportError
    code = "import sys; sys.modules['supertonic'] = sys.modules['mlx_audio'] = None; import dubber.engines.voices"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

# Every voice checks its request before it loads or runs anything.

@pytest.fixture(params=["say:Yelda", "supertonic:M1", "voxcpm"])
def voice(request):
    return voice_from_name(request.param)

def test_every_voice_refuses_blank_text(voice):
    with pytest.raises(VoiceError, match="empty"):
        voice.synthesize(" \n")

@pytest.mark.parametrize("speed", [0, -1.0, float("nan"), float("inf")])
def test_every_voice_rejects_a_speed_that_is_not_positive_and_finite(voice, speed):
    with pytest.raises(ValueError, match="speed"):
        voice.synthesize("Merhaba.", speed=speed)
