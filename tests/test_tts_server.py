# pc/tts_server.py, the voice service that runs on the PC, tested on the Mac with a stand-in
# engine: the key, the request checks, the answers, and the single GPU worker thread.
import concurrent.futures
import importlib.util
import io
import json
import pathlib
import threading
from http.server import ThreadingHTTPServer

import httpx
import numpy as np
import pytest
import soundfile as sf

_spec = importlib.util.spec_from_file_location("tts_server", pathlib.Path(__file__).parents[1] / "pc" / "tts_server.py")
tts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tts)

KEY = "the-key"


class FakeEngine:
    """Ready or not; answers each sentence with `samples`, or raises `error`."""

    def __init__(self, ready=True, error=None, samples=None, failure=None):
        self.ready, self.error, self.failure = ready, error, failure
        self.samples = np.full(48000, 0.5, dtype=np.float32) if samples is None else samples
        self.sample_rate, self.loaded_in_s = 48000, 150.0
        self.requests = []
        self.timeouts = 0

    def voices(self):
        return ["owner"]

    def gpu_memory_gb(self):
        return 6.2

    def idle_s(self):
        return 125.0

    def note_timeout(self):
        self.timeouts += 1

    def submit(self, text, voice, mode):
        self.requests.append((text, voice, mode))
        future = concurrent.futures.Future()
        if self.failure is not None:
            future.set_exception(self.failure)
        else:
            future.set_result(self.samples)
        return future


@pytest.fixture
def serve():
    servers = []

    def start(engine):
        server = ThreadingHTTPServer(("127.0.0.1", 0), tts.make_handler(engine, KEY))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout=5)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


AUTH = {"Authorization": f"Bearer {KEY}"}


def test_read_key_takes_the_first_line_that_is_not_blank_or_a_comment(tmp_path):
    path = tmp_path / "keys.txt"
    path.write_text("﻿# keys\n\n  the-key  \nsecond\n", encoding="utf-8")
    assert tts.read_key(path) == "the-key"
    path.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no API key"):
        tts.read_key(path)


def test_wav_bytes_is_mono_pcm16_at_the_rate_and_clipped():
    data, rate = sf.read(io.BytesIO(tts.wav_bytes(np.array([0.0, 0.5, 2.0, -2.0], dtype=np.float32), 24000)))
    assert rate == 24000 and data.ndim == 1
    assert data == pytest.approx([0.0, 0.5, 1.0, -1.0], abs=1e-4)


@pytest.mark.parametrize("body, expected", [
    ({"text": " Gemi devasa. "}, ("Gemi devasa.", "owner", "ultimate")),
    ({"text": "Gemi.", "voice": "anne_2", "mode": "reference"}, ("Gemi.", "anne_2", "reference")),
])
def test_parse_request_reads_text_voice_and_mode(body, expected):
    assert tts.parse_request(json.dumps(body).encode()) == expected


@pytest.mark.parametrize("body, message", [
    (b"not json", "not JSON"),
    (b"[1, 2]", "JSON object"),
    (json.dumps({"text": "   "}).encode(), "non-empty"),
    (json.dumps({"text": "a" * 1001}).encode(), "longer than 1000"),
    (json.dumps({"text": "Gemi.", "voice": "..\\..\\config\\api-keys"}).encode(), "voice must be a name"),
    (json.dumps({"text": "Gemi.", "mode": "fast"}).encode(), "mode must be one of"),
])
def test_parse_request_refuses_bad_bodies(body, message):
    with pytest.raises(tts.BadRequest, match=message):
        tts.parse_request(body)


def test_health_needs_no_key_and_says_whether_the_model_is_ready(serve):
    assert serve(FakeEngine(ready=False)).get("/health").status_code == 503
    assert serve(FakeEngine(ready=False, error="OutOfMemoryError")).get("/health").text == "failed"
    ready = serve(FakeEngine()).get("/health")
    assert (ready.status_code, ready.text) == (200, "ok")


def test_status_needs_the_key_as_bearer_or_x_api_key(serve):
    client = serve(FakeEngine())
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    status = client.get("/status", headers={"x-api-key": KEY}).json()
    assert status["ready"] is True and status["voices"] == ["owner"] and status["gpu_memory_gb"] == 6.2
    assert status["idle_s"] == 125.0


def test_tts_answers_a_wav_with_timing_headers(serve):
    engine = FakeEngine()
    response = serve(engine).post("/tts", json={"text": "Gemi devasa."}, headers=AUTH)
    assert response.status_code == 200 and response.headers["Content-Type"] == "audio/wav"
    data, rate = sf.read(io.BytesIO(response.content))
    assert rate == 48000 and len(data) == 48000
    assert float(response.headers["X-Audio-Seconds"]) == pytest.approx(1.0)
    assert float(response.headers["X-Synth-Seconds"]) >= 0
    assert engine.requests == [("Gemi devasa.", "owner", "ultimate")]


def test_tts_refuses_without_the_key_and_while_loading(serve):
    assert serve(FakeEngine()).post("/tts", json={"text": "Gemi."}).status_code == 401
    loading = serve(FakeEngine(ready=False)).post("/tts", json={"text": "Gemi."}, headers=AUTH)
    assert loading.status_code == 503 and "still loading" in loading.json()["message"]
    failed = serve(FakeEngine(ready=False, error="OutOfMemoryError: CUDA")).post("/tts", json={"text": "Gemi."}, headers=AUTH)
    assert failed.status_code == 503 and "OutOfMemoryError" in failed.json()["message"]


def test_tts_maps_errors_to_status_codes(serve):
    bad = serve(FakeEngine()).post("/tts", json={"text": ""}, headers=AUTH)
    assert bad.status_code == 400 and "non-empty" in bad.json()["message"]
    unknown = serve(FakeEngine(failure=tts.BadRequest("no voice 'x'"))).post("/tts", json={"text": "Gemi.", "voice": "x"}, headers=AUTH)
    assert unknown.status_code == 400 and "no voice" in unknown.json()["message"]
    crashed = serve(FakeEngine(failure=RuntimeError("CUDA error"))).post("/tts", json={"text": "Gemi."}, headers=AUTH)
    assert crashed.status_code == 500 and "RuntimeError: CUDA error" in crashed.json()["message"]
    silent = serve(FakeEngine(samples=np.zeros(0, dtype=np.float32))).post("/tts", json={"text": "Gemi."}, headers=AUTH)
    assert silent.status_code == 500
    too_big = serve(FakeEngine()).post("/tts", content=b"x" * (tts.MAX_BODY_BYTES + 1), headers=AUTH)
    assert too_big.status_code == 413
    assert serve(FakeEngine()).post("/other", json={}, headers=AUTH).status_code == 404


def test_the_engine_loads_and_speaks_on_one_worker_thread(tmp_path, monkeypatch):
    threads = []
    engine = tts.Engine(tmp_path)

    def fake_load(self):
        threads.append(threading.current_thread().name)
        self.model = object()

    def fake_speak(self, text, voice, mode):
        threads.append(threading.current_thread().name)
        if text == "bad":
            raise tts.BadRequest("no voice")
        return np.ones(10, dtype=np.float32)

    monkeypatch.setattr(tts.Engine, "_load", fake_load)
    monkeypatch.setattr(tts.Engine, "_speak", fake_speak)
    engine.start()
    futures = [engine.submit(f"cümle {i}", "owner", "ultimate") for i in range(5)]
    results = [f.result(timeout=5) for f in futures]
    with pytest.raises(tts.BadRequest):
        engine.submit("bad", "owner", "ultimate").result(timeout=5)
    assert engine.ready and all(r.size == 10 for r in results)
    assert set(threads) == {"gpu-worker"} and len(threads) == 7


def test_a_model_that_fails_to_load_is_reported_not_raised(tmp_path, monkeypatch):
    engine = tts.Engine(tmp_path)

    def broken_load(self):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(tts.Engine, "_load", broken_load)
    engine._work()  # returns instead of raising
    assert not engine.ready and engine.error == "RuntimeError: CUDA out of memory"


def test_voices_lists_the_clips_with_safe_names(tmp_path):
    voices = tmp_path / "voices"
    voices.mkdir()
    for name in ("owner.wav", "anne.wav", "owner.txt", "bad name.wav"):
        (voices / name).write_bytes(b"")
    assert tts.Engine(tmp_path).voices() == ["anne", "owner"]


def test_idle_time_restarts_with_each_sentence(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: clock[0])
    engine = tts.Engine(tmp_path)
    clock[0] = 1300.0
    assert engine.idle_s() == 300.0
    engine.submit("Gemi.", "owner", "ultimate")
    clock[0] = 1310.0
    assert engine.idle_s() == 10.0


def run_engine(tmp_path, monkeypatch, outcomes):
    """An Engine whose sentences give `outcomes` in turn: an exception to raise, or samples."""
    engine = tts.Engine(tmp_path)
    pending = list(outcomes)

    def fake_load(self):
        self.model = object()

    def fake_speak(self, text, voice, mode):
        outcome = pending.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(tts.Engine, "_load", fake_load)
    monkeypatch.setattr(tts.Engine, "_speak", fake_speak)
    engine.start()
    return engine


def settle(future):
    try:
        return future.result(timeout=5)
    except Exception as exc:
        return exc


def test_three_failed_sentences_in_a_row_mark_the_engine_failed(tmp_path, monkeypatch):
    ok = np.ones(4, dtype=np.float32)
    engine = run_engine(tmp_path, monkeypatch, [
        RuntimeError("CUDA error: an illegal memory access"), tts.BadRequest("no voice 'x'"),
        RuntimeError("CUDA error"), ok,                                  # a success resets the count
        RuntimeError("CUDA error"), RuntimeError("CUDA error"), RuntimeError("CUDA error: unspecified launch failure"),
    ])
    for _ in range(4):
        settle(engine.submit("Gemi.", "owner", "ultimate"))
    assert engine.ready and engine.error is None and engine.failures == 0
    for _ in range(3):
        settle(engine.submit("Gemi.", "owner", "ultimate"))
    assert not engine.ready
    assert engine.error.startswith("3 sentences failed in a row") and "unspecified launch failure" in engine.error


def test_a_failed_engine_answers_503_failed(serve):
    client = serve(FakeEngine(ready=False, error="3 sentences failed in a row; the last: RuntimeError: CUDA"))
    assert (client.get("/health").status_code, client.get("/health").text) == (503, "failed")
    reply = client.post("/tts", json={"text": "Gemi."}, headers=AUTH)
    assert reply.status_code == 503 and "has failed" in reply.json()["message"]


def test_a_timed_out_sentence_is_cancelled(serve, monkeypatch):
    monkeypatch.setattr(tts, "SYNTH_TIMEOUT_S", 0.2)
    stuck = concurrent.futures.Future()

    class StuckEngine(FakeEngine):
        def submit(self, text, voice, mode):
            return stuck

    engine = StuckEngine()
    reply = serve(engine).post("/tts", json={"text": "Gemi."}, headers=AUTH)
    assert reply.status_code == 504 and stuck.cancelled() and engine.timeouts == 1


def test_timeouts_count_as_failures_and_a_success_resets_them(tmp_path, monkeypatch):
    engine = run_engine(tmp_path, monkeypatch, [np.ones(4, dtype=np.float32)])
    engine.note_timeout()
    engine.note_timeout()
    settle(engine.submit("Gemi.", "owner", "ultimate"))  # a success resets the count
    assert engine.ready and engine.failures == 0
    for _ in range(3):
        engine.note_timeout()  # a stuck kernel: nothing raises, every sentence times out
    assert not engine.ready
    assert engine.error == f"3 sentences failed in a row; the last: no audio within {tts.SYNTH_TIMEOUT_S:.0f} s"


def test_a_sentence_times_out_before_the_mac_gives_up_on_it():
    from dubber.engines.voice_pc import PcVoice

    assert tts.SYNTH_TIMEOUT_S < PcVoice("http://pc.test:8191", "k")._client.timeout.read


def test_each_sentence_is_capped_by_its_length(tmp_path, monkeypatch):
    import sys
    import types

    seeds = []
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(manual_seed=seeds.append))  # torch lives on the PC
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "owner.wav").write_bytes(b"")
    (tmp_path / "voices" / "owner.txt").write_text("Merhaba.", encoding="utf-8")
    calls = []

    class Model:
        def generate(self, **options):
            calls.append(options)
            return np.ones(10, dtype=np.float32)

    engine = tts.Engine(tmp_path)
    engine.model = Model()
    engine._speak("x" * 100, "owner", "ultimate")
    assert calls[0]["max_len"] == 20 + 150 and calls[0]["retry_badcase"] is False
    assert calls[0]["prompt_text"] == "Merhaba." and calls[0]["reference_wav_path"].endswith("owner.wav")
    assert seeds == [tts.SEED]
    assert tts.max_steps("") == 20 and tts.max_steps("x" * 10000) == 4096


def test_the_server_refuses_to_share_its_port():
    assert tts._Server.allow_reuse_address is False


# Opus on the wire (design of 2026-11-14, 4.8): a WAV is 96 KB for each second of speech, which
# from far away was about 0.9 s of every request. Ogg Opus is about a twelfth of that.
def tone(seconds=3.0):
    return (0.3 * np.sin(2 * np.pi * 220 * np.arange(int(48000 * seconds)) / 48000)).astype(np.float32)


def test_tts_answers_ogg_opus_when_asked_and_it_is_far_smaller(serve):
    engine = FakeEngine(samples=tone())
    client = serve(engine)
    wav = client.post("/tts", json={"text": "Gemi devasa."}, headers=AUTH)
    opus = client.post("/tts", json={"text": "Gemi devasa.", "format": "opus"}, headers=AUTH)
    assert opus.status_code == 200 and opus.headers["Content-Type"] == "audio/ogg"
    assert len(opus.content) < len(wav.content) / 5
    data, rate = sf.read(io.BytesIO(opus.content), dtype="float32")
    assert rate == 48000 and abs(len(data) - 144000) <= 48   # the clip's length, within a millisecond
    assert float(np.sqrt(np.mean(data**2))) == pytest.approx(0.3 / np.sqrt(2), rel=0.1)   # and its loudness
    assert float(opus.headers["X-Audio-Seconds"]) == pytest.approx(3.0)
    assert engine.requests == [("Gemi devasa.", "owner", "ultimate")] * 2   # the engine is asked the same


def test_tts_without_a_format_or_with_wav_answers_the_same_wav(serve):
    client = serve(FakeEngine(samples=tone(0.5)))
    plain = client.post("/tts", json={"text": "Gemi."}, headers=AUTH)
    named = client.post("/tts", json={"text": "Gemi.", "format": "wav"}, headers=AUTH)
    assert plain.headers["Content-Type"] == named.headers["Content-Type"] == "audio/wav"
    assert plain.content == named.content == tts.wav_bytes(tone(0.5), 48000)


@pytest.mark.parametrize("fmt", ["mp3", "", 3, None])
def test_tts_refuses_a_format_it_does_not_know(serve, fmt):
    response = serve(FakeEngine()).post("/tts", json={"text": "Gemi.", "format": fmt}, headers=AUTH)
    assert response.status_code == 400 and response.json()["message"] == "format must be one of wav, opus"


def test_status_lists_the_formats_it_can_answer_in(serve):
    assert serve(FakeEngine()).get("/status", headers=AUTH).json()["formats"] == ["wav", "opus"]


def test_a_machine_that_cannot_write_opus_answers_wav_and_says_so(serve, monkeypatch):
    monkeypatch.setattr(tts, "opus_bytes", lambda samples, sample_rate: None)
    monkeypatch.setattr(tts, "_formats", None)   # found out anew
    client = serve(FakeEngine(samples=tone(0.5)))
    assert client.get("/status", headers=AUTH).json()["formats"] == ["wav"]
    response = client.post("/tts", json={"text": "Gemi.", "format": "opus"}, headers=AUTH)
    assert response.status_code == 200 and response.headers["Content-Type"] == "audio/wav"
    assert response.content == tts.wav_bytes(tone(0.5), 48000)


def test_the_macs_voice_and_this_service_agree_on_opus(serve):
    from dubber.engines.voice_pc import PcVoice

    engine = FakeEngine(samples=tone())
    client = serve(engine)
    voice = PcVoice(str(client.base_url), KEY)
    clip = voice.synthesize("Gemi devasa.")
    assert clip.sample_rate == 48000 and clip.seconds == pytest.approx(3.0, abs=0.001)
    assert float(np.sqrt(np.mean(clip.samples**2))) == pytest.approx(0.3 / np.sqrt(2), rel=0.1)
    assert engine.requests == [("Gemi devasa.", "owner", "ultimate")]
