# The fake extension (tools/fake_extension.py): its pretend player, and whole runs against a
# scripted server that answers the way `dubber serve` does, with no session and no audio.
# tests/test_e2e.py runs it against the real server.
import asyncio
import contextlib
import json
import socket
from itertools import pairwise

import pytest
from websockets.asyncio.server import serve as listen

from dubber.protocol import (
    Captions,
    Control,
    Duck,
    Hello,
    Start,
    State,
    Status,
    Stop,
    Subtitle,
    decode,
    encode,
)
from tools.fake_extension import FakePlayer, main, run

JSON3 = {"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello there."}]}]}


class Clock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def status(state, ahead=0.0, message=""):
    return Status("captions", state, ahead, 0.0, "scripted", message)


# The pretend player.

def test_the_player_starts_paused_at_the_start_time():
    clock = Clock()
    player = FakePlayer(start=42.0, clock=clock)
    clock.advance(10)
    assert player.paused and player.current_time == 42.0 and player.rate == 1.0 and player.volume == 1.0


def test_the_time_moves_in_real_time_only_while_the_player_plays():
    clock = Clock()
    player = FakePlayer(start=5.0, clock=clock)
    player.apply(Control("play", None))
    clock.advance(2.5)
    assert player.current_time == 7.5
    player.apply(Control("pause", None))
    clock.advance(100)
    assert player.paused and player.current_time == 7.5
    player.apply(Control("play", None))
    clock.advance(1)
    assert player.current_time == 8.5


def test_a_rate_applies_from_the_moment_it_is_set():
    clock = Clock()
    player = FakePlayer(clock=clock)
    player.apply(Control("play", None))
    clock.advance(4)
    player.apply(Control("rate", 0.5))
    clock.advance(4)
    assert player.rate == 0.5 and player.current_time == 6.0  # 4 s at 1x, then 4 s at half speed


def test_a_control_without_a_value_leaves_the_rate_alone():
    player = FakePlayer(clock=Clock())
    player.apply(Control("rate", None))
    assert player.rate == 1.0


def test_a_duck_lowers_the_volume_and_a_restore_gives_back_the_owners_exactly():
    player = FakePlayer(clock=Clock())
    player.volume = 0.63
    player.apply(Duck(0.2))
    assert player.volume == 0.2
    player.apply(Duck(0.15))  # a second duck must not become the volume to restore
    assert player.volume == 0.15
    player.apply(Duck(None))
    assert player.volume == 0.63
    player.apply(Duck(None))  # restoring when nothing is ducked changes nothing
    assert player.volume == 0.63


def test_the_state_report_carries_what_the_player_shows():
    clock = Clock()
    player = FakePlayer(start=3.0, clock=clock)
    player.apply(Duck(0.2))
    assert player.state("vid") == State("vid", 3.0, 1.0, True, False, 0.2)
    player.apply(Control("play", None))
    clock.advance(1)
    assert player.state("vid") == State("vid", 4.0, 1.0, False, False, 0.2)


# A run against a scripted server.

class Scripted:
    """Answers like `dubber serve`: it pauses the player at the start, plays it at the first
    state report and goes idle at the stop. `script` maps the number of a state report to
    the extra messages sent when it arrives; `close_after` is a message type after which
    the server hangs up; with `answer_stop` false it never goes idle."""

    def __init__(self, script, answer_stop, close_after):
        self.script, self.answer_stop, self.close_after = script, answer_stop, close_after
        self.received, self.origins, self.url = [], [], ""

    async def handle(self, ws):
        self.origins.append(ws.request.headers.get("Origin"))
        states = 0
        async for frame in ws:
            msg = decode(frame)
            self.received.append(msg)
            replies = []
            match msg:
                case Hello():
                    replies = [status("idle")]
                case Start():
                    replies = [Duck(0.2), Control("pause", None), status("preparing")]
                case Captions():
                    replies = [Subtitle("First line.", 1.0, 3.0), Subtitle("Second line.", 4.0, 6.0)]
                case State():
                    states += 1
                    if states == 1:
                        replies = [Control("play", None), status("playing", 12.5, "ready")]
                    replies += self.script.get(states, [])
                case Stop():
                    replies = [Duck(None), status("idle")] if self.answer_stop else []
            for reply in replies:
                await ws.send(encode(reply))
            if type(msg) is self.close_after:
                await ws.close()


@contextlib.asynccontextmanager
async def scripted(script=None, answer_stop=True, close_after=None):
    server = Scripted(script or {}, answer_stop, close_after)
    async with listen(server.handle, "127.0.0.1", 0) as running:
        server.url = f"ws://127.0.0.1:{running.sockets[0].getsockname()[1]}"
        yield server


def states_of(server):
    return [msg for msg in server.received if isinstance(msg, State)]


def quick(url, seconds, **options):
    """A run that is quiet and ticks every 50 ms; `options` replace any of its arguments."""
    arguments = {"tick": 0.05, "stop_wait": 3.0, "out": lambda line: None, **options}
    return run(url, JSON3, seconds=seconds, **arguments)


async def test_a_run_sends_what_the_extension_sends_in_its_order_without_an_origin():
    async with scripted() as server:
        await quick(server.url, 0.5, video_id="vid", start=100.0)
    sent = server.received
    assert sent[0] == Hello("fake-extension", "1")
    assert sent[1] == Start(None)  # the key is there and null: the server rejects a frame without it
    assert sent[2] == Captions("vid", "en", "asr", JSON3)
    assert all(isinstance(msg, State) for msg in sent[3:-1]) and sent[-1] == Stop()
    assert server.origins == [None]  # no Origin header, which the server accepts from a local tool


async def test_the_state_reports_start_paused_at_the_start_time_and_then_play_in_real_time():
    async with scripted() as server:
        result = await quick(server.url, 0.6, video_id="vid", start=100.0)
    reports = states_of(server)
    assert reports[0] == State("vid", 100.0, 1.0, True, False, 1.0)  # paused until the server says play
    assert any(not r.paused for r in reports[1:])
    assert all(r.video_id == "vid" and r.live is False and r.rate == 1.0 for r in reports)
    times = [r.current_time for r in reports]
    assert times == sorted(times) and times[-1] >= 100.0 + 0.6 - 0.1
    assert reports[-1].volume == 0.2  # the duck reached the pretend player
    assert 0.6 <= result.played < 1.6
    assert 25 >= len(reports) >= 5  # about every tick, not once and not in a flood


async def test_the_server_pausing_and_changing_the_rate_act_on_the_player():
    script = {2: [Control("pause", None)], 5: [Control("play", None), Control("rate", 2.0)]}
    async with scripted(script) as server:
        await quick(server.url, 0.8)
    reports = states_of(server)
    for before, after in pairwise(reports):
        if before.paused and after.paused:
            assert before.current_time == after.current_time  # frozen while paused
    assert any(r.paused for r in reports[2:5])
    assert reports[-1].rate == 2.0 and not reports[-1].paused


async def test_the_result_lists_what_the_server_sent_and_how_the_run_ended():
    async with scripted() as server:
        result = await quick(server.url, 0.3, video_id="vid")
    assert result.states == ["idle", "preparing", "playing", "idle"]
    assert [s.state for s in result.statuses] == ["idle", "preparing", "playing", "idle"]
    assert result.ducks == [Duck(0.2), Duck(None)]
    assert result.controls == [Control("pause", None), Control("play", None)]
    assert [s.text for s in result.subtitles] == ["First line.", "Second line."]
    assert result.stopped and not result.closed
    assert result.held is not None and 0 <= result.held < 1.0  # see the test of the hold below


async def test_the_hold_runs_from_the_first_paused_report_to_the_first_playing_one():
    # The server log shows the start-up hold as the time from its first "state: paused" line to the
    # next "state: playing" line. The server says play at once here, but the first report that shows
    # the player playing goes out one tick later (0.3 s), and that is where the hold ends.
    async with scripted() as server:
        result = await quick(server.url, 0.5, tick=0.3)
    first_playing = next(i for i, r in enumerate(states_of(server)) if not r.paused)
    assert first_playing == 1 and states_of(server)[0].paused
    assert 0.25 <= result.held < 0.9


async def test_a_run_prints_each_status_and_subtitle_then_a_summary_and_the_hint():
    lines = []
    async with scripted() as server:
        await quick(server.url, 0.3, video_id="vid", out=lines.append)
    text = "\n".join(lines)
    assert any("status" in line and "playing" in line and "12.5" in line and "ready" in line for line in lines)
    assert any("status" in line and "preparing" in line for line in lines)
    assert any("subtitle" in line and "1.00" in line and "3.00" in line and "First line." in line for line in lines)
    assert any("subtitle" in line and "Second line." in line for line in lines)
    assert "duck" in text and "control" in text  # the volume and the player calls are shown too
    assert lines[-2].startswith("summary: ") and "2 subtitles" in lines[-2] and "held" in lines[-2]
    assert "uv run dubber stats" in lines[-1]
    assert len(lines[-2].splitlines()) == 1  # a one-line summary


async def test_a_server_that_never_goes_idle_is_waited_for_only_so_long():
    lines = []
    async with scripted(answer_stop=False) as server:
        result = await quick(server.url, 0.2, stop_wait=0.3, out=lines.append)
    assert not result.stopped and not result.closed
    assert "no idle status within 0.3 s" in lines[-2]


async def test_a_server_that_hangs_up_ends_the_run_without_an_error():
    lines = []
    async with scripted(close_after=Captions) as server:
        result = await asyncio.wait_for(quick(server.url, 30.0, out=lines.append), timeout=10)
    assert result.closed and not result.stopped
    assert Stop() not in server.received  # nobody left to tell
    assert "closed the connection" in lines[-2]
    assert result.held is None and "never played" in lines[-2]  # it hung up before the player ever played


# The command.

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def one_line(err):
    assert err.endswith("\n") and err.count("\n") == 1 and "Traceback" not in err
    return err


def test_a_caption_file_that_cannot_be_read_is_one_error_line(tmp_path, capsys):
    assert main([str(tmp_path / "none.json3")]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "none.json3" in one_line(err)


@pytest.mark.parametrize("text", ["this is not json", "[1, 2]", "42"])
def test_a_caption_file_that_is_not_a_json_object_is_one_error_line(tmp_path, capsys, text):
    path = tmp_path / "captions.json3"
    path.write_text(text)
    assert main([str(path)]) == 2
    out, err = capsys.readouterr()
    assert out == "" and "captions.json3" in one_line(err)


def test_a_server_that_is_not_running_is_one_error_line_that_says_how_to_start_it(tmp_path, capsys):
    path = tmp_path / "captions.json3"
    path.write_text(json.dumps(JSON3))
    url = f"ws://127.0.0.1:{free_port()}"
    assert main([str(path), "--url", url]) == 1
    out, err = capsys.readouterr()
    assert out == "" and url in one_line(err) and "uv run dubber serve" in err


@pytest.mark.parametrize("option", [["--seconds", "0"], ["--seconds", "-3"], ["--start", "-1"]])
def test_a_negative_start_or_a_run_of_no_seconds_is_refused(tmp_path, capsys, option):
    path = tmp_path / "captions.json3"
    path.write_text(json.dumps(JSON3))
    with pytest.raises(SystemExit) as refused:
        main([str(path), *option])
    assert refused.value.code == 2


async def test_the_command_takes_the_video_id_from_the_file_name_and_prints_the_summary(tmp_path, capsys):
    path = tmp_path / "XP5k0bHmhqc.en.asr.json3"
    path.write_text(json.dumps(JSON3))
    async with scripted() as server:
        code = await asyncio.to_thread(main, [str(path), "--url", server.url, "--start", "10", "--seconds", "0.4"])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert server.received[2] == Captions("XP5k0bHmhqc", "en", "asr", JSON3)
    assert states_of(server)[0].current_time == 10.0
    lines = out.splitlines()
    assert lines[-2].startswith("summary: ") and "uv run dubber stats" in lines[-1]


async def test_the_video_id_option_wins_over_the_file_name(tmp_path):
    path = tmp_path / "XP5k0bHmhqc.en.asr.json3"
    path.write_text(json.dumps(JSON3))
    async with scripted() as server:
        await asyncio.to_thread(main, [str(path), "--url", server.url, "--seconds", "0.2", "--video-id", "other"])
    assert server.received[2].video_id == "other"
    assert all(r.video_id == "other" for r in states_of(server))


async def test_the_command_fails_when_the_server_does_not_report_idle(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("tools.fake_extension.STOP_WAIT", 0.2)
    path = tmp_path / "captions.json3"
    path.write_text(json.dumps(JSON3))
    async with scripted(answer_stop=False) as server:
        code = await asyncio.to_thread(main, [str(path), "--url", server.url, "--seconds", "0.2"])
    assert code == 1
    assert "no idle status" in capsys.readouterr().out
