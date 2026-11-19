"""The voice on the PC: VoxCPM2 on the RTX 4080 SUPER, through the dubber-tts service.

The service is pc/tts_server.py, run on the PC from C:\\AI\\tts (see pc/README.md). It
holds the owner's cloned voice and answers each sentence with an audio file: Ogg Opus when
it is asked for and can write it, else WAV. Opus is about a twelfth of the size, which from
far away is most of a second for every sentence.
"""

import io
import logging
import time

import httpx
import numpy as np
import soundfile as sf

from dubber.engines.base import Clip, Voice, VoiceError, failure_reason
from dubber.engines.voice_common import check_request, retime

log = logging.getLogger(__name__)

# A reply that takes this many seconds longer than the PC needed to make the clip was slow on
# the way: the log then says so, which tells a slow network from a slow PC.
_SLOW_REPLY = 2.0
# How long an idle connection to the PC is kept. Sentences come every few seconds, and from
# far away each new connection costs round trips; httpx's own 5 s would drop most of them.
_KEEP_ALIVE = 60.0


class PcVoice(Voice):
    """Speak through dubber-tts on the PC, in one of the voices it holds.

    `voice` names a voice the service keeps ("owner" is the owner's own); `mode` is
    "ultimate" (cloned from the clip and its transcript, the closest match) or
    "reference" (from the clip alone). `audio_format` is what the service is asked to
    answer in, "opus" or "wav"; whichever comes is played, so a service from before Opus
    still works. The model has no speed input, so a speed other
    than 1.0 resamples the clip, as the local VoxCPM2 adapter does. The client waits at
    most 3 s to connect, so a PC that is off fails fast, and 60 s for a sentence.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        voice: str = "owner",
        mode: str = "ultimate",
        audio_format: str = "opus",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.voice = voice
        self.mode = mode
        self.audio_format = audio_format
        self.name = f"pc-voxcpm2:{voice}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=3.0),
            limits=httpx.Limits(keepalive_expiry=_KEEP_ALIVE),
            transport=transport,
        )

    def status(self) -> dict:
        """The service's /status: whether its model is ready, and its voices."""
        response = self._request("GET", "/status")
        try:
            status = response.json()
        except ValueError:
            status = None
        if not isinstance(status, dict):
            raise VoiceError("the PC voice service sent a status that is not a JSON object")
        return status

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        check_request(text, speed)
        began = time.perf_counter()
        response = self._request("POST", "/tts", json=self._body(text))
        _note_reply(time.perf_counter() - began, response)
        try:
            samples, sample_rate = sf.read(io.BytesIO(response.content), dtype="float32", always_2d=True)
        except Exception:
            raise VoiceError("the PC voice service sent a reply that is not audio") from None
        samples = samples.mean(axis=1).astype(np.float32)
        if samples.size == 0:
            raise VoiceError("the PC voice service sent no audio")
        return retime(Clip(samples, int(sample_rate)), speed)

    def warm_up(self) -> None:
        """Speak one word and drop it: the service's first clip after it starts is slow."""
        self._request("POST", "/tts", json=self._body("Merhaba."))

    def close(self) -> None:
        self._client.close()

    def _body(self, text: str) -> dict:
        return {"text": text, "voice": self.voice, "mode": self.mode, "format": self.audio_format}

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise VoiceError(f"cannot reach the PC voice service at {self.base_url}: {failure_reason(exc)}") from None
        if response.status_code != 200:
            hint = " (is the PC in dubbing mode? pc-dublaj)" if response.status_code in (502, 503) else ""
            raise VoiceError(f"the PC voice service answered HTTP {response.status_code}: {_message(response)}{hint}")
        return response


def _note_reply(took: float, response: httpx.Response) -> None:
    """Log how long a clip's reply took against what the PC says the clip took to make.

    At INFO when the difference is `_SLOW_REPLY` or more, else at DEBUG. A service that does
    not say what the clip took (X-Synth-Seconds) is not logged.
    """
    try:
        made = float(response.headers.get("X-Synth-Seconds", ""))
    except ValueError:
        return
    slow = took - made >= _SLOW_REPLY
    log.log(
        logging.INFO if slow else logging.DEBUG,
        "the PC voice answered in %.1f s with a clip it made in %.1f s (%d KB)%s",
        took, made, len(response.content) // 1024, ": the reply was slow on the way" if slow else "",
    )


def _message(response: httpx.Response) -> str:
    """The service's own explanation in an error reply, or the start of the body."""
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        message = None
    return message or response.text[:200].strip() or "no explanation"
