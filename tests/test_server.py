import asyncio, json, pytest, websockets
from dubber.config import Config
from dubber.server import serve

@pytest.mark.asyncio
async def test_hello_gets_idle_status_and_start_gets_duck():
    cfg = Config(); cfg.server.port = 0
    server = await serve(cfg)
    try:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "hello", "client": "test", "version": "0"}))
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))["state"] == "idle"
            await ws.send(json.dumps({"type": "start", "mode": None}))
            types = {json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] for _ in range(2)}
            assert "duck" in types and "control" in types
    finally:
        server.close(); await server.wait_closed()

# What the test above leaves open (task 7 resolutions): the Origin check, the frame size,
# bad frames, messages sent from the producer thread, the producer thread's life, the
# disconnect and the logging. Each rig listens on a port the system picks and plays to a
# fake stream, so no test opens an audio device.
import contextlib
import logging
import threading
import time

import numpy as np
from websockets.exceptions import InvalidStatus

from dubber.engines.base import Clip, IdentityTranslator
from dubber.metrics import MetricsLog
from dubber.playback import AudioOut
from dubber.protocol import Hello, Start, Status, Stop, Subtitle
from dubber.server import default_session_factory
from dubber.session import Session

HELLO = {"type": "hello", "client": "test", "version": "0"}
START = {"type": "start", "mode": None}
STATE = {"type": "state", "video_id": "v", "current_time": 1.0, "rate": 1.0, "paused": True, "live": False, "volume": 0.8}


def track(*spans):
    """json3 with one six-word sentence per (start, end) span: one segment each."""
    return {"events": [
        {"tStartMs": round(a * 1000), "dDurationMs": round((b - a) * 1000),
         "segs": [{"utf8": f"This is sentence number {i} here."}]}
        for i, (a, b) in enumerate(spans)
    ]}


class FakeVoice:
    """1 s clips."""
    name = "fake"
    def synthesize(self, text, speed=1.0):
        return Clip(samples=np.zeros(22050, dtype=np.float32), sample_rate=22050)


class BlockingVoice(FakeVoice):
    """A voice that keeps the producer thread inside `synthesize` until it is released."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
    def synthesize(self, text, speed=1.0):
        self.entered.set()
        self.release.wait(10)
        return super().synthesize(text, speed)


class FakeStream:
    def __init__(self):
        self.started = self.stopped = self.closed = False
    def start(self):
        self.started = True
    def stop(self):
        self.stopped = True
    def close(self):
        self.closed = True


class SpySession(Session):
    """A Session that keeps the messages it was given."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handled = []
    def handle(self, msg):
        self.handled.append(msg)
        super().handle(msg)


class Exploding(SpySession):
    """A Session whose handling of a Seek fails."""
    def handle(self, msg):
        super().handle(msg)
        if msg.type == "seek":
            raise RuntimeError("boom")


class Rig:
    """A server on a free port; every connection gets a SpySession on a fake stream."""

    def __init__(self, tmp_path, session_class=SpySession, voice=None):
        self.session_class = session_class
        self.voice = voice or FakeVoice()
        self.sessions, self.sends = [], []
        self.stream = FakeStream()
        self.metrics = MetricsLog(tmp_path / "metrics.jsonl")
        self.server = None
        self.url = ""

    def make_session(self, cfg, send):
        audio = AudioOut(stream_factory=lambda callback: self.stream)
        session = self.session_class(IdentityTranslator(), self.voice, audio, self.metrics, cfg, send)
        self.sessions.append(session)
        self.sends.append(send)
        return session

    async def open(self):
        cfg = Config()
        cfg.server.port = 0
        self.server = await serve(cfg, self.make_session)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    def connect(self, **options):
        return websockets.connect(self.url, **options)


@contextlib.asynccontextmanager
async def running(tmp_path, session_class=SpySession, voice=None):
    rig = Rig(tmp_path, session_class, voice)
    await rig.open()
    try:
        yield rig
    finally:
        await rig.close()


@pytest.fixture
async def rig(tmp_path):
    async with running(tmp_path) as r:
        yield r


async def send(ws, **message):
    await ws.send(json.dumps(message))


async def recv(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the condition"
        await asyncio.sleep(0.01)


def producers():
    return [t for t in threading.enumerate() if t.name == "dubber-producer" and t.is_alive()]


# Who may connect.

@pytest.mark.parametrize("origin", [
    "https://example.com",
    "http://127.0.0.1:8765",  # a page on this machine is still a web page
    "null",
    "moz-extension://abc",
    "https://chrome-extension://abc",
])
async def test_an_origin_that_is_not_the_extension_is_refused_with_403(rig, origin):
    with pytest.raises(InvalidStatus) as refused:
        async with rig.connect(origin=origin):
            pass
    assert refused.value.response.status_code == 403
    assert rig.sessions == []


async def test_the_extension_origin_is_accepted(rig):
    async with rig.connect(origin="chrome-extension://abc") as ws:
        await send(ws, **HELLO)
        assert (await recv(ws))["state"] == "idle"


# What may arrive.

async def test_a_two_mebibyte_text_frame_is_accepted(rig):
    async with rig.connect() as ws:
        await send(ws, type="hello", client="x" * 2 * 1024 * 1024, version="0")
        assert (await recv(ws))["type"] == "status"


async def test_bad_frames_leave_the_connection_usable(rig, caplog):
    caplog.set_level(logging.INFO, logger="dubber.server")
    async with rig.connect() as ws:
        await ws.send("{" + "a" * 499)  # not JSON
        await send(ws, type="nonsense")
        await ws.send(b"\x00\x01")
        await ws.send(b"\x02")
        await send(ws, **HELLO)
        assert (await recv(ws))["state"] == "idle"
    messages = [r.getMessage() for r in caplog.records if r.name == "dubber.server"]
    # The malformed frame is logged by its first 200 characters, not all 500.
    assert any("{" + "a" * 199 in m and "a" * 200 not in m for m in messages)
    assert any("nonsense" in m for m in messages)
    assert sum("binary" in m for m in messages) == 1
    assert rig.sessions[0].handled == [Hello("test", "0")]


async def test_an_error_in_the_session_is_logged_and_the_connection_stays_open(tmp_path, caplog):
    async with running(tmp_path, Exploding) as rig, rig.connect() as ws:
        await send(ws, type="seek", current_time=5.0)
        await send(ws, **HELLO)
        assert (await recv(ws))["state"] == "idle"
    failures = [r for r in caplog.records if r.exc_info]
    assert len(failures) == 1 and failures[0].exc_info[0] is RuntimeError


# What goes out.

async def test_messages_sent_from_another_thread_arrive_in_order(rig):
    async with rig.connect() as ws:
        await send(ws, **HELLO)
        await recv(ws)
        session_send = rig.sends[0]

        def burst():
            for i in range(300):
                session_send(Subtitle(f"s{i}", float(i), i + 1.0))

        worker = asyncio.create_task(asyncio.to_thread(burst))
        for _ in range(3):  # the loop thread sends too, while the other thread does
            await send(ws, **HELLO)
        frames = [await recv(ws) for _ in range(303)]
        await worker
    assert [f["text"] for f in frames if f["type"] == "subtitle"] == [f"s{i}" for i in range(300)]
    assert sum(f["type"] == "status" for f in frames) == 3


async def test_each_connection_gets_its_own_session(rig):
    async with rig.connect() as a, rig.connect() as b:
        for ws in (a, b):
            await send(ws, **HELLO)
            await recv(ws)
        assert len(rig.sessions) == 2 and rig.sessions[0] is not rig.sessions[1]


# The producer thread.

async def test_start_runs_a_producer_thread_and_stop_ends_it(rig):
    assert producers() == []
    async with rig.connect() as ws:
        await send(ws, **START)
        await wait_until(lambda: len(producers()) == 1)
        assert producers()[0].daemon  # a stuck producer must not keep the process alive
        await send(ws, type="stop")
        await wait_until(lambda: producers() == [])
        assert rig.sessions[0].state == "idle"


async def test_a_second_start_adds_no_thread_and_a_start_after_stop_makes_a_new_one(rig):
    async with rig.connect() as ws:
        await send(ws, **START)
        await wait_until(lambda: len(producers()) == 1)
        first = producers()[0]
        await send(ws, **START)
        await wait_until(lambda: rig.sessions[0].handled.count(Start(None)) == 2)
        assert producers() == [first]
        await send(ws, type="stop")
        await wait_until(lambda: producers() == [])
        await send(ws, **START)
        await wait_until(lambda: len(producers()) == 1)
        assert producers() != [first]


async def test_a_disconnect_stops_the_producer_and_the_mixer(rig):
    async with rig.connect() as ws:
        await send(ws, **START)
        await send(ws, type="captions", video_id="v", lang="en", kind="asr", json3=track((10, 12), (20, 22)))
        while (await recv(ws))["type"] != "subtitle":
            pass  # the producer scheduled a clip, so the mixer's stream is running
        assert rig.stream.started and not rig.stream.closed
        assert len(producers()) == 1
    await wait_until(lambda: rig.stream.closed)
    await wait_until(lambda: producers() == [])
    session = rig.sessions[0]
    assert session.handled[-1] == Stop() and session.state == "idle"


async def test_a_disconnect_without_a_start_still_stops_the_session(rig):
    async with rig.connect() as ws:
        await send(ws, **HELLO)
        await recv(ws)
    await wait_until(lambda: rig.sessions[0].handled[-1:] == [Stop()])


async def hold_the_producer(ws, voice):
    """Start a run whose producer is inside `synthesize`, then stop it."""
    await send(ws, **START)
    await send(ws, type="captions", video_id="v", lang="en", kind="asr", json3=track((10, 12)))
    await wait_until(voice.entered.is_set)
    await send(ws, type="stop")


async def test_waiting_for_a_busy_producer_does_not_block_the_event_loop(tmp_path):
    voice = BlockingVoice()
    gaps = []

    async def watch():
        """Record how long the loop goes between two runs of this task."""
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            gaps.append(time.monotonic() - last)
            last = time.monotonic()

    watcher = asyncio.create_task(watch())
    try:
        async with running(tmp_path, voice=voice) as rig, rig.connect() as a, rig.connect() as b:
            try:
                await hold_the_producer(a, voice)
                await wait_until(lambda: rig.sessions[0].handled[-1:] == [Stop()])
                await send(b, **HELLO)
                # The server is waiting for a's producer (up to 2 s) and still answers b.
                assert (await recv(b))["state"] == "idle"
            finally:
                voice.release.set()
            await wait_until(lambda: producers() == [])
    finally:
        watcher.cancel()
        await asyncio.wait([watcher])
    assert max(gaps) < 0.5, f"the event loop stalled for {max(gaps):.2f} s"


async def test_a_producer_that_outlasts_the_join_still_ends(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("dubber.server._JOIN_TIMEOUT", 0.05)
    voice = BlockingVoice()
    async with running(tmp_path, voice=voice) as rig, rig.connect() as ws:
        try:
            await hold_the_producer(ws, voice)
            await wait_until(lambda: any("still busy" in r.getMessage() for r in caplog.records))
            assert len(producers()) == 1  # the join gave up; the thread is still in synthesize
        finally:
            voice.release.set()
        await wait_until(lambda: producers() == [])  # the stop event was set, so it ends after the segment


async def test_closing_the_server_ends_the_producer_threads(rig):
    async with rig.connect() as ws:
        await send(ws, **START)
        await wait_until(lambda: len(producers()) == 1)
        await rig.close()  # wait_closed returns once every handler has cleaned up
        assert producers() == []
        assert rig.sessions[0].handled[-1] == Stop()


# Logging.

def info_records(caplog, prefix=""):
    return [r.getMessage() for r in caplog.records
            if r.name == "dubber.server" and r.levelno == logging.INFO and r.getMessage().startswith(prefix)]


async def test_the_messages_that_matter_are_logged_at_info(rig, caplog):
    caplog.set_level(logging.INFO, logger="dubber.server")
    async with rig.connect(origin="chrome-extension://abc") as ws:
        await send(ws, type="hello", client="dubber-ext", version="0.1")
        await send(ws, **START)
        spans = [(10 * i, 10 * i + 2) for i in range(1, 8)]
        await send(ws, type="captions", video_id="XP5k0bHmhqc", lang="en", kind="asr", json3=track(*spans))
        await send(ws, type="seek", current_time=30.0)
        await send(ws, type="stop")
        await wait_until(lambda: len(rig.sessions[0].handled) == 5)
    await wait_until(lambda: info_records(caplog, "connection closed"))
    assert "chrome-extension://abc" in info_records(caplog, "connection open")[0]
    assert "chrome-extension://abc" in info_records(caplog, "connection closed")[0]
    hello = info_records(caplog, "hello")
    assert len(hello) == 1 and "dubber-ext" in hello[0] and "0.1" in hello[0]
    assert len(info_records(caplog, "start")) == 1
    assert len(info_records(caplog, "stop")) == 1
    assert len(info_records(caplog, "seek")) == 1 and "30" in info_records(caplog, "seek")[0]
    (captions,) = info_records(caplog, "captions")
    assert all(part in captions for part in ("XP5k0bHmhqc", "asr", "en", "7"))


async def test_only_the_first_hello_of_a_connection_is_logged_at_info(rig, caplog):
    """The service worker sends a hello every 20 s to keep the connection awake."""
    caplog.set_level(logging.DEBUG, logger="dubber.server")
    async with rig.connect() as first, rig.connect() as second:
        for ws in (first, first, first, second):
            await send(ws, **HELLO)
            await recv(ws)  # the answer comes after the line is logged
    hello = [r.levelno for r in caplog.records if r.name == "dubber.server" and r.getMessage().startswith("hello")]
    assert hello == [logging.INFO, logging.DEBUG, logging.DEBUG, logging.INFO]  # the first of each connection


async def test_a_captions_message_with_no_events_is_still_handled_and_logged(rig, caplog):
    caplog.set_level(logging.INFO, logger="dubber.server")
    async with rig.connect() as ws:
        await send(ws, **START)
        await send(ws, type="captions", video_id="v", lang="en", kind="asr", json3="not a dict")
        await wait_until(lambda: len(rig.sessions[0].handled) == 2)
    assert len(info_records(caplog, "captions")) == 1
    assert not [r for r in caplog.records if r.exc_info]


async def test_state_is_logged_at_info_for_the_first_report_and_for_changes_only(rig, caplog):
    caplog.set_level(logging.INFO, logger="dubber.server")
    reports = [(True, 1.0), (True, 1.0), (False, 1.0), (False, 1.0), (False, 1.25), (False, 1.25)]
    async with rig.connect() as ws:
        await send(ws, **START)
        for paused, rate in reports:
            await send(ws, **{**STATE, "paused": paused, "rate": rate})
        await wait_until(lambda: len(rig.sessions[0].handled) == 1 + len(reports))
        assert len(info_records(caplog, "state")) == 3  # first, paused changed, rate changed
        await send(ws, **START)
        await send(ws, **{**STATE, "paused": False, "rate": 1.25})
        await wait_until(lambda: len(rig.sessions[0].handled) == 3 + len(reports))
    assert len(info_records(caplog, "state")) == 4  # the first one after a new start


async def test_everything_else_is_logged_at_debug(rig, caplog):
    caplog.set_level(logging.DEBUG, logger="dubber.server")
    async with rig.connect() as ws:
        await send(ws, **START)
        await send(ws, **STATE)
        await send(ws, **STATE)
        await wait_until(lambda: len(rig.sessions[0].handled) == 3)
    debug = [r.getMessage() for r in caplog.records if r.name == "dubber.server" and r.levelno == logging.DEBUG]
    assert sum(m.startswith("state") for m in debug) == 1  # the repeat; the first one went to INFO


def test_a_send_after_the_loop_has_closed_is_ignored(tmp_path):
    """The producer thread can outlive the server, e.g. after a second Ctrl-C."""
    async def connect_and_leave():
        async with running(tmp_path) as rig, rig.connect() as ws:
            await send(ws, **HELLO)
            await recv(ws)
            return rig.sends[0]

    late_send = asyncio.run(connect_and_leave())
    late_send(Subtitle("late", 0.0, 1.0))  # must not raise


# The default wiring, which serve uses when it is given no factory. `dubber serve` builds its
# engines from the config instead (tests/test_cli.py).

def test_the_default_factory_wires_the_stand_in_engines(tmp_path):
    sent = []
    session = default_session_factory(MetricsLog(tmp_path / "m.jsonl"))(Config(), sent.append)
    session.handle(Hello("test", "0"))
    assert isinstance(sent[0], Status) and sent[0].state == "idle"
    assert sent[0].engine == "identity+say:Samantha"
