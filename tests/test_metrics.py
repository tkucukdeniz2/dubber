import dataclasses
import json
import threading

import numpy as np
import pytest

from dubber.metrics import MetricsLog, SegmentRecord, format_stats, stats

def rec(i, lag):
    return SegmentRecord(video_id="v", idx=i, src_start=float(i), src_end=i + 1.0, text_ready_at=0.0, mt_ms=10 * i, mt_engine="identity", tts_ms=50.0, tts_engine="say", audio_seconds=1.0, speed=1.0, scheduled_start=float(i), actual_start=float(i) + lag, lag=lag, dropped=False)

def test_write_and_stats(tmp_path):
    log = MetricsLog(tmp_path / "m.jsonl")
    for i, lag in enumerate([0.1, 0.2, 0.3, 0.4, 1.0]):
        log.write(rec(i, lag))
    s = stats(tmp_path / "m.jsonl")
    assert s["count"] == 5 and s["lag_p50"] == 0.3 and s["lag_p90"] >= 0.4 and s["dropped"] == 0

# Behaviour the brief leaves implicit: sessions, dropped records, the ahead measures,
# malformed input, concurrent writers and the printed form.

def rec_with(i, lag, **changes):
    return dataclasses.replace(rec(i, lag), **changes)

def write_all(path, *records):
    log = MetricsLog(path)
    for r in records:
        log.write(r)
    return path

def value_of(text, label):
    """What format_stats printed after `label` on its line."""
    for line in text.splitlines():
        if line.startswith(label):
            return line[len(label):].strip()
    raise AssertionError(f"no line starts with {label!r} in:\n{text}")

GOOD_LINE = json.dumps(dataclasses.asdict(rec(0, 0.1)))

EMPTY = {
    "session": None, "count": 0, "dropped": 0, "lag_p50": None, "lag_p90": None,
    "mt_ms_p50": None, "tts_ms_p50": None, "ahead_min": None, "ahead_p50": None,
    "ahead_by_minute": [],
}

def test_percentiles_are_observed_values_not_interpolated(tmp_path):
    lags = [0.1, 0.2, 0.3, 0.4, 1.0]
    s = stats(write_all(tmp_path / "m.jsonl", *(rec(i, lag) for i, lag in enumerate(lags))))
    assert s["lag_p50"] == 0.3 and s["lag_p90"] == 1.0    # interpolating would say 0.76
    assert s["mt_ms_p50"] == 20.0 and s["tts_ms_p50"] == 50.0

def test_default_session_is_the_last_lines_and_session_picks_another(tmp_path):
    path = write_all(
        tmp_path / "m.jsonl",
        rec_with(0, 0.1, session="a"), rec_with(1, 0.2, session="a"), rec_with(2, 0.3, session="a"),
        rec_with(3, 0.9, session="b"),
    )
    last = stats(path)
    assert (last["session"], last["count"], last["lag_p50"]) == ("b", 1, 0.9)
    first = stats(path, session="a")
    assert (first["session"], first["count"], first["lag_p50"]) == ("a", 3, 0.2)

def test_records_without_a_session_id_group_under_the_empty_id(tmp_path):
    path = write_all(tmp_path / "m.jsonl", rec_with(0, 0.1, session="s"), rec(1, 0.2), rec(2, 0.3))
    assert rec(1, 0.2).session == ""
    s = stats(path)    # the last line has no session id
    assert s["session"] == "" and s["count"] == 2
    assert stats(path, session="")["count"] == 2
    assert stats(path, session="s")["count"] == 1

def test_lines_written_before_session_and_ahead_existed_still_load(tmp_path):
    old = dataclasses.asdict(rec(0, 0.1))
    del old["session"], old["ahead"]
    path = tmp_path / "m.jsonl"
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    s = stats(path)
    assert s["session"] == "" and s["count"] == 1 and s["ahead_min"] is None

def test_dropped_records_are_counted_but_not_measured(tmp_path):
    path = write_all(
        tmp_path / "m.jsonl",
        rec(0, 0.1), rec(1, 0.3), rec(2, 0.5),
        rec_with(3, 9.0, dropped=True, actual_start=None, mt_ms=9000.0, tts_ms=9000.0, ahead=1.0),
    )
    s = stats(path)
    assert s["count"] == 4 and s["dropped"] == 1
    assert (s["lag_p50"], s["lag_p90"]) == (0.3, 0.5)
    assert s["mt_ms_p50"] == 10.0 and s["tts_ms_p50"] == 50.0
    assert s["ahead_min"] is None and s["ahead_by_minute"] == []    # a dropped clip is never heard

def test_a_session_of_only_dropped_records_has_counts_and_no_measures(tmp_path):
    s = stats(write_all(tmp_path / "m.jsonl", rec_with(0, 1.0, dropped=True), rec_with(1, 2.0, dropped=True)))
    assert s == {**EMPTY, "session": "", "count": 2, "dropped": 2}

def test_ahead_min_p50_and_least_ahead_per_minute(tmp_path):
    path = write_all(
        tmp_path / "m.jsonl",
        rec_with(0, 0.1, src_start=10.0, ahead=50.0),
        rec_with(1, 0.1, src_start=60.0, ahead=48.2),     # minute 1 starts at 60.0
        rec_with(2, 0.1, src_start=100.0, ahead=70.0),
        rec_with(3, 0.1, src_start=119.5, ahead=55.0),
        rec_with(4, 0.1, src_start=59.0, ahead=None),     # no ahead value: ignored
        rec_with(5, 0.1, src_start=130.0, ahead=None),    # so minute 2 has no entry
        rec_with(6, 0.1, src_start=40.0, ahead=30.0),     # back in minute 0, as after a seek
    )
    s = stats(path)
    assert s["ahead_min"] == 30.0
    assert s["ahead_p50"] == 50.0
    assert s["ahead_by_minute"] == [(0, 30.0), (1, 48.2)]

def test_empty_file_and_unknown_session_give_the_empty_summary(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert stats(empty) == EMPTY
    path = write_all(tmp_path / "m.jsonl", rec(0, 0.1))
    assert stats(path, session="nope") == {**EMPTY, "session": "nope"}

def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        stats(tmp_path / "nope.jsonl")

@pytest.mark.parametrize(
    "bad",
    ["{not json", GOOD_LINE[:40], "[1, 2]", '{"video_id": "v"}'],
    ids=["invalid-json", "truncated-record", "json-array", "missing-fields"],
)
def test_malformed_line_raises_value_error_naming_the_line(tmp_path, bad):
    path = tmp_path / "m.jsonl"
    path.write_text(f"{GOOD_LINE}\n\n{GOOD_LINE}\n{bad}\n{GOOD_LINE}\n", encoding="utf-8")    # line 4
    with pytest.raises(ValueError, match=r"line 4\b"):
        stats(path)

def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text(f"\n{GOOD_LINE}\n\n   \n{GOOD_LINE}\n\n", encoding="utf-8")
    assert stats(path)["count"] == 2

def test_write_creates_the_parent_directory(tmp_path):
    path = tmp_path / "a" / "b" / "m.jsonl"
    MetricsLog(path).write(rec(0, 0.1))
    assert stats(path)["count"] == 1

def test_paths_may_be_strings(tmp_path):
    path = str(tmp_path / "m.jsonl")
    MetricsLog(path).write(rec(0, 0.1))
    assert stats(path)["count"] == 1

def test_each_record_is_one_flushed_json_line(tmp_path):
    path = tmp_path / "m.jsonl"
    log = MetricsLog(path)
    r = rec_with(0, 0.1, mt_engine="çeviri", session="s1", ahead=12.5, actual_start=None)
    log.write(r)
    text = path.read_text(encoding="utf-8")    # the log is still in use: write has flushed
    assert text.endswith("\n") and text.count("\n") == 1
    assert "çeviri" in text    # kept as it is, not escaped to ç
    assert json.loads(text) == dataclasses.asdict(r)
    log.write(rec(1, 0.2))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2

def test_a_new_log_appends_to_the_file_of_earlier_sessions(tmp_path):
    path = tmp_path / "m.jsonl"
    MetricsLog(path).write(rec_with(0, 0.1, session="old"))
    MetricsLog(path).write(rec_with(0, 0.2, session="new"))
    assert stats(path)["session"] == "new"
    assert stats(path, session="old")["count"] == 1

def test_concurrent_writes_keep_every_line_whole(tmp_path):
    path = tmp_path / "m.jsonl"
    log = MetricsLog(path)
    writers, each = 8, 25
    start = threading.Barrier(writers)

    def work(n):
        start.wait()
        for i in range(each):
            log.write(rec_with(i, 0.1, session=f"w{n}"))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(path.read_text(encoding="utf-8").splitlines()) == writers * each
    assert [stats(path, session=f"w{n}")["count"] for n in range(writers)] == [each] * writers    # no torn line

def test_stats_returns_plain_python_numbers(tmp_path):
    path = write_all(
        tmp_path / "m.jsonl",
        *(rec_with(i, 0.1 * i, src_start=70.0 * i, ahead=40 + i) for i in range(4)),    # mt_ms and ahead are ints on disk
    )
    s = stats(path)
    assert type(s["count"]) is int and type(s["dropped"]) is int
    for key in ("lag_p50", "lag_p90", "mt_ms_p50", "tts_ms_p50", "ahead_min", "ahead_p50"):
        assert type(s[key]) is float, key
    assert s["ahead_by_minute"] and all(type(m) is int and type(a) is float for m, a in s["ahead_by_minute"])

def test_format_stats_lines_carry_their_units():
    d = {
        "session": "s1", "count": 142, "dropped": 3,
        "lag_p50": 0.2, "lag_p90": 0.481, "mt_ms_p50": 12.0, "tts_ms_p50": 180.4,
        "ahead_min": 31.44, "ahead_p50": 48.2,
        "ahead_by_minute": [(0, 31.44), (3, 48.2), (12, 7.0)],
    }
    text = format_stats(d)
    assert value_of(text, "session") == "s1"
    assert value_of(text, "segments") == "142"
    assert value_of(text, "dropped") == "3"
    assert value_of(text, "lag p50") == "0.20 s"
    assert value_of(text, "lag p90") == "0.48 s"
    assert value_of(text, "translation p50") == "12 ms"
    assert value_of(text, "voice p50") == "180 ms"
    assert value_of(text, "ahead min") == "31.4 s"
    assert value_of(text, "ahead p50") == "48.2 s"
    assert text.splitlines()[-3:] == ["0:00  31.4 s", "3:00  48.2 s", "12:00  7.0 s"]

def test_format_stats_shows_n_a_for_what_has_no_value(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    text = format_stats(stats(empty))
    for label in ("session", "lag p50", "lag p90", "translation p50", "voice p50", "ahead min", "ahead p50"):
        assert value_of(text, label) == "n/a", label
    assert value_of(text, "segments") == "0" and value_of(text, "dropped") == "0"
    assert text.splitlines()[-1] == "n/a"    # and no curve

def test_format_stats_prints_numpy_scalars_as_plain_numbers():
    d = {
        "session": "s", "count": np.int64(5), "dropped": np.int64(0),
        "lag_p50": np.float64(0.3), "lag_p90": np.float64(1.0),
        "mt_ms_p50": np.float64(20.0), "tts_ms_p50": np.int64(50),
        "ahead_min": np.float64(30.0), "ahead_p50": np.float64(50.0),
        "ahead_by_minute": [(np.int64(1), np.float64(48.2))],
    }
    text = format_stats(d)
    assert "np." not in text and "float64" not in text and "int64" not in text
    assert value_of(text, "lag p50") == "0.30 s" and "1:00  48.2 s" in text.splitlines()

def test_format_stats_prints_what_stats_returns(tmp_path):
    lags = [0.1, 0.2, 0.3, 0.4, 1.0]
    path = write_all(
        tmp_path / "m.jsonl",
        *(rec_with(i, lag, src_start=60.0 * i, ahead=40.0 + i) for i, lag in enumerate(lags)),
    )
    text = format_stats(stats(path))
    assert value_of(text, "lag p50") == "0.30 s" and value_of(text, "lag p90") == "1.00 s"
    assert value_of(text, "translation p50") == "20 ms" and value_of(text, "voice p50") == "50 ms"
    assert text.splitlines()[-5:] == ["0:00  40.0 s", "1:00  41.0 s", "2:00  42.0 s", "3:00  43.0 s", "4:00  44.0 s"]


def test_metrics_lines_without_shortened_still_load(tmp_path):
    old = dataclasses.asdict(rec(0, 0.5))
    del old["shortened"]
    path = tmp_path / "m.jsonl"
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    assert stats(path)["count"] == 1 and rec(1, 0.5).shortened is False
