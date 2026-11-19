"""Stand in for the Chrome extension, so that `dubber serve` can run without Chrome.

    uv run python tools/fake_extension.py CAPTIONS.json3 [--start SECONDS] [--seconds SECONDS]
        [--url ws://127.0.0.1:8765] [--video-id ID]

CAPTIONS.json3 is a YouTube caption file, for instance one of the Phase 0 files in
bakeoff/captions/. The tool says hello, starts a session and sends the captions, as the
extension does. Then it plays a pretend video: every 250 ms it reports the player's state,
and the player's time moves in real time from --start while the player plays. The player
starts paused and plays when the server says play, and the server's pause and rate calls
act on it too. The tool prints each status and subtitle the server sends. After --seconds
of video it sends stop, waits up to 5 s for the server to go idle, and prints a summary.
The summary's "held" is the start-up hold, timed the way the server log shows it: from the
first state report of a paused player to the first one of a playing player.

The server plays the voice through this Mac's speakers, as it does for the extension.
The run's lag and drops are in the server's metrics file: `uv run dubber stats`.

The logic is in `run`, `FakePlayer` and `RunResult`, which tests/test_e2e.py drives.
"""

import argparse
import asyncio
import functools
import json
import pathlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from dubber.protocol import (
    Captions,
    Control,
    Duck,
    Hello,
    ProtocolError,
    Start,
    State,
    Status,
    Stop,
    Subtitle,
    decode,
    encode,
)

CLIENT = "fake-extension"
VERSION = "1"
DEFAULT_URL = "ws://127.0.0.1:8765"
# The extension reports the player's state this often, in seconds.
REPORT_EVERY = 0.25
# How long to wait, after the stop, for the server to report that it is idle.
STOP_WAIT = 5.0


class FakePlayer:
    """The video element the extension stands in for.

    It starts paused at `start`. Its time moves in real time, at its rate, only while it
    plays. The server's messages act on it through `apply`: pause, play and rate calls,
    and the duck of the volume, which the next restore undoes exactly.
    """

    def __init__(self, start: float = 0.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._anchor = (start, clock())  # the video time at a wall time, from where it runs on
        self.paused = True
        self.rate = 1.0
        self.volume = 1.0  # what the owner set
        self._owner_volume: float | None = None  # the volume to give back, while ducked

    @property
    def current_time(self) -> float:
        media, wall = self._anchor
        return media if self.paused else media + (self._clock() - wall) * self.rate

    def apply(self, msg: Duck | Control) -> None:
        """Do what the server asks."""
        match msg:
            case Duck(volume=None):
                if self._owner_volume is not None:
                    self.volume, self._owner_volume = self._owner_volume, None
            case Duck(volume=level):
                if self._owner_volume is None:
                    self._owner_volume = self.volume
                self.volume = level
            case Control(action="pause"):
                self._reanchor()
                self.paused = True
            case Control(action="play"):
                self._reanchor()
                self.paused = False
            case Control(action="rate", value=rate) if rate is not None:
                self._reanchor()
                self.rate = rate

    def state(self, video_id: str) -> State:
        """The report the extension sends."""
        return State(video_id, self.current_time, self.rate, self.paused, False, self.volume)

    def _reanchor(self) -> None:
        """Run on from the time it is now, so that a change of rate or pause does not jump."""
        self._anchor = (self.current_time, self._clock())


@dataclass
class RunResult:
    """What a run saw: the server's messages, in order, and how the run ended.

    `played` is the seconds of video the player played. `held` is the start-up hold, in
    seconds, measured as the server log shows it: from the first state report of a
    paused player to the first one of a playing player (the log's first `state: paused`
    line to its next `state: playing` line). It is None if the player never played.
    `stopped` is whether the server reported idle within `stop_wait` of the stop
    message, and `closed` whether the server ended the connection first.
    """

    statuses: list[Status] = field(default_factory=list)
    subtitles: list[Subtitle] = field(default_factory=list)
    ducks: list[Duck] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    played: float = 0.0
    held: float | None = None
    stopped: bool = False
    closed: bool = False

    @property
    def states(self) -> list[str]:
        """The server's states, in order, a state repeated in a row counted once."""
        names = [status.state for status in self.statuses]
        return [name for i, name in enumerate(names) if i == 0 or name != names[i - 1]]


async def run(
    url: str,
    json3: dict,
    *,
    video_id: str = "fake",
    start: float = 0.0,
    seconds: float = 60.0,
    out: Callable[[str], None] = print,
    tick: float = REPORT_EVERY,
    stop_wait: float = STOP_WAIT,
) -> RunResult:
    """Play `seconds` of video from `start` against the server at `url`; return what it saw.

    The extension's order: hello, start, the captions `json3` (English, auto-generated),
    then a state report every `tick` seconds, and after `seconds` of video a stop, which
    the server answers with idle (waited for up to `stop_wait` seconds). Every status,
    subtitle and call of the server goes to `out` as one line, and so do a summary and
    a hint at the end. An OSError, such as a refused connection, is raised.
    """
    replay = _Replay(video_id, start, out)
    async with connect(url, proxy=None) as ws:  # no proxy: this is the loopback address
        out(f"fake extension: connected to {url}, video {video_id}, {seconds:g} s of video from {start:g} s")
        await replay.talk(ws, json3, seconds, tick, stop_wait)
    out(f"summary: {_summary(replay.result, start, stop_wait)}")
    out("see the lag and drops of this run: uv run dubber stats")
    return replay.result


class _Replay:
    """One run: the pretend player and the result, shared by the reporting and the listening."""

    def __init__(self, video_id: str, start: float, out: Callable[[str], None]) -> None:
        self.video_id, self.start, self.out = video_id, start, out
        self.player = FakePlayer(start)
        self.result = RunResult()
        self._paused_since: float | None = None  # when the first report of a paused player went out
        self._stopping = False  # the stop message is sent, so an idle status answers it
        self._idle = asyncio.Event()  # set by that idle status

    async def talk(
        self, ws: ClientConnection, json3: dict, seconds: float, tick: float, stop_wait: float
    ) -> None:
        """Say what the extension says and listen to the answers, until the stop is answered."""
        try:
            await ws.send(encode(Hello(CLIENT, VERSION)))
            await ws.send(encode(Start(None)))  # "mode": null, as the extension sends it
            await ws.send(encode(Captions(self.video_id, "en", "asr", json3)))
            listener = asyncio.create_task(self._listen(ws))
            try:
                await self._play(ws, seconds, tick)
                if not self.result.closed:
                    self._stopping = True
                    await ws.send(encode(Stop()))
                    await self._wait_for_idle(listener, stop_wait)
            finally:
                listener.cancel()
                await asyncio.wait([listener])
        except ConnectionClosed:
            self.result.closed = True
        self.result.played = self.player.current_time - self.start

    async def _play(self, ws: ClientConnection, seconds: float, tick: float) -> None:
        """Report the player's state every `tick` seconds until it has played `seconds`."""
        end = self.start + seconds
        next_report = time.monotonic()
        while not self.result.closed and self.player.current_time < end:
            report = self.player.state(self.video_id)
            self._note_hold(report)
            await ws.send(encode(report))
            next_report += tick
            delay = next_report - time.monotonic()
            if delay < 0:
                next_report = time.monotonic()  # late: report again at once, not once for each tick missed
            await asyncio.sleep(max(delay, 0.0))

    def _note_hold(self, report: State) -> None:
        """Time the start-up hold the way the server log shows it: from the first report of
        a paused player to the first one of a playing player."""
        now = time.monotonic()
        if report.paused:
            if self._paused_since is None:
                self._paused_since = now
        elif self._paused_since is not None and self.result.held is None:
            self.result.held = now - self._paused_since

    async def _wait_for_idle(self, listener: asyncio.Task, stop_wait: float) -> None:
        """Wait for the idle status that answers the stop, for the connection to close, or for the time to pass."""
        idle = asyncio.ensure_future(self._idle.wait())
        try:
            await asyncio.wait({idle, listener}, timeout=stop_wait, return_when=asyncio.FIRST_COMPLETED)
        finally:
            idle.cancel()
        self.result.stopped = self._idle.is_set()

    async def _listen(self, ws: ClientConnection) -> None:
        """Handle the server's messages until it closes the connection."""
        try:
            async for frame in ws:
                self._on_frame(frame)
        except ConnectionClosed:
            pass
        self.result.closed = True

    def _on_frame(self, frame: str | bytes) -> None:
        try:
            msg = decode(frame)
        except ProtocolError as exc:
            self._show("error", f"not a message: {exc}")
            return
        match msg:
            case Status():
                self.result.statuses.append(msg)
                self._show("status", f"{msg.state:<10} {msg.buffered_ahead:5.1f} s ahead  {msg.message}")
                if self._stopping and msg.state == "idle":
                    self._idle.set()
            case Subtitle():
                self.result.subtitles.append(msg)
                self._show("subtitle", f"{msg.start:7.2f} - {msg.end:7.2f} s  {msg.text}")
            case Duck():
                self.result.ducks.append(msg)
                self.player.apply(msg)
                self._show("duck", "restore the volume" if msg.volume is None else f"volume {msg.volume:g}")
            case Control():
                self.result.controls.append(msg)
                self.player.apply(msg)
                self._show("control", msg.action if msg.value is None else f"{msg.action} {msg.value:g}")

    def _show(self, kind: str, text: str) -> None:
        """One line of output, led by the video time at which the message arrived."""
        self.out(f"[{self.player.current_time:7.2f} s] {kind:<9} {text}".rstrip())


def _summary(result: RunResult, start: float, stop_wait: float) -> str:
    held = "never played" if result.held is None else f"held {result.held:.1f} s at the start"
    if result.closed:
        end = "the server closed the connection"
    elif result.stopped:
        end = "the server went idle after the stop"
    else:
        end = f"no idle status within {stop_wait:g} s of the stop"
    return (
        f"played {result.played:.1f} s of video from {start:g} s, "
        f"{len(result.subtitles)} subtitles, {held}, {end}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay a caption file against a running `dubber serve`, as the Chrome extension would."
    )
    parser.add_argument("captions", type=pathlib.Path, help="a YouTube caption file (json3)")
    parser.add_argument(
        "--start", type=float, default=0.0, help="the video time to start playing from, in seconds (default: 0)"
    )
    parser.add_argument(
        "--seconds", type=float, default=60.0, help="how many seconds of video to play (default: 60)"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"the server's address (default: {DEFAULT_URL})")
    parser.add_argument(
        "--video-id",
        help="the video id to report (default: the file name up to its first dot, as in XP5k0bHmhqc.en.asr.json3)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command `argv` names (default: the command line); return the exit status.

    0 when the server went idle after the stop, 1 when it did not or could not be
    reached, 2 for a caption file or an option that cannot be used.
    """
    parser = _parser()
    args = parser.parse_args(argv)
    if args.start < 0:
        parser.error("--start must not be negative")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    try:
        json3 = json.loads(args.captions.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"cannot read {args.captions}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # not UTF-8, or not JSON
        print(f"{args.captions} is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(json3, dict):
        print(f"{args.captions} is not a json3 caption file: its JSON is not an object", file=sys.stderr)
        return 2
    video_id = args.video_id or args.captions.name.split(".")[0]
    try:
        result = asyncio.run(
            run(
                args.url, json3, video_id=video_id, start=args.start, seconds=args.seconds,
                out=functools.partial(print, flush=True), stop_wait=STOP_WAIT,
            )
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (OSError, WebSocketException) as exc:
        print(f"cannot talk to {args.url}: {exc}; is `uv run dubber serve` running?", file=sys.stderr)
        return 1
    return 0 if result.stopped else 1


if __name__ == "__main__":
    sys.exit(main())
