# A dub that keeps going: what was seen, built and measured (2026-11-14)

The owner, travelling and far from the PC, wrote that the translation stops after a while and
asked for the project to be made as good as it can be. After a first look and a list of five
things to do, the owner answered "hepsini yap" (do all of it). This is the record of that work:
design `docs/superpowers/specs/2026-11-14-robust-dub-design.md`, plan
`docs/superpowers/plans/2026-11-14-robust-dub.md`, branch `robust-dub`.

## What was seen

Section 1 of the design has the figures. In short: over the long-distance link a second of speech cost 0.70 to
0.95 s to produce, against 0.44 to 0.52 at home, because every sentence was translated and
then voiced, one after the other, and every clip crossed the network as a 96 KB/s WAV. One
session lost 41 of 44 sentences: a cold start ran the start-up hold into its 20 s cap, and from
then on the producer made the sentence under the playhead, was late with it, asked for a
shorter rendering, was later still, and never caught up. The log that would have shown this
existed only in a Terminal window.

## What was built

| | Change | How it was checked |
|---|---|---|
| 1 | `dubber.log`: what the session did and what each sentence cost; a clip that came too late keeps its times in the metrics; a slow reply from the PC voice is named; the output device is named | unit tests; the child-process test reads the file |
| 2 | The next sentence is translated while the current one is voiced | unit tests with and without threads; a mutation check of the translator lock |
| 3 | A shorter rendering is asked for only with 30 s ready ahead and for a clip that is not late | unit tests |
| 4 | After a hold that ran out, the producer aims at the first sentence it can dub on time, and gives up what it jumps over; a hold ends and begins without waiting for the player to report | whole runs in a simulation of the real session (`tests/sim.py`, `tests/test_catch_up.py`), unit tests |
| 5 | The PC's engines are warmed when `serve` starts and when the extension connects, only in dubbing mode and never when a model is loaded | unit tests with mock transports |
| 6 | After two failed sentences in a row the producer tries again every 5 s, and the bar says why | unit tests |
| 7 | Pause and seek: the parked fixes R43 and R44, and R46; a hold that began paused sends no pause of its own; a seek back over sentences that were skipped makes the dub again from the first of them, in order | the parked tests and nine new ones |
| 8 | Clips travel as Ogg Opus when the Mac asks; connections to the PC are kept for 60 s | unit tests on both sides, and one of the Mac client against the new service |
| 9 | Captions: requests are taken from an observer, a slow one is waited for, an empty one asked for again (five asks and two downloads at most), a fetched track kept | reproduced and proven in the owner's Chrome, twice (see below); node tests |
| 10 | A tab that is dubbing starts over when the server comes up | node tests of the pure parts; not run in Chrome |
| | `tools/dry_run.py`: a real run without Chrome and without sound | slow test |

## What was measured

**The simulation** runs the real session on a clock of its own, with engines that cost
modelled time, on the timing of the test video and on a talker without a silence
(92 sentences in 420 s). "Before" is the rule that made the sentence under the playhead, with
changes 2 and 3 already in.

| Scenario | Before | After |
|---|---|---|
| Engines that keep up (warm, as measured over the long-distance link) | nothing dropped | 92 of 92 dubbed (100% of the speech), 0 for nothing, none more than 1.3 s late, no further hold |
| Cold start: the first translation waits 11.5 s, the first clips are slow | 54 of 54, clips up to 1.6 s late | 53 of 54 dubbed (98% of the speech), 0 for nothing, none more than 1.6 s late, no further hold |
| Every clip slower than its sentence (2 s + 1.5 times its length) | 5 of 91 dubbed, 33 clips made for nothing | 60 of 92 dubbed (46% of the speech), 0 for nothing, none more than 0.9 s late, no further hold |
| Four slow minutes (4 s more for each clip), then warm | 44 of 92, 28 for nothing | 75 of 92 dubbed (70% of the speech), 0 for nothing, none more than 0.9 s late, no further hold |
| A cold start, then one reply 25 s slow | not run | 86 of 92 dubbed (94% of the speech), 0 for nothing, none more than 4.6 s late, no further hold |

The counts are sentences; the share of the speech is given beside them, because the aim reaches a short sentence more easily than a long one. Where engines stay slower than the speech, about half of it is dubbed.

**In Chrome**, on a muted and paused tab, with nothing dubbed: YouTube clears the browser's
list of requests 2.0 s and 3.1 s after a page loads (36 entries fell to 5, then 28 to 1), so
the request the extension looked for could be gone before it looked; in the first 2.5 s the
player asks without its token and gets an empty answer; one first request for a track was
still unanswered after 5 s, when the old code cut it off. With the new page script, asked 1.3 s
after the page loaded, the track came in 1.5 s; asked again, in 7 ms. After the review's fix the
check was made again: asked 2.8 s after the page loaded, 1.6 s; asked again, 6 ms; and with
captions shown, 0.2 s from the player's earlier request, with the player left alone.

**On the real PC**, over the long-distance link, on 2026-11-14 at 19:50, after the owner's go-ahead ("devam
et"); nothing was played. The PC was put in dubbing mode, which stopped the owner's model.

- The warm-up, on a PC that had just entered dubbing mode with no model loaded: the voice spoke
  its word in 2.1 s and the translator loaded `gemma4-12b` in 7.0 s. A second warm-up
  asked nothing of the translator.
- Twelve clips each way, by turns: as WAV a clip was 270 KB and 0.68 s on the way (the reply's
  time less the PC's own); as Ogg Opus 22 KB and 0.19 s. A whole request fell from 0.69 s to
  0.50 s for a second of audio. Both decode to the same length on the Mac.
- Two dry runs of four minutes on the Dune track (`tools/dry_run.py`), one after the other: the
  new pipeline, and the old one's shape (each sentence translated and voiced in turn, clips as
  WAV, on the same new code).

| | Old shape | New |
|---|---|---|
| Wall seconds for a second of video, while the producer was busy | 0.55 | 0.37 |
| The dub's look-ahead one minute after the start | 49 s | 98 s |
| Start-up hold | 7.8 s | 6.6 s |
| A voice reply on the way, median | 0.50 s (450 KB) | 0.20 s (35 KB) |
| Sentences dubbed of the first 39, dropped, later holds | 39, 0, none | 39, 0, none |

This track has long sentences. On the two videos of the afternoon, with short ones, the old
pipeline needed 0.70 and 0.95 s; those were not run again.

## What was done at the hand-over, and what is the owner's

On "devam et" (2026-11-14): `robust-dub` was fast-forwarded into `main` and the owner's folder
moved to it (`phase2a-translation` still points at the code before, for a way back); the new
`pc/tts_server.py` was put on the PC, with the old one kept as
`C:\AI\tts\tts_server.py.yedek-20261114-214815`, and its `/status` lists `"formats": ["wav",
"opus"]`; the real run above was made; the owner's note on the Desktop was rewritten.

Left to the owner: restart `dubber serve`, and reload the extension in `chrome://extensions`
(changes 9 and 10 run only then; `content.js` and `sw.js` have not run in Chrome yet).

## The final review

One reviewer who had not written the code read the whole branch, ran the suites and the
simulation table again (both columns reproduced), stressed the session with real threads (240
quick stops and starts with seeks and pauses, nothing scheduled after a stop), and ran the
extension's scripts with stand-ins for Chrome. It found nothing critical and three things that
mattered; two more were raised from minor because of what they would do to a viewer. All five
were fixed, each with a test that failed first:

- "after a hold that began paused, stopping the dub starts the video" (Important 1) — a hold that begins while the owner has paused sends no pause — test_a_hold_that_began_while_the_owner_had_paused_sends_no_pause_of_its_own RED→GREEN, suite 986/986.
- "a seek back to a made sentence, with skipped sentences ahead, misplaces them" (Important 2) — such a seek cancels and un-marks everything from the first unmade sentence and bumps the generation, so the rest is made again in order — test_a_seek_back_with_a_given_up_sentence_ahead_makes_the_rest_again_in_order and test_a_clip_in_the_making_during_such_a_seek_is_not_placed_ahead_of_the_rest RED→GREEN, suite 986/986.
- "the aim trusts one make" (re-graded Important) — the estimate is the lesser of the last make and the middle of the last three, each scaled for a longer sentence — test_one_slow_make_does_not_send_the_aim_far_ahead RED→GREEN (27 s ahead before, 3 s after), suite 986/986. In the simulation a single reply 25 s slow after a cold start now costs 6 of 92 and 4 of 54 sentences in all; the review measured 16 and 9 for that reply alone.
- "holds end and are capped only on a player report" (re-graded Important) — Session.tick(), called by the producer thread between sentences, ends a hold when enough is ready or its cap has passed and begins one when the playhead runs into an unready sentence; nothing before the first report — test_a_hold_ends_when_enough_is_ready_without_a_report, test_a_hold_runs_out_at_its_cap_without_a_report, test_the_playhead_running_into_a_sentence_that_is_not_ready_holds_without_a_report, test_nothing_is_done_before_the_first_report_or_while_idle, test_the_producer_thread_ends_a_hold_by_itself RED→GREEN, suite 986/986.
- "a failed caption download is retried with no pause and no limit" (Important 3) — the retry logic moved into a tested helper (fetchTrack): two downloads at most with a 1 s pause, five asks of the player in all, a request YouTube refused (400 or more) counts as unusable, and an earlier request is given 4 s — "a download that fails is tried once more after a pause, and no more", "a YouTube that refuses the player is asked five times in all", "an earlier request that hangs is given 4 s, and then the player is asked", "a request that YouTube refused is unusable, whatever its body", "a track that keeps coming back empty is asked for four times and no more" RED→GREEN, node 84/84. Proven again in the owner's Chrome with the new page script injected (muted, paused, nothing dubbed, stored volume unchanged): asked 2.8 s after the page loaded, the first answer was empty and the track came in 1.6 s; asked again, 6 ms from the store; with captions shown, 0.2 s from the player's earlier request, the player not asked and the captions still shown.

Not checked by anyone yet: `content.js` and `sw.js` inside Chrome (they change only when the
extension is reloaded), and anything on the real PC.

## Decisions taken for the owner

Each is a ruling from the execution ledger, with why and what it costs if it is wrong. They
stand as they were written: where one says that the real run was not made or waits for the
owner, it was made at the hand-over, as told above.

- no wait for a plan review or an execution choice — the owner answered the proposal with "hepsini yap" and used inline execution for Phase 2a — costs a redo of whatever the owner would have changed in the plan.
- a git worktree made with git commands, without asking — the owner dubs from the main folder every day and half-done code there would break a restart; the native worktree tool is reserved for an explicit request — costs a hidden folder in the repo until the work is handed over.
- phase2a-translation fast-forwarded into main (d5daf7e voice command, 8302aaa ignore rule) — "hepsini yap" covered the pending merge decision, and the owner has dubbed from that branch since 2026-11-02 — undone by moving the main ref back to cd7df49.
- Task 1 -> Tasks 3, 4, 7: all four change Session._deliver. Ruling: in Task 3 "late" means the playhead bound only (t + margin > seg.start), worked out before Task 7 adds the end of the clip before it — a clip pushed back by a backlog is exactly the one a shorter rendering helps — costs nothing if wrong beyond a missed shortening.
- Task 5 vs Task 2: both planned a parameter named spawn with different shapes. Ruling: the Warmer's is called run_in_background — one name, one meaning.
- **Task 1.** the engines and listening lines reach the log file through the file handler alone, not through the logger — logged normally they would appear twice in the terminal, once printed and once as a log line — costs a helper (_to_file_only).
- **Task 1.** the hold line is written where the playhead runs into an unready sentence (_keep_up), not in _hold — a jump already says "holding" in its own line — costs nothing.
- **Task 1.** tests/test_cli.py runs every test in its own empty directory (autouse fixture) — serve now writes dubber.log where it is started, and the repository must stay clean — costs nothing.
- **Task 2.** a translation made ahead is keyed by the caption track and the segment, not by the generation — a seek on the same track then still uses it, and the generation check at delivery already discards stale clips — costs one wasted translation less per seek; wrong only if a translation could depend on where the playhead was.
- **Task 2.** Session's `_make` is split into `_translate` and `_speak`, and Coverage gains `next_unproduced(idx)` — the plan named only `_translate` and `_following` — costs nothing.
- **Task 3.** the four existing tests of the shorter rendering get lead_min=0.0 in their Rig — they test how a shorter rendering is asked for and placed, on the first sentence with nothing ready ahead, which the new rule forbids — costs nothing; the rule itself has three tests of its own.
- **Task 3.** the log line of a placed clip says "past its slot" when it runs long — the economy rule makes that case common and the log should show it — costs nothing.
- **Task 4.** the make-time estimate is the last make, scaled up for a longer sentence (never down, and from 2 s at least), times 1.1 — of six estimates tried it lost the fewest sentences in the cold start among those that kept every clip within 2 s of its sentence (medians adapt two makes late; unscaled ones put clips of long sentences up to 12 s late) — costs a sentence or two skipped too many after a single slow make.
- **Task 4.** what the aim jumps over is given up for good (Coverage.give_up) — in the spike a sentence skipped and made later was placed after a later clip, up to 46 s late; a seek forgets what was given up — costs sentences that a suddenly faster producer could still have made.
- **Task 4.** catch-up mode ends when a clip is ready lead_min (30 s) ahead, not when 12 s are covered — given-up sentences count as covered, and with 12 s the slow scenarios ran into the 20 s cap again and again (up to 16 holds, 267 s of frozen video in 420 s); with 30 s at most one further hold — costs, for a producer that recovers, some sentences skipped instead of a hold while it is under 30 s ahead.
- **Task 4.** the simulation's Model takes mt(n, seconds, now) and tts(n, seconds, now), and talk(i), instead of the plan's one cost(n, seconds) — a slow stretch is told by the clock and the two engines overlap — costs nothing.
- **Task 4.** the timing file moved to tests/fixtures (sentences now begin with their own token so a shorter rendering can be told apart), and the parked folder docs/superpowers/plans/2026-11-14-catch-up was removed — its patch and script are replaced by this task.
- **Task 5.** PcVoice.warm_up() only speaks one word and returns None; the Warmer asks the status itself — the plan had warm_up return the status and refuse when not ready, which made the Warmer ask twice — costs nothing.
- **Task 5.** the translator warms only when every listed model says "unloaded" — a model that is "loading" may be the owner's swap (deferred R51), and a server that reports no status is not the PC's router — costs a cold first sentence on a server that reports no status.
- **Task 5.** warm-ups start when serve listens and for each connection of the extension, not on Start — the captions arrive a second after Start, so warming there would gain a second; a connection opens when a YouTube tab loads, well before the button — costs one status request (and a model list) per YouTube page load while the PC voice is up.
- **Task 6.** while it waits, every status report carries "an engine is down, trying again every 5 s: <reason>" — the per-sentence failure message was replaced a second later by the next report, so the owner could not see why the dub was silent — costs nothing.
- **Task 7.** the three tests added for "what the patch left open" are a scrub while paused into the produced range, a second seek during a hold that began paused, and a new Start after such a hold — the plan named two cases the patch already tested — costs nothing.
- **Task 7.** R46 waits for the nearest clip before the segment (Coverage.end_of_clip_before), not only for segment idx-1 — the one before may be a failed or given-up sentence without a clip — costs nothing.
- **Task 7.** the parked patch file is removed and the deferred list says so — it is applied — costs nothing.
- **Task 8.** the format is read by a new audio_format(body) beside parse_request, which keeps its three values — two existing tests and the engine's submit depend on that shape — costs a second json.loads of a body of at most 64 KB.
- **Task 8.** the service keeps answering HTTP/1.0 — with keep-alive its 401 and 404 answers, which do not read the body, would leave the body to be read as the next request; the Mac talks to Tailscale's proxy, not to the service, so its connection is kept anyway — costs nothing measurable on the loopback.
- **Task 8.** the translator's client keeps idle connections for 60 s too — the spec says "its connections to the PC", and the translator makes two requests for each sentence — costs nothing.
- **Task 9.** the requests are taken from a PerformanceObserver, not from the buffer — it is told of each request whatever the page does to the list — costs nothing; setResourceTimingBufferSize is no longer called.
- **Task 9.** a slow request is waited for up to 16 s in all and never asked for twice, an empty one is asked for again after 0.7 s, and the content script waits 20 s — the plan's "try once more" would have cut a slow request off again — costs up to 16 s of a paused video before "no captions" is reported, where it was 5 s.
- **Task 10.** onSocket lives in lib/protocol.js, which the content script already loads — a new lib file would need the manifest changed — costs nothing.
- **Task 10.** the worker drops every message of a tab while it has no socket (before the router sees it) and resets the router also when it closes the socket itself — a start sent while disconnected made that tab the owner of a session the orchestrator never had, and other tabs were told "busy" — costs nothing: the tab starts over when the socket is up.
- **Task 11.** the real run against the PC (Step 1) was not made — the PC was in LLM mode with the owner's model loaded, and switching it, or sending that model translations, needs the owner's go-ahead — costs the one figure this work has not measured on the real engines: production seconds per second of speech with the two engines at once. The simulation and the unit tests stand in for it; the command is ready.
- **Task 11.** tools/dry_run.py was added as the instrument of that run (the real session and producer thread, a stand-in player in real time, no audio device, no metrics file), with a slow test — the fake extension needs a running server, which plays through the speakers — costs a tool of 170 lines the plan did not name.
- **Task 11.** the owner's Desktop note is rewritten at hand-over, not now — it describes commands of the code the owner runs, which is still the old code until the folder is switched.
- **Final.** declined line "playback rates other than 1.0 in the aim" — the spec excludes those rates and the README names the limit — stands; costs a wrong aim at 1.25x, where the mixer stutters anyway.
- **Final.** declined line "YouTube ads in the same video element" — the owner has Premium — stands; costs a confused dub during an ad for anyone without it.
- **Final.** declined lines "a button press lost in the second the worker is stopped while the server is down", "a tab left dubbing after the server ended its session needs two presses", "local MLX voices called by two producers after a quick Stop and Start" — all three are older than this branch and none was seen by the owner — stand as deferred minors.
- **Final.** declined line "the fast-forward of phase2a-translation into main and the worktree" — both are in the rulings list for the owner — stands.
- **Final.** declined line "the real PC and the real YouTube player" — they can only be judged by running against them; the real run waits for the owner's go-ahead, and the Chrome check was made for the caption fetch — stands; costs whatever the PC does differently from the stand-ins (the one-token warm-up request, Opus written on Windows, the proxy's keep-alive).
- **Final.** declined line "the warm-up's one word resets the PC's 45-minute idle timer once per serve start" — it is one clip, not a mode change, and serve is started to dub — stands.
- **Final.** declined lines "the caption wait and the start-up hold share one 20 s cap" and "a second caption request queued behind a failing first can outlast the content script's 20 s" — with captions that take 16 s the hold runs out and the dub starts by catching up, which is the designed path — stand; costs the first sentences of a video whose captions were very slow.
- **Final.** declined line "two serve processes writing one log file" — the second cannot bind the port and exits before it logs a sentence — stands.
- **Final.** unledgered deviation named by the review — the scenario tests allow a clip 2.0 s (3.0 s with uneven costs) after its sentence where the plan said 0.5 s — a clip's lag holds the timeline's shift from clips that run long (the model's clips are 0.8 to 1.3 times their sentence), not only a late make; the unit tests keep 0.5 s for the make — costs a weaker bound in the whole-run tests.
- **Final.** unledgered deviation named by the review — tests/test_voice_pc.py and tests/test_translate_llamacpp.py read httpx's private pool to see the 60 s keep-alive — there is no public way to read it — costs a test that breaks when httpx renames it.
- **Final.** two older tests of a seek back over an unmade sentence were changed (test_segments_skipped_before_start_or_by_a_seek_get_no_record, test_after_a_seek_back_a_segment_the_playhead_passes_unproduced_is_dropped) — they pinned that the clips after the unmade sentence stay; they are now made again in their turn — costs the making of those clips a second time on such a seek, and in one case (a seek back into such a stretch, then forward into the middle of a sentence whose clip was discarded) that sentence is not heard.

## Deferred

- **Task 9.** the new page script was proven on the auto-captioned video only; the human-track video was checked with the installed script (three asks, all good), not with the new one.
- **Final.** a restart after the server comes back is a full Start: a video the owner had paused plays once the dub is ready.
- **Final.** two tabs both pressed while the server was down: when it comes up the first to connect dubs, which may not be the one that dubbed before.
- **Final.** the aim favours short sentences: in the always-slow simulation 60 of 92 sentences are dubbed, which is 46% of the speech.
- **Final.** after the 5 s wait for an engine that is down, the first try uses a failed translation made ahead before the wait: recovery costs one more sentence and 5 s.
- **Final.** a log file that becomes unwritable mid-run prints a traceback for every log line; dubbing goes on.
- **Final.** log gaps: a sentence the playhead passes unmade gets no line; "lost" is also written for a clip that a seek skipped; lines carry no session id, so an old session's warning can appear under the new one.
- **Final.** the economy rule has no ceiling: with clips 1.25 to 1.3 times their sentence and a thin look-ahead the simulation trails 10 to 18 s; the owner's seven sessions have clips 0.69 to 0.97 of the speech.
- **Final.** while catching up, 15 of 54 translations made ahead were for a sentence that was then not the next one.
- **Final.** the docstring of Session._deliver still names _make, which is now _translate and _speak.
- **Final.** page.js, content.js and sw.js have no test of their own (the reviewer ran them in node vm harnesses, which are not in the repository), and no test of Stop goes through the extension's PlayerControl.
- Playback rates other than 1.0, live mode, punctuation restoration, a per-video cache and
  pruning of played clips stay where the earlier lists left them
  (`docs/superpowers/plans/2026-11-01-phase1-deferred.md`).
- The Mac's output device is not followed when it changes while dubbing; the log names the
  device that was opened.
