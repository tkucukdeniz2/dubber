"""A man's voice or a woman's, by who presents the video.

The session names the video to its voice (`prepare`), which starts looking who presents it,
on a thread of its own, and returns at once. The first sentence waits for the answer, though
not for ever: `patience` seconds after the video was named it is read in the man's voice. The
voice a video's first sentence was read in stays for that run of the video, so a late answer
never changes the voice half way. A woman is read in the woman's voice; a man, a video that
does not show who presents it, and every failure of the look are read in the man's.

The session names a video once for each run of it, so a video that is named again was started
again, and its voice is picked anew: a look that failed, or whose answer came too late, is not
what the video is left with. Stopping the dub and starting it again puts a wrong voice right
once the look can be made.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from dubber.engines.base import Clip, Voice

log = logging.getLogger(__name__)

# How long after the video was named the first sentence still waits for the look. The look
# takes about a second and a half; the start-up hold is longer than that anyway.
PATIENCE = 6.0


@dataclass(eq=False)
class _Look:
    """The look at one video: when the run that waits for it began, and its answer once `done` is set."""

    video_id: str
    began: float
    done: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


class PresenterVoice(Voice):
    """One session's voice: `male` or `female`, by what `who(video_id)` says of the video that is on.

    `who` gives "woman", "man" or None, and may take seconds. Before any video is named the
    man's voice reads. `name` is the name of the voice in use.
    """

    def __init__(
        self,
        male: Voice,
        female: Voice,
        who: Callable[[str], str | None],
        patience: float = PATIENCE,
    ) -> None:
        self._male, self._female, self._who = male, female, who
        self._patience = patience
        self._lock = threading.Lock()
        self._look: _Look | None = None  # the look at the video that is on
        self._voice: Voice | None = None  # what that video is read in, from its first sentence on

    @property
    def name(self) -> str:
        return (self._voice or self._male).name

    def prepare(self, video_id: str) -> None:
        """Begin a run of `video_id`: look who presents it; the run's first sentence picks the voice. Returns at once.

        A look at this video that is still being made is not made twice: the new run waits
        for it, with its own `patience`. One that has ended is made again, which costs nothing
        when its answer was kept (see dubber.presenter.Presenters) and is the second chance
        of one that failed.
        """
        now = time.monotonic()
        with self._lock:
            look = self._look
            self._voice = None
            if look is not None and look.video_id == video_id and not look.done.is_set():
                look.began = now
                return
            look = self._look = _Look(video_id, now)
        threading.Thread(target=self._find, args=(look,), name="dubber-presenter", daemon=True).start()

    def synthesize(self, text: str, speed: float = 1.0) -> Clip:
        return self._chosen().synthesize(text, speed)

    def _find(self, look: _Look) -> None:
        try:
            look.answer = self._who(look.video_id)
        except Exception:
            log.exception("looking who presents %s failed", look.video_id)
        finally:
            look.done.set()

    def _chosen(self) -> Voice:
        """The voice of the video that is on, picked now if this is its first sentence."""
        with self._lock:
            look, voice = self._look, self._voice
        if look is None:
            return self._male
        if voice is not None:
            return voice
        answered = look.done.wait(max(0.0, look.began + self._patience - time.monotonic()))
        with self._lock:
            if self._look is not look:
                return self._male  # another video came meanwhile: this sentence is no longer wanted
            if self._voice is None:
                woman = answered and look.answer == "woman"
                self._voice = self._female if woman else self._male
                if not answered:
                    why = f": who presents it was not known after {self._patience:g} s"
                elif look.answer is None:
                    why = ": nothing says who presents it"
                else:
                    why = f", a {look.answer} presents it"
                log.info("%s is read in the voice %s%s", look.video_id, self._voice.name, why)
            return self._voice


class PresenterVoices:
    """What the sessions of one server share when the voice goes by the presenter: the two voices and the looks."""

    def __init__(self, male: Voice, female: Voice, who: Callable[[str], str | None]) -> None:
        self.male, self.female, self.who = male, female, who
        self.name = f"{male.name} or {female.name}, by who presents the video"

    def for_session(self) -> PresenterVoice:
        return PresenterVoice(self.male, self.female, self.who)
