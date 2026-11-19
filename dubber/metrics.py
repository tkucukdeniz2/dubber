"""Per-segment metrics: one JSON line for each dubbed segment, and a session's summary.

The session hands a `SegmentRecord` to a `MetricsLog` for every segment it produces.
The file keeps growing across sessions, so each record carries the id of its session
and `stats` summarizes one of them: how far the dub lagged behind the video (`lag`) and
how far ahead of the playhead it was produced (`ahead`). `format_stats` turns that
summary into the lines `dubber stats` prints.
"""

import json
import math
import pathlib
import threading
from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class SegmentRecord:
    """What the session logs about one segment; one record is one line of the file.

    Times are seconds of video (media) time unless a field says otherwise.

    - `video_id` is the YouTube id of the video.
    - `idx` is the segment's index in its caption track, counted from 0.
    - `src_start` is the media time at which the segment's speech starts in the video.
    - `src_end` is the media time at which it ends.
    - `text_ready_at` is the seconds from the session's Start to the arrival of the
      captions the segment was cut from.
    - `mt_ms` is the wall time, in milliseconds, of the call that translated it, and of
      the call that shortened it, if one was made.
    - `mt_engine` is the name of the translator.
    - `tts_ms` is the wall time, in milliseconds, of the synthesize calls.
    - `tts_engine` is the name of the voice.
    - `audio_seconds` is the clip's length in seconds at speed 1.0, 0.0 without a clip.
    - `speed` is how many times faster than recorded the clip plays.
    - `scheduled_start` is the media time at which the clip is scheduled to start; for
      a dropped segment it is `src_start`.
    - `actual_start` is the media time at which the clip was heard to start, None when
      not measured (the mixer plays a clip at its scheduled time by construction).
    - `lag` is `scheduled_start - src_start`: how far the dub lags behind the video.
    - `dropped` is True for a segment never heard: the playhead passed it before it was
      produced, or an engine failed and only its subtitle was shown.
    - `session` is the id of the dubbing session, one run from Start to Stop.
    - `ahead` is `scheduled_start` minus the playhead's media time at the moment the
      clip was scheduled, None when not recorded.
    - `shortened` is True when the clip that plays is the translator's second, shorter
      rendering; lines written before it existed read as False.
    """

    video_id: str
    idx: int
    src_start: float
    src_end: float
    text_ready_at: float
    mt_ms: float
    mt_engine: str
    tts_ms: float
    tts_engine: str
    audio_seconds: float
    speed: float
    scheduled_start: float
    actual_start: float | None
    lag: float
    dropped: bool
    session: str = ""
    ahead: float | None = None
    shortened: bool = False


class MetricsLog:
    """Appends records, one JSON line each, to a file that every session shares.

    Lines written earlier are kept. `write` can be called from any thread.
    """

    def __init__(self, path: pathlib.Path | str) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()

    def write(self, rec: SegmentRecord) -> None:
        """Append `rec` as one line, creating the file's directory when it is missing.

        The file is opened and closed again for each record, so the line is flushed
        before `write` returns and a crash keeps every record written so far.
        """
        line = json.dumps(asdict(rec), ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)


def _read(path: pathlib.Path | str) -> list[SegmentRecord]:
    """Every record in the file, in order. Blank lines are skipped."""
    records = []
    with open(path, encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                records.append(SegmentRecord(**json.loads(line)))
            except (ValueError, TypeError) as e:
                # Not JSON, not an object, or not the fields of a record.
                why = e.msg if isinstance(e, json.JSONDecodeError) else e
                raise ValueError(
                    f"{path}: line {number} is not a metrics record ({why})"
                ) from e
    return records


def _percentile(values: list[float], q: float) -> float | None:
    """The `q`-th percentile as a float: the observed value nearest to it."""
    if not values:
        return None
    return float(np.percentile(values, q, method="nearest"))


def _least_ahead_by_minute(records: list[SegmentRecord]) -> list[tuple[int, float]]:
    """`(minute, least ahead)` for each minute of video time that has an ahead value."""
    least: dict[int, float] = {}
    for r in records:
        if r.ahead is not None:
            minute = int(r.src_start // 60)
            least[minute] = min(least.get(minute, math.inf), r.ahead)
    return [(minute, float(ahead)) for minute, ahead in sorted(least.items())]


def stats(path: pathlib.Path | str, session: str | None = None) -> dict:
    """Summarize one session of the metrics file at `path`.

    The session is `session`, or by default the one on the last line of the file:
    records are appended in time order, and records written without a session id share
    the id "". Blank lines are skipped. A line that is not a record raises ValueError
    naming its number in the file, counted from 1, and a missing file raises
    FileNotFoundError.

    The result holds plain Python numbers. `session` is the id summarized (None for a
    file without records), `count` the records of that session and `dropped` how many
    of them were dropped. Every other figure is computed over the records that were not
    dropped, since a dropped segment was never heard, and is None when there is no value
    to compute it from. `lag_p50` and `lag_p90` are in seconds, `mt_ms_p50` and
    `tts_ms_p50` in milliseconds, `ahead_min` and `ahead_p50` in seconds. A percentile
    is a value that occurs (numpy's "nearest" method), not an interpolation.
    `ahead_by_minute` lists `(minute, least)` for each whole minute of video time,
    `int(src_start // 60)`, that has records with an `ahead` value; `least` is the
    smallest of them. The list is sorted by minute, and empty when no record has an
    `ahead` value.
    """
    records = _read(path)
    if session is None and records:
        session = records[-1].session
    in_session = [r for r in records if r.session == session]
    heard = [r for r in in_session if not r.dropped]
    aheads = [r.ahead for r in heard if r.ahead is not None]
    return {
        "session": session,
        "count": len(in_session),
        "dropped": len(in_session) - len(heard),
        "lag_p50": _percentile([r.lag for r in heard], 50),
        "lag_p90": _percentile([r.lag for r in heard], 90),
        "mt_ms_p50": _percentile([r.mt_ms for r in heard], 50),
        "tts_ms_p50": _percentile([r.tts_ms for r in heard], 50),
        "ahead_min": float(min(aheads)) if aheads else None,
        "ahead_p50": _percentile(aheads, 50),
        "ahead_by_minute": _least_ahead_by_minute(heard),
    }


def _figure(value: float | None, spec: str, unit: str = "") -> str:
    """`value` in the format `spec` and followed by `unit`; n/a when there is none."""
    if value is None:
        return "n/a"
    return f"{value:{spec}} {unit}".rstrip()


def format_stats(d: dict) -> str:
    """The lines `dubber stats` prints for a `stats` dict, with units.

    Lag and ahead are in seconds, the translation and voice times in milliseconds, and
    the ahead curve has a line `m:ss  <seconds> s` for each minute of video time. A
    value that is missing or None prints as n/a.
    """
    rows = [
        ("session", d.get("session") or "n/a"),
        ("segments", _figure(d.get("count"), "d")),
        ("dropped", _figure(d.get("dropped"), "d")),
        ("lag p50", _figure(d.get("lag_p50"), ".2f", "s")),
        ("lag p90", _figure(d.get("lag_p90"), ".2f", "s")),
        ("translation p50", _figure(d.get("mt_ms_p50"), ".0f", "ms")),
        ("voice p50", _figure(d.get("tts_ms_p50"), ".0f", "ms")),
        ("ahead min", _figure(d.get("ahead_min"), ".1f", "s")),
        ("ahead p50", _figure(d.get("ahead_p50"), ".1f", "s")),
    ]
    width = max(len(label) for label, _ in rows)
    lines = [f"{label.ljust(width)}  {value}" for label, value in rows]
    lines.append("ahead by minute of video (least seconds ahead of the playhead)")
    # Each entry stands for a whole minute, so its time is m:00.
    entries = d.get("ahead_by_minute") or []
    curve = [f"{int(minute)}:00  {ahead:.1f} s" for minute, ahead in entries]
    return "\n".join(lines + (curve or ["n/a"]))
