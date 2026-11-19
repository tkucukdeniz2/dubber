# A man's voice or a woman's, by who presents the video: what was decided and measured

2026-11-15, branch `auto-voice`. Design: `docs/superpowers/specs/2026-11-14-voice-by-presenter-design.md`.
Plan: `docs/superpowers/plans/2026-11-14-voice-by-presenter.md`.

## What it does

With `[voice] voice = "auto"` (`uv run dubber voice auto`) the PC's model is asked, once for each video,
who presents it. It is shown what YouTube shows to anyone without playing the video: title, channel,
thumbnail and three frames. A video that a woman presents is read in `ipek`, every other video in `kalin`
(`female` and `male` under `[voice]`). The first sentence waits for the answer, 6 s after the video was
named at most, and the video keeps that voice.

## Measured

**The look, on 46 public videos** (21 presented by a man, 20 by a woman, 5 animations with a narrator),
one question each to Gemma 4 12B on the RTX 4070, temperature 0:

| what the model is given | women found | men found | called the wrong sex | time |
|---|---|---|---|---|
| the thumbnail and three frames | 19 of 20 | 18 of 21 | 5 | 0.64 s |
| the same, a stricter question | 20 | 19 | 3, all called woman | 0.65 s |
| the same pictures at 640x480 | 16 | 21 | 5 | 0.84 s |
| the title and channel only | 19 | 21 | 1; "man" whenever it does not know | 0.28 s |
| thumbnail, three frames, title and channel (built) | 20 | 21 | 0 | 0.71 s, at most 0.95 s |

Of the 5 animations the built one called 3 "unclear" and 2 "man". The thumbnail alone is useless: the
model called 4 of 5 women men from it, and the owner's own videos show a coffee machine and a spaceship
there. One answer for each picture, to be counted, was worse than one answer for all four.

**Why the model sees so little.** Its vision add-on feeds 48-pixel patches straight into the model, a
480x360 picture is 80 of them, and `ubatch-size = 256` in the preset caps a picture at 256. Asked to
describe single pictures it is often wrong. The names in the title make up for that; a video whose title
names nobody rests on the pictures alone, where 3 of 26 men's and narrators' videos were called a woman's.

**Real runs on the PC, without sound** (`tools/dry_run.py --video`, the Dune captions without punctuation):

| the video given | what the model said | look | voice | start-up hold | dubbed |
|---|---|---|---|---|---|
| a talk by a woman (`iCvmsMzlF7o`) | a woman presents it | 1.7 s | `ipek` | 8.4 s | 6 of 6 |
| the owner's coffee video (`uSV0PQAxqlA`) | a man presents it | 1.7 s | `kalin` | 8.6 s | 4 of 4 |
| an animation (`h6fcK_fRYaI`) | unclear | 1.5 s | `kalin` | 8.2 s | 4 of 4 |

The look is 0.5 to 0.8 s of fetching and 0.7 to 0.95 s of the model, which has one slot: the look takes
its turn with the restoring of the punctuation and the first translation. `ipek` costs the voice 0.39 s
for a second of speech, `kalin` 0.42 s. What the look adds to the hold was not measured against the same
voice without it: the 7.3 s of the run before this work was in the owner's voice, which is faster.

## The PC

- `C:\AI\config\models-dublaj.ini` has the `mmproj` line of `models-video.ini`; the file from before is
  `models-dublaj.ini.yedek-20261115-012535`. To undo: put that file back and restart the translator.
- The translator alone was restarted, while no session ran: its processes were ended and the mode
  service's own `start_hybrid` started it again (a line in `C:\AI\logs\gpu-mod.log` says so). 24 s without
  a translator. The model loaded in 7 s; its context is 71680 tokens now, 92928 before.
- `C:\AI\tts\voices` holds `ipek.wav` and `ipek.txt` beside `kalin` and `owner`. Eleven candidates were
  there for some minutes under `aday-…` names and are removed.
- The voice service spoke 48 test sentences and three dry runs; each use moves the PC's return to its LLM
  mode 45 minutes on.

## The voice `ipek`

25 candidates from five descriptions (VoxCPM2 voice design on the Mac), eleven spoken on the PC. Chosen:
the one whose pitch was steadiest from sentence to sentence, with nothing below 150 Hz. The cloned voice
follows its clip's pace and level, so the clip was made a tenth faster and 2.7 dB louder.

| four sentences on the PC | pitch | from sentence to sentence | length | level |
|---|---|---|---|---|
| `ipek` | 240 Hz | 229 to 242 Hz | 21.3 s | 0.063 |
| `kalin` | 104 Hz | 96 to 110 Hz | 21.3 s | 0.061 |
| `owner` | 186 Hz | | 18.1 s | 0.062 |

Nobody has heard it. The owner's own voice measures 186 Hz, between the two.

## Rulings

- The woman's voice is `ipek`, not `kadin`: one letter from `kalin`.
- What does not say, and every failure, is the man's voice. No third setting.
- The title and channel go into the question (the table above).
- The answers are kept in memory, not in a file.
- The voice is told the video by the first report, before the captions.
- The dry run got `--video`.

## The final review

A fresh reviewer read the whole branch: no critical finding, "merge with fixes". Fixed, each with a test
that failed first (1131 tests pass, and the five slow ones of the dry run):

- **A look that failed, or whose answer came too late, was never made again for that video** while the
  browser kept its connection: stopping the dub and starting it again read the video in `kalin` once
  more. A video that is named again was started again, so its voice is now picked anew; a look still
  being made is waited for, not made twice. A test runs the real voice inside a session for this.
- **A cookie that YouTube sets was sent back with the next look** (re-graded from minor: the code says no
  cookie is sent). The client now keeps none.
- **An answer from a look without title and channel was kept like any other** (re-graded from minor):
  the pictures alone are wrong too often, so it is used for that run and asked again at the next start.
- The README and the example settings now say that `auto` needs the PC translator, and what a model
  that cannot see does.

Checked, not ruled: the question in the code is the measured one, word for word.

Left as they are, for the owner to weigh:

- A look has no limit on its whole time: one slow picture holds back the four that came, until the
  first sentence gives up on it after 6 s. Ctrl-C waits for requests under way.
- `who` raises for a video id that is not text; it is caught, logged with a traceback, and the man's
  voice reads.
- Control characters in a title reach the log line and the question.
- The first sentence's wait for the look is counted as voice time in the metrics.
- A look whose reply is cut off or empty is logged in the words of a translation.
- The bar shows `kalin` through the start-up hold; the first status after it names the voice in use.
- The dry run calls its option `--video`, the fake extension `--video-id`.
- No line at start says that the loaded model cannot see; there is a warning for each video.
- The test set is below, but the script that scored it is not in the repository.

## The test set

Asked through `Presenters(YouTube().glimpse, translator.look).who(id)`, in dubbing mode.

- Presented by a man (21): `uSV0PQAxqlA aSNSHoqT6ac XP5k0bHmhqc qp0HIF3SfI4 iG9CE55wbtY arj7oStGLkU
  dQw4w9WgXcQ eIho2S0ZahI fLJsdqxnZb0 jHbyQ_AQP8c 9bZkp7q19f0 UF8uR6Z6KLc ji5_MqicxSo pxBQLFLei70
  rrkrvAUbU9Y hVimVzgtD6w nTFEUsudhfs Yocja_N5s1I xoxhDk-hwuo MFzDaBzBlL0 lLWEXRAnQd0`
- Presented by a woman (20): `iCvmsMzlF7o Ks-_Mh1QhMc c0KYU2j0TM4 H14bBuluwB8 D9Ihs241zeg RcGyVTAoXEU
  KM4Xe6Dlp0Y P_6vDLq64gE YQHsXMglC9A wHGqp8lz36c gkjW9PZBRfk 4ZNWYqDU948 18uDutylDa4 86x-u-tz0MA
  UyyjU8fzEYU v7AYKMP6rOE aHjpOzsQ9YI OYpwAtnywTk yFVXsjVdvmY 40riCqvRoMs`
- Animations with a narrator (5): `h6fcK_fRYaI aircAruvnKk MBRqu0YOH14 7Pq-S557XQU fNk_zzaMoSs`

Most of the set is talks whose title names the speaker, which is where the title helps most. Six videos
the owner watched on 2026-11-14, with no name in title or channel, were asked afterwards with the built
code and checked by eye: 4 presented by a man, 2 by a woman (`TUqK4urQSEE`, `K6REs6cASyY`), all six right.

## Not done, and known limits

- A video about somebody who does not speak in it (their name in the title, their face in the pictures)
  takes that person's voice. A narrator who is never seen gets the man's voice.
- One voice for a video: an interview is read in one voice.
- No choice by hand for one video, and no file of answers: `uv run dubber voice kalin` is the way back.
- The frames are YouTube's three, not the first frames of the video; a video too new to have them is
  judged from what there is, or read in the man's voice.
- A better eye (a vision model of its own, or frames from the player) was not needed for what was measured.
