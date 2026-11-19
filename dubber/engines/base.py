"""Shared types for the translation and voice engines, and the words for their errors."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True, eq=False)
class Clip:
    """Mono audio: float32 samples played at sample_rate Hz."""

    samples: np.ndarray
    sample_rate: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.sample_rate


class TranslatorError(RuntimeError):
    """A translator could not produce a translation."""


class VoiceError(RuntimeError):
    """A voice could not produce audio."""


def failure_reason(exc: BaseException) -> str:
    """Why `exc` was raised, in words: its message, else the type name of what caused it.

    httpx often leaves the message of a timeout empty, and the translator's error then has
    none either. With no message and no cause, the exception's own type name.
    """
    return str(exc) or type(exc.__cause__ or exc).__name__


class Translator(Protocol):
    """Turn English into Turkish.

    `shorten` is asked once when a rendering is too long for its slot: it gives a shorter
    one of `text` than `turkish`, in at most `max_words` words, or `turkish` itself when
    the engine cannot shorten.
    """

    name: str

    def translate(
        self,
        text: str,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
    ) -> str: ...

    def shorten(
        self,
        text: str,
        turkish: str,
        max_words: int,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
    ) -> str: ...


class Voice(Protocol):
    """Speak text as a Clip.

    speed is a request, not a promise: 1.0 asks for the engine's natural pace and a
    larger number for faster speech. The engines realize it differently: `say` as words
    per minute, Supertonic through its own duration control, VoxCPM2 by resampling the
    finished clip. So the same speed gives different lengths on different engines, and
    a caller that needs a length must measure `clip.seconds` of what it gets back.
    """

    name: str

    def synthesize(self, text: str, speed: float = 1.0) -> Clip: ...


class IdentityTranslator(Translator):
    """Stand-in that returns the text unchanged."""

    name = "identity"

    def translate(
        self,
        text: str,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        return text

    def shorten(
        self,
        text: str,
        turkish: str,
        max_words: int,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        return turkish
