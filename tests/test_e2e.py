# End to end, without Chrome and without sound: the real server, session, scheduler and mixer,
# and the real `say` with the voice Samantha, driven by the fake extension (tools/fake_extension.py)
# over a short track of five sentences. The mixer's output stream is a stand-in that opens no
# audio device; a task pulls the blocks a sound device would, so the test "hears" when Samantha
# speaks. Slow: it plays about 12 s of video in real time, so the default run skips it.
#
#     uv run pytest -m slow tests/test_e2e.py -v
import asyncio

import numpy as np
import pytest

from dubber.config import Config
from dubber.engines.base import IdentityTranslator, VoiceError
from dubber.engines.voice_say import SayVoice
from dubber.metrics import MetricsLog, stats
from dubber.playback import AudioOut
from dubber.protocol import Duck
from dubber.server import serve
from dubber.session import Session
from tools.fake_extension import run

pytestmark = pytest.mark.slow

SENTENCES = [
    "Nobody could explain how the navigators worked.",
    "Every guild kept its own secrets safe.",
    "The spice made travel between the stars possible.",
    "Without it the empire would slowly fall apart.",
    "The first ships were built long before that.",
]
FIRST_START, SPACING, WORD_SECONDS = 1.0, 4.0, 0.3
MIXER_RATE = 48000  # the default of AudioOut
BLOCK = 1024  # frames in a block a sound device asks for
AUDIBLE = 0.01  # a block whose loudest sample is above this is not silence


def track():
    """A json3 track like YouTube's auto-captions: a sentence every 4 s, a word every 0.3 s.

    That is five sentences in 20 s. Each is one event with a seg for each word. A seg
    carries its offset into the event, and every word after the first a space before it.
    """
    events = []
    for i, sentence in enumerate(SENTENCES):
        words = sentence.split()
        events.append({
            "tStartMs": round((FIRST_START + i * SPACING) * 1000),
            "dDurationMs": round(len(words) * WORD_SECONDS * 1000),
            "segs": [
                {"utf8": (" " if k else "") + word, "tOffsetMs": round(k * WORD_SECONDS * 1000)}
                for k, word in enumerate(words)
            ],
        })
    return {"events": events}


class CapturingStream:
    """An output stream that opens no device. It keeps the callback it was given, which is
    what a sound device would call for each block, and what was done to the stream."""

    def __init__(self, callback):
        self.callback = callback
        self.started = self.stopped = self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


async def pull_blocks(audios, streams, heard):
    """Be the sound device: once the mixer's stream is running, ask it for a block in real time.

    `heard` collects (the video time of the block, its loudest sample), the way a person
    would note when the dub speaks.
    """
    block = np.zeros((BLOCK, 2), dtype=np.float32)
    while True:
        if streams:
            streams[0].callback(block, BLOCK, None, None)
            heard.append((audios[0].media_now(), float(np.abs(block).max())))
        await asyncio.sleep(BLOCK / MIXER_RATE)


@pytest.fixture
def samantha():
    try:
        installed = "Samantha" in SayVoice.available_voices()
    except VoiceError:
        installed = False
    if not installed:
        pytest.skip("the say voice Samantha is not installed (System Settings, Accessibility, Spoken Content)")
    return SayVoice("Samantha")


async def test_the_stand_in_dub_runs_end_to_end_in_sync_and_stop_gives_the_volume_back(tmp_path, samantha):
    cfg = Config()
    cfg.server.port = 0
    cfg.scheduler.startup_ready = 3.0  # the default 12 s would only lengthen the test
    cfg.metrics_path = str(tmp_path / "metrics.jsonl")
    metrics = MetricsLog(cfg.metrics_path)
    audios, streams = [], []

    def capture(callback):
        streams.append(CapturingStream(callback))
        return streams[-1]

    def make_session(cfg, send):
        audios.append(AudioOut(sample_rate=MIXER_RATE, stream_factory=capture))
        return Session(IdentityTranslator(), samantha, audios[-1], metrics, cfg, send)

    heard = []
    server = await serve(cfg, make_session)
    speaker = asyncio.create_task(pull_blocks(audios, streams, heard))
    try:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        result = await asyncio.wait_for(
            run(url, track(), video_id="e2e", start=0.0, seconds=12.0, out=print), timeout=90
        )
    finally:
        speaker.cancel()
        await asyncio.wait([speaker])
        server.close()
        await server.wait_closed()  # also ends the producer thread, so every metrics line is written

    # The session went from preparing to playing, held the player for a short time, and stop
    # brought it back to idle.
    assert result.stopped and not result.closed
    assert "preparing" in result.states and "playing" in result.states
    assert result.states.index("preparing") < result.states.index("playing") and result.states[-1] == "idle"
    assert result.held is not None and result.held < 15
    assert result.played >= 12.0
    assert len(result.subtitles) >= 3

    # The volume was ducked at the start, and stop gave it back.
    assert result.ducks[0] == Duck(cfg.duck.volume) and result.ducks[-1] == Duck(volume=None)

    # In sync: each clip was scheduled at its sentence's own start and none was dropped.
    summary = stats(cfg.metrics_path)
    assert summary["count"] >= 3
    assert summary["dropped"] == 0
    assert summary["lag_p50"] <= 0.3

    # And heard in sync: nothing before the first sentence, and Samantha within a second of
    # the start of each sentence that played (the first three start at 1, 5 and 9 s).
    (stream,) = streams
    assert stream.started and stream.stopped and stream.closed
    loud = [time for time, peak in heard if peak > AUDIBLE]
    assert not [time for time, peak in heard if time < FIRST_START - 0.05 and peak > 0.0]
    for i in range(3):
        start = FIRST_START + i * SPACING
        assert any(start <= time <= start + 1.0 for time in loud), f"sentence {i} was not heard at {start} s"
    assert loud[0] <= FIRST_START + 0.5  # the first sentence starts within half a second of its time
