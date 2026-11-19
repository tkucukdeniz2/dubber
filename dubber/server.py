"""The loopback WebSocket server: one session for each connection of the extension.

The extension sends JSON text frames. Each is decoded and handed to the connection's
`Session.handle` on the event loop. The session answers through `send`, which is called
from the event loop and from the producer thread. It never blocks: it puts the message
on the connection's queue with `call_soon_threadsafe`, and one writer task sends the
queued messages in order. From Start until Stop, or until the connection ends, a
producer thread runs `Session.run_producer`.

Any web page can open a WebSocket to the loopback address, so a connection is accepted
only without an Origin header (a local tool) or from the extension.
"""

import asyncio
import contextlib
import http
import logging
import threading
from collections.abc import Callable

from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as _listen
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from dubber.config import Config
from dubber.engines.base import IdentityTranslator
from dubber.engines.voice_say import SayVoice
from dubber.metrics import MetricsLog
from dubber.playback import AudioOut
from dubber.protocol import (
    Captions,
    Hello,
    Message,
    ProtocolError,
    Seek,
    Start,
    State,
    Stop,
    decode,
    encode,
)
from dubber.session import Session

log = logging.getLogger(__name__)

Send = Callable[[Message], None]
SessionFactory = Callable[[Config, Send], Session]

_EXTENSION_ORIGIN = "chrome-extension://"
# A 20-minute video's English auto-captions are about 216 KB of JSON, 11 KB for each
# minute of video: the library's default of 1 MiB would close the socket on the
# Captions of a video longer than about 95 minutes.
_MAX_FRAME_BYTES = 16 * 1024 * 1024
# How long to wait for a producer thread to end; it may be finishing a translation.
_JOIN_TIMEOUT = 2.0
# How many characters of a frame that is not a message are logged.
_LOGGED_FRAME_CHARS = 200


def default_session_factory(metrics: MetricsLog) -> SessionFactory:
    """The Phase 1 wiring: the English text read aloud by `say`, a mixer for each connection.

    `metrics` is shared by all the sessions. The factory is called once for each
    connection. `serve` uses this when it is given no factory; `dubber serve` gives it
    one that builds the engines from the config instead (see dubber.cli).
    """

    def make(cfg: Config, send: Send) -> Session:
        return Session(IdentityTranslator(), SayVoice(cfg.voice.voice), AudioOut(), metrics, cfg, send)

    return make


async def serve(cfg: Config, session_factory: SessionFactory | None = None) -> Server:
    """Listen on `cfg.server.host` and `cfg.server.port`; return the running server.

    Every connection gets the session `session_factory(cfg, send)` makes, where `send`
    queues a message for the extension and may be called from any thread. The default
    factory is `default_session_factory` with one MetricsLog for the whole server.
    Raises OSError if the address cannot be bound. The caller stops the server with
    `close()` and `wait_closed()`, which also ends every connection's producer thread.
    """
    if session_factory is None:
        session_factory = default_session_factory(MetricsLog(cfg.metrics_path))

    async def serve_connection(ws: ServerConnection) -> None:
        await _Connection(ws, cfg, session_factory).run()

    return await _listen(
        serve_connection,
        cfg.server.host,
        cfg.server.port,
        process_request=_check_origin,
        max_size=_MAX_FRAME_BYTES,
    )


def _check_origin(ws: ServerConnection, request: Request) -> Response | None:
    """Answer 403 to a page that is not the extension, before the handshake completes."""
    origins = request.headers.get_all("Origin")
    if all(origin.startswith(_EXTENSION_ORIGIN) for origin in origins):
        return None  # no Origin at all, or the extension's
    log.warning("refused a connection from origin %s", ", ".join(origins))
    return ws.respond(http.HTTPStatus.FORBIDDEN, "Only the Dubber extension may connect.\n")


def _event_count(json3: object) -> int | str:
    """The number of events of a caption track, or "?" when it has no list of events."""
    events = json3.get("events") if isinstance(json3, dict) else None
    return len(events) if isinstance(events, list) else "?"


class _Connection:
    """One connection: its session, its queue of outgoing messages and its producer thread."""

    def __init__(self, ws: ServerConnection, cfg: Config, session_factory: SessionFactory) -> None:
        self._ws = ws
        self._loop = asyncio.get_running_loop()
        self._outbox: asyncio.Queue[Message] = asyncio.Queue()
        self._producer: tuple[threading.Thread, threading.Event] | None = None
        self._binary_seen = False
        self._hello_seen = False
        # The paused flag and rate of the last state report logged, None after a Start.
        self._player: tuple[bool, float] | None = None
        self._session = session_factory(cfg, self._send)

    def _send(self, msg: Message) -> None:
        """Queue `msg` for the writer task. Any thread may call this; it never blocks."""
        # A closed loop raises RuntimeError: the server is gone and nobody is listening.
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._outbox.put_nowait, msg)

    async def run(self) -> None:
        """Serve the connection until it ends, then clean up after it."""
        request = self._ws.request
        origin = (request.headers.get("Origin") if request is not None else None) or "none"
        log.info("connection open (origin %s)", origin)
        writer = asyncio.create_task(self._write())
        try:
            async for frame in self._ws:
                await self._receive(frame)
        except ConnectionClosed:
            pass  # the client went away without a closing handshake
        finally:
            await self._cleanup(writer)
            log.info("connection closed (origin %s)", origin)

    async def _write(self) -> None:
        """Send the queued messages one at a time, in the order they were queued."""
        while True:
            msg = await self._outbox.get()
            try:
                text = encode(msg)
                log.debug("send %s", text)
                await self._ws.send(text)
            except ConnectionClosed:
                return
            except Exception:
                log.exception("could not send a %s message", msg.type)

    async def _receive(self, frame: str | bytes) -> None:
        """Handle one frame; a frame that fails leaves the connection open."""
        if isinstance(frame, bytes):
            if not self._binary_seen:
                self._binary_seen = True
                log.info("ignoring binary frames: the extension sends JSON text")
            return
        try:
            msg = decode(frame)
        except ProtocolError as exc:
            log.warning("bad frame (%.100s): %s", exc, frame[:_LOGGED_FRAME_CHARS])
            return
        self._log(msg)
        try:
            self._session.handle(msg)
        except Exception:
            log.exception("the session failed on a %s message", msg.type)
        match msg:
            case Start():
                self._start_producer()
            case Stop():
                await self._join(self._signal_producer())

    def _log(self, msg: Message) -> None:
        """Log what arrived: the messages that matter at INFO, the rest at DEBUG."""
        match msg:
            case Hello():
                # The service worker sends one every 20 s to keep the connection awake:
                # only the first of a connection is worth a line at INFO.
                first = not self._hello_seen
                self._hello_seen = True
                log.log(
                    logging.INFO if first else logging.DEBUG,
                    "hello from %.80s, version %.40s", msg.client, msg.version,
                )
            case Start():
                self._player = None
                log.info("start, mode %s", msg.mode)
            case Stop():
                log.info("stop")
            case Seek():
                log.info("seek to %s s", msg.current_time)
            case Captions():
                log.info(
                    "captions for %.40s: %.20s, %.20s, %s events",
                    msg.video_id, msg.kind, msg.lang, _event_count(msg.json3),
                )
            case State():
                # Reports come four times a second: only the first one after a start
                # and the changes of the paused flag or the rate are worth a line.
                changed = (msg.paused, msg.rate) != self._player
                self._player = (msg.paused, msg.rate)
                log.log(
                    logging.INFO if changed else logging.DEBUG,
                    "state: %s at %s s, rate %s",
                    "paused" if msg.paused else "playing", msg.current_time, msg.rate,
                )
            case _:
                log.debug("received a %s message", msg.type)

    def _start_producer(self) -> None:
        """Start the producer thread, unless one is running."""
        if self._producer is not None:
            return
        # Each run gets its own event, so that a thread that outlived its join stays stopped.
        stop = threading.Event()
        thread = threading.Thread(
            target=self._session.run_producer, args=(stop,), name="dubber-producer", daemon=True
        )
        self._producer = (thread, stop)
        thread.start()

    def _signal_producer(self) -> threading.Thread | None:
        """Tell the producer thread to end; return it, so that the caller can wait for it."""
        if self._producer is None:
            return None
        thread, stop = self._producer
        self._producer = None
        stop.set()
        return thread

    @staticmethod
    async def _join(thread: threading.Thread | None) -> None:
        """Wait for a producer thread that was told to end, without blocking the event loop."""
        if thread is None:
            return
        await asyncio.to_thread(thread.join, _JOIN_TIMEOUT)
        if thread.is_alive():
            log.warning("the producer thread is still busy with a segment; it ends when that is done")

    async def _cleanup(self, writer: asyncio.Task[None]) -> None:
        """End the producer, stop the session so that the mixer closes its stream, drop the writer.

        What cannot wait comes before the first await, so that a cancelled handler
        still does it.
        """
        producer = self._signal_producer()
        try:
            self._session.handle(Stop())
        except Exception:
            log.exception("stopping the session failed")
        writer.cancel()
        await asyncio.wait([writer])
        await self._join(producer)
