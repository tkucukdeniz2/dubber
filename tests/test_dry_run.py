# The dry run (tools/dry_run.py): the real session and producer thread on a stand-in player, in
# real time and without sound. Slow: it plays 4 s of video.
#
#     uv run pytest -m slow tests/test_dry_run.py -v
import time

import numpy as np
import pytest

from dubber.config import Config
from dubber.engines.base import Clip, IdentityTranslator
from tools.dry_run import main, run

pytestmark = pytest.mark.slow


def track(sentences=6, every=1.0):
    """Six-word sentences, one each second from 0.5 s, each 0.8 s long."""
    return {"events": [
        {"tStartMs": round((0.5 + i * every) * 1000), "dDurationMs": 800, "segs": [{"utf8": f"This is sentence number {i} here."}]}
        for i in range(sentences)
    ]}


class QuickVoice:
    """Half a second of silence for each sentence, made in 50 ms; opens no device."""
    name = "quick"

    def __init__(self):
        self.calls = 0

    def synthesize(self, text, speed=1.0):
        self.calls += 1
        time.sleep(0.05)
        return Clip(np.zeros(11025, dtype=np.float32), 22050)


def test_a_dry_run_dubs_the_track_in_real_time_and_reports_how_the_engines_kept_up():
    cfg = Config()
    cfg.scheduler.startup_ready = 1.0
    lines = []
    voice = QuickVoice()
    began = time.monotonic()
    summary = run(track(), IdentityTranslator(), voice, cfg, start=0.0, seconds=4.0, out=lines.append)
    assert 4.0 <= time.monotonic() - began < 9.0        # the video's 4 s, and a short hold before them
    assert summary.sentences >= 4 and summary.dropped == 0 and summary.dubbed == summary.sentences
    assert voice.calls >= summary.dubbed
    assert 0 < summary.voice_per_second < 1 and summary.startup < 3.0 and summary.holds == []
    text = "\n".join(lines)
    assert "identity + quick" in text and f"dubbed {summary.dubbed}" in text and "dropped 0" in text


def test_the_command_reads_the_settings_and_the_caption_file(tmp_path, capsys, monkeypatch):
    import json
    captions = tmp_path / "c.json3"
    captions.write_text(json.dumps(track(3)), encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('[voice]\nengine = "supertonic"\nvoice = "M1"\n[scheduler]\nstartup_ready = 1.0\n', encoding="utf-8")
    monkeypatch.setattr("dubber.engines.voice_supertonic.SupertonicVoice.synthesize", QuickVoice().synthesize)
    assert main([str(captions), "--config", str(config), "--seconds", "2"]) == 0
    out = capsys.readouterr().out
    assert "identity + supertonic3:M1" in out and "dropped 0" in out
    assert not (tmp_path / "metrics.jsonl").exists()   # a dry run leaves the metrics file alone


def test_the_command_says_when_the_captions_cannot_be_read(tmp_path, capsys):
    assert main([str(tmp_path / "none.json3")]) == 2
    assert capsys.readouterr().err.startswith("dry run error:")


# A voice that goes by who presents the video needs the video's id, which a caption file does not hold.

class Told(QuickVoice):
    def __init__(self):
        super().__init__()
        self.prepared = []

    def prepare(self, video_id):
        self.prepared.append(video_id)


def test_a_dry_run_names_the_video_it_is_given_to_the_voice():
    cfg = Config()
    cfg.scheduler.startup_ready = 1.0
    voice = Told()
    run(track(3), IdentityTranslator(), voice, cfg, seconds=1.0, out=lambda line: None, video_id="iCvmsMzlF7o")
    assert voice.prepared == ["iCvmsMzlF7o"]


def test_the_command_reads_the_video_in_the_voice_its_presenter_gets(tmp_path, capsys, monkeypatch):
    import json
    from dubber.engines.voice_by_presenter import PresenterVoices
    captions = tmp_path / "c.json3"
    captions.write_text(json.dumps(track(3)), encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text("[scheduler]\nstartup_ready = 1.0\n", encoding="utf-8")
    man, woman = QuickVoice(), QuickVoice()
    man.name, woman.name = "the man", "the woman"
    voices = PresenterVoices(man, woman, lambda video_id: "woman" if video_id == "iCvmsMzlF7o" else None)
    monkeypatch.setattr("tools.dry_run._build_voice", lambda cfg, translator: voices)
    assert main([str(captions), "--config", str(config), "--seconds", "2", "--video", "iCvmsMzlF7o"]) == 0
    assert "identity + the woman" in capsys.readouterr().out
    assert woman.calls > 0 and man.calls == 0
