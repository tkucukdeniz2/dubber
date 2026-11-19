import http.server
import json
import pathlib
import re
import socket
import threading
import time

import httpx
import pytest

from tools.bakeoff_translate import run, YouTubeTurkishAligner
from tools.bakeoff_score import score_translate
from dubber.engines.base import TranslatorError
from dubber.engines.translate_llamacpp import LlamaCppTranslator
# For the tests added below the brief's.
from tools import bakeoff_translate
from tools.bakeoff_translate import build_engines, main, summarise_timings
from tools.bakeoff_score import main as score_main, translate_table, unread_translate_lines

class Fixed:
    def __init__(self, name, out): self.name, self.out = name, out
    def translate(self, text, context=(), glossary=None):
        if isinstance(self.out, Exception): raise self.out
        return self.out

SENTS = [{"id": "v:0", "start": 1.0, "end": 3.0, "text": "Hello."}, {"id": "v:1", "start": 4.0, "end": 6.0, "text": "Bye."}]

def test_run_shuffles_labels_and_hides_engines():
    sheet, key = run(SENTS, {"e1": Fixed("e1", "Merhaba."), "e2": Fixed("e2", "Selam.")}, seed=1)
    assert "e1" not in sheet and "e2" not in sheet
    assert set(key["v:0"].values()) == {"e1", "e2"}
    assert sorted(key["v:0"].keys()) == ["A", "B"]

def test_run_records_unavailable_engine_and_continues():
    sheet, key = run(SENTS, {"ok": Fixed("ok", "Merhaba."), "bad": Fixed("bad", TranslatorError("503: down"))}, seed=1)
    assert "[engine unavailable: 503: down]" in sheet and "Merhaba." in sheet

def test_aligner_joins_cues_in_window(tmp_path):
    p = tmp_path / "tr.json3"
    p.write_text('{"events":[{"tStartMs":900,"dDurationMs":1000,"segs":[{"utf8":"Merhaba"}]},{"tStartMs":2000,"dDurationMs":900,"segs":[{"utf8":" dünya."}]},{"tStartMs":5000,"dDurationMs":500,"segs":[{"utf8":"Hoşça kal."}]}]}')
    al = YouTubeTurkishAligner(p)
    assert al.for_sentence(1.0, 3.0) == "Merhaba dünya."

def test_score_counts_marks_per_engine():
    sheet = "### v:0\nHello.\n- **A** `Merhaba.` → mark: ok\n- **B** `Selam.` → mark: meaning\n"
    key = {"v:0": {"A": "e1", "B": "e2"}}
    assert score_translate(sheet, key) == {"e1": {"ok": 1, "meaning": 0, "style": 0}, "e2": {"ok": 0, "meaning": 1, "style": 0}}

# What the tests above leave open: the exact sheet, the call contract, the shuffle and the aligner window.

def candidates(sheet):
    """{sentence id: {label: candidate text}} read straight off the sheet."""
    found, current = {}, None
    for line in sheet.splitlines():
        if line.startswith("### "):
            current = found.setdefault(line[4:], {})
        elif m := re.fullmatch(r"- \*\*(\w+)\*\* `(.*)` → mark: ", line):
            current[m[1]] = m[2]
    return found

class Recorder:
    """Records every call it gets and answers with a fixed Turkish line."""
    def __init__(self): self.calls = []
    def translate(self, text, context=(), glossary=None):
        self.calls.append((text, list(context), glossary))
        return "Çeviri."

def numbered(n):
    return [{"id": f"v:{i}", "start": float(i), "end": i + 1.0, "text": f"S{i}."} for i in range(n)]

def write_tr(tmp_path, *cues, name="tr.json3"):
    """A Turkish json3 file, called name, with one event per (start in ms, text)."""
    events = [{"tStartMs": ms, "dDurationMs": 400, "segs": [{"utf8": text}]} for ms, text in cues]
    path = tmp_path / name
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return path

def test_sheet_has_one_block_per_sentence_in_the_exact_format():
    sheet, _ = run(SENTS, {"solo": Fixed("solo", "Merhaba.")}, seed=1)
    assert sheet == (
        "### v:0\nHello.\n- **A** `Merhaba.` → mark: \n"
        "\n"
        "### v:1\nBye.\n- **A** `Merhaba.` → mark: \n"
    )

def test_sheet_candidates_are_the_texts_the_key_assigns_to_each_engine():
    texts = {"e1": "Merhaba.", "e2": "Selam.", "e3": "Günaydın."}
    sheet, key = run(numbered(12), {name: Fixed(name, out) for name, out in texts.items()}, seed=3)
    assert candidates(sheet) == {
        sid: {label: texts[engine] for label, engine in labels.items()} for sid, labels in key.items()
    }

def test_candidates_stay_on_one_line_without_backticks():
    wild = "Merhaba `dünya`.\n\n İyi   günler."
    sheet, _ = run(SENTS[:1], {"solo": Fixed("solo", wild)}, seed=1)
    assert sheet == "### v:0\nHello.\n- **A** `Merhaba 'dünya'. İyi günler.` → mark: \n"

def test_an_unavailable_message_is_cleaned_like_any_candidate():
    down = TranslatorError("502: <html>\nbad `gateway`\n</html>")
    sheet, _ = run(SENTS[:1], {"solo": Fixed("solo", down)}, seed=1)
    assert sheet == "### v:0\nHello.\n- **A** `[engine unavailable: 502: <html> bad 'gateway' </html>]` → mark: \n"

def test_each_call_gets_the_previous_two_sentences_as_context_and_no_glossary():
    engine = Recorder()
    run(numbered(4), {"r": engine}, seed=1)
    assert engine.calls == [
        ("S0.", [], None),
        ("S1.", ["S0."], None),
        ("S2.", ["S0.", "S1."], None),
        ("S3.", ["S1.", "S2."], None),
    ]

def test_duplicate_sentence_ids_are_rejected_before_any_call():
    engine = Recorder()
    twins = [{"id": "v:0", "start": 0.0, "end": 1.0, "text": "A."}, {"id": "v:0", "start": 2.0, "end": 3.0, "text": "B."}]
    with pytest.raises(ValueError, match="v:0"):
        run(twins, {"r": engine}, seed=1)
    assert engine.calls == []

def test_labels_are_shuffled_per_sentence_and_repeat_with_the_same_seed():
    engines = {"e1": Fixed("e1", "Bir."), "e2": Fixed("e2", "İki."), "e3": Fixed("e3", "Üç.")}
    key = run(numbered(30), engines, seed=7)[1]
    assert all(sorted(labels) == ["A", "B", "C"] and sorted(labels.values()) == ["e1", "e2", "e3"] for labels in key.values())
    assert len({tuple(labels.values()) for labels in key.values()}) > 1  # not one fixed order
    assert run(numbered(30), engines, seed=7)[1] == key
    assert run(numbered(30), engines, seed=8)[1] != key

def test_labels_do_not_depend_on_which_engines_fail():
    healthy = {"e1": Fixed("e1", "Bir."), "e2": Fixed("e2", "İki.")}
    broken = {"e1": Fixed("e1", "Bir."), "e2": Fixed("e2", TranslatorError("503: down"))}
    assert run(numbered(30), healthy, seed=7)[1] == run(numbered(30), broken, seed=7)[1]

def test_aligner_window_starts_half_a_second_early_and_ends_half_a_second_late(tmp_path):
    cues = ((490, "erken"), (500, "bir"), (3490, "iki"), (3500, "geç"))
    al = YouTubeTurkishAligner(write_tr(tmp_path, *cues))
    assert al.for_sentence(1.0, 3.0) == "bir iki"
    assert al.for_sentence(10.0, 11.0) == ""

# One Turkish file per video: the youtube-tr engine serves each sentence from its own video's file.

VIDEO_A, VIDEO_B = "XP5k0bHmhqc", "qp0HIF3SfI4"

def youtube_engine(tmp_path, videos):
    """The youtube-tr engine over one Turkish file per video; videos maps a video id to its cues."""
    paths = [write_tr(tmp_path, *cues, name=f"{video}.tr.translated.json3") for video, cues in videos.items()]
    return build_engines({}, ["youtube-tr"], paths)["youtube-tr"]

def test_each_sentence_is_aligned_from_its_own_videos_turkish_file(tmp_path):
    engine = youtube_engine(tmp_path, {
        VIDEO_A: [(900, "Birinci videonun satırı.")],
        VIDEO_B: [(900, "İkinci videonun ılık satırı."), (5000, "Hoşça kal.")],
    })
    sentences = [  # both videos have a cue at 0.9 s: one shared file would give the same text to both
        {"id": f"{VIDEO_A}.en.asr:0", "start": 1.0, "end": 3.0, "text": "One."},
        {"id": f"{VIDEO_B}.en.manual:0", "start": 1.0, "end": 3.0, "text": "Two."},
        {"id": f"{VIDEO_B}.en.manual:1", "start": 4.0, "end": 6.0, "text": "Bye."},
    ]
    sheet, _ = run(sentences, {"youtube-tr": engine}, seed=1)
    assert candidates(sheet) == {
        f"{VIDEO_A}.en.asr:0": {"A": "Birinci videonun satırı."},
        f"{VIDEO_B}.en.manual:0": {"A": "İkinci videonun ılık satırı."},
        f"{VIDEO_B}.en.manual:1": {"A": "Hoşça kal."},
    }

def test_a_sentence_without_a_track_or_without_cues_gets_a_fixed_note(tmp_path):
    engine = youtube_engine(tmp_path, {VIDEO_A: [(900, "Merhaba.")]})
    sentences = [
        {"id": f"{VIDEO_A}.en.asr:0", "start": 1.0, "end": 3.0, "text": "Hello."},
        {"id": f"{VIDEO_A}.en.asr:1", "start": 60.0, "end": 62.0, "text": "Much later."},  # no cue near it
        {"id": "zzzzzzzzzzz.en.asr:0", "start": 1.0, "end": 3.0, "text": "Another video."},  # no file for it
    ]
    sheet, _ = run(sentences, {"youtube-tr": engine}, seed=1)
    assert candidates(sheet) == {
        f"{VIDEO_A}.en.asr:0": {"A": "Merhaba."},
        f"{VIDEO_A}.en.asr:1": {"A": "[no Turkish cues in window]"},
        "zzzzzzzzzzz.en.asr:0": {"A": "[no Turkish track for this video]"},
    }

def test_the_notes_are_not_failures_so_the_engine_goes_on_serving_its_own_video(tmp_path):
    engine = youtube_engine(tmp_path, {VIDEO_A: [(900, "Merhaba.")]})
    foreign = [{"id": f"other.en.asr:{i}", "start": 1.0, "end": 3.0, "text": f"F{i}."} for i in range(4)]
    own = {"id": f"{VIDEO_A}.en.asr:0", "start": 1.0, "end": 3.0, "text": "Hello."}
    timings = {}
    sheet, _ = run([*foreign, own], {"youtube-tr": engine}, seed=1, timings=timings)
    assert candidates(sheet)[own["id"]] == {"A": "Merhaba."}  # four notes in a row did not switch it off
    assert len(timings["youtube-tr"]) == 5  # every lookup counted as a success

# Reachability probe, the three-strikes rule and the timings.

class Scripted:
    """Answers each call from its list: a string is the translation, an exception is raised."""
    def __init__(self, *outcomes): self.outcomes, self.calls = list(outcomes), 0
    def translate(self, text, context=(), glossary=None):
        outcome = self.outcomes[self.calls]  # one call too many is an IndexError
        self.calls += 1
        if isinstance(outcome, Exception): raise outcome
        return outcome

class Lister:
    """A probe: list_models() answers or raises, and counts its calls."""
    def __init__(self, error=None): self.error, self.calls = error, 0
    def list_models(self):
        self.calls += 1
        if self.error: raise self.error
        return []

class Slow:
    def translate(self, text, context=(), glossary=None):
        time.sleep(0.05)
        return "Yavaş."

def text_by_engine(sheet, key):
    found = candidates(sheet)
    return {sid: {engine: found[sid][label] for label, engine in labels.items()} for sid, labels in key.items()}

def test_a_failed_probe_marks_every_sentence_unavailable_without_translating():
    down, up = Recorder(), Recorder()
    probes = {"down": Lister(TranslatorError("[Errno 61] Connection refused")), "up": Lister()}
    sheet, key = run(SENTS, {"down": down, "up": up}, seed=1, probes=probes)
    assert down.calls == []
    assert [call[0] for call in up.calls] == ["Hello.", "Bye."]
    assert [probes["down"].calls, probes["up"].calls] == [1, 1]  # each probed once, not once per sentence
    unavailable = "[engine unavailable: [Errno 61] Connection refused]"
    assert text_by_engine(sheet, key) == {
        "v:0": {"down": unavailable, "up": "Çeviri."},
        "v:1": {"down": unavailable, "up": "Çeviri."},
    }

class Down:
    """An engine whose server is off: list_models() fails, and translate() must not be reached."""
    def __init__(self): self.translated = 0
    def list_models(self): raise TranslatorError("[Errno 61] Connection refused")
    def translate(self, text, context=(), glossary=None):
        self.translated += 1
        return "unreachable"

def test_without_probes_run_probes_the_engines_that_can_list_models():
    down, up = Down(), Recorder()
    sheet, key = run(SENTS, {"down": down, "up": up}, seed=1)
    assert down.translated == 0 and len(up.calls) == 2  # Recorder has no list_models: not probed, translates
    unavailable = "[engine unavailable: [Errno 61] Connection refused]"
    assert text_by_engine(sheet, key)["v:1"] == {"down": unavailable, "up": "Çeviri."}

def test_a_probe_without_list_models_is_ignored(tmp_path):
    engine = youtube_engine(tmp_path, {VIDEO_A: [(900, "Merhaba.")]})
    sentence = {"id": f"{VIDEO_A}.en.asr:0", "start": 1.0, "end": 3.0, "text": "Hello."}
    sheet, _ = run([sentence], {"yt": engine}, seed=1, probes={"yt": engine})
    assert candidates(sheet) == {sentence["id"]: {"A": "Merhaba."}}

# A pinned PC model is never requested while the PC holds another: that would force a model swap.

PC_MODEL_IDS = ("qwen3.8-27b", "gemma4-12b", "mistral-24b")

def listing_with_loaded(*loaded):
    """The PC's model list, with the models named in loaded flagged loaded and the rest unloaded."""
    return [{"id": model, "status": {"value": "loaded" if model in loaded else "unloaded"}} for model in PC_MODEL_IDS]

def pc_qwen(listing, **options):
    """A pc-qwen engine, pinned to qwen3.8-27b, over a stand-in PC that lists `listing` and answers every chat; with the requests it gets."""
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": listing})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Çeviri."}}]})
    engine = LlamaCppTranslator("http://pc:8080", "k", model="qwen3.8-27b", transport=httpx.MockTransport(handler), **options)
    return engine, seen

def test_a_pinned_model_that_is_not_loaded_while_another_is_gets_no_chat_call():
    engine, seen = pc_qwen(listing_with_loaded("gemma4-12b"))
    sheet, key = run(SENTS, {"pc-qwen": engine}, seed=1)
    note = "[engine unavailable: qwen3.8-27b is not loaded (gemma4-12b is); load it on the PC first]"
    assert text_by_engine(sheet, key) == {"v:0": {"pc-qwen": note}, "v:1": {"pc-qwen": note}}
    assert [request.url.path for request in seen] == ["/v1/models"]  # the probe, and not one chat request

def test_the_note_names_every_model_the_pc_holds():
    engine, _ = pc_qwen(listing_with_loaded("gemma4-12b", "mistral-24b"))
    sheet, key = run(SENTS[:1], {"pc-qwen": engine}, seed=1)
    assert text_by_engine(sheet, key)["v:0"]["pc-qwen"] == (
        "[engine unavailable: qwen3.8-27b is not loaded (gemma4-12b, mistral-24b are); load it on the PC first]"
    )

@pytest.mark.parametrize("listing", [
    listing_with_loaded("qwen3.8-27b"),                        # the pinned model is the loaded one
    listing_with_loaded("qwen3.8-27b", "gemma4-12b"),  # ... one of two loaded
    listing_with_loaded(),                                     # nothing loaded: the request loads it and evicts nothing
    [{"id": model} for model in PC_MODEL_IDS],                 # no status at all, as Ollama lists
], ids=["pinned-is-loaded", "pinned-is-one-of-two", "nothing-loaded", "no-status"])
def test_a_pinned_model_is_translated_with_when_it_is_loaded_or_nothing_is(listing):
    engine, seen = pc_qwen(listing)
    sheet, key = run(SENTS, {"pc-qwen": engine}, seed=1)
    assert text_by_engine(sheet, key) == {"v:0": {"pc-qwen": "Çeviri."}, "v:1": {"pc-qwen": "Çeviri."}}
    assert [request.url.path for request in seen] == ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"]
    assert {json.loads(request.content)["model"] for request in seen[1:]} == {"qwen3.8-27b"}

def test_an_engine_that_follows_the_loaded_model_is_not_held_back():
    engine, seen = pc_qwen(listing_with_loaded("gemma4-12b"), prefer_loaded=True)  # it asks for the loaded one
    sheet, key = run(SENTS[:1], {"pc-qwen": engine}, seed=1)
    assert text_by_engine(sheet, key) == {"v:0": {"pc-qwen": "Çeviri."}}
    assert json.loads(seen[-1].content)["model"] == "gemma4-12b"

def test_the_probe_decides_not_the_engine():
    held = Recorder()  # has no model and no listing of its own
    engine, _ = pc_qwen(listing_with_loaded("gemma4-12b"))
    sheet, key = run(SENTS[:1], {"pc-qwen": held}, seed=1, probes={"pc-qwen": engine})
    assert held.calls == [] and text_by_engine(sheet, key)["v:0"]["pc-qwen"].startswith("[engine unavailable: qwen3.8-27b is not loaded")

def test_three_failures_in_a_row_mark_the_remaining_sentences_unavailable():
    bad = Scripted(TranslatorError("503: one"), TranslatorError("503: two"), TranslatorError("503: three"))
    good = Recorder()
    sheet, key = run(numbered(6), {"bad": bad, "good": good}, seed=1)
    assert bad.calls == 3  # a fourth call would be an IndexError
    assert len(good.calls) == 6
    shown = [text_by_engine(sheet, key)[f"v:{i}"]["bad"] for i in range(6)]
    assert shown == ["[engine unavailable: 503: one]", "[engine unavailable: 503: two]"] + ["[engine unavailable: 503: three]"] * 4

def test_a_success_resets_the_count_of_consecutive_failures():
    err = TranslatorError("503: down")
    flaky = Scripted(err, err, "Bir.", err, err, "İki.")
    sheet, key = run(numbered(6), {"flaky": flaky}, seed=1)
    assert flaky.calls == 6
    down = "[engine unavailable: 503: down]"
    assert [text_by_engine(sheet, key)[f"v:{i}"]["flaky"] for i in range(6)] == [down, down, "Bir.", down, down, "İki."]

def test_timings_collect_the_seconds_of_successful_calls_only():
    err = TranslatorError("503: down")
    engines = {"mixed": Scripted("Bir.", err, "Üç."), "slow": Slow(), "dead": Scripted(err, err, err)}
    timings = {}
    run(numbered(3), engines, seed=1, timings=timings)
    assert {name: len(seconds) for name, seconds in timings.items()} == {"mixed": 2, "slow": 3, "dead": 0}
    assert all(0.04 <= seconds < 5 for seconds in timings["slow"])

def test_summarise_timings_gives_the_mean_and_count_per_engine():
    assert summarise_timings({"a": [1.0, 3.0], "b": []}) == {
        "a": {"mean_seconds": 2.0, "n": 2},
        "b": {"mean_seconds": None, "n": 0},
    }

# The scorer: which marks count, and the table the translate command prints.

def sheet_with(*marks):
    """One sentence whose candidate lines A, B, ... end in the given text after "mark:"."""
    lines = [f"- **{label}** `x` → mark:{mark}" for label, mark in zip("ABCDEFGH", marks)]
    return "### v:0\nHello.\n" + "\n".join(lines) + "\n"

@pytest.mark.parametrize("after_mark, counted", [
    (" ok", "ok"), ("ok", "ok"), ("   style  ", "style"), (" OK", "ok"), (" Meaning", "meaning"),
    (" meaning (dropped the date)", "meaning"), (" ok, a bit formal", "ok"),
    ("", None), (" ", None), (" good", None), (" okay", None), (" 1", None),
])
def test_score_reads_the_first_word_after_mark_and_ignores_case_and_anything_unknown(after_mark, counted):
    expected = {"ok": 0, "meaning": 0, "style": 0}
    if counted:
        expected[counted] = 1
    assert score_translate(sheet_with(after_mark), {"v:0": {"A": "e1"}}) == {"e1": expected}

def test_score_lists_every_engine_of_the_key_even_without_marks():
    key = {"v:0": {"A": "e1", "B": "e2", "C": "e3"}}
    zeros = {"ok": 0, "meaning": 0, "style": 0}
    assert score_translate(sheet_with(" ok", " ", " fine"), key) == {"e1": {**zeros, "ok": 1}, "e2": zeros, "e3": zeros}

def test_score_follows_each_sentences_own_labels():
    sheet = sheet_with(" ok", " style") + "\n" + sheet_with(" ok", " style").replace("v:0", "v:1")
    key = {"v:0": {"A": "e1", "B": "e2"}, "v:1": {"A": "e2", "B": "e1"}}
    assert score_translate(sheet, key) == {"e1": {"ok": 1, "meaning": 0, "style": 1}, "e2": {"ok": 1, "meaning": 0, "style": 1}}

def test_score_ignores_lines_outside_a_known_sentence_and_labels_the_key_lacks():
    sheet = (
        "- **A** `before any heading` → mark: ok\n"
        "### v:9\nUnknown sentence.\n- **A** `x` → mark: ok\n"
        "### v:0\nHello.\n- **Z** `x` → mark: ok\n- **A** `y` → mark: style\n"
    )
    assert score_translate(sheet, {"v:0": {"A": "e1"}}) == {"e1": {"ok": 0, "meaning": 0, "style": 1}}

def test_translate_table_lines_up_the_counts_and_the_pass_rate():
    scores = {"pc-qwen": {"ok": 20, "meaning": 3, "style": 2}, "youtube-tr": {"ok": 0, "meaning": 0, "style": 0}}
    assert translate_table(scores) == (
        "engine      ok  meaning  style  marked  pass rate\n"
        "pc-qwen     20        3      2      25      80.0%\n"
        "youtube-tr   0        0      0       0        n/a"
    )

def test_score_cli_prints_the_table_for_the_translate_sheet(tmp_path, capsys):
    sheet, key = tmp_path / "sheet.md", tmp_path / "key.json"
    sheet.write_text(sheet_with(" ok", " style"), encoding="utf-8")
    key.write_text(json.dumps({"v:0": {"A": "e1", "B": "e2"}}), encoding="utf-8")
    score_main(["translate", "--sheet", str(sheet), "--key", str(key)])
    assert capsys.readouterr().out == (
        "engine  ok  meaning  style  marked  pass rate\n"
        "e1       1        0      0       1     100.0%\n"
        "e2       0        0      1       1       0.0%\n"
    )

# Lines the scorer cannot read are listed on stderr, and change no count.

@pytest.mark.parametrize("after_mark, unreadable, unmarked", [
    (" okk", [("v:0", "A", "okk")], []),
    (" tamam", [("v:0", "A", "tamam")], []),
    (" 1", [("v:0", "A", "1")], []),
    (" ?  ", [("v:0", "A", "?")], []),
    (" defect", [("v:0", "A", "defect")], []),  # ok and defect are the voice sheet's words
    ("", [], [("v:0", "A", "")]),
    (" ", [], [("v:0", "A", "")]),
    (" ok", [], []), (" OK, a bit formal", [], []), (" Meaning (dropped the date)", [], []), ("style", [], []),
])
def test_unread_translate_lines_tell_a_blank_mark_from_an_unreadable_one(after_mark, unreadable, unmarked):
    assert unread_translate_lines(sheet_with(after_mark)) == (unreadable, unmarked)

def test_unread_translate_lines_check_every_candidate_line_also_one_the_key_lacks():
    sheet = "- **A** `before any heading` → mark: okk\n### v:9\nx\n- **Q** `y` → mark: \n"
    assert unread_translate_lines(sheet) == ([("(no heading)", "A", "okk")], [("v:9", "Q", "")])

def test_score_cli_lists_the_lines_it_cannot_read_on_stderr_and_counts_the_rest_as_before(tmp_path, capsys):
    sheet, key = tmp_path / "sheet.md", tmp_path / "key.json"
    sheet.write_text(sheet_with(" ok", " okk", "", " tamam, dropped the date", " style"), encoding="utf-8")
    key.write_text(json.dumps({"v:0": dict(zip("ABCDE", ["e1", "e2", "e3", "e4", "e5"]))}), encoding="utf-8")
    score_main(["translate", "--sheet", str(sheet), "--key", str(key)])
    shown = capsys.readouterr()
    assert shown.out == (  # only the ok of e1 and the style of e5 are counted: the other three lines count for nothing
        "engine  ok  meaning  style  marked  pass rate\n"
        "e1       1        0      0       1     100.0%\n"
        "e2       0        0      0       0        n/a\n"
        "e3       0        0      0       0        n/a\n"
        "e4       0        0      0       0        n/a\n"
        "e5       0        0      1       1       0.0%\n"
    )
    assert shown.err == (
        "2 candidate lines unreadable (listed below), 1 unmarked\n"
        "unreadable (write one of: ok, meaning, style):\n"
        "  v:0  B  mark: okk\n"
        "  v:0  D  mark: tamam, dropped the date\n"
        "unmarked:\n"
        "  v:0  C\n"
    )

def test_score_cli_says_nothing_on_stderr_when_every_line_is_read(tmp_path, capsys):
    sheet, key = tmp_path / "sheet.md", tmp_path / "key.json"
    sheet.write_text(sheet_with(" ok", " Style, a bit formal"), encoding="utf-8")
    key.write_text(json.dumps({"v:0": {"A": "e1", "B": "e2"}}), encoding="utf-8")
    score_main(["translate", "--sheet", str(sheet), "--key", str(key)])
    assert capsys.readouterr().err == ""

# Building the engines from the config, and the command line, against a local stand-in for the servers.

KEY = "secret-key"
ENGINE_NAMES = ["pc-qwen", "pc-gemma", "mac-ollama", "youtube-tr"]

@pytest.fixture
def fake_server(monkeypatch):
    """A local stand-in for the PC and the Mac: lists models, answers each chat by its model, notes who asked."""
    monkeypatch.setenv("NO_PROXY", "*")  # no proxy of this machine sits between the test and the loopback
    seen, state = [], {"models_status": 200, "loaded": None}  # loaded: the one model reported loaded (None: no status at all, as Ollama)
    replies = {"qwen3.8-27b": "Qwen çevirisi.", "gemma4-12b": "Gemma çevirisi.", "qwen3.8:27b-mlx": "Mac çevirisi."}
    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            seen.append({"method": "GET", "path": self.path, "auth": self.headers["Authorization"], "model": None})
            if state["models_status"] != 200:
                return self.reply(state["models_status"], {"error": "loading"})
            if state["loaded"] is None:
                return self.reply(200, {"data": [{"id": model} for model in replies]})
            self.reply(200, {"data": [{"id": model, "status": {"value": "loaded" if model == state["loaded"] else "unloaded"}} for model in replies]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"method": "POST", "path": self.path, "auth": self.headers["Authorization"], "model": body["model"]})
            self.reply(200, {"choices": [{"message": {"content": replies[body["model"]]}}]})
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen, state
    server.shutdown()
    thread.join()
    server.server_close()

@pytest.fixture
def black_hole(monkeypatch):
    """A local port that accepts connections and never answers."""
    monkeypatch.setenv("NO_PROXY", "*")
    listener, held, stop = socket.socket(), [], threading.Event()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.05)
    def accept():
        while not stop.is_set():
            try:
                held.append(listener.accept()[0])
            except TimeoutError:
                pass
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    stop.set()
    thread.join()
    listener.close()
    for connection in held:
        connection.close()

def make_cfg(tmp_path, base_url):
    """A config like config.example.toml's, with both servers at base_url and the key in a file."""
    (tmp_path / "api-key").write_text(KEY + "\n", encoding="utf-8")
    return {
        "pc": {"base_url": base_url, "api_key_file": str(tmp_path / "api-key"), "preferred_model": "qwen3.8-27b"},
        "mac_llm": {"base_url": base_url + "/v1", "model": "qwen3.8:27b-mlx"},
    }

def test_build_engines_pins_the_models_and_sends_the_key_to_the_pc_only(tmp_path, fake_server):
    base_url, seen, _ = fake_server
    engines = build_engines(make_cfg(tmp_path, base_url), ["pc-qwen", "pc-gemma", "mac-ollama"], None)
    assert list(engines) == ["pc-qwen", "pc-gemma", "mac-ollama"]
    for engine in engines.values():
        engine.translate("Hello.")
    chat = "/v1/chat/completions"
    assert seen == [  # the models are requested as they are: no /v1/models lookup, no swap to the loaded one
        {"method": "POST", "path": chat, "auth": f"Bearer {KEY}", "model": "qwen3.8-27b"},
        {"method": "POST", "path": chat, "auth": f"Bearer {KEY}", "model": "gemma4-12b"},
        {"method": "POST", "path": chat, "auth": None, "model": "qwen3.8:27b-mlx"},
    ]

def test_build_engines_needs_the_pc_config_only_for_pc_engines(tmp_path):
    cfg = {"mac_llm": {"base_url": "http://127.0.0.1:1/v1", "model": "m"}}
    assert list(build_engines(cfg, ["mac-ollama", "youtube-tr"], [write_tr(tmp_path)])) == ["mac-ollama", "youtube-tr"]

def test_build_engines_rejects_unknown_names_and_a_youtube_engine_without_its_file(tmp_path):
    with pytest.raises(ValueError, match="unknown engine 'pc-llama'"):
        build_engines({}, ["pc-llama"], None)
    with pytest.raises(ValueError, match="--youtube-tr"):
        build_engines({}, ["youtube-tr"], None)
    with pytest.raises(ValueError, match="--youtube-tr"):
        build_engines({}, ["youtube-tr"], [])
    with pytest.raises(FileNotFoundError):
        build_engines({}, ["youtube-tr"], [tmp_path / "missing.json3"])

def test_build_engines_rejects_a_turkish_file_without_a_video_id_and_two_files_for_one_video():
    # None of these files exists: the names are checked before any file is read.
    with pytest.raises(ValueError, match="video id"):
        build_engines({}, ["youtube-tr"], [pathlib.Path(".tr.translated.json3")])
    twins = [pathlib.Path(f"{VIDEO_A}.tr.translated.json3"), pathlib.Path(f"elsewhere/{VIDEO_A}.tr.manual.json3")]
    with pytest.raises(ValueError, match=f"two Turkish files for video {VIDEO_A}"):
        build_engines({}, ["youtube-tr"], twins)

def test_build_engines_passes_the_timeout_on(tmp_path, black_hole):
    quick = build_engines(make_cfg(tmp_path, black_hole), ["pc-qwen"], None, timeout=0.3)["pc-qwen"]
    began = time.perf_counter()
    with pytest.raises(TranslatorError):
        quick.list_models()
    assert time.perf_counter() - began < 5

def spaced_id(i):
    return f"vid.en.asr:{i}"

def spaced(n):
    """n sentences ten seconds apart, so each has a cue of its own in youtube_args(...)."""
    return [{"id": spaced_id(i), "start": 10.0 * i, "end": 10.0 * i + 2.0, "text": f"Sentence {i}."} for i in range(n)]

def cli_args(tmp_path, base_url, sentences=None):
    """sentences.json (spaced(3) unless sentences are given), a TOML config for the server at base_url and an output folder."""
    rows = spaced(3) if sentences is None else sentences
    sentences = tmp_path / "sentences.json"
    sentences.write_text(json.dumps(rows), encoding="utf-8")
    config = tmp_path / "config.toml"
    toml = "".join(f"[{table}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items()) for table, values in make_cfg(tmp_path, base_url).items())
    config.write_text(toml, encoding="utf-8")
    return ["--sentences", str(sentences), "--config", str(config), "--out-dir", str(tmp_path / "out")]

def youtube_args(tmp_path, n=3):
    """--youtube-tr with the Turkish file of video "vid", whose cue i sits in the window of spaced sentence i."""
    captions = write_tr(tmp_path, *[(10000 * i + 500, f"Satır {i}.") for i in range(n)], name="vid.tr.translated.json3")
    return ["--youtube-tr", str(captions)]

def read_outputs(tmp_path):
    out = tmp_path / "out"
    sheet = (out / "translate_sheet.md").read_text(encoding="utf-8")
    key = json.loads((out / "translate_key.json").read_text(encoding="utf-8"))
    timing = json.loads((out / "translate_timing.json").read_text(encoding="utf-8"))
    return sheet, key, timing

def expected_key(seed, *names):
    """The key run() gives spaced(3) and these engines, in this order, whatever the engines answer."""
    return run(spaced(3), {name: Fixed(name, "x") for name in names}, seed=seed)[1]

def test_cli_writes_the_sheet_key_and_timing_after_one_probe_per_server(tmp_path, fake_server, capsys):
    base_url, seen, _ = fake_server
    argv = cli_args(tmp_path, base_url) + youtube_args(tmp_path) + ["--engines", "pc-qwen,mac-ollama,youtube-tr"]
    main(argv)
    sheet, key, timing = read_outputs(tmp_path)
    assert key == expected_key(7, "pc-qwen", "mac-ollama", "youtube-tr")  # the seed defaults to 7
    # Every label of the key points at the engine that wrote that text.
    assert text_by_engine(sheet, key) == {
        spaced_id(i): {"pc-qwen": "Qwen çevirisi.", "mac-ollama": "Mac çevirisi.", "youtube-tr": f"Satır {i}."} for i in range(3)
    }
    assert [(name, entry["n"]) for name, entry in timing.items()] == [("pc-qwen", 3), ("mac-ollama", 3), ("youtube-tr", 3)]
    assert all(entry["mean_seconds"] >= 0 for entry in timing.values())
    # The probes come first, one per server, then every sentence once per server.
    assert [(r["method"], r["path"], r["auth"]) for r in seen[:2]] == [("GET", "/v1/models", f"Bearer {KEY}"), ("GET", "/v1/models", None)]
    assert [r["method"] for r in seen] == ["GET", "GET"] + ["POST"] * 6
    shown = capsys.readouterr()
    assert re.search(r"pc-qwen: 3 of 3 sentences translated, mean \d+\.\d\d s", shown.out)
    # No file and no output carries the key; the sheet carries no engine name.
    written = [path.read_text(encoding="utf-8") for path in (tmp_path / "out").iterdir()]
    assert not any(KEY in text for text in [*written, shown.out, shown.err])
    assert not any(name in sheet for name in ENGINE_NAMES)

def test_cli_marks_every_sentence_unavailable_when_the_probe_fails(tmp_path, fake_server, capsys):
    base_url, seen, state = fake_server
    state["models_status"] = 503
    main(cli_args(tmp_path, base_url) + youtube_args(tmp_path) + ["--engines", "pc-qwen,mac-ollama,youtube-tr", "--seed", "8"])
    sheet, key, timing = read_outputs(tmp_path)
    assert key == expected_key(8, "pc-qwen", "mac-ollama", "youtube-tr") != expected_key(7, "pc-qwen", "mac-ollama", "youtube-tr")
    down = '[engine unavailable: 503: {"error": "loading"}]'
    assert text_by_engine(sheet, key) == {spaced_id(i): {"pc-qwen": down, "mac-ollama": down, "youtube-tr": f"Satır {i}."} for i in range(3)}
    assert [r["method"] for r in seen] == ["GET", "GET"]  # not one chat request
    assert timing["pc-qwen"] == {"mean_seconds": None, "n": 0} and timing["youtube-tr"]["n"] == 3
    assert "mac-ollama: 0 of 3 sentences translated\n" in capsys.readouterr().out

def test_cli_never_requests_a_pinned_pc_model_while_the_pc_holds_another(tmp_path, fake_server, capsys):
    base_url, seen, state = fake_server
    state["loaded"] = "gemma4-12b"
    main(cli_args(tmp_path, base_url) + ["--engines", "pc-qwen,pc-gemma"])
    sheet, key, timing = read_outputs(tmp_path)
    note = "[engine unavailable: qwen3.8-27b is not loaded (gemma4-12b is); load it on the PC first]"
    assert text_by_engine(sheet, key) == {spaced_id(i): {"pc-qwen": note, "pc-gemma": "Gemma çevirisi."} for i in range(3)}
    assert {r["model"] for r in seen if r["method"] == "POST"} == {"gemma4-12b"}  # qwen was never asked for
    assert (timing["pc-qwen"]["n"], timing["pc-gemma"]["n"]) == (0, 3)
    assert "pc-qwen: 0 of 3 sentences translated\n" in capsys.readouterr().out

def test_cli_probes_with_the_short_timeout_so_a_stalled_server_costs_one_short_wait(tmp_path, black_hole, monkeypatch):
    monkeypatch.setattr(bakeoff_translate, "PROBE_TIMEOUT", 0.3)
    began = time.perf_counter()
    main(cli_args(tmp_path, black_hole) + youtube_args(tmp_path) + ["--engines", "pc-qwen,youtube-tr"])
    assert time.perf_counter() - began < 10  # with the 120 s request timeout it would take two minutes
    sheet, key, timing = read_outputs(tmp_path)
    shown = text_by_engine(sheet, key)
    assert all(shown[spaced_id(i)]["pc-qwen"].startswith("[engine unavailable: ") for i in range(3))
    assert timing["pc-qwen"]["n"] == 0 and timing["youtube-tr"]["n"] == 3

def test_cli_takes_one_turkish_file_per_video(tmp_path):
    first = write_tr(tmp_path, (900, "Birinci."), name=f"{VIDEO_A}.tr.translated.json3")
    second = write_tr(tmp_path, (900, "İkinci."), name=f"{VIDEO_B}.tr.translated.json3")
    sentences = [
        {"id": f"{VIDEO_B}.en.asr:0", "start": 1.0, "end": 3.0, "text": "Two."},
        {"id": f"{VIDEO_A}.en.asr:0", "start": 1.0, "end": 3.0, "text": "One."},
    ]
    main(cli_args(tmp_path, "http://127.0.0.1:1", sentences) + ["--engines", "youtube-tr", "--youtube-tr", str(first), str(second)])
    sheet, key, _ = read_outputs(tmp_path)
    assert text_by_engine(sheet, key) == {
        f"{VIDEO_B}.en.asr:0": {"youtube-tr": "İkinci."},
        f"{VIDEO_A}.en.asr:0": {"youtube-tr": "Birinci."},
    }

def test_cli_reads_config_example_toml_and_writes_into_bakeoff_by_default(tmp_path, monkeypatch):
    cli_args(tmp_path, "http://127.0.0.1:1")  # writes sentences.json and config.toml here
    (tmp_path / "config.toml").rename(tmp_path / "config.example.toml")
    monkeypatch.chdir(tmp_path)
    main(["--sentences", "sentences.json", "--engines", "youtube-tr"] + youtube_args(tmp_path))
    assert sorted(path.name for path in (tmp_path / "bakeoff").iterdir()) == ["translate_key.json", "translate_sheet.md", "translate_timing.json"]

@pytest.mark.parametrize("args, complaint", [
    (["--engines", "pc-llama"], "unknown engine 'pc-llama'"),
    (["--engines", "youtube-tr"], "--youtube-tr"),
    (["--engines", "youtube-tr", "--youtube-tr", "missing.json3"], "missing.json3"),
    (["--engines", "youtube-tr", "--youtube-tr", ".tr.translated.json3"], "video id"),
    (["--engines", "youtube-tr", "--youtube-tr", f"{VIDEO_A}.tr.a.json3", f"{VIDEO_A}.tr.b.json3"], f"two Turkish files for video {VIDEO_A}"),
])
def test_cli_exits_with_a_usage_error_for_bad_engines_or_files(tmp_path, capsys, args, complaint):
    with pytest.raises(SystemExit) as raised:
        main(cli_args(tmp_path, "http://127.0.0.1:1") + args)
    assert raised.value.code == 2
    assert complaint in capsys.readouterr().err
