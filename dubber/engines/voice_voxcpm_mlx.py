"""Speak Turkish with VoxCPM2 through mlx-audio, on Apple Silicon."""

import pathlib

import numpy as np

from dubber.engines.base import Clip, Voice, VoiceError
from dubber.engines.voice_common import check_request, retime


class VoxCpmMlxVoice(Voice):
    """VoxCPM2 reading Turkish; with ref_audio it speaks in the voice of that clip.

    The model loads on the first synthesize (the 8-bit one is about 3 GB and needs
    about 5 GB of memory). The package is the optional `voxcpm` extra
    (uv sync --extra voxcpm). Speed is applied by resampling the clip (see
    voice_common.retime); the model has no speed parameter. ref_text is passed along
    when given, though mlx-audio 0.6.3 clones from ref_audio alone and ignores it.
    """

    def __init__(
        self,
        model: str = "mlx-community/VoxCPM2-8bit",
        ref_audio: pathlib.Path | None = None,
        ref_text: str | None = None,
    ):
        self.model = model
        self.ref_audio = None if ref_audio is None else pathlib.Path(ref_audio)
        self.ref_text = ref_text
        self.name = "voxcpm2-mlx"
        self._loaded = None

    def _load(self) -> None:
        try:
            from mlx_audio.tts.utils import load_model
        except ImportError as exc:
            raise VoiceError(f"mlx-audio is not installed: {exc}") from exc
        try:
            self._loaded = load_model(self.model)
        except Exception as exc:  # download and weight errors have no common base
            raise VoiceError(f"cannot load {self.model}: {exc}") from exc

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        check_request(text, speed)
        if self.ref_audio is not None and not self.ref_audio.is_file():
            raise VoiceError(f"reference audio not found: {self.ref_audio}")
        if self._loaded is None:
            self._load()
        options = {"lang_code": "tr"}  # VoxCPM2 in mlx-audio 0.6.3 takes no language input and ignores this
        if self.ref_audio is not None:
            options["ref_audio"] = str(self.ref_audio)  # a str: the model reads the file itself
        if self.ref_text is not None:
            options["ref_text"] = self.ref_text
        try:
            results = list(self._loaded.generate(text=text, **options))
            # mlx computes lazily: an error in the model can surface only on this conversion
            pieces = [np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in results]
        except Exception as exc:  # the model raises whatever mlx or the tokenizer raises
            raise VoiceError(f"VoxCPM2 failed: {exc}") from exc
        samples = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.float32)
        if samples.size == 0:
            raise VoiceError("VoxCPM2 produced no audio")
        return retime(Clip(samples, int(results[0].sample_rate)), speed)
