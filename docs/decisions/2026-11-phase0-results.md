# Phase 0 results: bake-off decision log

Date: 2026-11-01
Status: sheets generated; translation sheet preliminary (do not judge); voice sheet to be regenerated before judging. No decision has been made yet.
Evidence: the `bakeoff/` folder on this Mac (git-ignored, so none of it is in the repository), the tools at commit `e9d5f6c`, and section 8 of `docs/superpowers/specs/2026-10-31-live-turkish-dubbing-design.md`.

On 2026-11-01 the steps that need no person were run: one video's captions, the 25-sentence set, the translation sheet and the voice sheet. Section 5 lists what only the owner can do, with the commands. Section 6 holds the decisions, blank until then.

## 1. Status

Sheets generated; translation sheet preliminary (do not judge); voice sheet to be regenerated before judging (2026-11-01).

| Item | Where | State |
|---|---|---|
| English captions of video 1 | `bakeoff/captions/XP5k0bHmhqc.en.asr.json3` | in place |
| Caption inventory | `bakeoff/captions_inventory.json` | one video recorded |
| Sentence set | `bakeoff/sentences.json` | 25 sentences, all from video 1 |
| Translation sheet, key, timing | `bakeoff/translate/` | preliminary: the Mac engine was down (section 3), so do not judge it |
| Voice sheet, key, timing, 56 wavs | `bakeoff/voice/` | complete for four voices; section 5 re-runs it with Cem and enhanced Yelda added before judging |

## 2. Test set

Eight English videos from the owner's Watch Later list, all single-narrator recorded videos. Spec section 2 describes the list.

| # | Video id | Channel | Caption files in `bakeoff/captions/` |
|---|---|---|---|
| 1 | XP5k0bHmhqc | The Art Of Storytelling (The Horror Of Dune's Spaceships) | `XP5k0bHmhqc.en.asr.json3`, English auto-captions |
| 2 | lULieGSxVrU | Dr Brad Stanfield | none yet |
| 3 | yewRL8ir8pc | Real Men Real Style | none yet |
| 4 | LopuR2Cp34Y | Better Chapter | none yet |
| 5 | SwYDxh3PQJ0 | Asianometry | none yet |
| 6 | Qgwq53BXl6Y | Nostalgia Nerd | none yet |
| 7 | Qg4HTIFqOhA | The Vintage Fame | none yet |
| 8 | sEDPEdw5q2w | Darko Audio | none yet |

One of the eight has a caption file so far. Its inventory entry, recorded with `uv run python -m tools.bakeoff_captions --video=XP5k0bHmhqc --tracks '[{"lang":"en","kind":"asr"}]'`:

| Video | manual_tr | manual_en | asr_en | translated_tr | checked |
|---|---|---|---|---|---|
| XP5k0bHmhqc | false | false | true | false | 2026-11-01 |

The entry lists only the one track that was passed to the recorder. Spec section 2 says YouTube's auto-translated Turkish was fetched in the owner's browser for this video, so `translated_tr` should turn true when the snippet is run on it, and recording again replaces the entry. The English file came from an earlier yt-dlp fetch, not from the snippet.

## 3. Translation bake-off

Run on 2026-11-01 around 05:30 local time (UTC+3), seed 7, in about 30 seconds:

```
uv run python -m tools.bakeoff_translate --sentences bakeoff/sentences.json --engines pc-qwen,mac-ollama --seed 7 --out-dir bakeoff/translate/
```

Sentences: 25, all from XP5k0bHmhqc (ids `XP5k0bHmhqc.en.asr:0` to `XP5k0bHmhqc.en.asr:181`). The tool keeps sentences of at least six words and picks them at even spacing from the first to the last; these run from 6 to 43 words. One speaker and one subject (Dune's ships, Heighliner, Harkonnen, Guild Navigators) make this set narrow. The final set draws on all eight videos.

| Engine | Result | Mean seconds per sentence |
|---|---|---|
| pc-qwen (`qwen3.8-27b` on the PC, over Tailscale) | 25 of 25 translated | 1.16 |
| mac-ollama (`qwen3.8:27b-mlx`, local Ollama) | unavailable, 0 of 25 (`[Errno 61] Connection refused`) | none |
| youtube-tr | not run, no Turkish caption file yet | none |
| pc-gemma (`gemma4-12b`) | not run: it needs a model swap on the PC, which only the owner triggers | none |

Before and after the run, a read-only request for the PC's model list showed `qwen3.8-27b` loaded and the other models not loaded, so the pinned requests caused no swap.

The Mac engine failed because nothing was listening on 127.0.0.1:11434. The Ollama app's log records `shutting down ollama server` on 2026-10-31 at 22:54 local time. The app was not started for this run, because starting it loads an 18 GB model and that was left to the owner. The model is installed (`~/.ollama/models/manifests/registry.ollama.ai/library/qwen3.8/27b-mlx`). The run was not repeated.

In the sheet, each sentence has the pc-qwen translation on one label and `[engine unavailable: [Errno 61] Connection refused]` on the other. That note shows which label is which, so the sheet is not blind, and it holds one real candidate per sentence. It is not worth judging; section 5 regenerates it with every engine.

One observation, not a mark: the two sentences with digits in the English (1984 and 1968) came back from pc-qwen with the digits unchanged, although the prompt asks for numbers as words. Two samples are too few to call it a finding; watch for it when marking. Sentence d1 of the voice test (section 4) feeds digits to each voice.

Still to do:

1. The Ollama app running, so mac-ollama has something to answer.
2. Turkish caption files for the youtube-tr engine (the snippet downloads them).
3. pc-gemma, run by the owner when the PC is idle. It needs its own run and its own folder (`bakeoff/translate-gemma/`): each request names its model, so a run holding both PC models would ask the server to swap back and forth on every sentence. A one-engine sheet is also not blind.
4. The other seven videos, so the 25 sentences come from all eight.

## 4. Voice bake-off

Run on 2026-11-01 around 05:31 local time (UTC+3), seed 7, in 1 minute 46 seconds:

```
uv run python -m tools.bakeoff_voice --engines say:Yelda,supertonic:M1,supertonic:F1,voxcpm --seed 7 --out-dir bakeoff/voice/
```

Each voice read the 16 fixed sentences as 14 blocks (13 single sentences and one block for the three seam sentences), so there are 56 wavs, plus `voice_sheet.md`, `voice_key.json` and `voice_timing.json`. Every voice answered every block. No clip was flagged and no request failed.

Timings on this Mac (Apple M2 Max, 32 GB). `first_call_seconds` is the untimed warm-up request, which includes loading the model; the other columns leave it out.

| Voice | first_call_seconds | seconds_per_audio_second | Synthesis seconds | Audio seconds | suspect | errors |
|---|---|---|---|---|---|---|
| `say:Yelda` (compact) | 1.33 | 0.183 | 13.0 | 70.7 | none | none |
| `supertonic:M1` | 0.84 | 0.197 | 15.7 | 79.3 | none | none |
| `supertonic:F1` | 0.77 | 0.197 | 15.7 | 79.3 | none | none |
| `voxcpm` (VoxCPM2 8-bit through mlx-audio, stock voice) | 3.03 | 0.860 | 55.4 | 64.5 | none | none |

Notes on these numbers:

- The pre-filter flags a clip when its text runs outside 6 to 25 characters per second. It checks duration only. The round-trip recognition check that spec section 8 mentions is not built, so "none flagged" says nothing about defects an ear would catch.
- Supertonic M1 and F1 give clips of equal length in all 14 blocks, and the samples differ in every block, so only the sound separates the two presets.
- An earlier smoke run of the same tool on this Mac (`bakeoff/voice-smoke/`) measured 0.247 for Yelda, 0.343 for M1 and 0.945 for VoxCPM2. The order held, and the figures moved by 9 to 43 percent.

Still to do:

1. Listening. Nobody has marked the voice sheet.
2. Enhanced Yelda and Cem. Only the compact Yelda is installed, and `say -v '?'` lists no Cem, so `say:Cem` was left out of this run. Download both in System Settings → Accessibility → Spoken Content → System Voice → Manage Voices.
3. Optional, from spec section 8 gate 2: VoxCPM2 with a native Turkish reference clip, added to `--engines` as `voxcpm:<path to clip.wav>` (a `.txt` transcript beside the clip is read if present). No clip has been chosen.

## 5. Owner's checklist

Run every command from the repository root. The shell is zsh.

Audition first

1. MacDub (spec section 8, gate 1). It is free and open source: the MacDub app on GitHub, `lordbasex/macdub`. Download its en-US speech model, the English-to-Turkish translation pack and enhanced Yelda first, then try it on two videos from the test set: one lecture-style video, such as the Dr Brad Stanfield video `lULieGSxVrU`, and one essay with a music bed, such as `XP5k0bHmhqc`. Judge translation and voice by ear, and write the verdict under `MacDub verdict` in section 6. If it satisfies you, stop here and use it: leave the other fields of section 6 blank and go to step 13 to commit the log. Otherwise go on.

Collect the data

2. Caption files. For each of the eight ids, open `https://www.youtube.com/watch?v=<id>` in the Chrome profile you are logged in with, open the DevTools console (Cmd+Opt+J), paste the whole of `tools/caption_tracks_snippet.js` and press Enter. If Chrome refuses the paste, type `allow pasting`, press Enter and paste again. Two files download after a few seconds (Chrome may ask once to allow multiple downloads). If the console tells you to reload the page, reload and paste again.
3. Move the downloads into place. The two files per video land in `~/Downloads`, named `<id>.en.<asr|manual>.json3` and `<id>.tr.<translated|manual>.json3`.

   ```
   mv ~/Downloads/*.json3 bakeoff/captions/
   ```

   That moves every json3 file in Downloads, so move them by name if you keep others there. If Chrome added " (1)" to a name, delete the older copy and rename the new one: the tools read the video id from the start of the file name. The new file for video 1 replaces the yt-dlp copy.
4. Record the inventories. Each console prints a line after a `[dubber]` tag that reads `uv run python -m tools.bakeoff_captions --video=<id> --tracks '[...]'`. Copy from `uv` onward and run it. That is eight commands, video 1 again included.
5. Rebuild the sentence set from all the English files.

   ```
   uv run python -m tools.bakeoff_sentences --n 25 --out bakeoff/sentences.json bakeoff/captions/*.en.*.json3
   ```

   The sentence ids change, so a translation sheet made from the old set no longer matches. Make the translation sheets after this step.
6. A native Turkish voice for VoxCPM2 (spec section 8, gate 2; optional). Choose a clip of 10 to 30 seconds in which one native Turkish speaker talks, with no music or other voices. Save it as `bakeoff/ref/<name>.wav`, with `<name>` a short name you pick, and put the words spoken in `bakeoff/ref/<name>.txt`.

   ```
   mkdir -p bakeoff/ref
   ```

   The voice command in the next step already lists it as `voxcpm:bakeoff/ref/<name>.wav`: put your name in place of `<name>`, or delete that entry if you skip this voice.

Run the engines

7. Voice first, on a quiet machine: quit Ollama if it is running (its model takes about 18 GB of the Mac's 32 GB), so the timings come from an idle machine. Download enhanced Yelda and Cem in System Settings → Accessibility → Spoken Content → System Voice → Manage Voices, then list the names.

   ```
   say -v '?' | grep -i -E 'yelda|cem'
   ```

   `say` matches a voice name as a prefix, so `say:Cem` finds `Cem (Enhanced)`. With enhanced Yelda installed, both `Yelda` and `Yelda (Enhanced)` may be listed, and it is not certain which one plain `say:Yelda` would pick. Passing the full listed name removes the doubt:

   ```
   uv run python -m tools.bakeoff_voice --engines "say:Yelda (Enhanced),say:Cem (Enhanced),supertonic:M1,supertonic:F1,voxcpm,voxcpm:bakeoff/ref/<name>.wav" --seed 7 --out-dir bakeoff/voice/
   ```

   If the listing shows other names, use them exactly as listed (`say:Yelda,say:Cem,supertonic:M1,supertonic:F1,voxcpm,voxcpm:bakeoff/ref/<name>.wav` when there is no suffix). It takes a few minutes, and VoxCPM2 loads a 3 GB model. Wavs from the earlier run stay in the folder but are not in the new sheet; `rm bakeoff/voice/v_*.wav` before the run keeps the folder clean.
8. Translation, every engine except pc-gemma. Start the Mac engine and check both servers.

   ```
   open -a Ollama
   curl -s http://127.0.0.1:11434/api/tags | grep -o 'qwen3.8:27b-mlx' | head -1
   ```

   The curl line should print `qwen3.8:27b-mlx`; if it prints nothing, wait a few seconds and run it again. The PC should have `qwen3.8-27b` loaded: pc-qwen asks for that model by name, and the server would swap to it otherwise.

   ```
   uv run python -m tools.bakeoff_translate --sentences bakeoff/sentences.json --engines pc-qwen,mac-ollama,youtube-tr --youtube-tr bakeoff/captions/*.tr.translated.json3(N) bakeoff/captions/*.tr.manual.json3(N) --seed 7 --out-dir bakeoff/translate/
   ```

   The `(N)` after each pattern makes zsh pass nothing when no file matches. Without it zsh stops with `no matches found` as soon as one of the two patterns has no file, for example when no video has a human Turkish track. The pc-qwen engine takes about a second per sentence. mac-ollama takes 2 to 4 seconds per sentence, and its first call loads the model, which can take about a minute. The run replaces `bakeoff/translate/`, so do it before marking.
9. pc-gemma, when the PC is idle. Load Gemma on the PC by hand first: pc-gemma asks the server for `gemma4-12b` by name, and the server would swap models if it were not loaded.

   ```
   uv run python -m tools.bakeoff_translate --sentences bakeoff/sentences.json --engines pc-gemma --seed 7 --out-dir bakeoff/translate-gemma/
   ```

   It uses the same sentences and its own folder, so the first sheet stays. Load Qwen again afterwards if you want the PC as it was.

Mark and score

10. Mark the sheets. Do not open the `*_key.json` files before you finish.
    - Translation sheets: `bakeoff/translate/translate_sheet.md` and `bakeoff/translate-gemma/translate_sheet.md`. After `mark:` on each candidate line write one word: `ok` (faithful, natural Turkish), `meaning` (a dropped or altered content word, number, date or name; this is the spec's definition of a meaning error) or `style` (the meaning is right but the Turkish sounds unnatural; this reading of the third mark is a suggestion, and the scorer only counts the three words). Leave a line unmarked when it holds a note such as `[engine unavailable: ...]` or `[no Turkish track for this video]`, and the scorer's `marked` column will show how many sentences each engine answered. Mark such a line `meaning` instead if you want a missing translation to count against the engine.
    - Voice sheet: `bakeoff/voice/voice_sheet.md`. Play each file (`afplay bakeoff/voice/<file>.wav`) and write `ok` or `defect` after `mark:`. A defect is anything a native listener would call unnatural, including a misread number, date or name. The block `s1` plays the three seam sentences in one clip; one mark covers all three and counts as three sentences.

    A re-run replaces the sheet in its folder, so marks typed into an old sheet are lost. Judge only after the last run, or give a later run a new `--out-dir`.
11. Score the sheets.

    ```
    uv run python -m tools.bakeoff_score translate --sheet bakeoff/translate/translate_sheet.md --key bakeoff/translate/translate_key.json
    uv run python -m tools.bakeoff_score translate --sheet bakeoff/translate-gemma/translate_sheet.md --key bakeoff/translate-gemma/translate_key.json
    uv run python -m tools.bakeoff_score voice --sheet bakeoff/voice/voice_sheet.md --key bakeoff/voice/voice_key.json --timing bakeoff/voice/voice_timing.json
    ```

    The translate command prints, per engine, the counts of ok, meaning and style, how many lines were marked, and the pass rate (ok over marked). The voice command prints ok out of 16 per voice, whether it passes (14 or more), seconds_per_audio_second, and a `winner:` line naming the fastest voice that passes.
12. Fill in section 6.
13. Commit the log.

    ```
    git add docs/decisions/2026-11-phase0-results.md && git commit -m "docs: Phase 0 bake-off results and engine decisions"
    ```

## 6. Decisions

Left blank for the owner.

Translation rule: an engine passes when at least 90% of its marked candidates are `ok` (the scorer's pass rate), which leaves no more than 10% `meaning` or `style`. Among the engines that pass, the cheapest wins, and cheapest means fastest: youtube-tr first (it counts as zero seconds), then the PC engines by their mean seconds per sentence (`translate_timing.json` in the same folder as the sheet), then mac-ollama. If no engine passes, the engine with the highest `ok` rate wins, and you write "quality bar missed" beside its pass rate below. The runner-up is the fallback engine. The pc-qwen and pc-gemma rates come from different sheets, so compare their `marked` counts before trusting a gap: a rate over fewer marked sentences says less.

Voice rule: the voices that pass (at least 14 of 16 sentences ok) are ranked by `seconds_per_audio_second`, lowest first, and the first one wins unless the owner clearly prefers another passing voice by ear (spec section 8). Spec section 7.9 names VoxCPM2 the default from the spike listening and says the default is whatever wins this test. Two runs on this Mac gave different speeds for the same voices (section 4), so a gap as small as Yelda against the Supertonic presets today (0.183 against 0.197) is inside the variation between runs.

```
Translation winner:
  pass rate:                      mean seconds per sentence:                marked:
Translation runner-up (fallback):
  pass rate:                      mean seconds per sentence:                marked:

Voice winner:
  ok out of 16:                   seconds_per_audio_second:
Voice runner-up:
  ok out of 16:                   seconds_per_audio_second:

MacDub verdict:
```

Resulting `config.toml` defaults (`preferred_model` is `qwen3.8-27b` or `gemma4-12b`):

```
[voice]
engine = ""
voice = ""

[pc]
preferred_model = ""
```
