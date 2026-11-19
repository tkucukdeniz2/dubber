# tests/test_bakeoff_voice.py
import numpy as np, pathlib
from dubber.engines.base import Clip, VoiceError
from tools.voice_sentences import SENTENCES, SEAM_IDS
from tools.bakeoff_voice import run, prefilter
from tools.bakeoff_score import score_voice
# For the tests added below the brief's.
import json
import re
import pytest
import soundfile as sf
from tools import bakeoff_voice
from tools.bakeoff_score import main as score_main, unread_voice_lines

class Beep:
    def __init__(self, name, secs=1.0, fail=False): self.name, self.secs, self.fail = name, secs, fail
    def synthesize(self, text, speed=1.0):
        if self.fail: raise VoiceError("no model")
        return Clip(samples=np.zeros(int(22050 * self.secs), dtype=np.float32), sample_rate=22050)

def test_sentence_set_is_fixed():
    assert len(SENTENCES) == 16 and [i for i, _ in SENTENCES][:3] == ["n1", "n2", "n3"] and SEAM_IDS == ("s1", "s2", "s3")

def test_run_hides_engine_names_and_merges_seam(tmp_path):
    sheet, key = run({"a": Beep("a"), "b": Beep("b")}, tmp_path, seed=3)
    assert "Beep" not in sheet and "a`" not in sheet
    assert len(list(tmp_path.glob("v_*.wav"))) == 2 * 14   # 13 singles + 1 seam file per engine
    assert set(key["s1"].values()) == {"a", "b"} and "s2" not in key

def test_run_records_unavailable_engine(tmp_path):
    sheet, key = run({"a": Beep("a"), "bad": Beep("bad", fail=True)}, tmp_path, seed=3)
    assert "engine unavailable: bad: no model" in sheet

def test_prefilter_flags_implausible_length():
    assert prefilter("Merhaba dünya, bugün hava güzel.", 0.2) == "suspect"
    assert prefilter("Merhaba dünya, bugün hava güzel.", 2.0) == "ok"

def test_score_voice_pass_rule():
    sheet = "".join(f"### n{i}\nx\n- `f{i}a.wav` → mark: ok\n- `f{i}b.wav` → mark: {'defect' if i < 4 else 'ok'}\n" for i in range(16))
    key = {f"n{i}": {f"f{i}a.wav": "a", f"f{i}b.wav": "b"} for i in range(16)}
    res = score_voice(sheet, key)
    assert res["a"]["ok"] == 16 and res["a"]["pass"] is True
    assert res["b"]["ok"] == 12 and res["b"]["pass"] is False

# What the tests above leave open: the warm-up, the timing file, the pre-filter's effect on
# the sheet, the file names and the scorer.

TEXTS = dict(SENTENCES)

def clip(seconds, rate=22050, level=0.0):
    return Clip(np.full(int(rate * seconds), level, dtype=np.float32), rate)

class Script:
    """A voice that answers each text from a table (a Clip, or a VoiceError to raise) and any
    other text with 3.5 s of silence, a length the pre-filter accepts for all 16 sentences.
    It logs the texts it is asked."""
    def __init__(self, answers=None, rate=22050):
        self.answers, self.rate, self.calls = answers or {}, rate, []
    def synthesize(self, text, speed=1.0):
        self.calls.append(text)
        answer = self.answers.get(text, clip(3.5, self.rate))
        if isinstance(answer, Exception): raise answer
        return answer

class FakeClock:
    """Stands in for the time module: the clock moves only when a voice says it took time."""
    def __init__(self): self.now = 0.0
    def perf_counter(self): return self.now

class Timed:
    """A voice that takes `first` fake seconds for its first request and `each` for every
    later one, and answers each with 3.5 s of silence."""
    def __init__(self, clock, first, each):
        self.clock, self.first, self.each, self.asked = clock, first, each, 0
    def synthesize(self, text, speed=1.0):
        self.clock.now += self.each if self.asked else self.first
        self.asked += 1
        return clip(3.5)

def timing_of(folder):
    return json.loads((folder / "voice_timing.json").read_text(encoding="utf-8"))

def candidates(sheet):
    """The file names listed under each heading of the sheet, in sheet order. Only a line in
    the exact format - `v_<12 hex>.wav` -> mark: counts."""
    found, current = {}, None
    for line in sheet.splitlines():
        if line.startswith("### "):
            current = found.setdefault(line[4:], [])
        elif found_line := re.fullmatch(r"- `(v_[0-9a-f]{12}\.wav)` → mark: ", line):
            current.append(found_line[1])
    return found

@pytest.mark.parametrize("chars, seconds, verdict", [
    (14, 2.0, "ok"),        # 7 characters a second
    (12, 2.0, "ok"),        # exactly 6, the lower limit
    (11, 2.0, "suspect"),   # 5.5
    (10, 2.0, "suspect"),   # 5
    (50, 2.0, "ok"),        # exactly 25, the upper limit
    (51, 2.0, "suspect"),   # 25.5
    (10, 0.0, "suspect"),   # an empty clip is suspect, not a division by zero
])
def test_prefilter_limits_are_inclusive_and_an_empty_clip_is_suspect(chars, seconds, verdict):
    assert prefilter("x" * chars, seconds) == verdict

def test_warm_up_is_the_first_request_and_is_made_once(tmp_path):
    voice = Script()
    run({"a": voice}, tmp_path, seed=1)
    assert voice.calls == ["Merhaba."] + [text for _, text in SENTENCES]

def test_a_voice_that_fails_its_warm_up_is_unavailable_and_asked_nothing_more(tmp_path):
    dead = Script({"Merhaba.": VoiceError("model missing")})
    sheet, key = run({"a": Script(), "dead": dead}, tmp_path, seed=1)
    assert dead.calls == ["Merhaba."]
    assert "engine unavailable: dead: model missing" in sheet
    assert "dead" not in timing_of(tmp_path)
    assert all("dead" not in files.values() for files in key.values())

def test_an_error_on_one_sentence_drops_that_clip_and_the_run_goes_on(tmp_path):
    sheet, key = run({"a": Script({TEXTS["n3"]: VoiceError("bad token")}), "b": Script()}, tmp_path, seed=1)
    assert "engine unavailable" not in sheet
    assert timing_of(tmp_path)["a"]["errors"] == ["n3"] and timing_of(tmp_path)["b"]["errors"] == []
    assert [len(key[block]) for block in ("n2", "n3", "n4", "s1")] == [2, 1, 2, 2]

def test_an_error_inside_the_seam_drops_the_seam_for_that_voice_under_the_request_id(tmp_path):
    sheet, key = run({"a": Script({TEXTS["s2"]: VoiceError("cut off")}), "b": Script()}, tmp_path, seed=1)
    assert timing_of(tmp_path)["a"]["errors"] == ["s2"]
    assert len(key["s1"]) == 1 and len(key["c1"]) == 2

def test_timing_records_speed_total_and_first_call_apart(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(bakeoff_voice, "time", clock)
    run({"slow": Timed(clock, first=6.25, each=1.75)}, tmp_path, seed=1)
    # 16 requests of 1.75 s over 16 clips of 3.5 s; the 6.25 s warm-up is in neither
    assert timing_of(tmp_path)["slow"] == {
        "seconds_per_audio_second": 0.5,
        "synthesis_seconds_total": 28.0,
        "first_call_seconds": 6.25,
        "suspect": [],
        "errors": [],
    }

def test_a_voice_whose_every_sentence_fails_has_no_speed_and_no_crash(tmp_path):
    answers = {text: VoiceError("no") for text in TEXTS.values()}
    run({"a": Script(answers)}, tmp_path, seed=1)
    entry = timing_of(tmp_path)["a"]
    assert entry["seconds_per_audio_second"] is None and entry["synthesis_seconds_total"] == 0

def test_a_suspect_clip_stays_off_the_sheet_and_is_listed_in_the_timing(tmp_path):
    sheet, key = run({"a": Script({TEXTS["n3"]: clip(0.3)}), "b": Script()}, tmp_path, seed=1)
    listed = candidates(sheet)
    assert [len(listed[block]) for block in ("n2", "n3", "n4")] == [2, 1, 2]
    assert timing_of(tmp_path)["a"]["suspect"] == ["n3"] and timing_of(tmp_path)["b"]["suspect"] == []
    assert listed["n3"][0] in key["n3"] and len(key["n3"]) == 2   # the key still names every wav written

# The seam is pre-filtered once, as a whole: its joined text over its joined clip.

@pytest.mark.parametrize("seconds", [0.3, 8.0])   # s2 alone would run at 140 and at 5.25 characters a second
def test_one_odd_seam_sentence_does_not_drop_the_seam_when_the_three_together_are_plausible(tmp_path, seconds):
    assert prefilter(TEXTS["s2"], seconds) == "suspect"
    sheet, key = run({"a": Script({TEXTS["s2"]: clip(seconds)}), "b": Script()}, tmp_path, seed=1)
    assert len(candidates(sheet)["s1"]) == 2
    assert timing_of(tmp_path)["a"]["suspect"] == []

@pytest.mark.parametrize("seconds", [0.3, 20.0])   # 136 characters over 0.9 s and over 60 s
def test_a_seam_that_is_suspect_as_a_whole_is_kept_off_the_sheet_and_listed_as_s1(tmp_path, seconds):
    seam = {TEXTS[sentence_id]: clip(seconds) for sentence_id in SEAM_IDS}
    sheet, key = run({"a": Script(seam), "b": Script()}, tmp_path, seed=1)
    assert len(candidates(sheet)["s1"]) == 1
    assert timing_of(tmp_path)["a"]["suspect"] == ["s1"]
    assert len(key["s1"]) == 2   # the wav is written and keyed all the same

def test_the_seam_is_judged_on_its_joined_text_with_the_spaces_between_the_sentences(tmp_path):
    # 2.0 + 1.7 + 1.7 = 5.4 s. Each sentence alone is within the limit (25.0, 24.7 and 24.7 characters a
    # second) and the three hold 134 characters (24.8 a second), but the joined text is 136 (25.2).
    seam = {TEXTS["s1"]: clip(2.0), TEXTS["s2"]: clip(1.7), TEXTS["s3"]: clip(1.7)}
    assert [prefilter(TEXTS[i], s) for i, s in (("s1", 2.0), ("s2", 1.7), ("s3", 1.7))] == ["ok", "ok", "ok"]
    sheet, key = run({"a": Script(seam), "b": Script()}, tmp_path, seed=1)
    assert len(candidates(sheet)["s1"]) == 1
    assert timing_of(tmp_path)["a"]["suspect"] == ["s1"]

def without(files, *engines):
    """The (file, engine) pairs of one key block, in order, minus the given engines."""
    return [(name, engine) for name, engine in files.items() if engine not in engines]

def test_every_wav_is_named_v_and_twelve_hex_characters_and_the_key_names_each_one(tmp_path):
    sheet, key = run({"a": Script(), "b": Script()}, tmp_path, seed=5)
    keyed = sorted(name for files in key.values() for name in files)
    assert sorted(path.name for path in tmp_path.glob("*.wav")) == keyed and len(keyed) == 28
    assert all(re.fullmatch(r"v_[0-9a-f]{12}\.wav", name) for name in keyed)

def test_the_same_seed_reproduces_sheet_key_and_names_and_another_seed_does_not(tmp_path):
    def engines(): return {"a": Script(), "b": Script(), "c": Script()}
    first = run(engines(), tmp_path / "one", seed=7)
    assert run(engines(), tmp_path / "two", seed=7) == first
    assert run(engines(), tmp_path / "three", seed=8) != first

def test_names_and_order_do_not_depend_on_which_sentences_a_voice_answered(tmp_path):
    healthy = {"a": Script(), "b": Script(), "c": Script()}
    stumbling = {"a": Script(), "b": Script({TEXTS["n3"]: VoiceError("x")}), "c": Script()}
    _, key_1 = run(healthy, tmp_path / "one", seed=7)
    _, key_2 = run(stumbling, tmp_path / "two", seed=7)
    for block in key_1:
        missing = ("b",) if block == "n3" else ()
        assert list(key_2[block].items()) == without(key_1[block], *missing)

def test_names_and_order_do_not_depend_on_which_voices_were_available(tmp_path):
    dead = Script({"Merhaba.": VoiceError("off")})
    _, key_1 = run({"a": Script(), "b": Script(), "c": Script()}, tmp_path / "one", seed=7)
    _, key_2 = run({"a": Script(), "b": dead, "c": Script()}, tmp_path / "two", seed=7)
    for block in key_1:
        assert list(key_2[block].items()) == without(key_1[block], "b")

def test_the_voices_come_in_a_different_order_from_block_to_block(tmp_path):
    sheet, key = run({name: Script() for name in "abcd"}, tmp_path, seed=7)
    orders = [[key[block][file] for file in files] for block, files in candidates(sheet).items()]
    assert all(sorted(order) == list("abcd") for order in orders)
    assert len({tuple(order) for order in orders}) > 1   # unshuffled, position would give the voice away

def test_each_wav_keeps_its_clips_sample_rate_and_the_seam_joins_its_three_clips_end_to_end(tmp_path):
    seam = {
        TEXTS["s1"]: clip(3.5, 48000, level=0.25),
        TEXTS["s2"]: clip(3.0, 48000, level=0.5),
        TEXTS["s3"]: clip(4.0, 48000, level=-0.25),
    }
    sheet, key = run({"hi": Script(seam, rate=48000), "lo": Script()}, tmp_path, seed=2)
    def wav(block, engine):
        return tmp_path / next(name for name, owner in key[block].items() if owner == engine)
    joined, rate = sf.read(wav("s1", "hi"))
    assert rate == 48000 and len(joined) == 504000          # (3.5 + 3.0 + 4.0) s at 48 kHz, no silence between
    assert np.allclose(joined[:168000], 0.25, atol=1e-3)
    assert np.allclose(joined[168000:312000], 0.5, atol=1e-3)
    assert np.allclose(joined[312000:], -0.25, atol=1e-3)
    assert (sf.info(wav("s1", "lo")).samplerate, sf.info(wav("s1", "lo")).frames) == (22050, 231525)   # 3 x 3.5 s
    assert (sf.info(wav("n1", "hi")).samplerate, sf.info(wav("n1", "hi")).frames) == (48000, 168000)

def test_sheet_has_a_block_per_sentence_with_the_seam_as_one_and_names_no_engine(tmp_path):
    sheet, key = run({"quiet-voice": Script(), "loud-voice": Script()}, tmp_path, seed=3)
    listed = candidates(sheet)
    assert list(listed) == ["n1", "n2", "n3", "n4", "n5", "i1", "i2", "a1", "d1", "l1", "q1", "e1", "c1", "s1"]
    assert all(files and set(files) == set(key[block]) for block, files in listed.items())
    assert "quiet-voice" not in sheet and "loud-voice" not in sheet
    assert f"### n1\n{TEXTS['n1']}\n- `v_" in sheet
    assert f"### s1\n{TEXTS['s1']} {TEXTS['s2']} {TEXTS['s3']}\n- `v_" in sheet
    assert len(sheet.split("\n\n")) == 14 and sheet.endswith("\n") and not sheet.endswith("\n\n")

def test_unavailable_voices_are_listed_once_each_at_the_top_and_nowhere_else(tmp_path):
    sheet, key = run({
        "alpha-voice": Script({"Merhaba.": VoiceError("no\nmodel")}),
        "ok-voice": Script(),
        "beta-voice": Script({"Merhaba.": VoiceError("gone")}),
    }, tmp_path, seed=3)
    assert sheet.startswith("engine unavailable: alpha-voice: no model\nengine unavailable: beta-voice: gone\n\n### n1\n")
    assert sheet.count("alpha-voice") == 1 and sheet.count("beta-voice") == 1

def test_a_run_with_no_available_voice_still_writes_the_sheet_key_and_timing(tmp_path):
    sheet, key = run({"a": Script({"Merhaba.": VoiceError("off")})}, tmp_path, seed=1)
    assert sheet.startswith("engine unavailable: a: off\n\n### n1\n") and candidates(sheet)["n1"] == []
    assert key["n1"] == {} and timing_of(tmp_path) == {}

# The scorer. A voice's sheet has 14 blocks: 13 sentences and the seam, which stands for the
# last 3 of the 16 sentences.

SHEET_IDS = [sentence_id for sentence_id, _ in SENTENCES if sentence_id not in ("s2", "s3")]

def sheet_and_key(marks):
    """A sheet and key over the 14 blocks for marks = {engine: {block id: mark text}}. A mark
    of None is a clip the key names but the sheet does not list."""
    sheet, key = "", {}
    for block in SHEET_IDS:
        sheet += f"### {block}\ntext\n"
        key[block] = {}
        for engine, by_block in marks.items():
            key[block][f"{engine}-{block}.wav"] = engine
            if by_block[block] is not None:
                sheet += f"- `{engine}-{block}.wav` → mark: {by_block[block]}\n"
        sheet += "\n"
    return sheet, key

def all_ok(**changes):
    """One voice's marks: ok on every block except those given, as block id = mark text."""
    return {block: changes.get(block, "ok") for block in SHEET_IDS}

def test_the_seam_counts_for_its_three_sentences_so_a_perfect_voice_scores_16():
    sheet, key = sheet_and_key({
        "perfect": all_ok(),
        "bad-seam": all_ok(s1="defect"),
        "two-slips": all_ok(n1="defect", n2="defect"),
        "three-slips": all_ok(n1="defect", n2="defect", n3="defect"),
    })
    scores = score_voice(sheet, key)
    assert {engine: score["ok"] for engine, score in scores.items()} == {"perfect": 16, "bad-seam": 13, "two-slips": 14, "three-slips": 13}
    assert {engine: score["pass"] for engine, score in scores.items()} == {"perfect": True, "bad-seam": False, "two-slips": True, "three-slips": False}

def test_a_clip_kept_off_the_sheet_costs_the_voice_that_sentence():
    sheet, key = sheet_and_key({"a": all_ok(), "b": all_ok(n3=None)})
    scores = score_voice(sheet, key)
    assert (scores["a"]["ok"], scores["b"]["ok"]) == (16, 15)

@pytest.mark.parametrize("word, counts", [
    ("ok", True), ("OK", True), ("Ok - clean", True), ("ok.", True),
    ("defect", False), ("", False), ("okay", False), ("yes", False),
])
def test_only_the_word_ok_counts_whatever_its_case_or_the_note_after_it(word, counts):
    sheet, key = sheet_and_key({"a": all_ok(n1=word)})
    assert score_voice(sheet, key)["a"]["ok"] == (16 if counts else 15)

def test_marks_on_sentences_and_files_the_key_does_not_know_count_for_nothing():
    sheet, key = sheet_and_key({"a": all_ok(n1="defect")})
    sheet += "### zz\nx\n- `a-n1.wav` → mark: ok\n### n1\nx\n- `stray.wav` → mark: ok\n"
    assert score_voice(sheet, key)["a"]["ok"] == 15

def test_every_engine_in_the_key_is_scored_marked_or_not():
    sheet, key = sheet_and_key({"a": all_ok(), "silent": dict.fromkeys(SHEET_IDS)})
    assert score_voice(sheet, key)["silent"] == {"ok": 0, "pass": False}

def test_the_sheet_the_runner_writes_can_be_marked_and_scored(tmp_path):
    sheet, key = run({"a": Script(), "b": Script({TEXTS["n3"]: clip(0.3)})}, tmp_path, seed=4)
    marked = re.sub(r"(→ mark: )$", r"\1ok", sheet, flags=re.MULTILINE)
    assert score_voice(marked, key) == {"a": {"ok": 16, "pass": True}, "b": {"ok": 15, "pass": True}}

def score_command(folder, marks, speeds):
    """The argv of the voice command over files written from marks and speeds."""
    sheet, key = sheet_and_key(marks)
    (folder / "sheet.md").write_text(sheet, encoding="utf-8")
    (folder / "key.json").write_text(json.dumps(key), encoding="utf-8")
    timing = {engine: {"seconds_per_audio_second": speed} for engine, speed in speeds.items()}
    (folder / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
    return ["voice", "--sheet", str(folder / "sheet.md"), "--key", str(folder / "key.json"), "--timing", str(folder / "timing.json")]

def test_voice_command_prints_each_score_and_names_the_fastest_voice_that_passes(tmp_path, capsys):
    marks = {
        "steady": all_ok(),
        "fast": all_ok(n1="defect", n2="defect"),
        "quick-but-bad": all_ok(n1="defect", n2="defect", n3="defect"),
    }
    score_main(score_command(tmp_path, marks, {"steady": 0.9, "fast": 0.2, "quick-but-bad": 0.05}))
    lines = capsys.readouterr().out.splitlines()
    line = {name: next(l for l in lines if l.startswith(name)) for name in marks}
    assert all(token in line["steady"] for token in ("ok: 16/16", "pass: yes", "seconds_per_audio_second: 0.900"))
    assert all(token in line["fast"] for token in ("ok: 14/16", "pass: yes", "seconds_per_audio_second: 0.200"))
    assert all(token in line["quick-but-bad"] for token in ("ok: 13/16", "pass: no", "seconds_per_audio_second: 0.050"))
    assert lines[-1].startswith("winner: fast ") and "steady" not in lines[-1] and "quick" not in lines[-1]

def test_voice_command_says_so_when_no_engine_passed(tmp_path, capsys):
    score_main(score_command(tmp_path, {"a": all_ok(s1="defect")}, {"a": 0.3}))
    assert capsys.readouterr().out.splitlines()[-1] == "winner: no engine passed"

def test_a_passing_voice_with_no_speed_in_the_timing_is_slower_than_any_with_one(tmp_path, capsys):
    marks = {"unmeasured": all_ok(), "missing": all_ok(), "measured": all_ok()}
    score_main(score_command(tmp_path, marks, {"unmeasured": None, "measured": 0.5}))
    lines = capsys.readouterr().out.splitlines()
    assert "seconds_per_audio_second: n/a" in next(l for l in lines if l.startswith("unmeasured"))
    assert "seconds_per_audio_second: n/a" in next(l for l in lines if l.startswith("missing"))
    assert lines[-1].startswith("winner: measured ")

# Lines the scorer cannot read are listed on stderr, and change no count.

@pytest.mark.parametrize("word, unreadable, unmarked", [
    ("okk", [("n1", "a-n1.wav", "okk")], []),
    ("yes", [("n1", "a-n1.wav", "yes")], []),
    ("1", [("n1", "a-n1.wav", "1")], []),
    ("meaning", [("n1", "a-n1.wav", "meaning")], []),  # meaning and style are the translation sheet's words
    ("", [], [("n1", "a-n1.wav", "")]),
    (" ", [], [("n1", "a-n1.wav", "")]),
    ("ok", [], []), ("OK", [], []), ("Ok - clean", [], []), ("defect", [], []), ("Defect, clipped", [], []),
])
def test_unread_voice_lines_tell_a_blank_mark_from_an_unreadable_one(word, unreadable, unmarked):
    sheet, _ = sheet_and_key({"a": all_ok(n1=word)})
    assert unread_voice_lines(sheet) == (unreadable, unmarked)

def test_unread_voice_lines_check_every_candidate_line_also_one_the_key_lacks():
    sheet = "- `stray.wav` → mark: okk\n### zz\nx\n- `other.wav` → mark: \n"
    assert unread_voice_lines(sheet) == ([("(no heading)", "stray.wav", "okk")], [("zz", "other.wav", "")])

def test_voice_command_lists_the_clips_it_cannot_read_on_stderr_and_scores_the_rest_as_before(tmp_path, capsys):
    marks = {
        "typo": all_ok(n1="defect", n2="defect", n3="okk"),  # 14 ok would pass; the typo makes it 13
        "forgot": all_ok(n4=""),
        "clean": all_ok(),
    }
    score_main(score_command(tmp_path, marks, {"typo": 0.1, "forgot": 0.2, "clean": 0.3}))
    shown = capsys.readouterr()
    assert shown.out.splitlines() == [  # the typo and the blank cost a sentence each, as they did before
        "typo    ok: 13/16  pass: no   seconds_per_audio_second: 0.100",
        "forgot  ok: 15/16  pass: yes  seconds_per_audio_second: 0.200",
        "clean   ok: 16/16  pass: yes  seconds_per_audio_second: 0.300",
        "winner: forgot (the fastest voice that passes)",
    ]
    assert shown.err == (
        "1 candidate line unreadable (listed below), 1 unmarked\n"
        "unreadable (write one of: ok, defect):\n"
        "  n3  typo-n3.wav  mark: okk\n"
        "unmarked:\n"
        "  n4  forgot-n4.wav\n"
    )

def test_voice_command_says_nothing_on_stderr_when_every_line_is_read(tmp_path, capsys):
    score_main(score_command(tmp_path, {"a": all_ok(n1="defect", n2="OK, clean")}, {"a": 0.3}))
    assert capsys.readouterr().err == ""

# The command line of the runner.

def builder(voices):
    """A stand-in for voice_from_name: the voice of each spec in the table, or the error to raise."""
    def build(spec):
        if isinstance(voices[spec], Exception): raise voices[spec]
        return voices[spec]
    return build

def run_main(tmp_path, monkeypatch, voices, engines, seed="3"):
    monkeypatch.setattr(bakeoff_voice, "voice_from_name", builder(voices))
    bakeoff_voice.main(["--engines", engines, "--seed", seed, "--out-dir", str(tmp_path / "out")])
    return tmp_path / "out"

def three_voices():
    """A healthy voice, one with a suspect clip on n3 and a failure on q1, and a dead one."""
    return {
        "say:A": Script(),
        "say:B": Script({TEXTS["n3"]: clip(0.3), TEXTS["q1"]: VoiceError("x")}),
        "say:Gone": Script({"Merhaba.": VoiceError("off")}),
    }

def test_main_writes_the_wavs_sheet_key_and_timing_into_the_out_dir(tmp_path, monkeypatch):
    out = run_main(tmp_path, monkeypatch, three_voices(), "say:A, say:B,say:Gone")   # the spaces are trimmed
    sheet = (out / "voice_sheet.md").read_text(encoding="utf-8")
    key = json.loads((out / "voice_key.json").read_text(encoding="utf-8"))
    assert sheet.startswith("engine unavailable: say:Gone: off\n\n### n1\n") and len(candidates(sheet)) == 14
    assert [len(candidates(sheet)[block]) for block in ("n1", "n3", "q1")] == [2, 1, 1]
    assert all((out / name).is_file() for files in key.values() for name in files)
    assert set(key["n1"].values()) == {"say:A", "say:B"}
    assert set(timing_of(out)) == {"say:A", "say:B"}

def test_main_reports_each_voice_and_prints_the_command_that_scores_the_sheet(tmp_path, monkeypatch, capsys):
    out = run_main(tmp_path, monkeypatch, three_voices(), "say:A,say:B,say:Gone")
    printed = capsys.readouterr().out
    line = {name: next(l for l in printed.splitlines() if l.startswith(name + ":")) for name in ("say:A", "say:B", "say:Gone")}
    assert "n3" in line["say:B"] and "q1" in line["say:B"] and "unavailable" in line["say:Gone"]
    assert "n3" not in line["say:A"] and "q1" not in line["say:A"]
    assert f"--sheet {out / 'voice_sheet.md'} --key {out / 'voice_key.json'} --timing {out / 'voice_timing.json'}" in printed

def test_the_files_main_writes_can_be_marked_and_scored_with_the_voice_command(tmp_path, monkeypatch, capsys):
    out = run_main(tmp_path, monkeypatch, three_voices(), "say:A,say:B")
    capsys.readouterr()
    sheet_path = out / "voice_sheet.md"
    sheet_path.write_text(re.sub(r"(→ mark: )$", r"\1ok", sheet_path.read_text(encoding="utf-8"), flags=re.MULTILINE), encoding="utf-8")
    score_main(["voice", "--sheet", str(sheet_path), "--key", str(out / "voice_key.json"), "--timing", str(out / "voice_timing.json")])
    lines = capsys.readouterr().out.splitlines()
    assert "ok: 16/16" in next(l for l in lines if l.startswith("say:A")) and "ok: 14/16" in next(l for l in lines if l.startswith("say:B"))
    assert lines[-1].startswith("winner: say:")

def test_main_refuses_an_unknown_voice_before_running_anything(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        bakeoff_voice.main(["--engines", "say:Yelda,piper:x", "--out-dir", str(tmp_path / "out")])
    assert stop.value.code == 2 and "piper:x" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()

def test_main_lists_a_voice_that_cannot_be_built_as_unavailable_and_runs_the_others(tmp_path, monkeypatch):
    voices = {"voxcpm:/x.wav": VoiceError("reference audio not found: /x.wav"), "say:A": Script()}
    out = run_main(tmp_path, monkeypatch, voices, "voxcpm:/x.wav,say:A")
    sheet = (out / "voice_sheet.md").read_text(encoding="utf-8")
    assert "engine unavailable: voxcpm:/x.wav: reference audio not found: /x.wav" in sheet
    assert len(candidates(sheet)["n1"]) == 1

def test_main_with_the_real_builder_lists_a_say_voice_that_is_not_installed_as_unavailable(tmp_path):
    bakeoff_voice.main(["--engines", "say:NoSuchVoiceAnywhere", "--out-dir", str(tmp_path)])
    assert "engine unavailable: say:NoSuchVoiceAnywhere: " in (tmp_path / "voice_sheet.md").read_text(encoding="utf-8")
