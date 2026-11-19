# Warm engines (design of 2026-11-14, 4.5): the Warmer asks the PC voice whether it is ready,
# which also says that the PC is in dubbing mode, and only then touches the translator.
import logging

import pytest

from dubber.engines.base import TranslatorError, VoiceError
from dubber.warmup import Warmer


class Voice:
    """The PC voice: `statuses` are its answers to status(), in turn; the last one stays."""

    def __init__(self, *statuses):
        self.statuses, self.calls = list(statuses), []

    def status(self):
        self.calls.append("status")
        answer = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def warm_up(self):
        self.calls.append("warm_up")


class Translator:
    def __init__(self, answer="gemma4-12b"):
        self.answer, self.calls = answer, 0

    def warm_up(self):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


READY = {"ok": True, "ready": True, "loaded_in_s": 23.5}
now = lambda call: call()   # runs the warm-up before kick() returns


def test_a_kick_speaks_one_word_and_then_loads_the_translator(caplog):
    voice, translator = Voice(READY), Translator()
    with caplog.at_level(logging.INFO, logger="dubber.warmup"):
        assert Warmer(translator, voice, run_in_background=now).kick() is True
    assert voice.calls == ["status", "warm_up"] and translator.calls == 1
    said = [rec.getMessage() for rec in caplog.records]
    assert any("the PC voice spoke" in m for m in said) and any("gemma4-12b" in m for m in said)


def test_the_voice_is_warmed_once_for_each_start_of_the_service():
    voice, translator = Voice(READY, READY, {**READY, "loaded_in_s": 44.0}), Translator(None)
    warmer = Warmer(translator, voice, run_in_background=now)
    warmer.kick(); warmer.kick(); warmer.kick()
    assert voice.calls == ["status", "warm_up", "status", "status", "warm_up"]   # again only after a restart
    assert translator.calls == 3              # the translator is asked each time: it may have unloaded


@pytest.mark.parametrize("status", [
    {"ok": True, "ready": False, "loaded_in_s": None},                 # still loading its model
    VoiceError("the PC voice service answered HTTP 502: no explanation"),   # the PC is not in dubbing mode
])
def test_nothing_is_asked_of_the_translator_unless_the_voice_is_ready(status, caplog):
    voice, translator = Voice(status), Translator()
    with caplog.at_level(logging.INFO, logger="dubber.warmup"):
        assert Warmer(translator, voice, run_in_background=now).kick() is True
    assert voice.calls == ["status"] and translator.calls == 0
    assert [rec.levelname for rec in caplog.records] == ["INFO"] and "no warm-up" in caplog.records[0].getMessage()


def test_a_translator_that_fails_is_logged_and_nothing_is_raised(caplog):
    with caplog.at_level(logging.INFO, logger="dubber.warmup"):
        Warmer(Translator(TranslatorError("500: out of memory")), Voice(READY), run_in_background=now).kick()
    assert "no warm-up: 500: out of memory" in [rec.getMessage() for rec in caplog.records]


def test_an_unexpected_failure_is_logged_and_the_next_kick_still_runs(caplog):
    voice = Voice(READY)
    warmer = Warmer(Translator(RuntimeError("boom")), voice, run_in_background=now)
    with caplog.at_level(logging.ERROR, logger="dubber.warmup"):
        warmer.kick()
    assert len(caplog.records) == 1
    assert warmer.kick() is True and voice.calls.count("status") == 2


def test_a_kick_while_a_warm_up_runs_starts_nothing():
    waiting = []
    warmer = Warmer(Translator(), Voice(READY), run_in_background=waiting.append)
    assert warmer.kick() is True and warmer.kick() is False and len(waiting) == 1
    waiting[0]()                              # the first one runs to its end
    assert warmer.kick() is True and len(waiting) == 2
