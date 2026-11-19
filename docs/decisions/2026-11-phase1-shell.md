# Phase 1 results: the shell

Date: 2026-11-01
Status: built and reviewed. The browser run has not been done, so every blank below is for you to fill in.
Evidence: branch `phase1-shell`, Task 11 of `docs/superpowers/plans/2026-11-01-phase1-shell.md`, `extension/README.md`, and `tests/test_e2e.py`.

Phase 1 is the shell: the server, the Chrome extension and the scheduling between them, with an English stand-in voice (Samantha reads the English captions as they are). The code was written and reviewed without a browser, so loading the extension in Chrome and judging the sync by ear are your job. This file is the checklist for that run, in the order to do it, with room for the results. Run every command from the repository root. The shell is zsh.

## What was checked without a browser

`tests/test_e2e.py` is a slow test (`uv run pytest -m slow tests/test_e2e.py -v`). It runs the real server, scheduler and mixer, with the real `say` voice Samantha, against `tools/fake_extension.py`, which stands in for Chrome. The mixer's output stream is a stand-in that opens no audio device, and a task pulls the blocks a speaker would, so the test can tell when Samantha speaks. On 2026-11-01 it passed. Five sentences came out with a lag of 0.00 s and none dropped, Samantha was heard at the start of each sentence that played, the player was held for about 2 s at the start (the test lowers `startup_ready` from 12 s to 3 s), and stop gave the volume back.

The same day, `tools/fake_extension.py` was also run by hand as a script against that server wiring, with the real captions of `XP5k0bHmhqc` and the normal 12 s start-up rule, again with no audio device. The hold is timed the way the server log shows it (see Record below). In 40 s of video the player was held 3.5 s at the start, the server produced 48 sentences (it works up to 120 s ahead), the lag was 0.00 s at p50 and 0.50 s at p90, and none was dropped. A second run from 120 s into the video (`--start 120 --seconds 15`) held 4.3 s and produced 23 sentences with a lag of 0.28 s at p50 and 0.79 s at p90, none dropped: Samantha trails the narrator a little where the speech is dense, so the 0.3 s limit for p50 can be close. Those are the numbers to compare your browser run with.

That covers the server side. It says nothing about Chrome, YouTube's player or how the voice sounds against the narrator, which is what the rest of this file is for.

## Set up

1. Load the extension. Open `chrome://extensions`, turn on Developer mode, choose Load unpacked and pick the `extension` folder. Reload every YouTube tab that was already open. `extension/README.md` has the details.
2. Copy the settings if you have no `config.toml` yet (`-n` keeps one you already have), then start the server and leave it running in its own terminal:

   ```
   cp -n config.example.toml config.toml
   uv run dubber serve --config config.toml
   ```

   It should print `engines: identity + say:Samantha` and `listening on ws://127.0.0.1:8765`.

## Extension checks

These need a browser. Use `https://www.youtube.com/watch?v=XP5k0bHmhqc` with the server from Set up running. Press Alt+Shift+D (the Option key on a Mac keyboard) or click the Dubber button to start and stop dubbing.

1. [ ] Click Dubber. The server terminal shows `hello from chrome`, `start`, `state`, and within 6 s `captions ... asr, en, N events` with N above 1000. Did Chrome show a Local Network Access prompt?

   Note: ____
2. [ ] Look at YouTube's captions after the click. Try it once with CC off and once with CC on. They may flash for a moment, and afterwards they must be on or off just as before.

   Note: ____
3. [ ] While dubbing:
   - [ ] `document.querySelector("video").volume` reads 0.2 (type it in the DevTools console).
   - [ ] Dragging YouTube's volume slider is pushed back to 0.2 within 300 ms.
   - [ ] The player pauses on start and plays once the bar says `playing`.
   - [ ] The bar sits at the top and never blocks a click.
4. [ ] Click Dubber again (or press the shortcut). The volume returns to exactly what it was, the bar disappears, and the video keeps playing.
5. [ ] Press Ctrl-C in the server terminal while dubbing. The bar says `disconnected` within 2 s and the volume comes back. Start the server again. The bar shows the idle status within 10 s, without a page reload.
6. [ ] Set the speed to 1.5x while dubbing. It is held at 1.25x, and the bar says so. Stop dubbing. The speed goes back to what it was when you started.
7. [ ] Go to another video while dubbing. Dubbing stops and the volume comes back.
8. [ ] Open two YouTube tabs. Start dubbing in the second while the first is dubbing. The second shows `another tab is dubbing`. After you stop the first, the second's bar updates to `idle` within 20 s.
9. [ ] Reload the extension on `chrome://extensions` while dubbing. The volume comes back. Reload the tab before you use Dubber there again.

From the extension's review (browser only):

10. [ ] Local Network Access. After a YouTube page loads, the server log shows `connection open (origin chrome-extension://...)` and `hello from chrome`. If the bar stays on `disconnected` while the server logs no connection, Chrome is blocking the loopback socket. Write that down, open the service worker's console (`chrome://extensions`, Dubber, "service worker") to see why, and stop there. The fallback, Native Messaging, is outside Phase 1.

    Note: ____
11. [ ] On a video that has both human and automatic English captions, note which one the server log reports in its `captions` line (`manual` or `asr`).

    Note: ____
12. [ ] After you stop, YouTube's own volume slider shows your original value, and the next video and a page reload keep it. YouTube might remember the ducked 20%.

    Note: ____
13. [ ] A timestamp link in a video description does not stop dubbing. On a live stream, note what the bar shows as the mode. The server only knows captions mode in Phase 1, so it may say `captions` and not `live`.

    Note: ____

## End-to-end run

Settings: the stand-in, so Samantha reads the English captions. Reload the video page first so it starts fresh.

1. [ ] Open `https://www.youtube.com/watch?v=XP5k0bHmhqc`.
2. [ ] Click the Dubber button in the toolbar.

What should happen:

- [ ] The player pauses and the bar says `preparing`.
- [ ] Within 15 s the player resumes and Samantha reads the English captions.
- [ ] Each sentence starts within 0.5 s of the moment the narrator says it. Judge by ear against the English narration at 0.2 volume, for at least 5 minutes.
- [ ] Seek forward 3 minutes. The player pauses for at most 20 s, then resumes in sync.
- [ ] Seek back 30 s. It resumes at once.
- [ ] Stop dubbing. The volume comes back and the bar goes away.

Pausing and seeking are the basic versions in Phase 1, and the finer handling is deferred (README, "Known limits"). Seek while the video plays. A scrub while it is paused is a known Phase 1 gap: the Turkish can come out at the wrong place minutes later. If that happens, stop dubbing and start it again. The fix is ready for Phase 2. If a seek or a pause misbehaves, write down what you did and what happened.

Notes: ____

## Metrics check

Stop dubbing, then run:

```
uv run dubber stats --metrics metrics.jsonl
```

It summarizes the last run in the file, so do this before you start another one. The plan calls the sentence count `count`; the command prints it as `segments`.

- [ ] `segments` above 40
- [ ] `lag p50` at most 0.3 s
- [ ] `dropped` 0

Paste the output:

```


```

## Record

```
Lag p50 (s):
Lag p90 (s):
Start-up hold, from the click to the video playing (s):
Local Network Access prompt (yes or no; the Chrome version and the prompt's words if yes):
YouTube quirks met (caption state, volume resets, anything else):
```

For the start-up hold, take the time in the server log from the first `state: paused` line after the `start` line to the next `state: playing` line. Every log line begins with the clock time, in whole seconds. The `start` line is not where the hold begins. The video was playing when you clicked, so the first `state` line after `start` says `playing`, and the hold begins when the server pauses the video. Timing it by eye, from the moment the video stops until it plays again, is good enough too. `tools/fake_extension.py` reports its hold the same way.

Phase 1 verdict (does the shell hold up, and what should be fixed before Phase 2):

```


```

## Optional: first Turkish run

Only when the PC is on, Tailscale is up on both machines, the PC's llama.cpp server is in LLM mode with a model loaded, and `~/.qwen-pc/api-key` holds its key on one line. Before every sentence Dubber asks the PC which model is loaded and uses that one, so it does not ask the PC to switch.

1. [ ] In `config.toml` set `engine = "pc"` under `[translator]` and `voice = "Yelda"` under `[voice]`. The same preset sits in a comment in `config.example.toml`.
2. [ ] Restart the server. It asks the PC for its list of models once, and should then print `engines: llamacpp + say:Yelda`. If it stops with `engine error: cannot reach the PC at ...`, the PC is off or Tailscale is down on this Mac or on the PC. Check both and start it again.
3. [ ] Open the same video and click Dubber.
4. [ ] Turkish is spoken. A sentence that could not be translated, for instance because the PC dropped out, appears as English text on the bar with the reason, and is not spoken.
5. [ ] Run `uv run dubber stats` once you have stopped.

```
Start-up hold (s): 9 (from the server log, first run below)
Translation p50 (ms): 754
Voice p50 (ms): 837
Lag p50 and p90 (s): 3.81 and 4.87
Dropped: 0
How it sounds (is Yelda understandable, does the dub fall behind, does the player slow down):
```

First run, 2026-11-01 evening, from the server log and `uv run dubber stats` (session 20261101-205110-4, 25 sentences from 1:25 to 3:21):

- The first attempt failed: the page script never answered, so after 10 s the extension sent an empty track and the server gave the player back. Chrome had loaded the shared caption helper (`lib/timedtext.js`, listed for both worlds) into the extension's world only. Fixed in 9b63763; after reloading the extension and the YouTube tab, all 1108 caption events arrived at once.
- No Local Network Access prompt: the extension's service worker connected to the loopback server straight away.
- Dubbing started at 1:28, in the middle of a sentence that began at 1:25. That sentence was placed at the playhead, 3.0 s late, and its overhang set off the 0.9 slow-down at once.
- Every one of the 25 Turkish clips played at the 1.2 maximum and still ran past its slot, so the dub stayed about 4 s behind and the player stayed at 0.9 for the whole run.
- Phase 2 targets from this run: start from the next sentence (or seek back to the start of the current one) instead of voicing a half-spoken one; ask the translator for shorter phrasing; a faster Turkish voice; rate-aware mixing before the 0.9 slow-down is used.

Turkish may take longer to say than the English, and the voice speeds up only to 1.2x. When that is not enough the dub falls behind and the player slows to 0.9x, where the dub stutters. That stutter is a known Phase 1 limit.

## Finish

Commit the log:

```
git add docs/decisions/2026-11-phase1-shell.md && git commit -m "docs: Phase 1 shell results and the browser run"
```
