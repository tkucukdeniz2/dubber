import json, pathlib
from dubber.captions import parse_json3, cues_to_sentences

FIX = pathlib.Path(__file__).parent / "fixtures"

def load(name):
    return json.loads((FIX / name).read_text())

def test_parse_asr_words_and_skips_filler_and_duplicate():
    cues = parse_json3(load("captions_asr.json"))
    assert [c.text for c in cues] == ["The ship is called", "a Heighliner and I", "think it's imaginative.", "But think again?"]
    first = cues[0]
    assert first.start == 61.88 and first.end == 63.72
    assert [w.text for w in first.words] == ["The", "ship", "is", "called"]
    assert first.words[1].start == 62.2 and first.words[1].end == 62.48
    assert first.words[-1].end == 63.72

def test_parse_manual_single_word_per_cue():
    cues = parse_json3(load("captions_manual.json"))
    assert len(cues) == 3 and len(cues[0].words) == 1
    assert cues[0].words[0].start == 16.26 and cues[0].words[0].end == 20.23

def test_sentences_split_on_terminal_punctuation():
    cues = parse_json3(load("captions_asr.json"))
    sents = cues_to_sentences(cues)
    assert [s.text for s in sents] == ["The ship is called a Heighliner and I think it's imaginative.", "But think again?"]
    assert sents[0].start == 61.88 and sents[1].start == 68.04

def test_parse_skips_events_without_segs_or_text():
    doc = {"events": [
        {"tStartMs": 0, "dDurationMs": 10000, "id": 1, "wpWinPosId": 1, "wsWinStyleId": 1},
        {"tStartMs": 500, "dDurationMs": 100, "segs": []},
        {"tStartMs": 600, "dDurationMs": 100, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello there."}]},
    ]}
    cues = parse_json3(doc)
    assert [c.text for c in cues] == ["Hello there."]
    sents = cues_to_sentences(cues)
    assert [s.text for s in sents] == ["Hello there."]
