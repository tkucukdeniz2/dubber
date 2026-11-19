import io
import json

import httpx
import numpy as np
import pytest
import soundfile as sf

from dubber.engines.base import VoiceError
from dubber.engines.voice_pc import PcVoice


def wav(seconds: float, sample_rate: int = 48000) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.full(int(seconds * sample_rate), 0.25, dtype=np.float32), sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def voice_with(handler, **kwargs) -> PcVoice:
    return PcVoice("http://pc.test:8191/", "the-key", transport=httpx.MockTransport(handler), **kwargs)


def test_synthesize_posts_the_sentence_and_returns_the_clip():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=wav(1.5), headers={"Content-Type": "audio/wav"})

    clip = voice_with(handler).synthesize("Gemi devasa.")
    assert clip.sample_rate == 48000 and clip.seconds == pytest.approx(1.5)
    assert clip.samples.dtype == np.float32 and clip.samples.max() == pytest.approx(0.25, abs=1e-3)
    request = seen[0]
    assert request.url == "http://pc.test:8191/tts"
    assert request.headers["Authorization"] == "Bearer the-key"
    assert json.loads(request.content) == {"text": "Gemi devasa.", "voice": "owner", "mode": "ultimate", "format": "opus"}


def test_voice_and_mode_are_sent_and_named():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=wav(0.5))

    voice = voice_with(handler, voice="anne", mode="reference")
    voice.synthesize("Merhaba.")
    assert seen == [{"text": "Merhaba.", "voice": "anne", "mode": "reference", "format": "opus"}]
    assert voice.name == "pc-voxcpm2:anne"


def test_speed_shortens_the_clip():
    clip = voice_with(lambda request: httpx.Response(200, content=wav(1.2))).synthesize("Hızlı.", speed=1.2)
    assert clip.seconds == pytest.approx(1.0, abs=0.01)


def test_an_error_answer_is_a_voice_error_with_the_service_message():
    def handler(request):
        return httpx.Response(503, json={"ok": False, "message": "the model is still loading"})

    with pytest.raises(VoiceError, match="503.*still loading"):
        voice_with(handler).synthesize("Gemi.")


def test_an_unreachable_service_is_a_voice_error():
    def handler(request):
        raise httpx.ConnectTimeout("")

    with pytest.raises(VoiceError, match="cannot reach the PC voice service.*ConnectTimeout"):
        voice_with(handler).synthesize("Gemi.")


def test_a_reply_that_is_not_audio_is_a_voice_error():
    with pytest.raises(VoiceError, match="not audio"):
        voice_with(lambda request: httpx.Response(200, content=b"<html>proxy page</html>")).synthesize("Gemi.")


def test_empty_text_is_refused_before_any_request():
    def handler(request):
        raise AssertionError("no request expected")

    with pytest.raises(VoiceError):
        voice_with(handler).synthesize("   ")


def test_status_returns_the_service_state():
    def handler(request):
        assert request.url.path == "/status" and request.headers["Authorization"] == "Bearer the-key"
        return httpx.Response(200, json={"ok": True, "ready": True, "voices": ["owner"]})

    assert voice_with(handler).status() == {"ok": True, "ready": True, "voices": ["owner"]}


def test_status_of_an_unreachable_service_is_a_voice_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(VoiceError, match="cannot reach the PC voice service"):
        voice_with(handler).status()


def test_a_502_or_503_suggests_dubbing_mode():
    with pytest.raises(VoiceError, match=r"HTTP 502: no explanation \(is the PC in dubbing mode\? pc-dublaj\)"):
        voice_with(lambda request: httpx.Response(502, content=b"")).synthesize("Gemi.")
    with pytest.raises(VoiceError, match="pc-dublaj"):
        voice_with(lambda request: httpx.Response(503, json={"message": "the model is still loading"})).status()


def test_a_status_that_is_not_an_object_is_a_voice_error():
    with pytest.raises(VoiceError, match="not a JSON object"):
        voice_with(lambda request: httpx.Response(200, json=["owner"])).status()


# The run log: a reply that took much longer than the PC needed says so, which tells a slow
# network from a slow PC (2026-11-14: clips made in 1.1 s arrived after 7 s).
import logging

from dubber.engines import voice_pc


def said_by_the_voice(caplog, level):
    return [rec.getMessage() for rec in caplog.records if rec.name == "dubber.engines.voice_pc" and rec.levelno == level]


def test_a_reply_much_slower_than_the_pc_needed_is_logged(caplog, monkeypatch):
    monkeypatch.setattr(voice_pc, "_SLOW_REPLY", -1.0)   # the stand-in answers at once: count that as slow
    handler = lambda request: httpx.Response(200, content=wav(1.5), headers={"X-Synth-Seconds": "0.500"})
    with caplog.at_level(logging.DEBUG, logger="dubber.engines.voice_pc"):
        voice_with(handler).synthesize("Gemi devasa.")
    (line,) = said_by_the_voice(caplog, logging.INFO)
    assert "made in 0.5 s" in line and "slow on the way" in line and "140 KB" in line   # 144 044 bytes


def test_a_prompt_reply_is_only_a_debug_line(caplog):
    handler = lambda request: httpx.Response(200, content=wav(1.5), headers={"X-Synth-Seconds": "0.500"})
    with caplog.at_level(logging.DEBUG, logger="dubber.engines.voice_pc"):
        voice_with(handler).synthesize("Gemi devasa.")
    assert said_by_the_voice(caplog, logging.INFO) == []
    (line,) = said_by_the_voice(caplog, logging.DEBUG)
    assert "made in 0.5 s" in line and "slow" not in line


@pytest.mark.parametrize("headers", [{}, {"X-Synth-Seconds": "soon"}])
def test_a_service_that_does_not_say_what_the_clip_took_is_not_logged(caplog, headers):
    with caplog.at_level(logging.DEBUG, logger="dubber.engines.voice_pc"):
        clip = voice_with(lambda request: httpx.Response(200, content=wav(1.5), headers=headers)).synthesize("Gemi.")
    assert clip.seconds == pytest.approx(1.5)
    assert [rec for rec in caplog.records if rec.name == "dubber.engines.voice_pc"] == []


# Warm-up: the first clip after the service starts is slow (2.95 s against 1.1 s on 2026-11-14),
# so one word is spoken and dropped before the first sentence.
def test_warm_up_speaks_one_word_in_this_voice_and_keeps_nothing():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=wav(0.5))

    assert voice_with(handler, voice="anne").warm_up() is None
    (request,) = seen
    assert request.url.path == "/tts"
    assert json.loads(request.content) == {"text": "Merhaba.", "voice": "anne", "mode": "ultimate", "format": "opus"}


def test_warm_up_of_a_service_that_is_down_is_a_voice_error():
    with pytest.raises(VoiceError, match="502"):
        voice_with(lambda request: httpx.Response(502)).warm_up()


# Opus on the wire: the Mac asks for it and reads whichever comes, so a service from before
# the change keeps working.
def ogg(seconds: float, sample_rate: int = 48000) -> bytes:
    buf = io.BytesIO()
    wave = 0.25 * np.sin(2 * np.pi * 220 * np.arange(int(seconds * sample_rate)) / sample_rate)
    sf.write(buf, wave.astype(np.float32), sample_rate, format="OGG", subtype="OPUS")
    return buf.getvalue()


def test_the_voice_asks_for_opus_and_plays_what_comes():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=ogg(1.5), headers={"Content-Type": "audio/ogg"})

    clip = voice_with(handler).synthesize("Gemi devasa.")
    assert seen == [{"text": "Gemi devasa.", "voice": "owner", "mode": "ultimate", "format": "opus"}]
    assert clip.sample_rate == 48000 and clip.seconds == pytest.approx(1.5, abs=0.001)
    assert float(np.abs(clip.samples).max()) == pytest.approx(0.25, rel=0.15)


def test_wav_can_be_asked_for_instead():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=wav(0.5))

    voice_with(handler, audio_format="wav").synthesize("Gemi.")
    assert seen[0]["format"] == "wav"


def test_connections_to_the_pc_stay_open_for_a_minute():
    voice = PcVoice("http://pc.test:8191", "the-key")
    assert voice._client._transport._pool._keepalive_expiry == 60.0
