"""A dubbing run on a clock that only the simulation moves.

The real Session, scheduler, coverage and mixer run here; the engines and the player are
models. A translation and a clip cost the seconds a `Model` gives them, the player obeys the
session's pause and play and reports its state every 0.25 s, and time passes only while an
engine works or the producer idles. A translation made ahead costs its time beside the voice,
as it does on a thread of its own, unless `ahead` is off.

It is a helper for tests (tests/test_catch_up.py), not a test file.
"""

import json
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from dubber.config import Config
from dubber.engines.base import Clip
from dubber.metrics import MetricsLog, SegmentRecord
from dubber.playback import AudioOut
from dubber.protocol import Captions, Control, Start, State, Status
from dubber.session import Session

REPORT_EVERY = 0.25  # the extension's state reports, in seconds
_RATE = 8000  # the sample rate of the modelled clips: low, so a long run stays small


@dataclass
class Model:
    """What the engines cost, in seconds.

    `mt(n, seconds, now)` is the n-th translator call (a translation or a shorter rendering),
    for a sentence that lasts `seconds` in the video, begun at `now` on the run's clock;
    `tts(n, seconds, now)` the n-th clip, `seconds` of audio long. Both count from 1.
    `talk(i)` is the length of sentence i's Turkish clip over the sentence's own length.
    """

    mt: Callable[[int, float, float], float]
    tts: Callable[[int, float, float], float]
    talk: Callable[[int], float] = lambda i: 1.0


def warm_remote() -> Model:
    """The warm engines as measured over a long-distance link on 2026-11-14 (0.7 to 0.95 s of work for each second of speech)."""
    return Model(mt=lambda n, d, now: 0.4 + 0.1 * d, tts=lambda n, d, now: 0.7 + 0.42 * d)


def uneven(i: int) -> float:
    """A Turkish clip from 0.8 to 1.3 times its sentence, the same for sentence i in every run."""
    return 0.8 + 0.5 * ((i * 7919) % 100) / 100


@dataclass
class Run:
    """What a simulated run did."""

    records: list[SegmentRecord]
    startup: float  # how long the start-up hold lasted, in seconds
    holds: list[tuple[float, float]] = field(default_factory=list)  # (video time, seconds) of each later hold
    exceeded: int = 0  # how many holds ended at their cap

    def dubbed(self, after: float = -1.0) -> list[SegmentRecord]:
        return [r for r in self.records if not r.dropped and r.src_start >= after]

    def dropped(self, after: float = -1.0) -> list[SegmentRecord]:
        return [r for r in self.records if r.dropped and r.src_start >= after]


class _Recording(MetricsLog):
    """Keeps the records and writes no file."""

    def __init__(self) -> None:
        super().__init__(pathlib.Path("unused.jsonl"))
        self.records: list[SegmentRecord] = []

    def write(self, rec: SegmentRecord) -> None:
        self.records.append(rec)


class _Sim:
    def __init__(self, model: Model, start: float, cfg: Config, ahead: bool) -> None:
        self.model, self.now, self.pos, self.paused = model, 0.0, start, False
        self.durations: dict[str, float] = {}  # the video seconds of each source sentence
        self.words: dict[str, int] = {}
        self.index: dict[str, int] = {}
        self.mt_calls = self.tts_calls = 0
        self.translator_free_at = 0.0  # when the translator has finished what it was given
        self.virtual = False  # a translation made ahead: it costs its time beside the voice
        self.states: list[tuple[float, float, str, str]] = []  # (clock, video time, state, message)
        self.metrics = _Recording()
        spawn = self._ahead if ahead else self._in_turn
        self.session = Session(
            self, self, AudioOut(stream_factory=lambda callback: None), self.metrics, cfg,
            send=self._receive, clock=lambda: self.now, spawn=spawn,
        )

    # The player.

    def _receive(self, msg: object) -> None:
        if isinstance(msg, Control) and msg.action in ("pause", "play"):
            self.paused = msg.action == "pause"
        elif isinstance(msg, Status):
            self.states.append((self.now, self.pos, msg.state, msg.message))

    def report(self) -> None:
        self.session.handle(State(video_id="v", current_time=self.pos, rate=1.0, paused=self.paused, live=False, volume=0.8))

    def spend(self, seconds: float) -> None:
        """Let `seconds` pass: the video moves while it plays, and reports every 0.25 s."""
        while seconds > 1e-9:
            step = min(REPORT_EVERY, seconds)
            seconds -= step
            self.now += step
            if not self.paused:
                self.pos += step
            self.report()

    def wait_until(self, when: float) -> None:
        if when > self.now:
            self.spend(when - self.now)

    # The translator: one call at a time, as the session's lock has it.

    name = "modelled"

    def _translator_works(self, seconds: float) -> None:
        if self.virtual:
            self.translator_free_at = max(self.now, self.translator_free_at) + seconds
        else:
            self.wait_until(self.translator_free_at)
            self.spend(seconds)
            self.translator_free_at = self.now

    def translate(self, text: str, context: object = (), glossary: object = None) -> str:
        self.mt_calls += 1
        self._translator_works(self.model.mt(self.mt_calls, self.durations[text], self.now))
        return text

    def shorten(self, text: str, turkish: str, max_words: int, context: object = (), glossary: object = None) -> str:
        self.mt_calls += 1
        self._translator_works(self.model.mt(self.mt_calls, self.durations[text], self.now))
        return " ".join(turkish.split()[: max(1, max_words)])

    def _ahead(self, call: Callable[[], object]) -> "_Handle":
        self.virtual = True
        try:
            value = call()
        finally:
            self.virtual = False
        return _Handle(self, value, self.translator_free_at)

    def _in_turn(self, call: Callable[[], object]) -> "_Handle":
        return _Handle(self, None, 0.0, call)

    # The voice.

    def _source(self, text: str) -> str:
        """The sentence `text` renders: itself, or the one a shorter rendering was cut from."""
        if text in self.durations:
            return text
        return next(s for s in self.durations if s.startswith(text))

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        self.tts_calls += 1
        source = self._source(text)
        share = len(text.split()) / self.words[source]
        seconds = max(0.2, self.durations[source] * self.model.talk(self.index[source]) * share)
        self.spend(self.model.tts(self.tts_calls, seconds, self.now))
        return Clip(np.zeros(round(seconds * _RATE), dtype=np.float32), _RATE)


class _Handle:
    """A translation made ahead: ready at `ready_at`; or, in turn, made when it is asked for."""

    def __init__(self, sim: _Sim, value: object, ready_at: float, call: Callable[[], object] | None = None) -> None:
        self._sim, self._value, self._ready_at, self._call = sim, value, ready_at, call

    def result(self) -> object:
        if self._call is not None:
            self._value, self._call = self._call(), None
        self._sim.wait_until(self._ready_at)
        return self._value


def simulate(
    track: dict | pathlib.Path | str,
    start: float,
    seconds: float,
    model: Model,
    cfg: Config | None = None,
    ahead: bool = True,
) -> Run:
    """Dub `seconds` of video from `start` on the caption `track` (json3, or the path of a file) and say what happened."""
    if not isinstance(track, dict):
        track = json.loads(pathlib.Path(track).read_text(encoding="utf-8"))
    sim = _Sim(model, start, cfg or Config(), ahead)
    session = sim.session
    session.handle(Start(mode=None))
    sim.report()
    session.handle(Captions(video_id="v", lang="en", kind="asr", json3=track))
    for seg in session.segments:
        sim.durations[seg.text] = seg.end - seg.start
        sim.words[seg.text] = len(seg.text.split())
        sim.index[seg.text] = seg.idx
    end, limit = start + seconds, 20 * seconds + 600  # the limit ends a run whose video never plays
    while sim.pos < end and sim.now < limit:
        if not session.produce_next():
            sim.spend(0.05)
    records = [r for r in sim.metrics.records if r.src_start < end]
    run = Run(records, startup=0.0)
    held_at: tuple[float, float] | None = None
    began = 0.0
    for clock, pos, state, message in sim.states:
        if state in ("preparing", "holding") and held_at is None:
            held_at = (clock, pos)
        elif state == "playing" and held_at is not None:
            if not run.holds and run.startup == 0.0 and held_at[0] == began:
                run.startup = clock - held_at[0]
            else:
                run.holds.append((held_at[1], clock - held_at[0]))
            run.exceeded += message == "hold exceeded"
            held_at = None
    return run
