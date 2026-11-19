import json

from dubber.captions import Sentence
from tools.bakeoff_sentences import select_sentences, dedupe_key
from tools.bakeoff_sentences import build, main

def s(i, text):
    return Sentence(start=float(i), end=float(i) + 1, text=text)

def test_select_spreads_evenly_and_filters_short():
    sents = [s(i, f"word {i} " * 7) for i in range(10)] + [s(99, "too short here")]
    picked = select_sentences(sents, n=4)
    assert [p.start for p in picked] == [0.0, 3.0, 6.0, 9.0]

def test_dedupe_key_keeps_turkish_dotted_i():
    assert dedupe_key("İstanbul ışık") != dedupe_key("istanbul ışık")
    assert dedupe_key("  a  b ") == dedupe_key("a b")

def test_dedupe_key_does_not_fold_case():
    # A lower()-based key would pass the Turkish test above, as "İ".lower() is not "i".
    assert dedupe_key("Işık") != dedupe_key("işık")

def test_select_rounds_to_the_nearest_eligible_index():
    sents = [s(i, f"word {i} " * 7) for i in range(6)]
    assert [p.start for p in select_sentences(sents, n=4)] == [0.0, 2.0, 3.0, 5.0]

def test_select_one_picks_the_first_eligible_sentence():
    sents = [s(0, "too short here")] + [s(i, f"word {i} " * 7) for i in range(1, 4)]
    assert [p.start for p in select_sentences(sents, n=1)] == [1.0]

def test_select_returns_each_eligible_sentence_once_when_n_is_larger():
    sents = [s(i, f"word {i} " * 7) for i in range(3)]
    assert [p.start for p in select_sentences(sents, n=10)] == [0.0, 1.0, 2.0]

def test_select_counts_min_words_inclusively():
    five = s(0, "one two three four five")
    six = s(1, "one two three four five six")
    assert select_sentences([five, six], n=2) == [six]
    assert select_sentences([five, six], n=2, min_words=5) == [five, six]

def test_select_returns_nothing_when_no_sentence_is_eligible():
    assert select_sentences([s(0, "too short here")], n=3) == []

def write_captions(path, *lines):
    # One manual-style event per line, a single seg each, so every line is one
    # sentence: sentence i spans 2*i to 2*i + 2 seconds.
    events = [
        {"tStartMs": i * 2000, "dDurationMs": 2000, "segs": [{"utf8": line}]}
        for i, line in enumerate(lines)
    ]
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return path

def test_build_names_each_sentence_by_file_and_position(tmp_path):
    caps = write_captions(tmp_path / "vid.json3", "Too short.", "This sentence is long enough to keep.")
    assert build([caps], n=5) == [
        {"id": "vid:1", "start": 2.0, "end": 4.0, "text": "This sentence is long enough to keep."}
    ]

def test_build_drops_repeats_but_keeps_case_variants(tmp_path):
    first = write_captions(
        tmp_path / "a.json3",
        "The ship is called a Heighliner today.",
        "Spice is what makes space travel possible.",
        "The ship is called a Heighliner today.",
    )
    second = write_captions(
        tmp_path / "b.json3",
        "Spice is what makes space travel possible.",
        "the ship is called a Heighliner today.",
    )
    assert [r["id"] for r in build([first, second], n=10)] == ["a:0", "a:1", "b:1"]

def test_build_spreads_the_selection_over_all_files(tmp_path):
    first = write_captions(tmp_path / "a.json3", *(f"Sentence {i} comes from the first file." for i in range(4)))
    second = write_captions(tmp_path / "b.json3", *(f"Sentence {i} comes from the second file." for i in range(5)))
    assert [r["id"] for r in build([first, second], n=3)] == ["a:0", "b:0", "b:4"]

def test_cli_writes_the_json_and_prints_each_sentence(tmp_path, capsys):
    caps = write_captions(tmp_path / "vid.json3", "İstanbul ışık is a bright old city.")
    out = tmp_path / "made" / "sentences.json"
    main(["--n", "5", "--out", str(out), str(caps)])
    row = {"id": "vid:0", "start": 0.0, "end": 2.0, "text": "İstanbul ışık is a bright old city."}
    written = out.read_text(encoding="utf-8")
    assert json.loads(written) == [row]
    assert "İstanbul ışık" in written  # UTF-8 as it is, not \u escapes
    assert written.startswith('[\n  {\n    "id": "vid:0",')  # indent=2
    printed = capsys.readouterr().out
    assert "vid:0" in printed and row["text"] in printed

def test_cli_writes_into_an_existing_directory(tmp_path):
    caps = write_captions(tmp_path / "vid.json3", "This sentence is long enough to keep.")
    out = tmp_path / "sentences.json"
    main(["--n", "1", "--out", str(out), str(caps)])
    assert [r["id"] for r in json.loads(out.read_text(encoding="utf-8"))] == ["vid:0"]
