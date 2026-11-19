"""Speak with the macOS `say` command: the zero-setup voice."""

import pathlib
import re
import subprocess
import tempfile

import soundfile as sf

from dubber.engines.base import Clip, Voice, VoiceError
from dubber.engines.voice_common import check_request

SAMPLE_RATE = 22050
LOCALES = ("tr_TR", "en_US")

# One line of `say -v ?`: the voice name (it may hold spaces and parentheses), its
# locale, then "# sample sentence".
_LISTING_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<locale>[a-z]{2,3}_[A-Za-z0-9]+)\s+#")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """Run say. A say that cannot run, or exits with an error, is a VoiceError."""
    try:
        done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise VoiceError(f"cannot run say: {exc}") from exc
    if done.returncode != 0:
        raise VoiceError(done.stderr.strip() or f"say exited with status {done.returncode}")
    return done


def _installed_voices() -> list[tuple[str, str]]:
    """The (name, locale) pair of every voice `say -v ?` lists."""
    lines = _run(["say", "-v", "?"]).stdout.splitlines()
    return [(m["name"], m["locale"]) for m in map(_LISTING_LINE.match, lines) if m]


class SayVoice(Voice):
    """Speak through `say`; Turkish needs an installed tr_TR voice such as Yelda.

    The voice is a name that `say -v ?` prints, or the start of exactly one such name
    (see _resolve_voice). `say` is always given the full listed name.

    Speed is say's speaking rate: round(base_rate_wpm * speed) words per minute.
    """

    def __init__(self, voice: str = "Yelda", base_rate_wpm: int = 175):
        self.voice = voice
        self.base_rate_wpm = base_rate_wpm
        self.name = f"say:{voice}"
        self._resolved: str | None = None

    @staticmethod
    def available_voices() -> list[str]:
        """The names of the installed Turkish (tr_TR) and US English (en_US) voices."""
        return [name for name, locale in _installed_voices() if locale in LOCALES]

    def _resolve_voice(self) -> str:
        """The full listed name of the voice self.voice stands for, looked up once.

        On macOS 26 `say -v <unknown>` does not fail: it exits 0 and speaks in the
        default voice, and a name that starts several listed names gets one of them
        without a word. So the name is checked against the listing, ignoring case. A
        listed name that equals it wins ("Yelda" beside "Yelda (Enhanced)"). Otherwise
        it must be the start of exactly one listed name ("Cem" finds "Cem (Enhanced)").
        A VoiceError says that no voice fits, or names the several that do.
        """
        if self._resolved is None:
            voices = _installed_voices()
            wanted = self.voice.casefold()
            names = list(dict.fromkeys(name for name, _ in voices))  # a listing can repeat a name
            matches = []
            if wanted:
                matches = [name for name in names if name.casefold() == wanted] or [
                    name for name in names if name.casefold().startswith(wanted)
                ]
            if not matches:
                turkish = [name for name, locale in voices if locale == "tr_TR"]
                raise VoiceError(
                    f"say has no voice {self.voice!r}; "
                    f"Turkish voices installed: {', '.join(turkish) or 'none'}"
                )
            if len(matches) > 1:
                raise VoiceError(f"ambiguous voice {self.voice!r}: {', '.join(matches)}")
            self._resolved = matches[0]
        return self._resolved

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        check_request(text, speed)
        voice = self._resolve_voice()
        with tempfile.TemporaryDirectory(prefix="dubber-say-") as folder:
            wav = pathlib.Path(folder) / "say.wav"
            _run([
                "say", "-v", voice, "-r", str(round(self.base_rate_wpm * speed)),
                "--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}",
                "-o", str(wav), "--", text,  # "--": text that starts with "-" is not an option
            ])
            try:
                samples, sample_rate = sf.read(wav, dtype="float32")
            except (OSError, sf.SoundFileError) as exc:
                raise VoiceError(f"say wrote no readable audio: {exc}") from exc
        if len(samples) == 0:
            raise VoiceError("say produced no audio")
        return Clip(samples, sample_rate)
