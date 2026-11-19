import shutil, pytest
from dubber.engines.voice_say import SayVoice
from dubber.engines.base import VoiceError

pytestmark = pytest.mark.skipif(shutil.which("say") is None, reason="macOS say not available")

def test_say_yelda_returns_mono_clip_with_duration():
    clip = SayVoice("Yelda").synthesize("Merhaba, bu bir deneme.")
    assert clip.sample_rate == 22050 and clip.samples.ndim == 1
    assert 0.8 < clip.seconds < 4.0

def test_say_speed_shortens_audio():
    v = SayVoice("Yelda")
    assert v.synthesize("Bugün hava on sekiz derece.", speed=1.2).seconds < v.synthesize("Bugün hava on sekiz derece.", speed=1.0).seconds

def test_say_unknown_voice_raises():
    with pytest.raises(VoiceError):
        SayVoice("NoSuchVoiceXYZ").synthesize("x")

# What the tests above leave open: the command line, the temporary file, the error
# paths and the voice listing, all against a stand-in for `say`.
import pathlib, subprocess
import numpy as np
import soundfile as sf
from dubber.engines import voice_say

LISTING = (
    "Albert              en_US    # Hello! My name is Albert.\n"
    "Eddy (İngilizce (ABD)) en_US    # Hello! My name is Eddy.\n"
    "Cem (Enhanced)      tr_TR    # Merhaba, benim adım Cem.\n"
    "Kyoko               ja_JP    # こんにちは、私の名前はKyokoです。\n"
    "Yelda               tr_TR    # Merhaba, benim adım Yelda.\n"
)

class FakeSay:
    """Stands in for subprocess.run: lists the voices, and for a synthesis call writes
    a second of silence to the -o path (also when it then reports a failure)."""

    def __init__(self, returncode=0, stderr="", listing=LISTING):
        self.returncode, self.stderr, self.listing = returncode, stderr, listing
        self.calls = []    # (argv, kwargs) of every synthesis call
        self.outputs = []  # the -o path of each
        self.listings = 0  # how often the voices were listed

    def __call__(self, argv, **kwargs):
        if argv[1:] == ["-v", "?"]:
            self.listings += 1
            return subprocess.CompletedProcess(argv, 0, self.listing, "")
        out = pathlib.Path(argv[argv.index("-o") + 1])
        self.calls.append((argv, kwargs))
        self.outputs.append(out)
        sf.write(out, np.zeros(22050, dtype=np.float32), 22050, subtype="PCM_16")
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)

def install_say(monkeypatch, **kwargs):
    fake = FakeSay(**kwargs)
    monkeypatch.setattr(voice_say.subprocess, "run", fake)
    return fake

def test_say_command_line_passes_the_text_as_one_argument_without_a_shell(monkeypatch):
    fake = install_say(monkeypatch)
    text = "- Merhaba; $(echo hi) 'dünya'"
    clip = SayVoice("Yelda").synthesize(text, speed=1.2)  # the default rate is 175 words per minute
    ((argv, kwargs),) = fake.calls
    assert argv == ["say", "-v", "Yelda", "-r", "210", "--file-format=WAVE", "--data-format=LEI16@22050",
                    "-o", str(fake.outputs[0]), "--", text]
    assert not kwargs.get("shell")
    assert fake.outputs[0].suffix == ".wav"
    assert clip.sample_rate == 22050 and clip.samples.shape == (22050,) and clip.samples.dtype == np.float32

def test_say_rate_is_the_base_rate_times_the_speed(monkeypatch):
    fake = install_say(monkeypatch)
    SayVoice("Yelda", base_rate_wpm=100).synthesize("Merhaba.", speed=1.5)
    assert fake.calls[0][0][3:5] == ["-r", "150"]

def test_say_removes_its_temporary_file(monkeypatch):
    fake = install_say(monkeypatch)
    SayVoice("Yelda").synthesize("Merhaba.")
    assert not fake.outputs[0].exists()

def test_say_failure_raises_voice_error_with_stderr_and_removes_the_temporary_file(monkeypatch):
    fake = install_say(monkeypatch, returncode=1, stderr="say: boom\n")
    with pytest.raises(VoiceError, match="boom"):
        SayVoice("Yelda").synthesize("Merhaba.")
    assert not fake.outputs[0].exists()

def test_say_that_cannot_be_run_raises_voice_error(monkeypatch):
    def gone(argv, **kwargs):
        raise FileNotFoundError("say")
    monkeypatch.setattr(voice_say.subprocess, "run", gone)
    with pytest.raises(VoiceError, match="say"):
        SayVoice("Yelda").synthesize("Merhaba.")

def test_say_rejects_a_voice_that_is_not_installed_instead_of_letting_say_fall_back(monkeypatch):
    # On macOS 26 `say -v Nobody` exits 0 and speaks in the default voice.
    fake = install_say(monkeypatch)
    with pytest.raises(VoiceError, match="Nobody"):
        SayVoice("Nobody").synthesize("Merhaba.")
    assert fake.calls == []

def test_say_finds_a_name_that_starts_one_listed_voice_and_passes_say_the_full_name(monkeypatch):
    fake = install_say(monkeypatch)
    for spoken in ("cem", "Eddy", "Yelda"):  # ignoring case, as the start of the listed name
        SayVoice(spoken).synthesize("Merhaba.")
    assert [argv[2] for argv, _ in fake.calls] == ["Cem (Enhanced)", "Eddy (İngilizce (ABD))", "Yelda"]

def listing_of(*voices):
    """A `say -v ?` listing of these (name, locale) pairs."""
    return "".join(f"{name:<20} {locale}    # Merhaba.\n" for name, locale in voices)

def test_say_takes_an_exact_name_although_it_also_starts_a_longer_one(monkeypatch):
    fake = install_say(monkeypatch, listing=listing_of(("Yelda", "tr_TR"), ("Yelda (Enhanced)", "tr_TR")))
    for spoken in ("Yelda", "yelda", "YELDA (enhanced)"):  # ignoring case; say gets the name as listed
        SayVoice(spoken).synthesize("Merhaba.")
    assert [argv[2] for argv, _ in fake.calls] == ["Yelda", "Yelda", "Yelda (Enhanced)"]

@pytest.mark.parametrize("spoken, installed, candidates", [
    ("Yel", [("Yelda", "tr_TR"), ("Yelda (Enhanced)", "tr_TR")], "Yelda, Yelda (Enhanced)"),
    ("yelda", [("Yelda (Enhanced)", "tr_TR"), ("Yelda (Premium)", "tr_TR")], "Yelda (Enhanced), Yelda (Premium)"),
    ("Eddy", [("Eddy (İngilizce (ABD))", "en_US"), ("Eddy (Almanca (Almanya))", "de_DE")], "Eddy (İngilizce (ABD)), Eddy (Almanca (Almanya))"),
])
def test_say_rejects_a_name_that_starts_several_listed_voices_and_lists_them(monkeypatch, spoken, installed, candidates):
    fake = install_say(monkeypatch, listing=listing_of(*installed))
    with pytest.raises(VoiceError) as raised:
        SayVoice(spoken).synthesize("Merhaba.")
    assert str(raised.value) == f"ambiguous voice {spoken!r}: {candidates}"
    assert fake.calls == []

def test_say_counts_a_name_the_listing_repeats_as_one_voice(monkeypatch):
    # The real listing has "Aru ((null))" twice.
    fake = install_say(monkeypatch, listing=listing_of(("Aru ((null))", "kk_KZ"), ("Aru ((null))", "kk_KZ"), ("Albert", "en_US")))
    SayVoice("Aru").synthesize("Merhaba.")
    assert fake.calls[0][0][2] == "Aru ((null))"

def test_say_looks_the_voice_up_once(monkeypatch):
    fake = install_say(monkeypatch)
    voice = SayVoice("cem")
    voice.synthesize("Bir.")
    voice.synthesize("İki.")
    assert fake.listings == 1 and [argv[2] for argv, _ in fake.calls] == ["Cem (Enhanced)", "Cem (Enhanced)"]

def test_available_voices_lists_the_turkish_and_us_english_names(monkeypatch):
    install_say(monkeypatch)
    assert SayVoice.available_voices() == ["Albert", "Eddy (İngilizce (ABD))", "Cem (Enhanced)", "Yelda"]
