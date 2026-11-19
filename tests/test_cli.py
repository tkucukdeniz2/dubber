# The `dubber` command: `stats` on good and bad files, the errors `serve` reports without a
# traceback, the engines `serve` builds from the settings, and Ctrl-C on a real `dubber serve`
# process with a client connected.
import importlib.metadata
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import asdict

import httpx
import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.sync.client import connect

from dubber.cli import _EngineError, _build_translator, _build_voice, main
from dubber.config import Config
from dubber.engines import voice_say
from dubber.engines.base import IdentityTranslator
from dubber.engines.base import VoiceError
from dubber.engines.translate_llamacpp import LlamaCppTranslator
from dubber.engines.voice_pc import PcVoice
from dubber.engines.voice_say import SayVoice
from dubber.engines.voice_supertonic import SupertonicVoice
from dubber.engines.voice_voxcpm_mlx import VoxCpmMlxVoice
from dubber.metrics import MetricsLog, SegmentRecord, format_stats, stats
from dubber.protocol import Hello, Status

@pytest.fixture(autouse=True)
def in_a_scratch_directory(tmp_path, monkeypatch):
    """Run every test in an empty directory of its own: `serve` writes dubber.log where it is started."""
    monkeypatch.chdir(tmp_path)


HELLO = {"type": "hello", "client": "test", "version": "0"}
START = {"type": "start", "mode": None}

# A job started in the background can have SIGINT ignored, and asyncio.run turns Ctrl-C
# into a clean shutdown only where Python's own SIGINT handler is in place.
CHILD = (
    "import signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
    "from dubber.cli import main; sys.exit(main())"
)


def record(idx, lag, session="s1"):
    return SegmentRecord(
        video_id="v", idx=idx, src_start=10.0 * idx, src_end=10.0 * idx + 2, text_ready_at=1.0,
        mt_ms=5.0, mt_engine="identity", tts_ms=100.0, tts_engine="say:Samantha",
        audio_seconds=1.5, speed=1.0, scheduled_start=10.0 * idx + lag, actual_start=None,
        lag=lag, dropped=False, session=session, ahead=4.0,
    )


def write_records(path, *records):
    log = MetricsLog(path)
    for rec in records:
        log.write(rec)


def one_error_line(capsys, prefix):
    """The only thing printed is one line to stderr that starts with `prefix`; return it."""
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith(prefix) and err.count("\n") == 1 and "Traceback" not in err
    return err


def test_the_dubber_script_is_registered():
    (script,) = importlib.metadata.entry_points(group="console_scripts", name="dubber")
    assert script.value == "dubber.cli:main"


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exit_:
        main([])
    assert exit_.value.code == 2
    assert "usage" in capsys.readouterr().err


# stats

def test_stats_prints_the_formatted_summary(tmp_path, capsys):
    path = tmp_path / "m.jsonl"
    write_records(path, record(0, 0.1), record(1, 0.2), record(2, 0.3))
    assert main(["stats", "--metrics", str(path)]) == 0
    out, err = capsys.readouterr()
    assert out == format_stats(stats(path)) + "\n" and err == ""
    assert "lag p50" in out and "0.20 s" in out and "s1" in out


def test_stats_summarizes_the_last_session_unless_one_is_asked_for(tmp_path, capsys):
    path = tmp_path / "m.jsonl"
    write_records(path, record(0, 0.1, "first"), record(1, 0.4, "first"), record(0, 0.9, "second"))
    assert main(["stats", "--metrics", str(path)]) == 0
    last = capsys.readouterr().out
    assert main(["stats", "--metrics", str(path), "--session", "first"]) == 0
    first = capsys.readouterr().out
    assert last == format_stats(stats(path, "second")) + "\n"
    assert first == format_stats(stats(path, "first")) + "\n"
    assert "second" in last and "first" in first


def test_stats_reads_metrics_jsonl_in_the_working_directory_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_records(tmp_path / "metrics.jsonl", record(0, 0.1))
    assert main(["stats"]) == 0
    assert "lag p50" in capsys.readouterr().out


def test_stats_on_a_missing_file_is_one_error_line(tmp_path, capsys):
    assert main(["stats", "--metrics", str(tmp_path / "missing.jsonl")]) == 1
    assert "missing.jsonl" in one_error_line(capsys, "stats error: ")


def test_stats_on_a_directory_is_one_error_line(tmp_path, capsys):
    assert main(["stats", "--metrics", str(tmp_path)]) == 1
    one_error_line(capsys, "stats error: ")


@pytest.mark.parametrize("line", ["this is not json", '{"video_id": "v"}', "[1, 2]"])
def test_stats_on_a_file_that_is_not_metrics_is_one_error_line(tmp_path, capsys, line):
    path = tmp_path / "m.jsonl"
    path.write_text(line + "\n")
    assert main(["stats", "--metrics", str(path)]) == 1
    assert "line 1" in one_error_line(capsys, "stats error: ")


def test_stats_on_a_record_with_a_missing_value_is_one_error_line(tmp_path, capsys):
    path = tmp_path / "m.jsonl"
    path.write_text(json.dumps({**asdict(record(0, 0.1)), "lag": None}) + "\n")  # a TypeError inside stats
    assert main(["stats", "--metrics", str(path)]) == 1
    one_error_line(capsys, "stats error: ")


# serve

@pytest.mark.parametrize("content", [
    "[server\nport = ",  # not TOML
    "[server]\nprot = 8765\n",  # an unknown setting
    "[server]\nport = 'abc'\n",  # a value of the wrong type
    "[duck]\nvolume = 0.9\n",  # outside the allowed range
])
def test_serve_with_a_bad_config_returns_2(tmp_path, capsys, content):
    config = tmp_path / "config.toml"
    config.write_text(content)
    assert main(["serve", "--config", str(config)]) == 2
    one_error_line(capsys, "config error: ")


def test_serve_with_a_missing_config_returns_2(tmp_path, capsys):
    assert main(["serve", "--config", str(tmp_path / "none.toml")]) == 2
    assert "none.toml" in one_error_line(capsys, "config error: ")


def test_serve_says_when_the_port_is_in_use(tmp_path, capsys):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        config = tmp_path / "config.toml"
        config.write_text(f"[server]\nport = {port}\n")
        assert main(["serve", "--config", str(config)]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"port {port} is in use: is another dubber serve running?\n"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_listening_lines(proc, port, engines, timeout=20.0):
    """Read the two lines `dubber serve` prints once it listens, without hanging the test.

    They are its engines, then its address. A child that prints less is killed after
    `timeout` seconds, which ends the read. (Not select: the second line may already be
    in Python's buffer, where select cannot see it.)
    """
    watchdog = threading.Timer(timeout, proc.kill)
    watchdog.start()
    try:
        lines = [proc.stdout.readline(), proc.stdout.readline()]
    finally:
        watchdog.cancel()
    assert lines == [f"engines: {engines}\n", f"listening on ws://127.0.0.1:{port}\n"]


@pytest.mark.parametrize("verbose", [False, True])
def test_serve_prints_the_listening_line_and_ctrl_c_ends_it_cleanly(tmp_path, verbose):
    port = free_port()
    config = tmp_path / "config.toml"
    # Supertonic is built without loading anything, so the child process never runs `say`.
    config.write_text(f'[server]\nport = {port}\n[voice]\nengine = "supertonic"\nvoice = "M1"\n')
    command = [sys.executable, "-c", CHILD, "serve", "--config", str(config)]
    proc = subprocess.Popen(
        command + (["--verbose"] if verbose else []),
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        read_listening_lines(proc, port, "identity + supertonic3:M1")
        with connect(f"ws://127.0.0.1:{port}") as ws:
            ws.send(json.dumps(HELLO))
            assert json.loads(ws.recv(timeout=10))["state"] == "idle"
            ws.send(json.dumps(START))  # a producer thread is running now
            assert json.loads(ws.recv(timeout=10))["type"] == "duck"
            proc.send_signal(signal.SIGINT)
            with pytest.raises(ConnectionClosedOK):  # told "going away", not dropped
                while True:
                    ws.recv(timeout=10)  # the rest of the answer to Start, then the close
            out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0
    assert out == ""  # the engines and listening lines were read above
    assert "Traceback" not in err
    assert "connection closed" in err  # the open connection was cleaned up before the exit
    assert "still busy" not in err
    assert "websockets" not in err  # the library's own lines repeat the server's
    assert ("DEBUG" in err) == verbose
    # The same lines are in the log file, with the date, beside what was printed.
    log = (tmp_path / "dubber.log").read_text(encoding="utf-8")
    stamp = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO "
    assert re.search(stamp + rf"dubber\.cli: listening on ws://127\.0\.0\.1:{port}$", log, re.MULTILINE)
    assert re.search(stamp + r"dubber\.cli: engines: identity \+ supertonic3:M1$", log, re.MULTILINE)
    assert re.search(stamp + r"dubber\.server: connection open", log, re.MULTILINE)
    assert ("DEBUG" in log) == verbose


# The engines: `serve` builds them from the settings, once, before it listens. In these
# tests the server is replaced, so nothing listens, and the voices `say` lists are fixed, so
# no test runs the real `say`. None of them reaches the PC: a request would fail the test.

@pytest.fixture(autouse=True)
def installed_voices(monkeypatch):
    monkeypatch.setattr(voice_say, "_installed_voices", lambda: [("Samantha", "en_US"), ("Yelda", "tr_TR")])


class StoppedServer:
    """What the replaced serve returns: a server that has already stopped."""

    async def serve_forever(self):
        return None


@pytest.fixture
def served(monkeypatch):
    """Replace the server; the list holds the (settings, session factory) of each call."""
    calls = []

    async def fake_serve(cfg, session_factory=None):
        calls.append((cfg, session_factory))
        return StoppedServer()

    monkeypatch.setattr("dubber.cli.serve", fake_serve)
    return calls


@pytest.fixture
def no_network(monkeypatch):
    def refuse(self, request, **kwargs):
        raise AssertionError(f"a request went out: {request.url}")

    monkeypatch.setattr(httpx.Client, "send", refuse)


class FakePc:
    """The PC, at the bottom of httpx, so that no socket is opened.

    It answers the list of models and nothing else: any other request fails the test, which
    is how the tests know that `serve` asks for the list and never for a translation. Set
    `fail` to an exception to raise instead of answering, or to a status code to answer with.
    `requests` holds the (method, path) of each request and `authorizations` its credentials.
    """

    def __init__(self):
        self.requests, self.authorizations = [], []
        self.models = [("qwen3.8-27b", "loaded")]
        self.fail = None

    def __call__(self, request):
        self.requests.append((request.method, request.url.path))
        self.authorizations.append(request.headers.get("authorization"))
        if isinstance(self.fail, Exception):
            raise self.fail
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, text="bad key")
        assert (request.method, request.url.path) == ("GET", "/v1/models"), f"asked for {request.url.path}"
        return httpx.Response(200, json={"data": [{"id": m, "status": {"value": s}} for m, s in self.models]})


@pytest.fixture
def pc(monkeypatch):
    fake = FakePc()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake)
    return fake


def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def pc_settings(key_file):
    return f'[translator]\nengine = "pc"\n[pc]\napi_key_file = "{key_file}"\nbase_url = "http://pc.example:8080"\n'


def engine_of(factory, cfg):
    """The engine name a session made by `factory` reports in its Status."""
    sent = []
    factory(cfg, sent.append).handle(Hello("test", "0"))
    (status,) = sent
    assert isinstance(status, Status)
    return status.engine


def test_serve_builds_the_stand_in_engines_from_the_default_settings(served, capsys, pc):
    assert main(["serve"]) == 0
    assert pc.requests == []  # the stand-in translator has no PC to ask
    out, err = capsys.readouterr()
    # The engines come before the address, and both after the server is bound.
    assert out == "engines: identity + say:Samantha\nlistening on ws://127.0.0.1:8765\n"
    ((cfg, factory),) = served
    assert engine_of(factory, cfg) == "identity+say:Samantha"


def test_connections_share_the_engines_and_the_metrics_log_but_not_the_mixer(served, tmp_path):
    metrics = tmp_path / "run" / "dub.jsonl"
    assert main(["serve", "--config", write_config(tmp_path, f'metrics_path = "{metrics}"\n')]) == 0
    ((cfg, factory),) = served
    first, second = factory(cfg, lambda msg: None), factory(cfg, lambda msg: None)
    assert first is not second
    assert first._translator is second._translator and first._voice is second._voice
    assert first._metrics is second._metrics and first._metrics.path == metrics
    assert first._audio is not second._audio
    assert not metrics.parent.exists()  # nothing is written until a segment is produced


def test_building_the_engines_and_a_session_opens_no_audio_device(served, monkeypatch):
    def refuse(self, callback):
        raise AssertionError("an audio stream was opened")

    monkeypatch.setattr("dubber.playback.AudioOut._open_output_stream", refuse)
    assert main(["serve"]) == 0
    ((cfg, factory),) = served
    factory(cfg, lambda msg: None)  # a session starts its stream only when the first clip is scheduled


def test_serve_wires_the_pc_translator_and_the_configured_voice(tmp_path, served, capsys, pc):
    key = tmp_path / "key"
    key.write_text("SECRET-KEY-VALUE\n")
    settings = pc_settings(key) + '[voice]\nvoice = "Yelda"\n'
    assert main(["serve", "--config", write_config(tmp_path, settings)]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("engines: llamacpp + say:Yelda\n")
    assert "SECRET" not in out + err  # the key file is read, and never shown
    assert pc.authorizations == ["Bearer SECRET-KEY-VALUE"]  # it is used, though
    ((cfg, factory),) = served
    assert engine_of(factory, cfg) == "llamacpp+say:Yelda"


def test_the_pc_translator_follows_the_loaded_model_and_has_the_configured_address(tmp_path, no_network):
    key = tmp_path / "key"
    key.write_text("SECRET-KEY-VALUE\n")
    cfg = Config()
    cfg.translator.engine, cfg.pc.api_key_file, cfg.pc.base_url = "pc", str(key), "http://pc.example:8080"
    translator = _build_translator(cfg)
    assert isinstance(translator, LlamaCppTranslator)
    assert translator.prefer_loaded and translator.model == "qwen3.8-27b"  # the preferred one, if none is loaded
    assert str(translator._client.base_url) == "http://pc.example:8080"
    assert translator._client.timeout == httpx.Timeout(120.0, connect=3.0)  # a PC that is off fails in 3 s


def test_the_pc_table_is_handed_to_translator_from_config_as_it_is(monkeypatch, tmp_path):
    seen = []

    class Stub:
        name = "stub"

    monkeypatch.setattr("dubber.cli.translator_from_config", lambda table: seen.append(table) or Stub())
    key = tmp_path / "key"
    key.write_text("the-key\n")
    cfg = Config()
    cfg.translator.engine, cfg.pc.base_url, cfg.pc.preferred_model = "pc", "http://pc.example:8080", "gemma"
    cfg.pc.api_key_file = str(key)
    assert _build_translator(cfg).name == "stub"
    assert seen == [{"pc": {
        "base_url": "http://pc.example:8080", "api_key_file": str(key), "preferred_model": "gemma",
        "tts_url": "http://my-pc.example.ts.net:8191",
    }}]


def test_the_identity_translator_reads_no_key_file():
    cfg = Config()
    cfg.pc.api_key_file = "/nonexistent/key"
    assert isinstance(_build_translator(cfg), IdentityTranslator)


@pytest.mark.parametrize("engine, voice, kind, name", [
    ("say", "Yelda", SayVoice, "say:Yelda"),
    ("supertonic", "F1", SupertonicVoice, "supertonic3:F1"),
    ("voxcpm", "", VoxCpmMlxVoice, "voxcpm2-mlx"),
])
def test_the_voice_is_the_engine_and_the_voice_setting_joined(monkeypatch, engine, voice, kind, name):
    if engine != "say":
        monkeypatch.setattr(voice_say, "_installed_voices", lambda: pytest.fail("only a say voice is looked up"))
    cfg = Config()
    cfg.voice.engine, cfg.voice.voice = engine, voice
    built = _build_voice(cfg)
    assert isinstance(built, kind) and built.name == name


def test_a_voxcpm_voice_setting_is_the_reference_clip(tmp_path):
    cfg = Config()
    cfg.voice.engine, cfg.voice.voice = "voxcpm", str(tmp_path / "ref.wav")
    built = _build_voice(cfg)
    assert isinstance(built, VoxCpmMlxVoice) and built.ref_audio == tmp_path / "ref.wav"


def test_the_say_voice_is_looked_up_once_at_start_up_and_kept(served, monkeypatch):
    listings = []

    def listing():
        listings.append(1)
        return [("Samantha", "en_US")]

    monkeypatch.setattr(voice_say, "_installed_voices", listing)
    assert main(["serve"]) == 0
    assert listings == [1]  # found at once, so that a typo cannot wait for the first sentence
    ((cfg, factory),) = served
    session = factory(cfg, lambda msg: None)
    assert session._voice._resolve_voice() == "Samantha" and listings == [1]


@pytest.mark.parametrize("settings, prefix, mentions", [
    ('[translator]\nengine = "gemini"\n', "config error: ", "gemini"),
    ('[translator]\nengine = ""\n', "config error: ", "translator.engine"),
    ('[voice]\nengine = "piper"\nvoice = "x"\n', "config error: ", "piper:x"),
    ('[voice]\nengine = "say"\nvoice = ""\n', "config error: ", "say"),
    ('[voice]\nengine = "supertonic"\nvoice = ""\n', "config error: ", "supertonic"),
    ('[voice]\nvoice = "Yeldaa"\n', "engine error: ", "Yeldaa"),  # `say` would speak it in the default voice
])
def test_serve_refuses_an_engine_setting_it_cannot_use(tmp_path, served, capsys, settings, prefix, mentions):
    assert main(["serve", "--config", write_config(tmp_path, settings)]) == 2
    assert mentions in one_error_line(capsys, prefix)
    assert served == []  # it never listened


def test_a_say_voice_may_be_the_start_of_one_listed_name_as_say_itself_allows(served, tmp_path, capsys):
    assert main(["serve", "--config", write_config(tmp_path, '[voice]\nvoice = "yel"\n')]) == 0
    assert capsys.readouterr().out.startswith("engines: identity + say:yel\n")


def test_a_say_voice_that_starts_several_listed_voices_is_an_engine_error(tmp_path, served, capsys, monkeypatch):
    both = [("Cem (Enhanced)", "tr_TR"), ("Cem (Premium)", "tr_TR")]
    monkeypatch.setattr(voice_say, "_installed_voices", lambda: both)
    assert main(["serve", "--config", write_config(tmp_path, '[voice]\nvoice = "Cem"\n')]) == 2
    assert "ambiguous" in one_error_line(capsys, "engine error: ")


def test_a_say_that_cannot_run_is_an_engine_error(tmp_path, served, capsys, monkeypatch):
    def no_say():
        raise voice_say.VoiceError("cannot run say: [Errno 2] No such file or directory: 'say'")

    monkeypatch.setattr(voice_say, "_installed_voices", no_say)
    assert main(["serve"]) == 2
    assert "cannot run say" in one_error_line(capsys, "engine error: ")
    assert served == []


def test_an_error_message_with_several_lines_is_still_one_line(served, capsys, monkeypatch):
    def noisy():
        raise voice_say.VoiceError("say failed\nbecause of\n  something")

    monkeypatch.setattr(voice_say, "_installed_voices", noisy)
    assert main(["serve"]) == 2
    assert one_error_line(capsys, "engine error: ") == "engine error: say failed because of something\n"


def key_file_problem(tmp_path, kind, request):
    """A key file path that cannot be read as the key: missing, a folder, not text or not allowed."""
    key = tmp_path / "key"
    if kind == "directory":
        key.mkdir()
    elif kind == "binary":
        key.write_bytes(b"\xff\xfeSECRET-KEY-VALUE")
    elif kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root can read any file")
        key.write_text("SECRET-KEY-VALUE\n")
        key.chmod(0)
        request.addfinalizer(lambda: key.chmod(0o600))
    return key


@pytest.mark.parametrize("kind", ["missing", "directory", "binary", "unreadable"])
def test_a_pc_key_file_that_cannot_be_read_is_an_engine_error_that_names_the_file(
    tmp_path, served, capsys, request, kind
):
    key = key_file_problem(tmp_path, kind, request)
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 2
    err = one_error_line(capsys, "engine error: ")
    assert str(key) in err and "PC key file" in err
    assert "SECRET" not in err  # the file is named, its contents never shown
    assert served == []


def test_a_home_directory_in_the_pc_key_file_setting_is_named_in_full(tmp_path, served, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = pc_settings("~/.qwen-pc/api-key")
    assert main(["serve", "--config", write_config(tmp_path, settings)]) == 2
    assert str(tmp_path / ".qwen-pc" / "api-key") in one_error_line(capsys, "engine error: ")


# The PC must be reachable when `serve` starts (ruling R52). It asks for the list of models once,
# which is read-only and not a translation, and stops with one line if that fails: with the PC off
# or Tailscale down, every sentence would otherwise wait and the video would play undubbed.

def test_serve_asks_the_pc_for_its_models_once_before_it_listens_and_translates_nothing(
    tmp_path, served, capsys, pc
):
    key = tmp_path / "key"
    key.write_text("the-key\n")
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 0
    assert pc.requests == [("GET", "/v1/models")]  # FakePc fails the test on any other request
    assert len(served) == 1


def test_serve_keeps_the_http_clients_request_lines_out_of_the_info_log(tmp_path, served, pc, caplog):
    """The PC translator makes two requests for each sentence, and httpx logs every one at INFO."""
    caplog.set_level(logging.INFO)
    key = tmp_path / "key"
    key.write_text("the-key\n")
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 0
    assert pc.requests == [("GET", "/v1/models")]  # a request did go through httpx
    assert [r.getMessage() for r in caplog.records if r.name == "httpx"] == []


@pytest.mark.parametrize("failure, reason", [
    (httpx.ConnectError("[Errno 61] Connection refused"), "[Errno 61] Connection refused"),
    (httpx.ConnectTimeout("timed out"), "timed out"),
    (httpx.ConnectTimeout(""), "ConnectTimeout"),  # httpx often leaves a timeout without words
    (httpx.ReadTimeout(""), "ReadTimeout"),
    (401, "401: bad key"),  # something answers, but not with the model list
])
def test_serve_stops_with_an_engine_error_when_the_pc_cannot_be_reached(
    tmp_path, served, capsys, pc, failure, reason
):
    key = tmp_path / "key"
    key.write_text("SECRET-KEY-VALUE\n")
    pc.fail = failure
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 2
    err = one_error_line(capsys, "engine error: ")
    assert err == f"engine error: cannot reach the PC at http://pc.example:8080: {reason}\n"
    assert "SECRET" not in err
    assert served == []  # it never listened
    assert pc.requests == [("GET", "/v1/models")]  # one try, and it was the list


def test_a_pc_that_lists_no_model_at_all_is_still_reachable(tmp_path, served, pc):
    key = tmp_path / "key"
    key.write_text("the-key\n")
    pc.models = []
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 0


def test_the_stand_in_translator_is_never_checked_against_the_pc(served, pc):
    pc.fail = httpx.ConnectError("Connection refused")  # a PC that is off must not matter to the stand-in
    assert main(["serve"]) == 0
    assert pc.requests == []


# The key file (ruling R53). The key goes into a header, and a key that is not one printable line
# ends up in an error such as "Illegal header value b'Bearer ...'" that the session logs and shows
# on the YouTube page, key included. So `serve` refuses such a file and says only which file it is.

@pytest.mark.parametrize("contents", [
    "CANARY-ONE\nCANARY-TWO\n",
    "CANARY-ONE\r\nCANARY-TWO",
    "CANARY-ONE\n\nCANARY-TWO",
    "CANARY-ONE\tCANARY-TWO",
    "CANARY\x00ONE",
    "CANARY\x1b[31mONE",
    "CANARY-\u043a\u043b\u044e\u0447",  # not ASCII: httpx cannot put it in a header
    "",
    "\n",
    "  \r\n \n",
], ids=["second-line", "windows-line-ends", "blank-line-between", "tab", "nul", "escape", "not-ascii",
        "empty", "only-a-newline", "only-blanks"])
def test_a_pc_key_file_that_is_not_one_printable_line_is_refused_without_showing_it(
    tmp_path, served, capsys, pc, contents
):
    key = tmp_path / "key"
    key.write_bytes(contents.encode("utf-8"))
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 2
    err = one_error_line(capsys, "engine error: ")  # and nothing on stdout
    assert err == f"engine error: the PC key file {key} must hold the key on one line\n"
    assert "CANARY" not in err
    assert served == [] and pc.requests == []  # the PC was not even asked


@pytest.mark.parametrize("contents", ["the-key", "the-key\n", "the-key\r\n", "  the-key  \n\n"])
def test_a_pc_key_file_with_the_key_on_one_line_is_used_without_its_line_end(tmp_path, served, pc, contents):
    key = tmp_path / "key"
    key.write_bytes(contents.encode("utf-8"))
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key))]) == 0
    assert pc.authorizations == ["Bearer the-key"]



# The voice on the PC (dubber-tts): `serve` asks its /status once and builds the voice
# only when the service is reachable, has loaded its model and holds the named voice.


def pc_voice_cfg(tmp_path, name="owner"):
    key = tmp_path / "api-key"
    key.write_text("the-key\n", encoding="utf-8")
    cfg = Config()
    cfg.voice.engine, cfg.voice.voice = "pc", name
    cfg.pc.api_key_file, cfg.pc.tts_url = str(key), "http://pc.example:8191"
    return cfg


def answer_status(monkeypatch, status):
    def fake_status(self):
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(PcVoice, "status", fake_status)


def test_a_pc_voice_is_built_when_the_service_is_ready_and_holds_it(tmp_path, monkeypatch):
    answer_status(monkeypatch, {"ok": True, "ready": True, "voices": ["owner"]})
    voice = _build_voice(pc_voice_cfg(tmp_path))
    assert isinstance(voice, PcVoice) and voice.name == "pc-voxcpm2:owner"
    assert voice.base_url == "http://pc.example:8191" and voice._client.headers["Authorization"] == "Bearer the-key"


def test_an_empty_pc_voice_setting_means_the_owners_voice(tmp_path, monkeypatch):
    answer_status(monkeypatch, {"ok": True, "ready": True, "voices": ["owner"]})
    assert _build_voice(pc_voice_cfg(tmp_path, name="")).voice == "owner"


@pytest.mark.parametrize("status, message", [
    ({"ok": True, "ready": False, "error": None, "voices": ["owner"]}, "still loading its model"),
    ({"ok": True, "ready": False, "error": "OutOfMemoryError: CUDA", "voices": ["owner"]}, "could not load its model: OutOfMemoryError"),
    ({"ok": True, "ready": True, "voices": ["anne"]}, "has no voice 'owner'; it has: anne"),
    (VoiceError("cannot reach the PC voice service at http://pc.example:8191: ConnectTimeout"), "cannot reach the PC voice service"),
])
def test_a_pc_voice_that_cannot_speak_is_an_engine_error(tmp_path, monkeypatch, status, message):
    answer_status(monkeypatch, status)
    with pytest.raises(_EngineError, match=message):
        _build_voice(pc_voice_cfg(tmp_path))


def test_a_pc_voice_needs_a_usable_key_file(tmp_path, monkeypatch):
    answer_status(monkeypatch, AssertionError("the service must not be asked"))
    cfg = pc_voice_cfg(tmp_path)
    pathlib_key = tmp_path / "api-key"
    pathlib_key.write_text("two\nlines\n", encoding="utf-8")
    with pytest.raises(_EngineError, match="must hold the key on one line"):
        _build_voice(cfg)


# `dubber voice`: choosing between the owner's voice and Yelda.
OWNER_CONFIG = '[voice]\nengine = "pc"  # where\nvoice = "owner"  # who\n'


def test_voice_with_no_name_shows_the_voice_and_changes_nothing(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text(OWNER_CONFIG, encoding="utf-8")
    assert main(["voice", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "owner" in out and "pc-dublaj" in out and "Restart" not in out
    assert path.read_text(encoding="utf-8") == OWNER_CONFIG


def test_voice_yelda_then_owner_rewrites_the_file_and_says_to_restart(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text(OWNER_CONFIG, encoding="utf-8")
    assert main(["voice", "yelda", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "yelda" in out and "Restart" in out and "not needed" in out
    assert 'engine = "say"  # where' in path.read_text(encoding="utf-8")
    assert 'voice = "Yelda"  # who' in path.read_text(encoding="utf-8")
    assert main(["voice", "owner", "--config", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == OWNER_CONFIG


def test_voice_kalin_is_the_deep_voice_on_the_pc(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text(OWNER_CONFIG, encoding="utf-8")
    assert main(["voice", "kalin", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "voice: kalin (pc:kalin)" in out and "pc-dublaj" in out and "Restart" in out
    assert 'engine = "pc"  # where' in path.read_text(encoding="utf-8")
    assert 'voice = "kalin"  # who' in path.read_text(encoding="utf-8")


def test_voice_uses_config_toml_in_the_working_directory_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(OWNER_CONFIG, encoding="utf-8")
    assert main(["voice", "yelda"]) == 0
    assert 'voice = "Yelda"' in (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_voice_names_a_voice_that_is_neither_choice(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text('[voice]\nengine = "say"\nvoice = "Samantha"\n', encoding="utf-8")
    assert main(["voice", "--config", str(path)]) == 0
    assert "say:Samantha" in capsys.readouterr().out


def test_voice_refuses_an_unknown_name_and_leaves_the_file(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text(OWNER_CONFIG, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        main(["voice", "samantha", "--config", str(path)])
    assert caught.value.code == 2
    assert path.read_text(encoding="utf-8") == OWNER_CONFIG


def test_voice_on_a_missing_file_is_one_error_line(tmp_path, capsys):
    assert main(["voice", "yelda", "--config", str(tmp_path / "none.toml")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("config error:") and len(err.strip().splitlines()) == 1
    assert not (tmp_path / "none.toml").exists()


# The log file: what `serve` logs also goes to `log_path`, unless that is empty.
def file_handlers():
    return [h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def test_serve_logs_to_dubber_log_in_the_working_directory_by_default(served, tmp_path):
    assert main(["serve"]) == 0
    log = (tmp_path / "dubber.log").read_text(encoding="utf-8")
    assert "INFO dubber.cli: engines: identity + say:Samantha" in log
    assert "INFO dubber.cli: listening on ws://127.0.0.1:8765" in log


def test_serve_logs_to_the_file_the_settings_name(served, tmp_path):
    assert main(["serve", "--config", write_config(tmp_path, 'log_path = "run/d.log"\n')]) == 0
    assert "listening on" in (tmp_path / "run" / "d.log").read_text(encoding="utf-8")
    assert not (tmp_path / "dubber.log").exists()


def test_an_empty_log_path_writes_no_file(served, tmp_path):
    assert main(["serve", "--config", write_config(tmp_path, 'log_path = ""\n')]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_a_log_file_that_cannot_be_opened_is_one_config_error_line(served, tmp_path, capsys):
    (tmp_path / "logs").mkdir()
    assert main(["serve", "--config", write_config(tmp_path, 'log_path = "logs"\n')]) == 2
    out, err = capsys.readouterr()
    assert out == "" and served == []  # it never listened
    assert err.startswith("config error: cannot write the log file logs: ") and err.count("\n") == 1


def test_the_log_file_handler_is_gone_when_serve_returns(served):
    assert main(["serve"]) == 0
    assert file_handlers() == []
    assert main(["serve", "--config", "none.toml"]) == 2
    assert file_handlers() == []


# Warm engines: `serve` warms the PC's engines once it listens and whenever the extension
# connects, but only when the voice is the PC's, whose answer says the PC is in dubbing mode.
class FakeWarmer:
    made = []

    def __init__(self, translator, voice):
        self.translator, self.voice, self.kicks = translator, voice, 0
        FakeWarmer.made.append(self)

    def kick(self):
        self.kicks += 1
        return True


@pytest.fixture
def warmers(monkeypatch):
    FakeWarmer.made = []
    monkeypatch.setattr("dubber.cli.Warmer", FakeWarmer)
    return FakeWarmer.made


def test_serve_warms_pc_engines_once_it_listens_and_for_each_connection(tmp_path, served, pc, warmers, monkeypatch):
    answer_status(monkeypatch, {"ok": True, "ready": True, "voices": ["owner"]})
    key = tmp_path / "key"
    key.write_text("the-key\n")
    settings = pc_settings(key) + 'tts_url = "http://pc.example:8191"\n[voice]\nengine = "pc"\nvoice = "owner"\n'
    assert main(["serve", "--config", write_config(tmp_path, settings)]) == 0
    (warmer,) = warmers
    assert isinstance(warmer.translator, LlamaCppTranslator) and isinstance(warmer.voice, PcVoice)
    assert warmer.kicks == 1                  # once it listens
    ((cfg, factory),) = served
    factory(cfg, lambda msg: None)
    factory(cfg, lambda msg: None)
    assert warmer.kicks == 3                  # and for each connection


@pytest.mark.parametrize("settings", [
    "",                                                   # the stand-in engines
    '[voice]\nengine = "say"\nvoice = "Yelda"\n',         # a Mac voice: the PC may be in its LLM mode
])
def test_serve_warms_nothing_unless_the_voice_is_the_pcs(tmp_path, served, pc, warmers, settings):
    key = tmp_path / "key"
    key.write_text("the-key\n")
    assert main(["serve", "--config", write_config(tmp_path, pc_settings(key) + settings)]) == 0
    ((cfg, factory),) = served
    factory(cfg, lambda msg: None)
    assert warmers == [] and pc.requests == [("GET", "/v1/models")]


# voice = "auto": a man's voice or a woman's, by who presents the video. Both voices are the
# PC's, and the PC translator's model does the looking. The warm-up is replaced in these tests:
# the real one would speak through the fake PC, on a thread.
from dubber.engines.voice_by_presenter import PresenterVoice
from dubber.presenter import Presenters, YouTube


def auto_settings(tmp_path, voice='engine = "pc"\nvoice = "auto"\n'):
    key = tmp_path / "key"
    key.write_text("the-key\n")
    return pc_settings(key) + 'tts_url = "http://pc.example:8191"\n[voice]\n' + voice


def holding(monkeypatch, *voices):
    """The voice service is ready and holds `voices`; the list gets the voice each status was asked through."""
    asked = []

    def fake_status(self):
        asked.append(self.voice)
        return {"ok": True, "ready": True, "voices": list(voices)}

    monkeypatch.setattr(PcVoice, "status", fake_status)
    return asked


def test_auto_asks_the_voice_service_once_and_names_both_voices_when_it_starts(tmp_path, served, pc, warmers, capsys, monkeypatch):
    asked = holding(monkeypatch, "ipek", "kalin", "owner")
    assert main(["serve", "--config", write_config(tmp_path, auto_settings(tmp_path))]) == 0
    assert len(asked) == 1
    out = capsys.readouterr().out
    assert "engines: llamacpp + pc-voxcpm2:kalin or pc-voxcpm2:ipek, by who presents the video" in out


def test_auto_gives_every_session_a_voice_of_its_own_over_the_same_two_pc_voices(tmp_path, served, pc, warmers, monkeypatch):
    holding(monkeypatch, "ipek", "kalin")
    assert main(["serve", "--config", write_config(tmp_path, auto_settings(tmp_path))]) == 0
    ((cfg, factory),) = served
    one, two = factory(cfg, lambda msg: None), factory(cfg, lambda msg: None)
    assert isinstance(one._voice, PresenterVoice) and isinstance(two._voice, PresenterVoice)
    assert one._voice is not two._voice
    assert one._voice._male is two._voice._male and one._voice._female is two._voice._female
    assert isinstance(one._voice._male, PcVoice) and (one._voice._male.voice, one._voice._female.voice) == ("kalin", "ipek")
    assert engine_of(factory, cfg) == "llamacpp+pc-voxcpm2:kalin"  # the man's voice until a video says otherwise


def test_auto_reads_a_man_and_a_woman_in_the_voices_the_settings_name(tmp_path, served, pc, warmers, capsys, monkeypatch):
    holding(monkeypatch, "anne", "owner")
    voice = 'engine = "pc"\nvoice = "auto"\nmale = "owner"\nfemale = "anne"\n'
    assert main(["serve", "--config", write_config(tmp_path, auto_settings(tmp_path, voice))]) == 0
    assert "pc-voxcpm2:owner or pc-voxcpm2:anne" in capsys.readouterr().out


def test_auto_looks_with_the_pc_translators_model_at_what_youtube_shows(tmp_path, served, pc, warmers, monkeypatch):
    holding(monkeypatch, "ipek", "kalin")
    made = []

    class Recording(Presenters):
        def __init__(self, glimpse, look):
            made.append((glimpse, look))
            super().__init__(glimpse, look)

    monkeypatch.setattr("dubber.cli.Presenters", Recording)
    assert main(["serve", "--config", write_config(tmp_path, auto_settings(tmp_path))]) == 0
    ((glimpse, look),) = made
    assert isinstance(glimpse.__self__, YouTube) and glimpse.__name__ == "glimpse"
    assert isinstance(look.__self__, LlamaCppTranslator) and look.__name__ == "look"
    ((cfg, factory),) = served
    assert factory(cfg, lambda msg: None)._voice._who.__self__.__class__ is Recording
    assert pc.requests == [("GET", "/v1/models")]  # nothing was looked at: no video is on


def test_auto_warms_the_pc_through_the_mans_voice(tmp_path, served, pc, warmers, monkeypatch):
    holding(monkeypatch, "ipek", "kalin")
    assert main(["serve", "--config", write_config(tmp_path, auto_settings(tmp_path))]) == 0
    (warmer,) = warmers
    assert isinstance(warmer.voice, PcVoice) and warmer.voice.voice == "kalin" and warmer.kicks == 1


def test_auto_names_the_voice_the_service_does_not_hold(tmp_path, served, pc, warmers, capsys, monkeypatch):
    holding(monkeypatch, "kalin", "owner")
    assert main(["serve", "--config", write_config(tmp_path, auto_settings(tmp_path))]) == 2
    assert "has no voice 'ipek'; it has: kalin, owner" in one_error_line(capsys, "engine error:")
    assert served == []


@pytest.mark.parametrize("settings, needs", [
    ('[voice]\nengine = "say"\nvoice = "auto"\n', 'voice.engine = "pc"'),
    ('[voice]\nengine = "pc"\nvoice = "auto"\n', 'translator.engine = "pc"'),
    ('[translator]\nengine = "pc"\n[voice]\nengine = "pc"\nvoice = "auto"\nfemale = ""\n', "voice.male and voice.female must each name a voice"),
])
def test_auto_without_what_it_needs_is_a_config_error(tmp_path, served, pc, warmers, capsys, monkeypatch, settings, needs):
    holding(monkeypatch, "ipek", "kalin")
    key = tmp_path / "key"
    key.write_text("the-key\n")
    settings += f'[pc]\napi_key_file = "{key}"\n'
    assert main(["serve", "--config", write_config(tmp_path, settings)]) == 2
    assert needs in one_error_line(capsys, "config error:")
    assert served == []


def test_voice_auto_goes_by_the_presenter_and_says_which_voice_reads_whom(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text(OWNER_CONFIG, encoding="utf-8")
    assert main(["voice", "auto", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "voice: auto (pc:auto)" in out and "pc-dublaj" in out and "Restart" in out
    assert "a woman presents is read in ipek, every other video in kalin" in out
    assert 'voice = "auto"  # who' in path.read_text(encoding="utf-8")


def test_voice_shows_the_voices_the_file_names_for_auto(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text('[voice]\nengine = "pc"\nvoice = "auto"\nmale = "owner"\nfemale = "anne"\n', encoding="utf-8")
    assert main(["voice", "--config", str(path)]) == 0
    assert "a woman presents is read in anne, every other video in owner" in capsys.readouterr().out


def test_voice_ipek_is_the_womans_voice_on_the_pc(tmp_path, capsys):
    path = tmp_path / "c.toml"
    path.write_text(OWNER_CONFIG, encoding="utf-8")
    assert main(["voice", "ipek", "--config", str(path)]) == 0
    assert "voice: ipek (pc:ipek)" in capsys.readouterr().out
    assert 'voice = "ipek"  # who' in path.read_text(encoding="utf-8")
