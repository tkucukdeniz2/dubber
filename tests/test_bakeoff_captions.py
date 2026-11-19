import datetime
import json

import pytest

from tools.bakeoff_captions import main
from tools.bakeoff_captions import record

def test_record_merges_inventory(tmp_path):
    p = tmp_path / "inv.json"
    record(p, "XP5k0bHmhqc", [{"lang": "en", "kind": "asr"}, {"lang": "tr", "kind": "translated"}])
    record(p, "qp0HIF3SfI4", [{"lang": "en", "kind": "manual"}, {"lang": "tr", "kind": "manual"}])
    inv = json.loads(p.read_text())
    assert inv["XP5k0bHmhqc"]["asr_en"] is True and inv["XP5k0bHmhqc"]["manual_tr"] is False
    assert inv["qp0HIF3SfI4"]["manual_tr"] is True and inv["qp0HIF3SfI4"]["manual_en"] is True

FLAGS = ("manual_tr", "manual_en", "asr_en", "translated_tr")
NOV_1 = datetime.date(2026, 11, 1)

def test_record_stamps_the_date_it_is_given(tmp_path):
    inv = record(tmp_path / "inv.json", "XP5k0bHmhqc", [], today=NOV_1)
    assert inv["XP5k0bHmhqc"]["checked"] == "2026-11-01"

def test_record_stamps_today_by_default(tmp_path):
    before = datetime.date.today().isoformat()
    inv = record(tmp_path / "inv.json", "XP5k0bHmhqc", [])
    after = datetime.date.today().isoformat()
    assert inv["XP5k0bHmhqc"]["checked"] in {before, after}

def test_record_entry_is_the_four_flags_and_the_date(tmp_path):
    tracks = [{"lang": "en", "kind": "asr"}, {"lang": "tr", "kind": "translated"}]
    inv = record(tmp_path / "inv.json", "XP5k0bHmhqc", tracks, today=NOV_1)
    assert inv["XP5k0bHmhqc"] == {
        "manual_tr": False,
        "manual_en": False,
        "asr_en": True,
        "translated_tr": True,
        "checked": "2026-11-01",
    }

def test_record_returns_what_it_wrote(tmp_path):
    p = tmp_path / "inv.json"
    record(p, "XP5k0bHmhqc", [{"lang": "en", "kind": "asr"}])
    inv = record(p, "qp0HIF3SfI4", [])
    assert inv == json.loads(p.read_text())
    assert list(inv) == ["XP5k0bHmhqc", "qp0HIF3SfI4"]

def test_record_replaces_a_videos_entry_and_keeps_the_others(tmp_path):
    p = tmp_path / "inv.json"
    both = [{"lang": "en", "kind": "asr"}, {"lang": "tr", "kind": "translated"}]
    record(p, "XP5k0bHmhqc", both, today=NOV_1)
    record(p, "qp0HIF3SfI4", [{"lang": "en", "kind": "manual"}], today=NOV_1)
    record(p, "XP5k0bHmhqc", [{"lang": "en", "kind": "asr"}], today=NOV_1 + datetime.timedelta(days=1))
    inv = json.loads(p.read_text())
    assert inv["XP5k0bHmhqc"]["translated_tr"] is False
    assert inv["XP5k0bHmhqc"]["checked"] == "2026-11-02"
    assert inv["qp0HIF3SfI4"]["manual_en"] is True and inv["qp0HIF3SfI4"]["checked"] == "2026-11-01"

def test_record_creates_the_inventory_folder(tmp_path):
    p = tmp_path / "bakeoff" / "nested" / "inv.json"
    record(p, "XP5k0bHmhqc", [])
    assert p.is_file()

def test_record_writes_indented_json(tmp_path):
    p = tmp_path / "inv.json"
    record(p, "XP5k0bHmhqc", [{"lang": "en", "kind": "asr"}], today=NOV_1)
    text = p.read_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"

def test_record_reads_a_region_code_as_its_language(tmp_path):
    tracks = [{"lang": "en-GB", "kind": "manual"}, {"lang": "tr-TR", "kind": "manual"}]
    entry = record(tmp_path / "inv.json", "XP5k0bHmhqc", tracks)["XP5k0bHmhqc"]
    assert entry["manual_en"] is True and entry["manual_tr"] is True

def test_record_ignores_tracks_that_set_no_flag(tmp_path):
    # Auto-generated Turkish is not a human Turkish track, and German is not tracked.
    tracks = [{"lang": "tr", "kind": "asr"}, {"lang": "de", "kind": "manual"}]
    entry = record(tmp_path / "inv.json", "XP5k0bHmhqc", tracks)["XP5k0bHmhqc"]
    assert [entry[flag] for flag in FLAGS] == [False, False, False, False]

TRACKS = '[{"lang": "en", "kind": "asr"}, {"lang": "tr", "kind": "translated"}]'

def test_main_records_the_video_and_says_so(tmp_path, capsys):
    p = tmp_path / "inv.json"
    main(["--video", "XP5k0bHmhqc", "--tracks", TRACKS, "--inventory", str(p)])
    entry = json.loads(p.read_text())["XP5k0bHmhqc"]
    assert entry["asr_en"] is True and entry["translated_tr"] is True
    assert entry["manual_en"] is False and entry["manual_tr"] is False
    assert "XP5k0bHmhqc" in capsys.readouterr().out

def test_main_takes_a_video_id_that_starts_with_a_dash(tmp_path):
    # The snippet prints --video=<id>: written with a space, argparse reads "-abc12345678" as an option.
    p = tmp_path / "inv.json"
    main(["--video=-abc12345678", "--tracks", TRACKS, "--inventory", str(p)])
    entry = json.loads(p.read_text())["-abc12345678"]
    assert entry["asr_en"] is True and entry["translated_tr"] is True

def test_main_adds_to_the_inventory_it_finds(tmp_path):
    p = tmp_path / "inv.json"
    main(["--video", "XP5k0bHmhqc", "--tracks", TRACKS, "--inventory", str(p)])
    main(["--video", "qp0HIF3SfI4", "--tracks", "[]", "--inventory", str(p)])
    assert list(json.loads(p.read_text())) == ["XP5k0bHmhqc", "qp0HIF3SfI4"]

def test_main_keeps_the_inventory_in_the_bakeoff_folder_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["--video", "XP5k0bHmhqc", "--tracks", TRACKS])
    inv = json.loads((tmp_path / "bakeoff" / "captions_inventory.json").read_text())
    assert inv["XP5k0bHmhqc"]["asr_en"] is True

@pytest.mark.parametrize(
    "tracks",
    ["[{lang: en}]", '{"lang": "en", "kind": "asr"}', '["en"]', '[{"lang": "en"}]', '[{"lang": 1, "kind": "asr"}]'],
)
def test_main_rejects_tracks_that_are_not_the_snippets_list(tmp_path, capsys, tracks):
    p = tmp_path / "inv.json"
    with pytest.raises(SystemExit) as stop:
        main(["--video", "XP5k0bHmhqc", "--tracks", tracks, "--inventory", str(p)])
    assert stop.value.code == 2
    assert "--tracks" in capsys.readouterr().err
    assert not p.exists()

def test_main_names_an_inventory_it_cannot_read_and_leaves_it_alone(tmp_path, capsys):
    p = tmp_path / "inv.json"
    p.write_text("not json")
    with pytest.raises(SystemExit) as stop:
        main(["--video", "XP5k0bHmhqc", "--tracks", TRACKS, "--inventory", str(p)])
    assert stop.value.code == 2
    assert str(p) in capsys.readouterr().err
    assert p.read_text() == "not json"
