#!/usr/bin/env python3
"""dubber-tts: the dubber's voice on qwen-pc's RTX 4080 SUPER (VoxCPM2 with a cloned voice).

The Mac's dubber sends each Turkish sentence here and plays the WAV it gets back.
HTTP on 127.0.0.1:8191; `tailscale serve` makes it reachable on the tailnet only.
Key: the LLM server's API key (the first line of C:\\AI\\config\\api-keys.txt), sent as
"Authorization: Bearer <key>" or "x-api-key: <key>".

  GET  /health   no key; "ok" once the model is ready; 503 "loading" while it loads, 503 "failed"
                 when it did not load or when sentences keep failing (a dead CUDA context)
  GET  /status   {"ok", "ready", "error", "voices", "sample_rate", "loaded_in_s", "gpu_memory_gb",
                  "idle_s": seconds since the last /tts request, or since the model became ready,
                  "formats": what /tts can answer in, ["wav", "opus"] or ["wav"]}
  POST /tts      {"text": str, "voice": "owner", "mode": "ultimate" | "reference", "format": "wav" | "opus"}
                 -> audio/wav (mono PCM16) or, for "opus", audio/ogg (Ogg Opus, about a twelfth of the
                 size, which matters to a Mac far away), with X-Synth-Seconds and X-Audio-Seconds.
                 "format" defaults to "wav". Where Opus cannot be written the answer is the WAV.

Voices live in <base>\\voices: <name>.wav and, for "ultimate", its transcript <name>.txt.
"ultimate" clones from the clip and its transcript, the closest match; "reference" from
the clip alone. Every sentence runs with the same seed, so a sentence always comes out
the same. The model is compiled when the service starts (Triton): about 2.5 minutes the
first time, 30 to 80 s once the compile cache (C:\\AI\\tts\\inductor-cache) holds the
compiled code. Until then /health answers 503.

All GPU work runs on one worker thread: the compiled model records its CUDA graphs per
thread, and the HTTP server answers each connection on a thread of its own.
"""

import concurrent.futures
import hmac
import io
import json
import logging
import os
import pathlib
import queue
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

BASE = pathlib.Path(os.environ.get("DUBBER_TTS_BASE", r"C:\AI\tts"))
KEY_FILE = pathlib.Path(os.environ.get("DUBBER_TTS_KEY_FILE", r"C:\AI\config\api-keys.txt"))
HOST = os.environ.get("DUBBER_TTS_HOST", "127.0.0.1")
PORT = int(os.environ.get("DUBBER_TTS_PORT", "8191"))

MODES = ("ultimate", "reference")
FORMATS = ("wav", "opus")
MAX_TEXT_CHARS = 1000
MAX_BODY_BYTES = 64 * 1024
# Under the Mac's 60 s per sentence (dubber/engines/voice_pc.py), so the Mac hears why it got nothing.
SYNTH_TIMEOUT_S = 55.0
SEED = 42
FAILURES_TO_GIVE_UP = 3  # sentences failing in a row (not bad requests): the GPU state is broken
# One generation step is 160 ms of speech (4 latent frames of 640 samples at 16 kHz). Turkish runs at
# about 14 characters a second, so this allows some 3.5 times the expected length before cutting off.
STEPS_BASE, STEPS_PER_CHAR = 20, 1.5
_VOICE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

log = logging.getLogger("dubber-tts")


class BadRequest(ValueError):
    """A request the service refuses; the message says why, and the status is 400."""


def read_key(path: pathlib.Path) -> str:
    """The first line of `path` that is neither blank nor a # comment."""
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    raise RuntimeError(f"no API key in {path}")


def wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """`samples` (float, -1..1) as a mono 16-bit WAV file."""
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def opus_bytes(samples: np.ndarray, sample_rate: int) -> bytes | None:
    """`samples` (float, -1..1) as a mono Ogg Opus file; None when this machine cannot write one.

    soundfile is imported here: the service must start, and answer WAV, where it is missing
    or its libsndfile is too old for Opus. Opus takes 8, 12, 16, 24 and 48 kHz only.
    """
    try:
        import soundfile

        buf = io.BytesIO()
        clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        soundfile.write(buf, clipped, int(sample_rate), format="OGG", subtype="OPUS")
        return buf.getvalue()
    except Exception:
        return None


_formats: list[str] | None = None  # found out once, by writing a few samples


def supported_formats() -> list[str]:
    """What /tts can answer in: ["wav", "opus"], or ["wav"] where Opus cannot be written."""
    global _formats
    if _formats is None:
        can = opus_bytes(np.zeros(480, dtype=np.float32), 48000) is not None
        _formats = list(FORMATS) if can else ["wav"]
    return _formats


def audio_format(body: bytes) -> str:
    """The "format" of a /tts body that parse_request has accepted: "wav" when it names none."""
    fmt = json.loads(body.decode("utf-8")).get("format", "wav")
    if not isinstance(fmt, str) or fmt not in FORMATS:
        raise BadRequest(f"format must be one of {', '.join(FORMATS)}")
    return fmt


def max_steps(text: str) -> int:
    """The longest generation allowed for `text`, so a runaway sentence cannot hold the GPU for minutes."""
    return min(4096, STEPS_BASE + int(STEPS_PER_CHAR * len(text)))


class _Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy bind a port the first is listening on.
    allow_reuse_address = False


class Engine:
    """VoxCPM2 behind one worker thread that loads the model and then speaks the queued sentences.

    A sentence that fails for any reason other than a bad request counts, and so does one that gets
    no audio within SYNTH_TIMEOUT_S (a stuck kernel raises nothing); FAILURES_TO_GIVE_UP of them in
    a row mean the GPU state is broken (a driver reset leaves the process alive with a dead CUDA
    context), so the engine reports itself failed and /health turns 503: the mode service then
    leaves dubbing mode, and `pc-dublaj` starts a fresh service.
    """

    def __init__(self, base: pathlib.Path):
        self.base = pathlib.Path(base)
        self.model = None
        self.sample_rate = 48000
        self.loaded_in_s: float | None = None
        self.error: str | None = None
        self.failures = 0
        self._failures_lock = threading.Lock()  # the worker and the request threads both count
        self.last_used = time.monotonic()
        self._jobs: queue.Queue = queue.Queue()

    @property
    def ready(self) -> bool:
        """Loaded and not given up: sentences can be spoken."""
        return self.model is not None and self.error is None

    def start(self) -> None:
        threading.Thread(target=self._work, name="gpu-worker", daemon=True).start()

    def voices(self) -> list[str]:
        return sorted(p.stem for p in (self.base / "voices").glob("*.wav") if _VOICE_NAME.fullmatch(p.stem))

    def idle_s(self) -> float:
        """Seconds since the last sentence was asked for, or since the model became ready."""
        return time.monotonic() - self.last_used

    def submit(self, text: str, voice: str, mode: str) -> concurrent.futures.Future:
        """Queue one sentence; the future gives its samples, or raises BadRequest or the model's error."""
        self.last_used = time.monotonic()
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((text, voice, mode, future))
        return future

    def gpu_memory_gb(self) -> float | None:
        try:
            import torch

            return round(torch.cuda.max_memory_allocated() / 1e9, 2) if torch.cuda.is_available() else None
        except Exception:
            return None

    def _work(self) -> None:
        try:
            self._load()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("the model did not load")
            return
        while True:
            text, voice, mode, future = self._jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                samples = self._speak(text, voice, mode)
            except BadRequest as exc:
                future.set_exception(exc)
                continue
            except BaseException as exc:
                self._count_failure(f"{type(exc).__name__}: {exc}")
                future.set_exception(exc)
                continue
            with self._failures_lock:
                self.failures = 0
            future.set_result(samples)

    def note_timeout(self) -> None:
        """A sentence got no audio in time: it counts as a failure, since a stuck kernel raises nothing."""
        self._count_failure(f"no audio within {SYNTH_TIMEOUT_S:.0f} s")

    def _count_failure(self, last: str) -> None:
        with self._failures_lock:
            self.failures += 1
            if self.failures >= FAILURES_TO_GIVE_UP and self.error is None:
                self.error = f"{self.failures} sentences failed in a row; the last: {last}"
                log.error("giving up: %s", self.error)

    def _load(self) -> None:
        import torch
        from voxcpm import VoxCPM

        torch.set_float32_matmul_precision("high")
        began = time.perf_counter()
        log.info("loading and compiling VoxCPM2 (30 s to 2.5 minutes)")
        self.model = VoxCPM.from_pretrained(str(self.base / "models" / "VoxCPM2"), load_denoiser=False)
        self.sample_rate = int(self.model.tts_model.sample_rate)
        self.loaded_in_s = round(time.perf_counter() - began, 1)
        self.last_used = time.monotonic()
        log.info("ready in %.1f s; voices: %s", self.loaded_in_s, ", ".join(self.voices()) or "none")

    def _speak(self, text: str, voice: str, mode: str) -> np.ndarray:
        import torch

        clip = self.base / "voices" / f"{voice}.wav"
        if not clip.is_file():
            raise BadRequest(f"no voice {voice!r}; there are: {', '.join(self.voices()) or 'none'}")
        options = dict(text=text, cfg_value=2.0, inference_timesteps=10, retry_badcase=False, max_len=max_steps(text))
        if mode == "ultimate":
            transcript = clip.with_suffix(".txt")
            if not transcript.is_file():
                raise BadRequest(f"voice {voice!r} has no transcript {voice}.txt, which mode ultimate needs")
            options.update(
                prompt_wav_path=str(clip),
                prompt_text=transcript.read_text(encoding="utf-8").strip(),
                reference_wav_path=str(clip),
            )
        else:
            options.update(reference_wav_path=str(clip))
        torch.manual_seed(SEED)
        np.random.seed(SEED)
        return np.asarray(self.model.generate(**options), dtype=np.float32).reshape(-1)


def parse_request(body: bytes) -> tuple[str, str, str]:
    """The text, voice and mode of a /tts body; BadRequest for anything else."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BadRequest("the body is not JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("the body must be a JSON object")
    text, voice, mode = data.get("text"), data.get("voice", "owner"), data.get("mode", "ultimate")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("text must be a non-empty string")
    if len(text) > MAX_TEXT_CHARS:
        raise BadRequest(f"text is longer than {MAX_TEXT_CHARS} characters")
    if not isinstance(voice, str) or not _VOICE_NAME.fullmatch(voice):
        raise BadRequest("voice must be a name of letters, digits, - and _")
    if mode not in MODES:
        raise BadRequest(f"mode must be one of {', '.join(MODES)}")
    return text.strip(), voice, mode


def make_handler(engine, key: str):
    """The request handler class for `engine` (anything with ready, error, voices(), submit())."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "dubber-tts"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            got = self.headers.get("x-api-key") or ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            return bool(got) and hmac.compare_digest(got.encode("utf-8"), key.encode("utf-8"))

        def _not_ready(self) -> None:
            message = f"the voice service has failed: {engine.error}" if engine.error else "the model is still loading"
            self._json(503, {"ok": False, "message": message})

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/health":
                if engine.ready:
                    return self._send(200, b"ok", "text/plain; charset=utf-8")
                return self._send(503, b"failed" if engine.error else b"loading", "text/plain; charset=utf-8")
            if not self._authorized():
                return self._json(401, {"ok": False, "message": "API key required"})
            if path == "/status":
                return self._json(200, {
                    "ok": True,
                    "ready": engine.ready,
                    "error": engine.error,
                    "voices": engine.voices(),
                    "sample_rate": engine.sample_rate,
                    "loaded_in_s": engine.loaded_in_s,
                    "gpu_memory_gb": engine.gpu_memory_gb(),
                    "idle_s": round(engine.idle_s(), 1),
                    "formats": supported_formats(),
                })
            self._json(404, {"ok": False, "message": "unknown path"})

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            if path != "/tts":
                return self._json(404, {"ok": False, "message": "unknown path"})
            if not self._authorized():
                return self._json(401, {"ok": False, "message": "API key required"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                return self._json(413 if length > MAX_BODY_BYTES else 400, {"ok": False, "message": "bad body length"})
            body = self.rfile.read(length)
            if not engine.ready:
                return self._not_ready()
            began = time.perf_counter()
            future = None
            try:
                text, voice, mode = parse_request(body)
                fmt = audio_format(body)
                future = engine.submit(text, voice, mode)
                samples = future.result(timeout=SYNTH_TIMEOUT_S)
            except BadRequest as exc:
                return self._json(400, {"ok": False, "message": str(exc)})
            except concurrent.futures.TimeoutError:
                future.cancel()  # still queued: the worker skips it; already running: it finishes unheard
                engine.note_timeout()
                return self._json(504, {"ok": False, "message": f"no audio within {SYNTH_TIMEOUT_S:.0f} s"})
            except Exception as exc:
                log.exception("synthesis failed")
                return self._json(500, {"ok": False, "message": f"synthesis failed: {type(exc).__name__}: {exc}"})
            if samples.size == 0:
                return self._json(500, {"ok": False, "message": "the model produced no audio"})
            took = time.perf_counter() - began
            seconds = samples.size / engine.sample_rate
            log.info("%.1f s of audio in %.2f s (%s, %s): %.60s", seconds, took, voice, mode, text)
            payload = opus_bytes(samples, engine.sample_rate) if fmt == "opus" else None
            content_type = "audio/ogg"
            if payload is None:  # WAV was asked for, or Opus cannot be written here
                payload, content_type = wav_bytes(samples, engine.sample_rate), "audio/wav"
            self._send(200, payload, content_type, {
                "X-Synth-Seconds": f"{took:.3f}",
                "X-Audio-Seconds": f"{seconds:.3f}",
            })

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    key = read_key(KEY_FILE)
    engine = Engine(BASE)
    server = _Server((HOST, PORT), make_handler(engine, key))
    engine.start()
    log.info("listening on http://%s:%d", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
