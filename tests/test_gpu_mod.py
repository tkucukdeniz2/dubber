# pc/gpu_mod.py, the PC's mode service, run in its own fake mode (GPU_MOD_FAKE): the processes
# it would start and stop are flag files in a folder. These tests cover the dubbing mode.
import importlib.util
import json
import pathlib
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

ROOT = pathlib.Path(__file__).parents[1]


@pytest.fixture
def gm(tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.mkdir()
    dub_preset = tmp_path / "models-dublaj.ini"
    dub_preset.write_text("[translategemma-12b]\n", encoding="utf-8")
    video_preset = tmp_path / "models-video.ini"
    video_preset.write_text("[gemma4-12b]\n", encoding="utf-8")
    config = tmp_path / "gpu-mod.json"
    config.write_text(json.dumps({
        "log": str(tmp_path / "gpu-mod.log"),
        "dub_preset": str(dub_preset),
        "hybrid_preset": str(video_preset),
        "dub_idle_minutes": 45,
    }), encoding="utf-8")
    monkeypatch.setenv("GPU_MOD_FAKE", str(fake))
    monkeypatch.setenv("GPU_MOD_CONFIG", str(config))
    spec = importlib.util.spec_from_file_location(f"gpu_mod_{tmp_path.name}", ROOT / "pc" / "gpu_mod.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    module.fake, module.dub_preset, module.video_preset = fake, dub_preset, video_preset
    return module


def on(gm, name):
    return (gm.fake / name).exists()


def in_llm_mode(gm):
    (gm.fake / "llm.on").touch()
    return gm.Modes()


def test_dubbing_mode_stops_qwen_and_starts_the_translator_and_the_voice(gm):
    modes = in_llm_mode(gm)
    code, res = modes.to_dub()
    assert code == 200 and res["tamam"] and modes.mode == "dublaj"
    assert not on(gm, "llm.on") and on(gm, "tts.on")
    assert (gm.fake / "hybrid.on").read_text() == str(gm.dub_preset)  # the translator runs the dubbing preset
    status = modes.status()
    assert status["mod"] == "dublaj" and status["llm"]["dublaj_cevirmen_4070"] is True
    assert status["dublaj"]["ses_calisiyor"] is True and status["dublaj"]["bosta_dk"] == 0
    assert modes.to_dub() == (200, {"tamam": True, "mesaj": "Zaten dublaj modunda."})


def test_dubbing_mode_waits_for_busy_comfyui_unless_forced(gm):
    (gm.fake / "comfy.on").touch()
    (gm.fake / "queue").write_text("2")
    modes = gm.Modes()
    assert modes.mode == "video"
    code, res = modes.to_dub()
    assert code == 409 and res["kuyruk"] == 2 and on(gm, "comfy.on") and modes.mode == "video"
    code, _ = modes.to_dub(force=True)
    assert code == 200 and not on(gm, "comfy.on") and on(gm, "tts.on") and modes.mode == "dublaj"


def test_dubbing_mode_needs_its_translator_preset(gm):
    gm.dub_preset.unlink()
    modes = in_llm_mode(gm)
    code, res = modes.to_dub()
    assert code == 500 and "ön ayarı yok" in res["mesaj"]
    assert modes.mode == "llm" and on(gm, "llm.on") and not on(gm, "tts.on")


def test_returning_to_llm_mode_stops_the_voice_first(gm):
    modes = in_llm_mode(gm)
    modes.to_dub()
    code, _ = modes.to_llm()
    assert code == 200 and modes.mode == "llm"
    assert not on(gm, "tts.on") and not on(gm, "hybrid.on") and on(gm, "llm.on")
    assert modes.status()["llm"]["dublaj_cevirmen_4070"] is False


def test_switching_to_video_mode_frees_the_4080_super_from_the_voice(gm):
    modes = in_llm_mode(gm)
    modes.to_dub()
    code, _ = modes.to_video()
    assert code == 200 and modes.mode == "video"
    assert not on(gm, "tts.on") and on(gm, "comfy.on")
    assert (gm.fake / "hybrid.on").read_text() == str(gm.video_preset)  # Gemma's video preset, not the dubbing one


def test_an_llm_mode_with_a_stray_voice_service_is_not_already_llm_mode(gm):
    modes = in_llm_mode(gm)
    (gm.fake / "tts.on").touch()  # a voice service someone started by hand
    modes.mode = "llm"
    code, res = modes.to_llm()
    assert code == 200 and res["mesaj"] != "Zaten LLM modunda." and not on(gm, "tts.on")


def test_idle_dubbing_returns_to_llm_mode_after_the_configured_minutes(gm):
    modes = in_llm_mode(gm)
    modes.to_dub()
    (gm.fake / "tts.idle").write_text(str(44 * 60))
    modes.watch_dub()
    assert modes.mode == "dublaj"
    (gm.fake / "tts.idle").write_text(str(46 * 60))
    modes.watch_dub()
    assert modes.mode == "llm" and not on(gm, "tts.on") and on(gm, "llm.on")


def test_a_voice_service_that_died_returns_to_llm_mode_after_three_checks(gm):
    modes = in_llm_mode(gm)
    modes.to_dub()
    (gm.fake / "tts.on").unlink()
    modes.watch_dub()
    modes.watch_dub()
    assert modes.mode == "dublaj"
    modes.watch_dub()
    assert modes.mode == "llm" and on(gm, "llm.on")


def test_a_restarted_mode_service_finds_dubbing_mode(gm):
    (gm.fake / "tts.on").touch()
    (gm.fake / "hybrid.on").write_text("x")
    modes = gm.Modes()
    assert modes.mode == "dublaj" and modes.dub_llm_on is True


def test_post_dublaj_needs_the_key_and_switches_the_mode(gm):
    (gm.fake / "llm.on").touch()
    gm.MODES, gm.KEY = gm.Modes(), "the-key"
    server = ThreadingHTTPServer(("127.0.0.1", 0), gm.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert httpx.post(base + "/dublaj").status_code == 401
        reply = httpx.post(base + "/dublaj", headers={"Authorization": "Bearer the-key"})
        assert reply.status_code == 200 and reply.json()["durum"]["mod"] == "dublaj"
    finally:
        server.shutdown()
        server.server_close()


def test_dubbing_gives_up_at_once_when_the_translator_does_not_start(gm):
    (gm.fake / "hybrid.fail").touch()
    modes = in_llm_mode(gm)
    code, res = modes.to_dub()
    assert code == 500 and "çevirmen başlamadı" in res["mesaj"]
    assert not on(gm, "tts.on") and on(gm, "llm.on") and modes.mode == "llm"  # the voice never started


def test_dubbing_stops_waiting_when_the_voice_reports_it_failed(gm, monkeypatch):
    monkeypatch.setattr(gm.SYS, "start_tts", lambda: (gm.fake / "tts.failed").touch())
    modes = in_llm_mode(gm)
    code, res = modes.to_dub()
    assert code == 500 and "ses servisi başarısız" in res["mesaj"] and modes.mode == "llm" and on(gm, "llm.on")


def test_forced_dubbing_restarts_both_services_even_in_dubbing_mode(gm, monkeypatch):
    modes = in_llm_mode(gm)
    modes.to_dub()
    starts = []
    real_start = gm.SYS.start_tts
    monkeypatch.setattr(gm.SYS, "start_tts", lambda: starts.append(1) or real_start())
    assert modes.to_dub()[1]["mesaj"] == "Zaten dublaj modunda." and starts == []
    code, res = modes.to_dub(force=True)
    assert code == 200 and res["mesaj"] == "Dublaj modu hazır." and starts == [1]


def test_a_dead_translator_is_restarted_while_the_voice_lives(gm):
    modes = in_llm_mode(gm)
    modes.to_dub()
    (gm.fake / "hybrid.on").unlink()  # the 4070's llama-server died
    modes.watch_dub()
    modes.watch_dub()
    assert not on(gm, "hybrid.on")
    modes.watch_dub()
    assert modes.mode == "dublaj" and (gm.fake / "hybrid.on").read_text() == str(gm.dub_preset)


def test_a_return_to_llm_whose_reason_is_gone_does_nothing(gm):
    modes = in_llm_mode(gm)
    modes.to_dub()
    code, res = modes.to_llm(force=True, reason="test", only_if=lambda: False)
    assert code == 200 and "gerek kalmadı" in res["mesaj"] and modes.mode == "dublaj" and on(gm, "tts.on")


@pytest.fixture
def clock(gm, monkeypatch):
    """A clock that moves only when the mode service sleeps, so the waits in to_dub run at once."""
    now = [1000.0]
    monkeypatch.setattr(gm.time, "time", lambda: now[0])
    monkeypatch.setattr(gm.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_dubbing_gives_up_when_the_translator_exits_right_after_launch(gm, clock, monkeypatch):
    monkeypatch.setattr(gm.SYS, "start_hybrid", lambda preset=None, log_path=None: True)  # launched, then gone
    modes = in_llm_mode(gm)
    began = clock[0]
    code, res = modes.to_dub()
    assert code == 500 and "çevirmen kapandı" in res["mesaj"] and modes.mode == "llm" and on(gm, "llm.on")
    assert clock[0] - began < 30  # not the ten-minute wait


def test_dubbing_gives_up_when_the_voice_process_is_gone_after_30_s(gm, clock, monkeypatch):
    monkeypatch.setattr(gm.SYS, "start_tts", lambda: None)  # the voice service never starts
    modes = in_llm_mode(gm)
    began = clock[0]
    code, res = modes.to_dub()
    assert code == 500 and "ses servisi süreci yok" in res["mesaj"] and modes.mode == "llm"
    assert 30 < clock[0] - began < 45


def test_dubbing_gives_up_after_ten_minutes_and_lists_processes_only_every_ten_seconds(gm, clock, monkeypatch):
    checks = []
    monkeypatch.setattr(gm.SYS, "tts_up", lambda: False)  # alive, but never ready
    monkeypatch.setattr(gm.SYS, "tts_alive", lambda: checks.append(clock[0]) or True)
    modes = in_llm_mode(gm)
    code, res = modes.to_dub()
    assert code == 500 and "10 dk içinde hazır olmadı" in res["mesaj"] and modes.mode == "llm"
    assert 40 < len(checks) < 60 and all(b - a >= 10 for a, b in zip(checks, checks[1:]))


@pytest.mark.parametrize("rc, out, alive", [
    (0, "0\r\n", False),
    (0, "2\r\n", True),
    (-1, "Command timed out after 60 seconds", True),
    (0, "", True),
    (1, "Get-CimInstance : Access denied", True),
])
def test_only_a_sure_zero_means_the_voice_process_is_gone(gm, monkeypatch, rc, out, alive):
    monkeypatch.setattr(gm, "FAKE", None)
    monkeypatch.setattr(gm, "ps", lambda script, timeout=60: (rc, out))
    assert gm.SYS.tts_alive() is alive


def test_a_qwen_task_seen_while_dubbing_is_stopped_only_if_still_dubbing(gm, monkeypatch):
    modes = in_llm_mode(gm)
    modes.to_dub()
    stops = []
    monkeypatch.setattr(gm.SYS, "llm_task_running", lambda: True)
    monkeypatch.setattr(gm.SYS, "stop_llm", lambda: stops.append(1))
    modes.mode = "llm"  # a return to LLM mode finished while the watchdog waited for the lock
    modes.watch_dub()
    assert stops == []
    modes.mode = "dublaj"
    modes.watch_dub()
    assert stops == [1]
