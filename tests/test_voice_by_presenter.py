# A session's voice when it goes by the presenter: which voice reads a video, how long the first
# sentence waits for the look, and that a video keeps its voice. The look is a function here.
import threading
import time

from dubber.engines.voice_by_presenter import PresenterVoice, PresenterVoices


class Named:
    """A voice that answers with its own name, the text and the speed, in place of a clip."""

    def __init__(self, name):
        self.name = name

    def synthesize(self, text, speed=1.0):
        return (self.name, text, speed)


MAN, WOMAN = Named("pc-voxcpm2:kalin"), Named("pc-voxcpm2:ipek")


class Looks:
    """`who`: answers each video from `answers`, after `hold` is set when one is given; counts the looks."""

    def __init__(self, hold=None, **answers):
        self.answers, self.hold, self.asked = answers, hold, []
        self.returned = threading.Event()

    def __call__(self, video_id):
        self.asked.append(video_id)
        if self.hold is not None:
            self.hold.wait(5)
        answer = self.answers.get(video_id)
        self.returned.set()
        if isinstance(answer, Exception):
            raise answer
        return answer


def voice(who, patience=5.0):
    return PresenterVoice(MAN, WOMAN, who, patience=patience)


def spoken_by(v, text="Merhaba."):
    return v.synthesize(text)[0]


def test_a_video_a_woman_presents_is_read_in_the_womans_voice():
    v = voice(Looks(her="woman"))
    v.prepare("her")
    assert v.synthesize("Merhaba.", speed=1.1) == ("pc-voxcpm2:ipek", "Merhaba.", 1.1)


def test_a_mans_video_and_one_that_does_not_say_are_read_in_the_mans_voice():
    v = voice(Looks(his="man", cartoon=None))
    v.prepare("his")
    assert spoken_by(v) == "pc-voxcpm2:kalin"
    v.prepare("cartoon")
    assert spoken_by(v) == "pc-voxcpm2:kalin"


def test_before_any_video_is_named_the_mans_voice_reads_and_nobody_looks():
    who = Looks(her="woman")
    assert spoken_by(voice(who)) == "pc-voxcpm2:kalin" and who.asked == []


def test_prepare_returns_while_the_look_is_still_being_made():
    hold = threading.Event()
    who = Looks(hold, her="woman")
    v = voice(who)
    began = time.perf_counter()
    v.prepare("her")
    assert time.perf_counter() - began < 1.0 and not who.returned.is_set()
    hold.set()
    assert spoken_by(v) == "pc-voxcpm2:ipek"


def test_the_first_sentence_waits_for_the_answer():
    hold = threading.Event()
    v = voice(Looks(hold, her="woman"))
    v.prepare("her")
    threading.Timer(0.05, hold.set).start()
    assert spoken_by(v) == "pc-voxcpm2:ipek"


def test_an_answer_that_comes_too_late_leaves_the_mans_voice_for_the_whole_video(caplog):
    hold = threading.Event()
    who = Looks(hold, her="woman")
    v = voice(who, patience=0.05)
    v.prepare("her")
    with caplog.at_level("INFO", logger="dubber.engines.voice_by_presenter"):
        assert spoken_by(v) == "pc-voxcpm2:kalin"
    assert "her" in caplog.text and "not known" in caplog.text
    hold.set()
    assert who.returned.wait(2)
    assert spoken_by(v) == "pc-voxcpm2:kalin"  # the video does not change its voice half way


def test_the_wait_is_counted_from_when_the_video_was_named():
    hold = threading.Event()
    v = voice(Looks(hold, her="woman"), patience=0.2)
    v.prepare("her")
    time.sleep(0.3)
    began = time.perf_counter()
    assert spoken_by(v) == "pc-voxcpm2:kalin"
    assert time.perf_counter() - began < 0.15
    hold.set()


def test_a_look_that_raises_is_the_mans_voice(caplog):
    v = voice(Looks(her=RuntimeError("the model fell over")))
    v.prepare("her")
    with caplog.at_level("ERROR", logger="dubber.engines.voice_by_presenter"):
        assert spoken_by(v) == "pc-voxcpm2:kalin"
    assert "the model fell over" in caplog.text


def looks_ended():
    """Wait until every look that was started has ended."""
    for thread in threading.enumerate():
        if thread.name == "dubber-presenter":
            thread.join(2)


# The session names a video again only when it is started again: that is a new run of it.

def test_a_video_named_again_while_it_is_still_looked_at_is_not_looked_at_twice():
    hold = threading.Event()
    who = Looks(hold, her="woman")
    v = voice(who)
    v.prepare("her")
    v.prepare("her")
    hold.set()
    assert spoken_by(v) == "pc-voxcpm2:ipek" and who.asked == ["her"]


def test_a_video_started_again_is_asked_about_again_and_an_answer_that_was_kept_gives_the_same_voice():
    who = Looks(her="woman")
    v = voice(who)
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:ipek"
    looks_ended()
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:ipek" and who.asked == ["her", "her"]


def test_a_look_that_failed_is_made_again_when_the_video_is_started_again():
    answers = iter([None, "woman"])  # the first time the PC's model could not be asked
    v = voice(lambda video_id: next(answers))
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:kalin"
    looks_ended()
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:ipek"


def test_an_answer_that_came_too_late_is_used_when_the_video_is_started_again():
    hold = threading.Event()
    answers = iter(["woman", "woman"])

    def who(video_id):
        hold.wait(5)
        return next(answers)

    v = voice(who, patience=0.05)
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:kalin"
    hold.set()
    looks_ended()
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:ipek"


def test_a_video_started_again_while_its_look_still_runs_waits_for_that_look_afresh():
    hold = threading.Event()
    who = Looks(hold, her="woman")
    v = voice(who, patience=0.2)
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:kalin"  # the first run gave up waiting
    v.prepare("her")
    threading.Timer(0.05, hold.set).start()
    assert spoken_by(v) == "pc-voxcpm2:ipek" and who.asked == ["her"]


def test_another_video_is_looked_at_anew():
    who = Looks(her="woman", his="man")
    v = voice(who)
    v.prepare("her")
    assert spoken_by(v) == "pc-voxcpm2:ipek"
    v.prepare("his")
    assert spoken_by(v) == "pc-voxcpm2:kalin" and who.asked == ["her", "his"]


def test_a_look_for_the_video_before_cannot_pick_the_voice_of_the_one_that_is_on():
    hold = threading.Event()
    slow = Looks(hold, her="woman")
    quick = Looks(his="man")
    v = PresenterVoice(MAN, WOMAN, lambda video_id: (slow if video_id == "her" else quick)(video_id))
    v.prepare("her")
    v.prepare("his")
    hold.set()
    assert slow.returned.wait(2)
    assert spoken_by(v) == "pc-voxcpm2:kalin"


def test_a_sentence_that_was_waiting_when_another_video_came_is_read_in_the_mans_voice():
    hold = threading.Event()
    who = Looks(hold, her="woman", other="woman")
    v = voice(who)
    v.prepare("her")
    spoken = []
    waiting = threading.Thread(target=lambda: spoken.append(spoken_by(v)))
    waiting.start()
    time.sleep(0.05)
    v.prepare("other")
    hold.set()
    waiting.join(2)
    assert spoken == ["pc-voxcpm2:kalin"]
    assert spoken_by(v) == "pc-voxcpm2:ipek"  # and the new video gets its own answer


def test_the_name_is_that_of_the_voice_in_use():
    v = voice(Looks(her="woman"))
    assert v.name == "pc-voxcpm2:kalin"
    v.prepare("her")
    spoken_by(v)
    assert v.name == "pc-voxcpm2:ipek"


def test_each_session_gets_a_voice_of_its_own_and_they_share_the_looks():
    who = Looks(her="woman", his="man")
    voices = PresenterVoices(MAN, WOMAN, who)
    one, two = voices.for_session(), voices.for_session()
    one.prepare("her")
    two.prepare("his")
    assert (spoken_by(one), spoken_by(two)) == ("pc-voxcpm2:ipek", "pc-voxcpm2:kalin")
    assert voices.male is MAN and voices.female is WOMAN
    assert voices.name == "pc-voxcpm2:kalin or pc-voxcpm2:ipek, by who presents the video"
