"""Build a voice from the short name used on the command line and in the config."""

import pathlib

from dubber.engines.base import Voice
from dubber.engines.voice_say import SayVoice
from dubber.engines.voice_supertonic import SupertonicVoice
from dubber.engines.voice_voxcpm_mlx import VoxCpmMlxVoice


def voice_from_name(spec: str) -> Voice:
    """The voice for "say:<Voice>", "supertonic:<preset>", "voxcpm" or
    "voxcpm:<reference.wav>"; any other spec is a ValueError.

    The transcript of a reference clip, when there is one, is the text file beside it
    with the same name and a .txt suffix. Nothing is loaded here: models load on the
    first synthesize.
    """
    kind, _, arg = spec.partition(":")
    if kind == "say" and arg:
        return SayVoice(arg)
    if kind == "supertonic" and arg:
        return SupertonicVoice(arg)
    if spec == "voxcpm":
        return VoxCpmMlxVoice()
    if kind == "voxcpm" and arg:
        ref_audio = pathlib.Path(arg).expanduser()
        sidecar = ref_audio.with_suffix(".txt")
        ref_text = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else None
        return VoxCpmMlxVoice(ref_audio=ref_audio, ref_text=ref_text or None)
    raise ValueError(
        f"unknown voice {spec!r}: use say:<Voice>, supertonic:<preset>, voxcpm or voxcpm:<reference.wav>"
    )
