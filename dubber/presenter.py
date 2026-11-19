"""Who presents a video, a man or a woman, so that the dub can be read in a fitting voice.

YouTube shows a little of every video to anyone, without playing it and without a cookie: its
title and channel (oEmbed), its thumbnail, and three frames taken from a quarter, the middle
and three quarters of it. The PC's model is asked one question about those. It sees coarsely
(a picture is some eighty patches to it), so the title and the channel go into the question:
a name there says what the pictures often cannot. On 46 public videos, 21 presented by a man,
20 by a woman and 5 by nobody, this found every man and woman and called none the wrong sex;
the pictures alone called three men's videos a woman's, and the thumbnail alone was useless
(see docs/decisions/2026-11-voice-by-presenter.md).

Nothing here may hold up or stop a dub: what cannot be fetched is left out, and `Presenters.who`
never raises.
"""

import concurrent.futures
import http.cookiejar
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from dubber.engines.base import failure_reason

log = logging.getLogger(__name__)

# The pictures asked for, in the order the model gets them: the thumbnail, then the frames.
PICTURES = ("hqdefault", "hq1", "hq2", "hq3")

# The measured wording; a change to it wants the test set run again.
QUESTION = (
    "These pictures are from one YouTube video: its thumbnail and frames taken from it. "
    "Decide who presents the video: the one real person who talks to the viewer. "
    "Answer with exactly one word. "
    "man: the presenter is a man. woman: the presenter is a woman. "
    "unclear: no real person presents it. Drawings, cartoons and animated characters are not presenters; "
    "neither are objects, text, screens, or scenes from a film or a game. "
    "Also unclear when the pictures show different people and none is clearly the presenter."
)
_NAMES = '\nThe video\'s title is "{title}" and its channel is "{channel}"; a person named there is likely the presenter.'

# A title or channel is cut to this many characters: it is YouTube's text, not ours, and long enough to name someone.
_NAME_CHARS = 200
# A thumbnail is some tens of kilobytes. A larger file is not one, and is not sent on to the PC.
_PICTURE_BYTES = 1_000_000
_JPEG = b"\xff\xd8"
# A YouTube video id. Anything else is not put into an address.
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


@dataclass(frozen=True)
class Glimpse:
    """What YouTube shows of a video without playing it; what could not be fetched is empty or left out."""

    title: str = ""
    channel: str = ""
    pictures: tuple[bytes, ...] = ()


def question(glimpse: Glimpse) -> str:
    """What the model is asked about `glimpse`'s pictures: QUESTION, and the title and channel when known."""
    if not (glimpse.title or glimpse.channel):
        return QUESTION
    return QUESTION + _NAMES.format(title=_one_line(glimpse.title), channel=_one_line(glimpse.channel))


def _one_line(text: str) -> str:
    """`text` on one line, without double quotes, cut to `_NAME_CHARS`: it goes between quotes in the question."""
    return " ".join(text.split()).replace('"', "'")[:_NAME_CHARS]


def read_answer(reply: str) -> str | None:
    """ "man" or "woman" when `reply` is that one word, in any case and with a full stop or quotes; else None."""
    word = reply.strip().strip(".!\"'").lower()
    return word if word in ("man", "woman") else None


class YouTube:
    """Fetches the glimpse of a video. Only the video's id is sent: no cookie, no key.

    Each request has `timeout` seconds for each of its steps, and they are made at the same time.
    A cookie YouTube sets is not kept, so nothing ties one look to the next.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None, timeout: float = 3.0) -> None:
        no_cookies = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self._client = httpx.Client(timeout=timeout, transport=transport, cookies=no_cookies)

    def glimpse(self, video_id: str) -> Glimpse:
        """The title, the channel and the pictures of `video_id`; never raises."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1 + len(PICTURES), thread_name_prefix="dubber-glimpse") as pool:
            about = pool.submit(self._about, video_id)
            pictures = [pool.submit(self._picture, video_id, name) for name in PICTURES]
        title, channel = about.result()
        return Glimpse(title, channel, tuple(p for p in (f.result() for f in pictures) if p is not None))

    def _about(self, video_id: str) -> tuple[str, str]:
        """The title and the channel from oEmbed; two empty strings when it cannot be had."""
        try:
            response = self._client.get(
                "https://www.youtube.com/oembed",
                params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            )
            record = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return "", ""
        if not isinstance(record, dict):
            return "", ""
        title, channel = record.get("title"), record.get("author_name")
        return (title if isinstance(title, str) else ""), (channel if isinstance(channel, str) else "")

    def _picture(self, video_id: str, name: str) -> bytes | None:
        """One picture of the video as a JPEG file; None when it is missing or is not one."""
        try:
            response = self._client.get(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg")
        except httpx.HTTPError:
            return None
        body = response.content
        if response.status_code != 200 or not body.startswith(_JPEG) or len(body) > _PICTURE_BYTES:
            return None
        return body


class Presenters:
    """Who presents each video: "man", "woman", or None when the look does not say.

    `glimpse(video_id)` fetches what YouTube shows and `look(question, pictures)` asks the model.
    A video is looked at once while the server runs: its answer is kept, also one that does not
    say. A look that failed (no picture, a model that cannot see or cannot be reached) is not
    kept, so the next start of the video looks again. Nor is the answer of a look that had no
    title and channel, though it is used: the pictures alone called 3 of 26 men's videos a
    woman's, and the next look may have the names.
    """

    def __init__(self, glimpse: Callable[[str], Glimpse], look: Callable[[str, Sequence[bytes]], str]) -> None:
        self._glimpse = glimpse
        self._look = look
        self._lock = threading.Lock()
        self._known: dict[str, str | None] = {}

    def who(self, video_id: str) -> str | None:
        """ "man" or "woman" for the presenter of `video_id`, or None; it may take seconds and never raises."""
        if not _VIDEO_ID.fullmatch(video_id):
            return None
        with self._lock:
            if video_id in self._known:
                return self._known[video_id]
        began = time.perf_counter()
        try:
            seen = self._glimpse(video_id)
            if not seen.pictures:
                log.info("%s: YouTube gave no picture of it, so nobody looked who presents it", video_id)
                return None
            reply = self._look(question(seen), seen.pictures)
        except Exception as exc:  # the dub goes on in the usual voice, whatever broke
            log.warning("%s: could not look who presents it: %s", video_id, failure_reason(exc))
            return None
        answer = read_answer(reply)
        named = bool(seen.title or seen.channel)
        if named:
            with self._lock:
                self._known[video_id] = answer
        log.info(
            '%s ("%s" by %s): %s, the model says after %.1f s with %d pictures%s',
            video_id, _one_line(seen.title), _one_line(seen.channel) or "?",
            f"a {answer} presents it" if answer else f"it does not show who presents it ({reply.strip()[:40]!r})",
            time.perf_counter() - began, len(seen.pictures),
            "" if named else "; YouTube gave no title, so it is asked again at the next start",
        )
        return answer
