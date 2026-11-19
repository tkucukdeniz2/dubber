"""A caption track without punctuation: the session restores it a stretch at a time, ahead of the video."""

import logging
import re

import numpy as np
import pytest

from dubber.captions import parse_json3
from dubber.config import Config
from dubber.engines.base import Clip, IdentityTranslator, TranslatorError
from dubber.protocol import Control, Seek, Start, Captions
from dubber.punctuation import FIRST_STRETCH, LEFT_CONTEXT, STRETCH
from dubber.segmenter import segment
from tests.test_session import FIVE, Rig

STEP = 0.5            # every word takes half a second, and there is no gap anywhere
LENGTHS = [8] * 40    # 40 sentences of 8 words: 320 words, 160 s


def bare_words(lengths=LENGTHS):
    return [f"s{k}w{j}" for k, n in enumerate(lengths) for j in range(n)]


def captions(lengths=LENGTHS, per_event=10):
    """Auto-captions of the sentences without a mark or a capital, ten words to an event."""
    tokens = bare_words(lengths)
    events = []
    for a in range(0, len(tokens), per_event):
        chunk = tokens[a : a + per_event]
        events.append({
            "tStartMs": round(a * STEP * 1000),
            "dDurationMs": round(len(chunk) * STEP * 1000),
            "segs": [{"utf8": (" " if n else "") + t, "tOffsetMs": round(n * STEP * 1000)} for n, t in enumerate(chunk)],
        })
    return {"events": events}


def marked(text, lengths=LENGTHS):
    """What a model that knows the sentences gives back: a capital on each first word, a full stop on each last."""
    out = []
    for token in text.split():
        k, j = (int(x) for x in re.fullmatch(r"s(\d+)w(\d+)", token).groups())
        out.append((token.capitalize() if j == 0 else token) + ("." if j == lengths[k] - 1 else ""))
    return " ".join(out)


def sentence(k, lengths=LENGTHS):
    return marked(" ".join(f"s{k}w{j}" for j in range(lengths[k])), lengths)


class Punctuating(IdentityTranslator):
    """The stand-in translator with a `punctuate` that knows the sentences; `fail` lists the calls that raise."""

    name = "punctuating"

    def __init__(self, fail=(), during=None, lengths=LENGTHS):
        self.asked = []
        self.fail = set(fail)
        self.during = during  # called inside the first punctuate, as something that happens meanwhile
        self.lengths = lengths

    def punctuate(self, text):
        self.asked.append(text)
        if self.during is not None and len(self.asked) == 1:
            self.during()
        if len(self.asked) - 1 in self.fail:
            raise TranslatorError("the model is away")
        return marked(text, self.lengths)


def first_stretch(rig):
    """Produce what the first stretch settles: the six sentences that end 5 words or more before word 60."""
    while rig.s.produced_count < 6:
        assert rig.s.produce_next()


def test_a_track_without_punctuation_is_dubbed_a_sentence_at_a_time(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator, lead_max=10_000)
    rig.start(captions(), at=0.0)
    assert rig.s.segments == []                      # nothing is cut before the model has answered
    rig.produce_all()
    assert [s.text for s in rig.s.segments] == [sentence(k) for k in range(40)]
    assert [s.idx for s in rig.s.segments] == list(range(40))
    assert rig.s.produced_count == 40
    assert rig.voice.calls == [sentence(k) for k in range(40)]
    # three stretches: 60 words, of which 48 are settled, then 150, then the rest
    assert [len(text.split()) for text in translator.asked] == [FIRST_STRETCH, STRETCH, 320 - 192]
    assert translator.asked[1].split()[0] == "s6w0" and translator.asked[2].split()[0] == "s24w0"


def test_the_video_plays_once_the_first_sentences_are_ready_and_is_held_at_the_edge(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(), at=0.0)
    first_stretch(rig)                               # 24 s are made, and the edge is there
    rig.state(0.0, paused=True)
    assert rig.s.state == "playing"
    rig.state(0.0)                                   # the player plays
    rig.sent.clear()
    rig.advance(24.2)
    rig.state(24.2)
    assert rig.s.state == "holding" and rig.of(Control)[-1].action == "pause"
    rig.produce_all()
    # the track went on from its edge: it was not begun again
    assert len(translator.asked) == 3 and rig.s.segments[0].text == sentence(0)
    rig.state(24.2, paused=True)
    assert rig.s.state == "playing" and rig.of(Control)[-1].action == "play"


def test_the_last_stretch_closes_the_track(tmp_path):
    rig = Rig(tmp_path, translator=Punctuating(), lead_max=10_000)
    rig.start(captions(), at=0.0)
    rig.produce_all()
    rig.state(0.0, paused=True)
    rig.advance(170.0)                               # past the end of the track: nothing holds the video there
    rig.state(170.0)
    assert rig.s.state == "playing"


def test_a_jump_past_the_edge_begins_the_track_again_there(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(), at=0.0)
    first_stretch(rig)
    rig.state(0.0, paused=True)
    rig.s.handle(Seek(current_time=102.0))           # word 204: two seconds into sentence 25
    assert rig.s.state == "holding" and rig.s.segments == []
    rig.produce_all()
    # the stretch begins 30 words before the playhead, in the middle of sentence 21
    assert translator.asked[1].split()[0] == f"s21w{204 - LEFT_CONTEXT - 21 * 8}"
    assert len(translator.asked[1].split()) == FIRST_STRETCH
    # the sentence the playhead is in is not dubbed, and none before it: the dub begins with the next one
    assert rig.voice.calls[6] == sentence(26)
    assert all(not text.startswith(("S21", "S22", "S23", "S24", "S25", "s21")) for text in rig.voice.calls[6:])
    rig.state(102.0, paused=True)
    assert rig.s.state == "playing"


def test_a_jump_inside_the_open_part_keeps_what_was_made(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(), at=0.0)
    first_stretch(rig)
    rig.state(0.0, paused=True)
    asked, made = len(translator.asked), len(rig.voice.calls)
    rig.s.handle(Seek(current_time=12.0))
    assert rig.s.state == "playing" and len(rig.s.segments) == 6
    assert len(translator.asked) == asked and len(rig.voice.calls) == made


def test_a_jump_before_the_beginning_of_the_open_part_begins_again_there(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(), at=0.0)
    first_stretch(rig)
    rig.state(0.0, paused=True)
    rig.s.handle(Seek(current_time=102.0))
    rig.produce_all()
    asked, made = len(translator.asked), len(rig.voice.calls)
    rig.s.handle(Seek(current_time=20.0))            # word 40, before word 174 where the track was begun again
    assert rig.s.segments == [] and rig.s.state == "holding"
    rig.produce_all()
    assert translator.asked[asked].split()[0] == "s1w2"   # 30 words before word 40
    assert rig.voice.calls[made] == sentence(5)           # the sentence at 20 s is made again


def test_the_first_report_elsewhere_begins_the_track_there(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.s.handle(Start(mode=None))
    rig.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=captions()))
    assert rig.s.produce_next()                      # the player has not said where it is: begun at the top
    assert translator.asked[0].split()[0] == "s0w0"
    rig.state(102.0, paused=True)                    # it is at 102 s
    assert rig.s.segments == []
    rig.produce_all()
    assert translator.asked[1].split()[0] == "s21w6"
    assert rig.voice.calls[0] == sentence(26)


def test_a_first_report_near_the_top_does_not_begin_the_track_again(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.s.handle(Start(mode=None))
    rig.s.handle(Captions(video_id="v", lang="en", kind="asr", json3=captions()))
    rig.state(1.2, paused=True)                      # before anything is restored
    assert rig.s.produce_next()
    rig.state(1.2, paused=True)                      # and after
    while rig.s.produced_count < 5:
        assert rig.s.produce_next()
    assert len(translator.asked) == 1 and translator.asked[0].split()[0] == "s0w0"
    assert rig.voice.calls[0] == sentence(1)         # sentence 0 began 1.2 s before the playhead


def test_a_stretch_restored_for_a_track_that_was_begun_again_is_dropped(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    translator.during = lambda: rig.s.handle(Seek(current_time=102.0))
    rig.start(captions(), at=0.0)
    assert rig.s.produce_next()                      # the jump comes while the model copies the first stretch
    assert rig.s.segments == []                      # its sentences are for the old place
    rig.produce_all()
    assert translator.asked[1].split()[0] == "s21w6" and rig.voice.calls[0] == sentence(26)


def test_a_stretch_that_cannot_be_restored_is_cut_without_punctuation_and_the_dub_goes_on(tmp_path, caplog):
    translator = Punctuating(fail={0})
    rig = Rig(tmp_path, translator=translator, lead_max=10_000)
    rig.start(captions(), at=0.0)
    with caplog.at_level(logging.WARNING, logger="dubber.session"):
        rig.produce_all()
    assert any("punctuation" in r.getMessage() and "the model is away" in r.getMessage() for r in caplog.records)
    texts = [s.text for s in rig.s.segments]
    covered = " ".join(texts).split()
    assert [re.sub(r"\W", "", w).lower() for w in covered] == bare_words()   # every word once, in order
    unmarked = [t for t in texts if not t.endswith(".")]
    assert unmarked and " ".join(unmarked).split() == bare_words()[:STRETCH]  # the failed stretch: 150 words as they came
    assert translator.asked[1].split()[0] == f"s18w{STRETCH - 18 * 8}"         # the next one is restored again
    assert rig.s.produced_count == len(texts)


def test_two_failures_in_a_row_cut_the_rest_of_the_track_without_punctuation(tmp_path):
    translator = Punctuating(fail={0, 1})
    rig = Rig(tmp_path, translator=translator, lead_max=10_000)
    rig.start(captions(), at=0.0)
    rig.produce_all()
    assert len(translator.asked) == 2                # the model is not asked a third time
    assert " ".join(s.text for s in rig.s.segments).split() == bare_words()
    rig.state(0.0, paused=True)
    rig.advance(170.0)
    rig.state(170.0)
    assert rig.s.state == "playing"                  # the track is closed: nothing is held at its end


def test_a_translator_that_cannot_punctuate_cuts_the_track_as_before(tmp_path, caplog):
    rig = Rig(tmp_path)
    with caplog.at_level(logging.WARNING, logger="dubber.session"):
        rig.start(captions(), at=0.0)
    assert rig.s.segments == segment(parse_json3(captions()))
    assert any("almost no sentence punctuation" in r.getMessage() for r in caplog.records)


def test_restore_punctuation_can_be_switched_off(tmp_path):
    translator, cfg = Punctuating(), Config()
    cfg.translator.restore_punctuation = False
    rig = Rig(tmp_path, translator=translator, cfg=cfg)
    rig.start(captions(), at=0.0)
    rig.produce_all()
    assert translator.asked == [] and rig.s.segments == segment(parse_json3(captions()))


def test_a_track_with_punctuation_is_never_sent_to_the_model(tmp_path):
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator)
    rig.start(FIVE, at=0.0)
    rig.produce_all()
    assert translator.asked == [] and len(rig.s.segments) == 5


def test_a_hold_past_its_cap_before_the_first_stretch_does_not_end_the_run(tmp_path):
    rig = Rig(tmp_path, translator=Punctuating())
    rig.start(captions(), at=0.0)
    rig.advance(21.0)                                # the cap is 20 s, and the model has not answered
    rig.state(0.0, paused=True)
    assert rig.s.state != "idle"
    rig.produce_all()
    assert rig.voice.calls and rig.voice.calls[0].startswith("S")


def test_no_stretch_is_restored_while_an_engine_is_down(tmp_path):
    class Down(Punctuating):
        def translate(self, text, context=(), glossary=None):
            raise TranslatorError("the model is away")
    translator = Down()
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(), at=0.0)
    while rig.s.produce_next():                      # the first stretch; two sentences fail, and the producer waits
        pass
    assert len(translator.asked) == 1
    for _ in range(4):                               # the other four fail too, a wait after each
        rig.advance(6.0)
        assert rig.s.produce_next()
    assert rig.s.produced_count == 6
    assert not rig.s.produce_next()                  # at the edge, within the wait: no stretch is asked for
    assert len(translator.asked) == 1
    rig.advance(6.0)                                 # the wait is over
    assert rig.s.produce_next() and len(translator.asked) == 2


def test_the_log_has_the_english_and_the_turkish_of_a_placed_sentence(tmp_path, caplog):
    class Turkish(IdentityTranslator):
        def translate(self, text, context=(), glossary=None):
            return "Türkçesi: " + text
    rig = Rig(tmp_path, translator=Turkish())
    rig.start(FIVE, at=0.0)
    with caplog.at_level(logging.INFO, logger="dubber.session"):
        rig.s.produce_next()
    said = 'segment 0: "This is sentence number 0 here." -> "Türkçesi: This is sentence number 0 here."'
    assert said in [r.getMessage() for r in caplog.records]


# What the final review found.

LONG = [34] * 12      # twelve sentences of 34 words, 17 s each: under the caps, and longer than a first stretch can see past


def test_reports_while_preparing_do_not_begin_the_track_again(tmp_path):
    # A start 2.5 s into a 34-word sentence. The first stretch (30 words back, 60 in all) has two
    # sentence ends: one behind the playhead, and one three words before the stretch ends, too near
    # to trust. So what is restored ends behind the playhead.
    translator = Punctuating(lengths=LONG)
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(LONG), at=36.5)
    for _ in range(3):
        rig.state(36.5, paused=True)                 # the extension reports four times a second
        rig.s.produce_next()
    assert [text.split()[0] for text in translator.asked[:2]] == ["s1w9", "s2w0"]   # not the same stretch again
    assert len(translator.asked) == 2
    rig.produce_all()
    assert rig.of(Control)[-1].action == "play"      # and the video is given back once the dub is ready


def test_the_sentence_the_playhead_is_in_is_skipped_when_it_comes_with_a_later_stretch(tmp_path):
    translator = Punctuating(lengths=LONG)
    rig = Rig(tmp_path, translator=translator)
    rig.start(captions(LONG), at=36.5)               # sentence 2 began at 34.0 s and comes with the second stretch
    rig.produce_all()
    assert rig.voice.calls[0] == sentence(3, LONG)   # the dub begins at the next sentence, as on any track
    assert sentence(2, LONG) not in rig.voice.calls


def test_the_last_restored_sentence_knows_where_the_next_one_begins(tmp_path):
    # Its clip is 5.4 s for a slot of 4 s, which may run 0.8 s over. The sentence after it is not
    # restored yet, but its first word is in the track, so the clip is sped up to fit like any
    # other, not placed as the last of the video.
    class Long:
        name = "long"
        def __init__(self):
            self.calls = []
        def synthesize(self, text, speed=1.0):
            self.calls.append(text)
            seconds = 5.4 if text == sentence(5) else 1.0
            return Clip(samples=np.zeros(round(seconds * 22050), dtype=np.float32), sample_rate=22050)
    rig = Rig(tmp_path, translator=Punctuating(), voice=Long())
    rig.start(captions(), at=0.0)
    first_stretch(rig)
    record = rig.metrics.records[-1]
    assert record.idx == 5 and record.speed == pytest.approx(1.13)


def test_a_track_of_whole_lines_is_restored_by_its_words_too(tmp_path):
    # captions typed by a person, without punctuation: a line of eight words to an event, with no word times
    tokens = bare_words()
    events = [
        {"tStartMs": round(a * STEP * 1000), "dDurationMs": round(8 * STEP * 1000), "segs": [{"utf8": " ".join(tokens[a : a + 8])}]}
        for a in range(0, len(tokens), 8)
    ]
    translator = Punctuating()
    rig = Rig(tmp_path, translator=translator, lead_max=10_000)
    rig.start({"events": events}, at=0.0)
    rig.produce_all()
    assert len(translator.asked[0].split()) == FIRST_STRETCH
    assert [s.text for s in rig.s.segments] == [sentence(k) for k in range(40)]
