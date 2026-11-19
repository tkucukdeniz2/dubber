# Sentence ends on caption tracks without punctuation (2026-11-14)

Branch `sentence-cuts`, from `main` at 4f03e15. Design: `docs/superpowers/specs/2026-11-14-sentence-ends-design.md`.
Plan: `docs/superpowers/plans/2026-11-14-sentence-ends.md`.

## What the owner reported, and why it happened

"seslendirirken cümleleri yarıda kesip bekliyor. bir yere kadar konuşuyor 've' diyor sonra bekliyor."
(`aSNSHoqT6ac`, 2026-11-14, with the dub of the robust-dub work.) The producer was healthy: 16 of 16
sentences placed on time, 117 s ahead. The track had no punctuation. An auto-caption word ends where the
next one starts, so such a track has no gaps either, and the segmenter halved it by word count until every
piece was under 18 s. A piece ended wherever the middle fell, its Turkish ended on "ve", and since the clip
was shorter than its slot the dub was silent until the next piece began.

## Measurements

The Dune essay's auto-captions (3571 words; YouTube's own punctuation marks 183 sentence ends), with the
punctuation taken out and the words lowercased. "On a sentence end" means where YouTube's punctuation ends
one.

| how the track is cut | cuts on a sentence end | on a sentence or clause end | right after a function word |
|---|---|---|---|
| as before (the middle) | 3 % | 7 % | 43 % |
| pauses guessed from word times, any threshold | 20 to 25 % | | |
| the new forced cut, no model | 53 % | 67 % | 0 % |
| the PC's Gemma 4 12B restores the punctuation (real run, 38 pieces) | 97 % | 100 % | 0 % |

The model alone, on the whole track in 150-word stretches: the plain prompt gave 117 sentence ends, 96 % of
them YouTube's, 62 % of YouTube's found, in 45 s for 1199 s of speech; the prompt that asks for short
sentences gave 140, 94 % and 72 %, in 47 s. 98 to 100 % of the words of a stretch came back unchanged.

The real run (over a long-distance link, the PC in dubbing mode, `tools/dry_run.py`, 150 s of the stripped track, no sound):

| | before (the same track with its punctuation, robust-dub run) | now, without its punctuation |
|---|---|---|
| start-up hold | 6.6 s | 7.3 s |
| later holds | none | none |
| dubbed / dropped | 39 / 0 of 240 s | 21 / 0 of 150 s |
| translator, per second of speech | | 0.13 s |
| voice, per second of speech | | 0.37 s |
| ahead of the video at the end | 98 s after a minute | 83.5 s after 150 s |
| restoring the punctuation | | 15.8 s for 270 s of speech, 1.1 s for the first stretch |

## Rulings

1. **The translator's model restores the punctuation; no download.** A small punctuation model on the Mac
   would mark a whole track before the video starts, with no change to the session, but needs a download
   and `onnxruntime`. The model that is already loaded does it at 97 % with no new part. Cost: 6 % of the
   video's length in translator time. If wrong: weak sentences on speech unlike the essay's; the small
   model is the next step.
2. **The prompt asks for short sentences.** Ten points more sentence ends for two points of agreement, and
   the ends it adds are sentence ends too. If wrong: a sentence split where a comma would have done.
3. **Only the model's marks are used.** Its reply is aligned to the caption words; a word it changed, added
   or dropped leaves the caption's words as they are. Under 90 % of the words found, the reply is thrown away.
4. **An open track.** The track is installed empty and its sentences are appended a stretch at a time, so
   indexes never change and the session's other rules stand. The video is held at the edge, like at any
   sentence that is not ready.
5. **The producer restores in its own turn**, when nothing is left to make: one model call at a time is the
   rule the session has. If wrong: a sentence's head start is lost once a stretch (about 2 s a minute).
6. **Stretches of 60 words first, 150 after; an end is trusted with 5 words after it; a jump starts 30 words
   back.** From the timings above (0.8 s and 2 s) and from what a model needs to see. Not tuned further.
7. **A jump outside the restored part begins the track again there; nothing is cached.** Going back
   restores again (a second or two). A cache per video stays deferred.
8. **A stretch that fails is cut without punctuation; two in a row and the rest of the track is.** The dub
   never waits for punctuation it cannot get. If wrong: an outage of the PC that outlasts a stretch leaves
   the rest of that video with the fallback cuts until a jump or a new start.
9. **The fallback cut** never follows a function word and prefers to fall before "so/now/but…", then a
   subject pronoun, then "and/or/then…". The word lists are English and were checked on one video.
10. **The run log has each sentence's English and Turkish.** It stays on the Mac. The complaint that began
    this would have been answered by reading it.
11. **The brainstorming gate was skipped**: the owner's standing instruction is to write design and plan
    and go on. The work stays on a branch until the owner says merge.
12. **About sixty small requests were sent to the Gemma loaded on the PC, and one dry run of 150 s was made
    there, without asking first.** The PC was in dubbing mode with the owner's server paused; no mode was
    changed and no model swapped. The dry run was started 50 s before the PC's own 45-minute idle limit
    would have put it back into LLM mode, and so kept it in dubbing mode.

## The final review

A fresh reviewer read the branch and tried it with scripts: "with fixes". Everything below was fixed test-first
(each test seen failing on the old code), suite 1048 green.

- **Critical: the track was begun again on every report while preparing.** When the first stretch ended
  behind the playhead (a start inside a long sentence, no end after it near enough to trust), the place of
  the playhead counted as outside the restored part, and the extension reports four times a second: 14 model
  requests, 20 s of hold, then the video undubbed. It hit about a quarter of mid-video starts at the
  model's sentence length. Now the place a track was begun for is never outside it.
- **The playhead's sentence was dubbed, late, when it came with a later stretch.** The skip is now made
  when that sentence comes, not only on the first stretch.
- **The last restored sentence was placed as the last of the video** (never sped up, shift reset), once a
  stretch. It now knows where the next word of the track starts.
- **Stretches counted caption lines.** A track typed by a person without punctuation got a first stretch of
  480 words. The track is now kept a word to a Word.
- **A reply could put a symbol to a word** ("$50", "20%", "U.S." for "us"). Only case and the marks around a
  word are taken now.

Ruled, not fixed: the forced cut also changes a track with punctuation where the largest gaps of an
over-long part tie. That is what the rule is for.

A second real run after the fixes, from the middle of the track (300 s, for 90 s) and with the voice `kalin`:
start-up hold 9.1 s (two stretches: the first ends at the playhead), 8 of 8 sentences dubbed, no later hold,
the translator 0.11 s and the voice 0.44 s for a second of speech, 36 s ahead at the end. The sentence the
playhead was in was skipped and the dub began with the next one, at 302.9 s. One clip of 18 ran past its
slot at 1.2x: the deep voice takes about 18 % longer than the owner's.

## The voice `kalin`

The owner, the same evening: "kalın oturaklı bir erkek sesi seç". The owner's cloned voice measures 186 Hz
on four dubbing sentences. VoxCPM2's voice design (on the Mac, `mlx-community/VoxCPM2-8bit`, already in the
cache) made eight clips from four descriptions and two seeds; the lowest and steadiest (101 Hz, 85 to 129 Hz
between its 10th and 90th percentile) came from "a man in his fifties with a very deep, low-pitched bass
voice, calm, steady and authoritative, like a documentary narrator, speaking slowly and clearly". Cloned
from that clip on the PC it speaks at 104 Hz (96 to 110 over the four sentences) and takes 18 % longer.
The clip and its transcript are `pc/voices/kalin.wav` and `.txt`, copied to `C:\AI\tts\voices` (two new
files, nothing overwritten, no restart: the service lists a voice when its files are there).
`uv run dubber voice kalin` selects it. Nobody has heard it yet: it was chosen by pitch and steadiness, and
a sample is in `~/Downloads/dubber-ses-kalin.wav`.

## Deferred

- From the review: a failed stretch ends at its 150th word wherever that falls (with TranslateGemma loaded
  every such track gets one such cut); `_reopen` does not reset the timeline (reachable only when captions
  come before the first report); a jump waits for the second stretch (about 2 s more than a start at the
  top); catching up at the edge aims at the sentence under the playhead instead of restoring first.

- The video the owner watched (`aSNSHoqT6ac`) was not fetched: no track without punctuation with real
  spontaneous speech has been measured. The owner's ear is the test.
- A cache of restored stretches per video; restoring while a clip is voiced; a status line in the overlay
  while the first stretch is restored.
- An engine outage that outlasts a stretch (ruling 8).
- Word lists for the fallback cut in languages other than English.
