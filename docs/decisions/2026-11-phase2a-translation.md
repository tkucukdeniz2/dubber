# Phase 2a results: translation quality

Date: 2026-11-02
Status: built and tested without the PC, because Qwen was busy with your own work. The listening check below has not been done, so the translations themselves are not judged yet.
Evidence: branch `phase2a-translation`, `docs/superpowers/plans/2026-11-02-phase2a-translation-quality.md`, and the tests it names.

## What changed

- **Whole sentences.** A sentence is split only past 60 words or 18 s, after a comma near its middle when it has one. Before, anything past 15 words or 10 s was cut at its largest pause.
- **Human subtitles.** A sentence that ends inside a cue now ends there, with the cue's time shared out by characters. A title, an initial, a dotted abbreviation or an ellipsis is not a sentence end, and on a punctuated track only a pause of 1.5 s cuts.
- **Context and glossary.** Each sentence goes to the translator with the three before it and the `[glossary]` table of `config.toml`.
- **Shorter phrasing.** A clip that does not fit even at 1.2x is translated once more, in fewer words. The metrics mark it `shortened`.
- **Start and seek.** After a start or a seek inside a sentence, the dub begins at the next one instead of placing the rest late.
- **Long replies.** The translator's token limit grows with the sentence, and a reply cut off by that limit is an error, so half a sentence is never spoken.
- **No more 0.9x slow-down, for now.** It stays off until the mixer follows the player's rate.

## What was measured

On the Dune essay's auto-captions (`bakeoff/captions/XP5k0bHmhqc.en.asr.json3`):

| | Before | After |
|---|---|---|
| Segments | 340 | 184 |
| Segments ending mid-sentence | 158 | 0 |

The median segment is now 18 words and 6.0 s, and the longest is 59 words and 16.0 s. This covers one auto-caption track; no real human-subtitle track has been saved and measured.

The final reviewer also ran the real session and segmenter on this track in a simulation, with stub engines and clip lengths modelled from the PC rows of `metrics.jsonl`: 184 sentences, none dropped, a start-up hold of 8.6 s and no later holds, lag p50 0 s, p90 0.76 s and at most 1.5 s. That is a simulation, not a run.

Not measured yet: how the translations read, and how the longer clips sit against the video. Both need the PC in dubbing mode.

## Rulings

These are the calls made on your behalf. Each says what it costs if it is wrong.

- **R1. Limits of 60 words and 18 s, not the design's 15 and 10.** The evidence is above and in `bakeoff/demo/translategemma_vs_gemma.md`, where both models mistranslate half sentences. A first choice of 40 words still cut 6 segments mid-sentence. Cost: a long clip can drift a little inside its sentence, and one that overruns leaves the dub behind until a silence or a faster clip makes it up. Both limits are settings.
- **R2. No punctuation restoration by the model yet (design 7.6).** The one auto-caption track saved so far is punctuated. The server logs a warning for a track where fewer than 1 word in 40 ends a sentence. Cost: such a track dubs in chunks of up to 60 words, with sentences run together or split. Dune with its punctuation stripped gives a median of 48 words and 14.9 s.
- **R3. Context is the English of the previous three sentences.** Turkish context would carry one bad rendering into the next.
- **R4. A start or seek inside a sentence skips to the next one; it does not rewind the player.** The extension has no seek control. Cost: a start a couple of seconds into a video misses its first sentence. Rewinding would need a small extension change, and it is your call whether that is worth it.
- **R5. TranslateGemma gets no context, glossary or shorter phrasing.** Its fixed instruction has no room for them. Gemma 4 12B in dubbing mode gets all three, so it is now the better fit. The choice stays yours.
- **R6. No syllable budget in the prompt (design 7.8).** Models count syllables poorly. The measured clip and a word count do that job. Cost: more sentences may need the second request.
- **The 0.9x slow-down is off until the mixer follows the rate (design 7.10 keeps it as a last resort).** Clips sit at fixed places in video time, so a slowed player makes up none of the delay and the dub only stutters; the rate change also goes out when a clip is placed, up to a minute before it plays. Cost: none today. `slow_player` in `[scheduler]` brings it back, and Phase 2b should do that with the rate-aware mixer.
- **On a punctuated track, only a pause of 1.5 s cuts (design 7.6 says 0.6 s).** The design's cut prepares a track for punctuation restoration; on a punctuated track the full stops say where sentences end. Cost: a clip plays through a shorter pause, so inside a sentence it can sit up to that far off the English. It is a setting.
- **A title, a dotted abbreviation or an initial never ends a sentence.** Missing a real end joins two sentences, which translate well together; a false cut leaves two halves. Cost: "in the U.S. Then we left." is one segment.
- **The extra skip when the player's first report comes after the captions stays, as a safeguard.** The extension sends its report first today, so this path is not reached. Cost: none.
- **A shorter phrasing is never asked to go under 60% of the first one's words.** Rephrasing cannot cut more without dropping content, which the design counts as a meaning error. Cost: some clips still run over and are caught up later.
- **A comma counts as a split only when each side keeps a quarter of the words.** A lone "So," is never a segment. Cost: a long sentence whose only comma is off-centre splits at its largest pause.
- **The token limit never goes under today's 200.** The plan's formula would have lowered it for short sentences. Cost: none in practice.

## Your listening check

1. Run `pc-dublaj`. Qwen stops while dubbing, and `pc-llm` brings it back.
2. On this branch, run `uv run dubber serve --config config.toml`.
3. Dub the Dune video (`XP5k0bHmhqc`) from 0:00 and from 1:28, then `QwKEaTfCqU0`.
4. Run `uv run dubber stats`. Compare with the first Turkish run: lag p50 3.81 s and p90 4.87 s, with Yelda on the old segments.
5. Judge about 20 sentences for meaning errors, against the half-sentence translations in `bakeoff/demo/translategemma_vs_gemma.md`.
6. Choose the dubbing model. Keep only that one in `pc/models-dublaj.ini`, then deploy it to `C:\AI\config\models-dublaj.ini` and run `pc-dublaj --zorla`.

Listen in particular for these:

- Long sentences: does the Turkish stay close enough to the video until the sentence ends?
- The start from 1:28: is hearing the rest of the English sentence before the Turkish begins acceptable, or should the player rewind to the sentence start? The final reviewer points out that nearly every start lands inside a sentence, which can run up to 18 s.
- A real `shorter` reply: does it stay plain Turkish? Replies are only checked for being non-empty and shorter, so any chatter would be spoken.
- Whether Gemma handles the follow-up turn for a shorter phrasing. Only a stand-in has answered it so far; if it fails, the first clip is kept.
- A 60-word subtitle in the overlay: does it cover too much of the picture? It has not been seen in a browser.

## Deferred

The final review's minor findings, left for you to decide on or for Phase 2b:

- A seek that lands inside a skipped sentence, with clips ready ahead of it, pauses the player, cancels those clips and voices them again.
- A skipped sentence counts as covered, so a start inside a long one plays at once with nothing produced, and the bar shows it as ready. It was harmless in simulation, and it can clear the hold-exceeded state early.
- A job in flight for a sentence that then gets skipped still places its clip inside that sentence. The extension's message order does not reach this today.
- A single human cue is never split, however long: a 91-word, 30 s cue stays one segment. A one-event unpunctuated track gives one huge segment, which the voice service refuses, so it becomes a subtitle.
- A track without punctuation could use smaller limits when `looks_unpunctuated` is true.
- A very short rendering can be asked for "at most 1 words". About half of the shorter requests come from an earlier clip's overrun rather than the sentence's own length.
- Stale wording remains in the `Segment` docstring and at the "next pick logs it as dropped" comment in `_deliver`, and `[segmenter]` values have no range check, so `max_words = 0` gives one-word segments.
- With a voice 1.5 times slower, the new limits gave holds of 8.6 s and 19.6 s where the old ones gave none, which is close to the 20 s hold cap.
- Save one real human-subtitle track, rerun the mid-sentence measure on it, and keep it as a test fixture.

Left as they are, and pre-existing: a seek into a produced long clip resumes in the middle of its Turkish, as the design asks; `[Music]`-style markers go to the translator; restated roll-up cues stay garbled; drag scrubs while paused stay parked with the pause and seek patch. A caption's text reaches only the translator, and the translator's reply only the voice.

## Every ruling made during the build

This is the build ledger's full list, kept here because the ledger itself was scratch. Each line says what was decided, why, and what it costs if wrong.

- Tasks 1 -> 2: Ruling: Task 2's clause-mark test strips closing quotes and brackets the same way Task 1's sentence-end test does (one helper) — consistent rules for `end,"` — cost if wrong: a comma before a quote is not a preferred cut
- Task 2: Ruling: SegmenterConfig is defined in dubber/segmenter.py and imported by config.py (the plan's Files list said config.py) — mirrors SchedulerConfig in scheduler.py and keeps the defaults beside segment() — cost if wrong: one move
- Task 2: Ruling: a clause mark is a cut only when each side keeps at least a quarter of the tokens — a lone "So," must not become a segment — cost if wrong: an overlong sentence whose only comma is off-centre splits at its largest gap
- Task 2: Ruling: two tests beyond the brief (a comma beats a longer pause; a comma before a closing quote cuts) — they pin the comma-first rule and the pre-flight ruling — cost: none
- Task 2: Ruling: the twelve split tests that assumed the Phase 1 caps call a segment15 helper that passes max_words=15, max_seconds=10.0 — as the brief asks, expectations unchanged — cost: none
- Task 3: Ruling: the brief's test_translategemma_translates_the_sentence_alone_even_with_context is not added — the existing test_translategemma_gets_its_own_instruction_as_a_completion already passes a context and a glossary and checks the prompt holds only the sentence — cost: none
- Task 3: Ruling: test_context_sentences_zero_sends_no_context passed before the feature (no context was ever sent) — kept as the guard for the 0 setting — cost: none
- Task 3: Ruling: the loader rejects a negative context_sentences and a glossary value that is not a string (beyond the brief) — the config's rule is that a wrong value is an error, never silently used — cost: none
- Task 4: Ruling: the words asked for never go under 60% of the first rendering (_SHRINK_FLOOR, not in the brief) — a rephrasing that keeps every content word cannot cut more, and the spec counts dropped content as a meaning error — cost if wrong: some clips still overrun and are caught up by the shift
- Task 4: Ruling: a shorter text identical to the first is not voiced again (beyond the brief) — it saves a voice call; the outcome is the brief's, the first clip stays — cost: none
- Task 4: Ruling: mt_ms and tts_ms of a record add up both attempts — the metrics measure the time spent on the sentence — cost: the first attempt's time is no longer visible alone
- Task 4: Ruling: one test beyond the brief, test_the_identity_stand_in_cannot_shorten — it pins the stand-in's half of the interface — cost: none
- Task 5: Ruling: Session._on_state repeats skip_to while preparing (the brief expected no session change) — the first report can come after the captions, and only it says where the player is; without this the partly passed sentence is placed late — cost if wrong: one bisect per report while preparing
- Task 5: Ruling: Coverage.missing_at also treats a skipped segment as not missing (the brief named _run_end and passed) — otherwise the session holds as soon as the video plays inside the skipped sentence — cost: none
- Task 5: Ruling: three tests beyond the brief (a gap skips only what lies behind; a produced segment is unaffected; a first report after the captions) — they pin the rule's edges — cost: none
- Task 6: Ruling: max_tokens is max(200, 64 + 8 * words), not 64 + 8 * words as in the brief — short sentences keep today's room, since a smaller limit would turn any stray thinking into cut-off errors; a 40-word sentence still gets 384 — cost if wrong: a little unused room
- Task 6: Ruling: _request's "malformed reply" also catches AttributeError — a choice that is not an object now fails on .get — cost: none
- Task 6: Ruling: tests beyond the brief for shorten's room and cut-off, and for TranslateGemma's cut-off — all three requests share the rule — cost: none
- Task 7: Ruling: the word cap is 60, not the plan's 40 (R1 updated in the plan) — measured after Task 2: all 6 segments still ending mid-sentence on the Dune track came from sentences over 40 words that lasted under 18 s, and 4 of them were cut mid-clause ("give us an | insight"); with 60 none ends mid-sentence (184 segments, longest 59 words, 16.0 s) and the 18 s cap still bounds the clip — cost if wrong: a very fast 60-word sentence is one clip, which can drift a little inside itself
- Final: Ruling: on a track where at least 1 token in 40 ends a sentence, a pause cuts only at long_pause = 1.5 s, not at the spec's 0.6 s (7.6) — 7.6's 0.6 s cut prepares a track for punctuation restoration, and on a punctuated track the marks say where sentences end; a comma plus a short pause does not cut either, simpler than the reviewer's clause-mark variant and it keeps sentences whole — cost if wrong: a clip plays through a pause under 1.5 s and can sit up to that far off the English inside the sentence; it is a setting
- Final: Ruling: a full stop after a title, a dotted abbreviation or an initial never ends a sentence, whatever follows — missing a real end joins two sentences, which translate well together, while a false cut leaves two halves — cost if wrong: "in the U.S. Then we left." is one segment
- Final: Ruling: "next word is lowercase" is not a general rule, only after a closing quote or "etc." — existing behaviour cuts "seven. eight", and a lowercase punctuated track must still cut — cost if wrong: a stray lowercase word after a full stop still cuts
- Final: Ruling: the slow-down is off by default (scheduler.slow_player = false) rather than given a better trigger — a first trigger change (slow only when the backlog grows) barely helped in simulation, and reading the mixer showed clips sit at fixed places in video time, so a slowed player makes up no delay and only stutters; spec 7.10 keeps it as a last resort, so it returns with the rate-aware mixer in 2b — cost if wrong: none today, since it could not catch up
- Final: Ruling: the Task 5 re-skip while preparing stays although the extension cannot reach it (it posts start, a report, then requests captions) — harmless, and a safeguard if that order changes — cost: one bisect per report while preparing
- Final: Ruling: R1's evidence covers one auto-caption track and no human track — no human track is saved; the owner's listening check includes QwKEaTfCqU0, and saving one is deferred — cost if wrong: human tracks may split differently than measured
- Final: Ruling: R4 stands (skip, no rewind) and goes to the owner as a decision — the reviewer notes nearly every start lands inside a sentence of up to 18 s; a seek control is an extension change the owner should choose — cost if wrong: part of a sentence, sometimes the first, stays in ducked English
- Final: Ruling: declined by the reviewer, translation quality and chatter in a real shorter reply — only non-empty and shorter are checked, so chatter would be spoken; the listening check covers it — cost if wrong: an odd spoken phrase, fixed in the prompt
- Final: Ruling: declined by the reviewer, Gemma on the follow-up assistant and user turns with reasoning on — only mocked; a failed or empty reply keeps the first clip, so a person gets an overrun, not an error — cost if wrong: shorter phrasing does not help
- Final: Ruling: declined by the reviewer, VoxCPM2 on long sentences — needs the PC; the listening check covers it — cost if wrong: long clips sound worse and the caps come down
- Final: Ruling: declined by the reviewer, paused or drag scrubs bypassing the skip — parked with R44 and R46 by the owner — cost: a misplaced dub until that patch lands
- Final: Ruling: declined by the reviewer, stutter at rates other than 1.0 — scheduled for 2b — cost: the known stutter
- Final: Ruling: declined by the reviewer, a seek into a produced long clip resuming mid-Turkish — spec 7.10 asks for an immediate resume; stands — cost: up to one sentence heard from its middle
- Final: Ruling: declined by the reviewer, [Music]-style markers and restated roll-up cues — pre-existing, none in Dune; stand — cost: a marker may be spoken, a roll-up track stays garbled
- Final: Ruling: declined by the reviewer, overlay height for a 60-word subtitle — not seen in a browser; in the listening check — cost: a long subtitle covers more of the picture
- Final: Ruling: declined by the reviewer, prompt injection through captions — a caption reaches only the translator and its reply only the voice; stands — cost: a hostile caption could make the dub say something odd
