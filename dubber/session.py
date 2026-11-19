"""One dubbing session: the state machine between the extension and the dub.

The extension's messages arrive through `handle`, on the server's event loop. Between
Start and Stop a producer thread runs `run_producer`, which keeps calling
`produce_next`: it translates and voices the next segment the playhead will need,
places the clip on the video timeline, schedules it on the mixer, sends its subtitle
and logs its metrics. `handle` holds the player (paused, with the overlay saying
"preparing" or "holding") until enough is ready, follows the player's pause, rate and
seeks, and reports a Status that the overlay shows.

One lock guards the session. Translation and synthesis run outside it, so a clip can
come back for a timeline that has changed meanwhile. A generation counter, bumped by
Start, Captions, Stop and every seek that leaves the produced range, tells the
producer to discard such a clip. `send` is called from both threads.

A hold lasts at most `hold_cap` seconds; then the video plays on, and the producer
catches up. It no longer takes the sentence at the playhead, which would be over before
its clip is ready, but aims at the first one whose clip can be ready when it begins,
judged from how long the last make took (`_aim`). What it jumps over is given up and
logged as dropped. Holds come back once a clip is ready `lead_min` seconds ahead.

The translator and the voice run on different machines or cards, so the producer does
not wait for one to start the other: before it voices a sentence it starts the
translation of the next one, on a thread of its own (`spawn`). A sentence's context is
the English before it, so that translation needs nothing from the clip in the making.

Times are media time, seconds into the video, except the hold timer and the Status
throttle, which read the session's `clock`.
"""

import collections
import functools
import itertools
import logging
import math
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from dubber.captions import parse_json3
from dubber.config import Config
from dubber.coverage import Coverage
from dubber.engines.base import Clip, Translator, Voice, failure_reason
from dubber.metrics import MetricsLog, SegmentRecord
from dubber.playback import AudioOut
from dubber.protocol import (
    Captions,
    Control,
    Duck,
    Hello,
    Message,
    Seek,
    Start,
    State,
    Status,
    Stop,
    Subtitle,
)
from dubber.scheduler import Placement, ReadyGate, Timeline, clamp_rate
from dubber.punctuation import LEFT_CONTEXT, OpenTrack, restore_stretch
from dubber.segmenter import Segment, looks_unpunctuated, segment, segment_words, track_words

log = logging.getLogger(__name__)

# A state report this many seconds away from the media clock is a seek.
_JUMP = 1.0
# A Seek message this close to the playhead changes nothing: it is the "seeked" report
# of a jump that a state report has already shown.
_SAME_PLACE = 0.25
# While the video plays, a clip starts at least this far ahead of the playhead.
_MARGIN = 0.15
# How late a clip may start after its sentence begins when the producer aims: Coverage.skip_to's `early`.
_LATE_START = 0.5
# The aim takes a clip to need this many times what the last one took, since a clip that comes
# too late costs a whole make, and one aimed too far only a sentence.
_AIM_MARGIN = 1.1
# Below this many seconds a sentence's make is mostly fixed cost, so a longer one is not scaled from less.
_SHORTEST = 2.0
# After this many sentences in a row have failed at an engine, the producer waits this many
# seconds before each further try, until one succeeds: an engine that is down then costs a
# sentence now and then, not the whole look-ahead in a few seconds.
_FAILURES_BEFORE_WAIT = 2
_FAILURE_WAIT = 5.0
# The player's rate while the dub catches up on a backlog.
_SLOW_RATE = 0.9
# Seconds between the Status reports sent while playing.
_STATUS_EVERY = 1.0
# How long the producer waits when there is nothing to produce.
_IDLE_WAIT = 0.05
# This many stretches of an open track in a row that the model could not restore, and the
# rest of the track is cut without punctuation: a model that cannot do it is not asked again.
_OPEN_FAILURES = 2
# A shorter rendering is never asked to be under this share of the first one's words: a
# rephrasing that keeps every content word cannot cut more, and dropping content is a
# meaning error. What still does not fit runs long and is caught up later.
_SHRINK_FLOOR = 0.6

# Numbers the session ids of this process, so that two runs never share one.
_run_numbers = itertools.count(1)


@dataclass(frozen=True)
class _Made:
    """What translating and voicing a segment gave.

    `text` is the translation, or the source text when the translation failed. `clip`
    is None when there is nothing to play, and `error` then says why. `shortened` is
    True for the translator's second, shorter rendering.
    """

    text: str
    clip: Clip | None
    mt_ms: float
    tts_ms: float
    error: str = ""
    shortened: bool = False


@dataclass(frozen=True)
class _Translated:
    """What translating a segment gave: the Turkish, or the source text and why it failed."""

    text: str
    mt_ms: float
    error: str = ""


class Handle(Protocol):
    """A translation that may still be running."""

    def result(self) -> _Translated | None:
        """Wait for the translation and return it; None if the call itself broke."""


# Starts a translation somewhere else and returns at once with its handle.
Spawn = Callable[[Callable[[], _Translated]], Handle]


class _Thread:
    """Runs a call on a daemon thread of its own, so it never keeps the process from ending."""

    def __init__(self, call: Callable[[], _Translated]) -> None:
        self._call = call
        self._done = threading.Event()
        self._value: _Translated | None = None
        threading.Thread(target=self._run, name="dubber-translate", daemon=True).start()

    def _run(self) -> None:
        try:
            self._value = self._call()
        finally:
            self._done.set()

    def result(self) -> _Translated | None:
        self._done.wait()
        return self._value


# What the producer works on: a segment, the start of the segment after it (None for
# the last), the generation it was picked in, and the texts of the segments before it
# that go with it as context.
_Job = tuple[Segment, float | None, int, tuple[str, ...]]


def _ms_since(began: float) -> float:
    return (time.perf_counter() - began) * 1000


def _cost(made: "_Made") -> str:
    """What a sentence took to make, for the log: its translation, its voice and the clip's length."""
    clip = made.clip.seconds if made.clip is not None else 0.0
    return f"translation {made.mt_ms / 1000:.1f} s, voice {made.tts_ms / 1000:.1f} s, clip {clip:.1f} s"


def _describe(exc: Exception) -> str:
    """`TypeName: why` for the bar and the log; the type name alone when there is no more to say."""
    name, why = type(exc).__name__, failure_reason(exc)
    return name if why == name else f"{name}: {why}"


class Session:
    """The session of one connection; see the module docstring.

    `state` is "idle", "preparing", "playing", "paused" or "holding" ("stopped" is
    reserved and never used: a stopped session is idle). `segments` is the caption
    track cut into segments, and
    `produced_count` the number of them produced: scheduled, or shown as a subtitle
    when an engine failed. `produced_until` is the media time up to which the
    segments from the playhead on are produced without a gap, and `session_id` names
    the current run in the metrics.
    """

    def __init__(
        self,
        translator: Translator,
        voice: Voice,
        audio: AudioOut,
        metrics: MetricsLog,
        cfg: Config,
        send: Callable[[Message], None],
        clock: Callable[[], float] = time.monotonic,
        spawn: Spawn = _Thread,
    ) -> None:
        self._translator = translator
        self._voice = voice
        self._audio = audio
        self._metrics = metrics
        self._cfg = cfg
        self._send = send
        self._clock = clock
        self._gate = ReadyGate(cfg.scheduler)
        self._timeline = Timeline(cfg.scheduler)
        self._lock = threading.Lock()
        self._spawn = spawn
        # One translator call at a time: a shorter rendering and a translation made ahead take turns.
        self._translating = threading.Lock()
        # The translation made ahead, with the track and segment it is for; guarded by the lock.
        self._ahead: tuple[tuple[int, int], Handle] | None = None
        self._track = 0  # counts the caption tracks, so a translation is never used for another one
        # What the last three makes took on the session clock, each with the seconds of its
        # sentence: the producer's speed, by which it aims once it has fallen behind. Guarded by the lock.
        self._makes: collections.deque[tuple[float, float]] = collections.deque(maxlen=3)
        # Engine failures: how many sentences in a row failed, the clock time before which
        # the producer does not try again, and what the status says meanwhile. Guarded by the lock.
        self._failures = 0
        self._retry_at = -math.inf
        self._down = ""
        self.state = "idle"
        self.session_id = ""
        self._coverage = Coverage()
        # A track without punctuation, restored a stretch at a time (see _grow): its words, the
        # word it was begun at, the first word that is not restored yet (None once all are, and
        # for a track that came with punctuation), and how many stretches in a row failed.
        # Guarded by the lock.
        self._open: OpenTrack | None = None
        self._open_first = 0
        self._open_next: int | None = None
        self._open_failures = 0
        # A jump or a start skipped to a place of an open track whose sentence is not
        # restored yet: the skip is made again when that sentence comes (see _skip_to).
        self._skip_pending = False
        self._generation = 0
        self._video_id: str | None = None
        # The video the voice was last told is on (see _prepare_voice); None after a Start.
        self._voice_video: str | None = None
        self._started_at = 0.0  # clock() at Start
        self._text_ready_at = 0.0  # seconds from Start to the captions
        self._hold_since = 0.0  # clock() when the current hold began
        # The last hold ended at its cap; holds resume once the dub has caught up.
        self._hold_exceeded = False
        self._running = False  # whether the mixer clock runs
        self._anchored = False  # whether a report or a seek has set the clock since Start
        self._owner_paused = False  # the current hold began while the owner had paused
        self._rate = 1.0  # the player's last reported rate
        self._pushed_back: float | None = None  # the offending rate last pushed back
        self._clamped_from: float | None = None  # the owner's rate, given back on stop
        self._slow_from: float | None = None  # the rate given back when the slow ends
        self._last_status = -math.inf
        self._audio_failed = False
        self._metrics_failed = False

    @property
    def segments(self) -> list[Segment]:
        return self._coverage.segments

    @property
    def _engine(self) -> str:
        """The engines in use, for the status. A voice that goes by the video changes its name with it."""
        return f"{self._translator.name}+{self._voice.name}"

    @property
    def produced_count(self) -> int:
        with self._lock:
            return self._coverage.produced_count

    @property
    def produced_until(self) -> float:
        with self._lock:
            return self._coverage.covered_until(self._playhead())

    def handle(self, msg: Message) -> None:
        """Act on one message from the extension; other kinds of message are ignored."""
        with self._lock:
            match msg:
                case Start():
                    self._on_start(msg.mode)
                case Captions():
                    self._on_captions(msg)
                case State():
                    self._on_state(msg)
                case Seek():
                    t = float(msg.current_time)
                    if self.state != "idle" and abs(t - self._playhead()) > _SAME_PLACE:
                        self._seek(t)
                case Stop():
                    # A player the session holds paused plays again, unless the owner had paused it.
                    held = self.state in ("preparing", "holding") and not self._owner_paused
                    self._finish(None, resume=held)
                case Hello():
                    self._send_status()
                case _:
                    log.debug("ignoring a %s message", msg.type)

    def produce_next(self) -> bool:
        """Produce the first unproduced segment that ends after the playhead.

        Returns False when there is nothing to do: the session is idle, there are no
        segments, every segment from the playhead on is produced, or `lead_max`
        seconds are ready ahead. On a track without punctuation, when every sentence
        restored so far is produced, the next stretch is restored instead (see _grow),
        and that counts as something done. Segments the playhead went by unproduced while the
        video played are logged as dropped. An engine failure never raises: the
        segment is shown as a subtitle and logged as dropped.
        """
        with self._lock:
            job, records = self._next_job()
            pending = self._take_ahead(job)
            grow = job is None and self._wants_more()
        self._write(records)
        if job is None:
            if grow:
                self._grow()
            return grow
        seg, next_start, generation, context = job
        began = self._clock()
        translated = (pending.result() if pending is not None else None) or self._translate(seg, context)
        self._translate_ahead(seg, generation)
        made = self._speak(seg, translated)
        took = self._clock() - began
        with self._lock:
            if made.clip is not None:
                self._makes.append((took, seg.end - seg.start))
            records, shrink = self._deliver(seg, next_start, generation, made)
            if generation == self._generation:
                self._count_failure(made)
        if shrink is not None:
            # Too long even at the top speed: one shorter rendering, then it is placed as it is.
            max_words = max(1, math.floor(len(made.text.split()) * max(shrink, _SHRINK_FLOOR)))
            made = self._make_shorter(seg, context, made, max_words)
            with self._lock:
                records, _ = self._deliver(seg, next_start, generation, made, shortened=True)
        self._write(records)
        return True

    def _count_failure(self, made: _Made) -> None:
        """Note whether the engines made `made`'s clip; after failures in a row, begin or go on waiting."""
        if made.clip is not None:
            if self._down:
                log.info("the engines answer again")
            self._failures, self._retry_at, self._down = 0, -math.inf, ""
            return
        self._failures += 1
        if self._failures < _FAILURES_BEFORE_WAIT:
            return
        if not self._down:
            log.warning("%d sentences in a row failed: trying again every %g s", self._failures, _FAILURE_WAIT)
        self._retry_at = self._clock() + _FAILURE_WAIT
        self._down = f"an engine is down, trying again every {_FAILURE_WAIT:g} s: {made.error}"
        self._send_status()

    def tick(self) -> None:
        """Do for the hold what a state report does, without one.

        A hold ends when enough is ready or its cap has passed, and one begins when the
        playhead runs into a sentence that is not ready. Both were decided only when the
        player reported, and its reports can be far apart: Chrome lets a hidden tab report
        once a second, and a hidden, silent one once a minute. The producer thread calls
        this between sentences. Nothing is done before the first report after a Start:
        until then where the player is is not known.
        """
        with self._lock:
            if not self._anchored:
                return
            now = self._clock()
            if self.state in ("preparing", "holding"):
                self._try_release(now)
            elif self.state == "playing" and self._running:
                self._keep_up(now)

    def run_producer(self, stop_event: threading.Event) -> None:
        """Produce until `stop_event` is set, waiting 50 ms whenever there is nothing to do."""
        while not stop_event.is_set():
            try:
                busy = self.produce_next()
                self.tick()
            except Exception:
                log.exception("producing a segment failed")
                busy = False
            if not busy:
                stop_event.wait(_IDLE_WAIT)

    # Messages. Everything below runs under the lock unless it says otherwise.

    def _on_start(self, mode: str | None) -> None:
        if mode == "live":
            self._finish("live mode is not available yet", resume=True)
            return
        slow_rate = self._use_track([])
        self._video_id = self._voice_video = None
        self.session_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{next(_run_numbers)}"
        self._started_at = self._hold_since = self._clock()
        self._makes.clear()
        log.info("session %s started", self.session_id)
        # The player is about to be paused: the next report sets the clock afresh.
        self._audio.set_clock(self._playhead(), False, self._rate, self._started_at)
        self._running = self._anchored = self._owner_paused = False
        self.state = "preparing"
        self._send(Duck(self._cfg.duck.volume))
        self._send(Control("pause", None))
        self._send_status()
        if slow_rate is not None:
            self._send(Control("rate", slow_rate))

    def _on_captions(self, msg: Captions) -> None:
        if self.state == "idle":
            self._send_status("captions ignored while idle")
            return
        try:
            cues = parse_json3(msg.json3)
            segments = segment(cues, **asdict(self._cfg.segmenter))
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            log.warning("malformed captions for %s: %s", msg.video_id, _describe(exc))
            cues, segments = [], []
        if not segments:
            self._finish("no usable captions", resume=True)
            return
        open_track = None
        if looks_unpunctuated(segments):
            if self._cfg.translator.restore_punctuation and callable(getattr(self._translator, "punctuate", None)):
                # Nothing in such a track says where a sentence ends, so nothing is cut yet:
                # the translator's model restores the punctuation, a stretch at a time.
                open_track = OpenTrack(track_words(cues))
            else:
                log.warning(
                    "the captions of %s have almost no sentence punctuation: they are cut at pauses "
                    "and every %d words, so sentences may be split",
                    msg.video_id,
                    self._cfg.segmenter.max_words,
                )
        self._text_ready_at = self._clock() - self._started_at
        self._video_id = msg.video_id
        self._prepare_voice(msg.video_id)
        if open_track is None:
            log.info("%s: %d sentences from the %s %s captions", msg.video_id, len(segments), msg.kind, msg.lang)
            slow_rate = self._use_track(segments)
        else:
            log.info(
                "%s: the %s %s captions have no sentence punctuation (%d words): "
                "the translator restores it a stretch at a time, ahead of the video",
                msg.video_id, msg.kind, msg.lang, len(open_track),
            )
            slow_rate = self._use_track([], open_track)
        if slow_rate is not None:
            self._send(Control("rate", slow_rate))

    def _on_state(self, msg: State) -> None:
        if self.state == "idle":
            return
        if self._video_id is not None and msg.video_id != self._video_id:
            self._finish("video changed", resume=False)
            return
        self._prepare_voice(msg.video_id)
        now = self._clock()
        current, rate = float(msg.current_time), float(msg.rate)
        # A jump, whether the video plays or is paused; the first report after Start sets the clock.
        if self._anchored and abs(current - self._audio.media_now(now)) > _JUMP:
            self._seek(current)
        self._audio.set_clock(current, not msg.paused, rate, now)
        self._running, self._rate, self._anchored = not msg.paused, rate, True
        self._follow_rate(rate)
        # While preparing or holding, the session paused the player itself, so a
        # paused report is not the owner pausing; a playing one is the owner playing.
        if self.state in ("preparing", "holding"):
            if not msg.paused:
                self._owner_paused = False
            if self.state == "preparing":
                # The first report can come after the captions, and only it says where the
                # player really is, so the skip of a partly passed sentence is made again,
                # and an open track that was begun somewhere else is begun again here.
                if self._outside(current):
                    self._reopen(current)
                self._skip_to(current)
            self._try_release(now)
        elif self.state == "playing" and msg.paused:
            self._set_state("paused")
        elif self.state == "paused" and not msg.paused:
            self._set_state("playing")
            self._keep_up(now)
        elif self.state == "playing":
            self._keep_up(now)

    def _prepare_voice(self, video_id: str) -> None:
        """Tell a voice that goes by the video which one is on, once for each video of a run.

        The first report after a Start names the video before the captions do, and such a
        voice wants every moment of the start-up for its look. `prepare` returns at once; one
        that fails is logged, and the voice then speaks as it does without a video.
        """
        if video_id == self._voice_video:
            return
        self._voice_video = video_id
        prepare = getattr(self._voice, "prepare", None)
        if not callable(prepare):
            return
        try:
            prepare(video_id)
        except Exception:
            log.exception("the voice could not prepare for %s", video_id)

    def _seek(self, t: float) -> None:
        """Re-anchor at `t`; hold and produce from there unless the segment at `t` is produced."""
        now = self._clock()
        self._audio.set_clock(t, self._running, self._rate, now)
        self._anchored = True
        slow_rate = self._reset_timeline()
        if slow_rate is not None:
            self._send(Control("rate", slow_rate))
        if self._outside(t):
            log.info("jump to %.1f s: the punctuation is not restored there, the track is begun again from there", t)
            self._reopen(t)
            self._hold(now)
            return
        # What the seek jumps over was skipped, not missed.
        self._skip_to(t)
        if self._coverage.produced_at(t):
            hole = self._coverage.first_unproduced(t)
            if hole is not None and self._coverage.produced_after(hole):
                # A sentence ahead is not made (the catch-up gave it up, or the playhead
                # passed it) and later ones are. It is wanted again now, but its clip would
                # be placed after the later clips, which are on the timeline already, or
                # under one of them. So everything from it on is made again, in order.
                start = self._coverage.segments[hole].start
                self._audio.cancel_after(start)
                self._coverage.unmark_from(start)
                self._generation += 1
                log.info("jump to %.1f s: the dub is ready there, and is made again from %.1f s, where a sentence is missing", t, start)
            else:
                log.info("jump to %.1f s: the dub is ready there", t)
            if self.state == "holding":
                self._release()
            return
        # Nothing produced at t (also when no segment is left there): start over from t.
        log.info("jump to %.1f s: nothing is ready there, holding", t)
        self._audio.cancel_after(t)
        self._coverage.unmark_from(t)
        self._generation += 1
        self._hold(now)

    def _finish(self, message: str | None, resume: bool) -> None:
        """End the run, give back the player's rate and volume, and play it if `resume`."""
        if self.state != "idle":
            log.info("session %s ended%s", self.session_id, f": {message}" if message else "")
        slow_rate = self._use_track([])
        rate = self._clamped_from if self._clamped_from is not None else slow_rate
        self._clamped_from = self._pushed_back = None
        self._video_id = None
        self._owner_paused = False
        self.state = "idle"
        if rate is not None:
            self._send(Control("rate", rate))
        self._send(Duck(None))
        if resume:
            self._send(Control("play", None))
        self._send_status(message)
        # Last, so that an output stream failing to stop cannot keep the player held.
        self._audio.stop()

    def _use_track(self, segments: list[Segment], open_track: OpenTrack | None = None) -> float | None:
        """Start over on `segments`; return the rate to give back if the player was slowed.

        With an `open_track` there are no segments yet: they come a stretch at a time, from
        where the video is (see _grow).
        """
        self._generation += 1
        self._track += 1
        self._failures, self._retry_at, self._down = 0, -math.inf, ""
        self._audio.flush()
        self._open, self._open_failures = open_track, 0
        if open_track is None:
            self._open_next = None
            self._coverage = Coverage(segments)
        else:
            self._open_first = self._open_next = open_track.first_word(self._playhead())
            self._coverage = Coverage(open=True)
        self._skip_to(self._playhead())
        self._hold_exceeded = False
        return self._reset_timeline()

    def _skip_to(self, t: float) -> None:
        """Account for what lies behind `t` without reporting it (see Coverage.skip_to).

        That also skips the sentence the playhead is partly past. On an open track that
        sentence may not be restored yet; then the skip is made again when it comes.
        """
        coverage = self._coverage
        coverage.skip_to(t)
        self._skip_pending = coverage.open and coverage.index_at(t) == len(coverage.segments)

    def _outside(self, t: float) -> bool:
        """Whether `t` lies outside the part of an open track that is restored or about to be.

        That part runs from the word the track was begun at to the first word that is not
        restored yet, which the next stretch begins with, and never ends before the place
        the track was begun for, the lead-in past its first word. A first stretch can end
        behind that place (the sentence there is long, and no end after it can be trusted
        yet): the playhead is then past what is restored, and still where the track is
        being restored for. False for a track that came with punctuation.
        """
        if self._open is None:
            return False
        word = self._open.word_at(t)
        if word < self._open_first:
            return True
        if self._open_next is None:
            return False  # restored to its end
        return word > max(self._open_next, self._open_first + LEFT_CONTEXT)

    def _reopen(self, t: float) -> None:
        """Begin the open track again for a playhead at `t`.

        What was restored and made for another place is let go, and nothing is kept of it:
        going back there restores it again. A stretch being restored meanwhile is dropped
        when it comes (see _grow).
        """
        assert self._open is not None
        self._generation += 1
        self._track += 1
        self._audio.flush()
        self._open_first = self._open_next = self._open.first_word(t)
        self._open_failures = 0
        self._coverage = Coverage(open=True)
        self._skip_pending = True
        self._hold_exceeded = False

    def _reset_timeline(self) -> float | None:
        """Forget the placed clips; return the rate to give back if the player was slowed."""
        self._timeline.reset()
        rate, self._slow_from = self._slow_from, None
        return rate

    # Player state.

    def _follow_rate(self, rate: float) -> None:
        """Push a rate above the clamp back to it, once for each offending rate."""
        limit = self._cfg.scheduler.rate_clamp
        if rate > limit:
            if rate != self._pushed_back:
                self._pushed_back = self._clamped_from = rate
                self._send(Control("rate", clamp_rate(rate, self._cfg.scheduler)))
                self._send_status(f"playback rate held at {limit:g}x while dubbing")
            return
        self._pushed_back = None
        slowed = self._slow_from is not None and rate == _SLOW_RATE
        if rate != limit and not slowed:
            self._clamped_from = None  # a rate the owner chose: stop leaves it

    def _try_release(self, now: float) -> None:
        """Play once enough is ready, or once the hold has lasted longer than its cap."""
        if self._ready(self._playhead()):
            self._release()
        elif self._gate.hold_expired(self._hold_since, now):
            if not self._coverage.segments and not self._coverage.open:
                # The captions never came, so there is nothing to dub: end the run as an
                # empty track does, which gives the volume back and plays the video.
                self._finish("no captions arrived", resume=True)
                return
            # Phase 3 switches to live mode here; until then the video plays on.
            log.warning(
                "the hold at %.1f s passed its cap of %.0f s: the video plays on and the dub catches up",
                self._playhead(), self._cfg.scheduler.hold_cap,
            )
            self._release("hold exceeded")
            self._hold_exceeded = True

    def _ready(self, t: float) -> bool:
        """Enough is produced ahead of `t`, or everything that is left.

        The start-up rule applies to every hold: a hold begins only where nothing is
        produced, and the gate's rule for a started session would end it at once.
        """
        coverage = self._coverage
        if not coverage.segments:
            return False
        if coverage.all_produced_from(t):
            return True
        return self._gate.ready(coverage.covered_until(t), t, started=False)

    def _release(self, message: str | None = None) -> None:
        # Segments behind the playhead now were skipped while held, not dropped.
        t = self._playhead()
        self._coverage.passed(t)
        # A hold that began while the owner had paused ends paused: the owner plays.
        owner_paused, self._owner_paused = self._owner_paused, False
        log.info(
            "%s from %.1f s after %.1f s of %s",
            "ready" if owner_paused else "playing", t, self._clock() - self._hold_since, self.state,
        )
        if not owner_paused:
            self._send(Control("play", None))
        self._set_state("paused" if owner_paused else "playing", message)

    def _keep_up(self, now: float) -> None:
        """While playing: hold when the producer has fallen behind, report once a second."""
        t = self._playhead()
        if self._hold_exceeded:
            # The cap let the video play on unproduced segments. Nothing holds it again
            # until a clip is ready `lead_min` ahead (see _deliver): a producer that could
            # not keep up would only run into the cap once more.
            pass
        elif self._coverage.missing_at(t):
            log.info("holding at %.1f s: the sentence there is not ready", t)
            self._hold(now)
            return
        if now - self._last_status >= _STATUS_EVERY:
            self._send_status()

    def _hold(self, now: float) -> None:
        if self.state != "holding":  # a hold within a hold keeps what the first found
            self._owner_paused = self.state == "paused"
        if not self._owner_paused:
            # A video the owner has paused needs no pause, and must get none: the extension
            # takes a pause from here as the session holding the player, and gives the player
            # back by playing it when dubbing stops.
            self._send(Control("pause", None))
        self._hold_since = now
        self._hold_exceeded = False
        self._set_state("holding")

    def _set_state(self, state: str, message: str | None = None) -> None:
        self.state = state
        self._send_status(message)

    def _send_status(self, message: str | None = None) -> None:
        """Report the state; without a `message`, say when the look-ahead runs low."""
        t = self._playhead()
        ahead = self._coverage.covered_until(t) - t
        if message is None and self._down:
            message = self._down
        elif message is None:
            left = self._coverage.first_unproduced(t) is not None or self._coverage.open
            low = self.state == "playing" and left and ahead < self._cfg.scheduler.lead_min
            message = "look-ahead low" if low else ""
        self._send(Status("captions", self.state, ahead, self._timeline.shift, self._engine, message))
        self._last_status = self._clock()

    def _playhead(self) -> float:
        return float(self._audio.media_now(self._clock()))

    # Production.

    def _next_job(self) -> tuple[_Job | None, list[SegmentRecord]]:
        """Pick the segment to produce next.

        Returns the job, or None when there is nothing to do, and the records of the
        segments the playhead went by unproduced while the video played.
        """
        coverage, segments = self._coverage, self._coverage.segments
        if self.state == "idle" or not segments:
            return None, []
        t = self._playhead()
        gone = coverage.passed(t)
        playing = self.state in ("playing", "paused")
        dropped = [self._record(segments[k]) for k in gone] if playing else []
        if self._clock() < self._retry_at:
            return None, dropped  # an engine is down: the next try waits its turn
        k = coverage.first_unproduced(t)
        if k is None or not self._gate.should_produce(coverage.covered_until(t), t):
            return None, dropped
        if self._hold_exceeded and self.state == "playing":
            aimed = self._aim(k, t)
            jumped = [i for i in range(k, aimed) if not coverage.is_produced(i)]
            if jumped:
                # Their clips could not be ready in time: give them up, so that the next one is.
                coverage.give_up(jumped)
                dropped += [self._record(segments[i]) for i in jumped]
                log.info(
                    "catching up at %.1f s: segment %d at %.1f s is next, %d before it cannot be ready in time",
                    t, aimed, segments[aimed].start, len(jumped),
                )
                self._send_status(f"catching up: {len(jumped)} sentence{'s' if len(jumped) > 1 else ''} skipped")
            k = aimed
        if k + 1 < len(segments):
            after = segments[k + 1].start
        elif self._open is not None and self._open_next is not None:
            # The sentence after it is not restored yet, but its first word is in the track.
            after = self._open.starts_at(self._open_next)
        else:
            after = None
        return (segments[k], after, self._generation, self._context(k)), dropped

    def _wants_more(self) -> bool:
        """Whether the next stretch of an open track is to be restored now.

        It is when no sentence restored so far is left to make, the look-ahead is not full,
        and no engine is being waited for.
        """
        if self.state == "idle" or self._open is None or self._open_next is None:
            return False
        if self._clock() < self._retry_at:
            return False
        t = self._playhead()
        coverage = self._coverage
        return coverage.first_unproduced(t) is None and self._gate.should_produce(coverage.covered_until(t), t)

    def _grow(self) -> None:
        """Restore the next stretch of the open track and append its sentences, outside the lock. It never raises.

        A stretch the model cannot restore is appended as it is, cut at pauses and by
        length, so the dub goes on; after `_OPEN_FAILURES` of them in a row the rest of the
        track is, and the model is not asked again. A stretch that comes back after the
        track was begun again or replaced (a jump, new captions, a stop) is dropped.
        """
        with self._lock:
            track, words, i = self._track, self._open, self._open_next
            first = i == self._open_first
        if words is None or i is None:
            return
        began = time.perf_counter()
        failure: Exception | None = None
        try:
            with self._translating:
                restored, after = restore_stretch(words, i, first, self._translator.punctuate)
        except Exception as exc:  # no engine failure may stop the producer
            failure = exc
            restored = words.stretch(i, first=False)
            after = i + len(restored)
        with self._lock:
            if track != self._track:
                return
            if failure is None:
                self._open_failures = 0
            else:
                self._open_failures += 1
                rest = self._open_failures >= _OPEN_FAILURES
                if rest:
                    restored, after = words.words[i:], len(words)
                log.warning(
                    "the punctuation from %.1f s on could not be restored (%s): %s cut at pauses and every %d words",
                    words.starts_at(i),
                    _describe(failure),
                    "the rest of the track is" if rest else f"the next {len(restored)} words are",
                    self._cfg.segmenter.max_words,
                )
            coverage = self._coverage
            segments = segment_words(
                restored, first_idx=len(coverage.segments), punctuated=failure is None, **asdict(self._cfg.segmenter)
            )
            done = after >= len(words)
            coverage.extend(segments, open=not done)
            self._open_next = None if done else after
            t = self._playhead()
            if self._skip_pending and coverage.index_at(t) < len(coverage.segments):
                # A jump or a start skips the sentence the playhead is partly past, and when
                # it was handled that sentence was not restored. Now it is.
                self._skip_to(t)
            if failure is None:
                log.info(
                    "punctuation restored from %.1f s to %.1f s: %d words, %d sentences, in %.1f s",
                    restored[0].start, restored[-1].end, len(restored), len(segments), time.perf_counter() - began,
                )

    def _aim(self, k: int, t: float) -> int:
        """The segment to make while the video plays on past a hold's cap, with the playhead at `t`.

        It is the first segment from `k` on, not yet produced, whose clip can be ready when
        the sentence begins, give or take `_LATE_START`: a clip is taken to need what the
        last one did, more for a longer sentence. A producer that fell behind would
        otherwise make the sentence at the playhead, find it over when the clip is ready,
        and do the same with the next one to the end of the video. Returns `k` before any
        make was timed, and when no segment begins late enough.
        """
        coverage, segments = self._coverage, self._coverage.segments
        i: int | None = k
        while i is not None:
            needs = self._estimate(segments[i])
            if needs is None or segments[i].start >= t + needs - _LATE_START:
                return i
            i = coverage.next_unproduced(i)
        return k

    def _estimate(self, seg: Segment) -> float | None:
        """How long making `seg` will take, from the last makes; None before one was timed.

        Each of the last three makes says what `seg` would take: its own time, more for a
        longer sentence. The estimate is the lesser of the last one and the middle one.
        That leaves out a single reply that hung (one make of 25 s among makes of 2 s), and
        still follows engines that have become slow, which two slow makes show, or fast
        again, which one fast make shows.
        """
        if not self._makes:
            return None
        seconds = seg.end - seg.start
        scaled = [took * max(1.0, seconds / max(of, _SHORTEST)) for took, of in self._makes]
        return _AIM_MARGIN * min(scaled[-1], statistics.median(scaled))

    def _take_ahead(self, job: _Job | None) -> Handle | None:
        """The translation made ahead, if it is for `job`; one made for anything else is dropped."""
        if job is None:
            return None  # nothing to do now: what was made ahead waits for its turn
        ahead, self._ahead = self._ahead, None
        if ahead is not None and ahead[0] == (self._track, job[0].idx):
            return ahead[1]
        return None

    def _translate_ahead(self, seg: Segment, generation: int) -> None:
        """Start translating the sentence that comes after `seg`, outside the lock.

        Nothing is started when the timeline has changed since `seg` was picked, when no
        sentence follows, or when the look-ahead is full once `seg` is produced.
        """
        with self._lock:
            following = self._following(seg, generation)
            track = self._track
        if following is None:
            return
        handle = self._spawn(functools.partial(self._translate, *following))
        with self._lock:
            self._ahead = ((track, following[0].idx), handle)

    def _following(self, seg: Segment, generation: int) -> tuple[Segment, tuple[str, ...]] | None:
        """The segment the producer takes after `seg`, with its context; None when it takes none."""
        coverage, segments = self._coverage, self._coverage.segments
        if generation != self._generation or self.state == "idle":
            return None
        t = self._playhead()
        # With `seg` produced the look-ahead reaches its end at least.
        if not self._gate.should_produce(max(seg.end, coverage.covered_until(t)), t):
            return None
        k = coverage.next_unproduced(seg.idx)
        if k is None:
            return None
        if self._hold_exceeded and self.state == "playing":
            # Catching up: the pick after `seg` aims from where the playhead will be by then.
            k = self._aim(k, t + (self._estimate(seg) or 0.0))
        return segments[k], self._context(k)

    def _context(self, k: int) -> tuple[str, ...]:
        """The texts of the segments before segment `k` that go with it as context."""
        n = self._cfg.translator.context_sentences
        segments = self._coverage.segments
        return tuple(seg.text for seg in segments[max(0, k - n) : k]) if n > 0 else ()

    def _translate(self, seg: Segment, context: tuple[str, ...]) -> _Translated:
        """Translate `seg` with its context and the glossary, outside the lock. It never raises."""
        with self._translating:
            began = time.perf_counter()
            try:
                text = self._translator.translate(seg.text, context=context, glossary=self._cfg.glossary or None)
            except Exception as exc:  # no engine failure may stop the producer
                log.warning("segment %d not translated", seg.idx, exc_info=True)
                return _Translated(seg.text, _ms_since(began), f"not translated ({_describe(exc)})")
            return _Translated(text, _ms_since(began))

    def _speak(self, seg: Segment, translated: _Translated) -> _Made:
        """Voice what `_translate` gave, outside the lock. An engine failure is an error."""
        text, mt_ms = translated.text, translated.mt_ms
        if translated.error:
            return _Made(text, None, mt_ms, 0.0, translated.error)
        began = time.perf_counter()
        try:
            clip = self._voice.synthesize(text, speed=1.0)
        except Exception as exc:  # no engine failure may stop the producer
            log.warning("segment %d not voiced", seg.idx, exc_info=True)
            return _Made(text, None, mt_ms, _ms_since(began), f"not voiced ({_describe(exc)})")
        tts_ms = _ms_since(began)
        if len(clip.samples) == 0:
            log.warning("segment %d not voiced: the voice gave an empty clip", seg.idx)
            return _Made(text, None, mt_ms, tts_ms, "not voiced (empty clip)")
        return _Made(text, clip, mt_ms, tts_ms)

    def _make_shorter(self, seg: Segment, context: tuple[str, ...], made: _Made, max_words: int) -> _Made:
        """Ask once for a shorter rendering of `made` and voice it, outside the lock.

        `made` stays, with the time spent added, when the translator fails or gives back an
        empty or the same text, when the voice fails, or when the new clip is not shorter.
        """
        assert made.clip is not None  # only a clip can be too long
        with self._translating:
            began = time.perf_counter()
            try:
                text = self._translator.shorten(
                    seg.text, made.text, max_words, context=context, glossary=self._cfg.glossary or None
                )
            except Exception:  # no engine failure may stop the producer
                log.warning("segment %d: no shorter rendering", seg.idx, exc_info=True)
                return replace(made, mt_ms=made.mt_ms + _ms_since(began))
            mt_ms = made.mt_ms + _ms_since(began)
        if not text.strip() or text.strip() == made.text.strip():
            return replace(made, mt_ms=mt_ms)
        began = time.perf_counter()
        try:
            clip = self._voice.synthesize(text, speed=1.0)
        except Exception:
            log.warning("segment %d: the shorter rendering was not voiced", seg.idx, exc_info=True)
            return replace(made, mt_ms=mt_ms, tts_ms=made.tts_ms + _ms_since(began))
        tts_ms = made.tts_ms + _ms_since(began)
        if len(clip.samples) == 0 or clip.seconds >= made.clip.seconds:
            return replace(made, mt_ms=mt_ms, tts_ms=tts_ms)
        return _Made(text, clip, mt_ms, tts_ms, shortened=True)

    def _deliver(
        self, seg: Segment, after: float | None, generation: int, made: _Made, shortened: bool = False
    ) -> tuple[list[SegmentRecord], float | None]:
        """Schedule what `_make` gave, unless the timeline has moved on meanwhile.

        Returns the records to log, and a shrink factor (see Placement) when the clip does
        not fit even at the top speed and no shorter rendering has been tried (`shortened`
        is False): then nothing is scheduled, and the caller asks for one and delivers again
        with `shortened=True`.
        """
        if generation != self._generation or self._coverage.is_produced(seg.idx):
            return [], None
        t = self._playhead()
        if seg.end <= t:
            # It went by while it was made. It is logged here, with what it cost, and not by
            # the next pick, which knows only that the playhead passed it.
            log.info(
                "segment %d at %.1f s: lost, its clip was ready %.1f s after the sentence ended; %s",
                seg.idx, seg.start, t - seg.end, _cost(made),
            )
            gone = self._coverage.passed(t)
            if self.state not in ("playing", "paused"):
                return [], None
            segments = self._coverage.segments
            return [self._record(segments[k], made if k == seg.idx else None) for k in gone], None
        if made.clip is None:
            self._coverage.mark(seg.idx, seg.start)
            self._send(Subtitle(made.text, seg.start, seg.end))
            self._send_status(f"segment {seg.idx} at {seg.start:.1f} s {made.error}")
            return [self._record(seg, made)], None
        seconds = float(made.clip.seconds)
        bound = t + _MARGIN if self._running else t  # the playhead: a clip never starts in the past
        # Nor under the clip before it: after a seek inside the produced range the timeline
        # starts anew and no longer knows that a clip a backlog pushed late is still playing.
        before = self._coverage.end_of_clip_before(seg.idx)
        not_before = bound if before is None else max(bound, before)
        placement = self._timeline.place(seg.start, after, seconds, shortened, not_before)
        if placement.action == "shorten":
            # A shorter rendering costs a second translation and a second clip. There is time
            # for that only when enough is ready ahead and the clip is not late already;
            # otherwise it plays as it is, at the top speed and past its slot.
            late = bound > seg.start
            thin = self._coverage.covered_until(t) - t < self._cfg.scheduler.lead_min
            if not (late or thin):
                return [], placement.shrink
            placement = self._timeline.place(seg.start, after, seconds, True, not_before)
        self._start_audio()
        self._audio.schedule(made.clip, placement.target_start, speed=placement.speed)
        end = placement.target_start + seconds / placement.speed
        self._coverage.mark(seg.idx, placement.target_start, end)
        self._send(Subtitle(made.text, placement.target_start, end))
        self._follow_slowing(placement)
        record = self._record(seg, made, placement, ahead=placement.target_start - t)
        if self._hold_exceeded and record.ahead >= self._cfg.scheduler.lead_min:
            self._hold_exceeded = False  # caught up: a clip is ready this far ahead again
            log.info("caught up at %.1f s: the dub is %.0f s ahead again", t, record.ahead)
        log.info(
            "segment %d at %.1f s: placed at %.1f s, %.1f s late, %.1f s ahead, speed %.2f%s%s; %s",
            seg.idx, seg.start, placement.target_start, record.lag, record.ahead, placement.speed,
            ", shortened" if made.shortened else "", ", past its slot" if placement.action == "overrun" else "",
            _cost(made),
        )
        # What was said, so that a complaint about the dub can be read from the log.
        log.info('segment %d: "%s" -> "%s"', seg.idx, seg.text, made.text)
        return [record], None

    def _follow_slowing(self, placement: Placement) -> None:
        """Slow the player while the dub runs far behind, if allowed; give its rate back after."""
        if not self._cfg.scheduler.slow_player:
            return
        if placement.action == "slow":
            if self._slow_from is None:
                self._slow_from = clamp_rate(self._rate, self._cfg.scheduler)
            self._send(Control("rate", _SLOW_RATE))
            self._send_status("slowing to catch up")
        elif self._slow_from is not None and not self._timeline.slowing:
            rate, self._slow_from = self._slow_from, None
            self._send(Control("rate", rate))

    def _start_audio(self) -> None:
        """Start the output stream before a clip is scheduled; it may be running already."""
        try:
            self._audio.start()
        except Exception as exc:
            if not self._audio_failed:
                self._audio_failed = True
                log.warning("audio output failed to start", exc_info=True)
                self._send_status(f"audio output failed: {_describe(exc)}")

    def _record(
        self,
        seg: Segment,
        made: _Made | None = None,
        placement: Placement | None = None,
        ahead: float | None = None,
    ) -> SegmentRecord:
        """The metrics of `seg`: dropped unless it has a placement."""
        start = seg.start if placement is None else placement.target_start
        return SegmentRecord(
            video_id=self._video_id or "",
            idx=seg.idx,
            src_start=seg.start,
            src_end=seg.end,
            text_ready_at=self._text_ready_at,
            mt_ms=made.mt_ms if made else 0.0,
            mt_engine=self._translator.name,
            tts_ms=made.tts_ms if made else 0.0,
            tts_engine=self._voice.name,
            audio_seconds=float(made.clip.seconds) if made and made.clip else 0.0,
            speed=placement.speed if placement else 1.0,
            scheduled_start=start,
            actual_start=None,
            lag=start - seg.start,
            dropped=placement is None,
            session=self.session_id,
            ahead=ahead,
            shortened=bool(made and made.shortened),
        )

    def _write(self, records: list[SegmentRecord]) -> None:
        """Log `records`, outside the lock; a file that cannot be written is reported once."""
        for rec in records:
            try:
                self._metrics.write(rec)
            except OSError as exc:
                if not self._metrics_failed:
                    self._metrics_failed = True
                    log.warning("cannot write metrics to %s: %s", self._metrics.path, exc)
