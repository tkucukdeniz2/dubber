"""Speak Turkish with Supertonic-3 (ONNX, on the CPU)."""

import numpy as np

from dubber.engines.base import Clip, Voice, VoiceError
from dubber.engines.voice_common import check_request

# The speed range the engine documents (0.7 = slower, 2.0 = faster); beyond it the
# engine raises a ValueError.
MIN_SPEED = 0.7
MAX_SPEED = 2.0


class SupertonicVoice(Voice):
    """Supertonic-3 reading Turkish in one of its preset voices (M1 to M5, F1 to F5).

    The model loads on the first synthesize, and the very first run also downloads
    about 400 MB of model files. The package is the optional `supertonic` extra
    (uv sync --extra supertonic).

    Speed is the engine's own speed parameter, which scales the durations it predicts
    and leaves the pitch alone; the clip is not resampled afterwards. A speed outside
    the engine's range of 0.7 to 2.0 is clamped to it. Speed 1.0 is the engine at 1.0,
    a little slower than the 1.05 it uses when it is given no speed.
    """

    def __init__(self, voice_name: str = "M1"):
        self.voice_name = voice_name
        self.name = f"supertonic3:{voice_name}"
        self._tts = None
        self._style = None

    def _load(self) -> None:
        try:
            from supertonic import TTS
        except ImportError as exc:
            raise VoiceError(f"supertonic is not installed: {exc}") from exc
        try:
            tts = TTS()
            style = tts.get_voice_style(voice_name=self.voice_name)
        except Exception as exc:  # download, ONNX and preset errors have no common base
            raise VoiceError(f"cannot load Supertonic-3 voice {self.voice_name}: {exc}") from exc
        self._tts, self._style = tts, style

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        check_request(text, speed)
        if self._tts is None:
            self._load()
        engine_speed = float(min(max(speed, MIN_SPEED), MAX_SPEED))
        try:
            wav, _ = self._tts.synthesize(text, voice_style=self._style, lang="tr", speed=engine_speed)
        except Exception as exc:  # the engine raises ValueError, onnxruntime errors, ...
            raise VoiceError(f"Supertonic-3 failed: {exc}") from exc
        samples = np.asarray(wav, dtype=np.float32).reshape(-1)  # the engine returns (1, n)
        if samples.size == 0:
            raise VoiceError("Supertonic-3 produced no audio")
        return Clip(samples, int(self._tts.sample_rate))
