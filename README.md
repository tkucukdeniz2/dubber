# Dubber

[![tests](https://github.com/tkucukdeniz2/dubber/actions/workflows/tests.yml/badge.svg)](https://github.com/tkucukdeniz2/dubber/actions/workflows/tests.yml)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)

Dubber dubs English YouTube videos into Turkish on a Mac. It has two parts. A Chrome extension (`extension/`) reads the YouTube player and the video's English captions. A server on this Mac, `dubber serve` (`dubber/`), turns the captions into speech and plays it in time with the video, while the video's own sound is turned down to 20%.

## Where it stands

The whole path works: the extension, the server, the scheduling that keeps the voice in step with the video, and the metrics. The default voice is an English stand-in. It does not translate: the macOS voice Samantha reads the English captions as they are. That is enough to check the timing. For Turkish, see "First Turkish" below, and `pc/README.md` for the setup that dubs in a cloned voice on the PC (`pc-dublaj`, then `dubber serve`). Phase 2 is under way: whole-sentence translation is in, and pause and seek refinements, rate-aware mixing and a per-video cache are still to come.

## Install

You need a Mac, Chrome and [uv](https://docs.astral.sh/uv/).

```
uv sync
cp config.example.toml config.toml
```

`config.toml` is yours to change and is not committed. The example explains each setting, and you can use it as it is.

## Run the server

```
uv run dubber serve --config config.toml
```

It prints the engines it uses and its address, then logs what it receives:

```
engines: identity + say:Samantha
listening on ws://127.0.0.1:8765
```

Leave it running. Add `--verbose` to log every message. Press Ctrl-C to stop it. The extension notices and gives the video its volume back.

You can press the Dubber button before the server runs: the tab waits, and dubbing starts by itself when the server is up. The same happens when the server goes away and comes back.

What the server logs also goes to `dubber.log`, in the folder you started it from (the `log_path` setting; an empty value writes no file). It says when a session starts and ends and why, when the video is held and played again, and for each sentence where it was placed, how late and how far ahead, what the translation and the voice took, and on a line of its own the English that was translated and the Turkish that was spoken. A clip that came too late is marked `lost`. On captions without punctuation it also says which stretch was restored and how long that took. At 2 MB a new file is begun and three old ones are kept.

With the PC's engines the server warms them once it listens and whenever a YouTube tab connects: it has the translator load its model when none is loaded, and has the voice speak one word. It does this only while the PC's voice answers, which means the PC is in dubbing mode, and it never touches a model that is loaded.

## Load the extension

Follow `extension/README.md`: open `chrome://extensions`, turn on Developer mode, choose Load unpacked and pick the `extension` folder. Reload any YouTube tab that was already open.

## Start and stop

Open a YouTube video (a `youtube.com/watch?v=...` page) that has English captions. Automatic ones are fine. Click the Dubber button in the toolbar, or press Alt+Shift+D (the Option key on a Mac keyboard).

The player pauses, the video's volume drops to 20%, and a small bar appears at the top of the player. Once enough of the dub is ready, which should take no more than 15 seconds, the video plays and the voice follows it. Click the button or press the shortcut again to stop. The volume comes back and the bar goes away. `extension/README.md` describes the bar, and what happens when the server is not running.

## Metrics

The server writes one line for each dubbed sentence to `metrics.jsonl`, in the folder you started it from (the `metrics_path` setting). The file keeps growing across runs and is not committed. To summarize the last run:

```
uv run dubber stats
```

It shows how many sentences were dubbed and how many were dropped (never heard), the lag p50 and p90 (how many seconds after the narrator the voice began; 0 means in time), the time spent translating and speaking, and how far ahead of the video the dub was ready. Use `--metrics PATH` for another file and `--session ID` for an earlier run.

## Try it without Chrome

`tools/fake_extension.py` plays a pretend video against a running server. For example, with the captions saved in Phase 0:

```
uv run python tools/fake_extension.py bakeoff/captions/XP5k0bHmhqc.en.asr.json3 --seconds 30
```

It prints each status and subtitle the server sends, and a summary at the end. `--start SECONDS` begins later in the video. The server still plays the voice through the Mac's speakers, so turn the volume down first if you want.

To measure the engines without a server and without sound:

```
uv run python -m tools.dry_run bakeoff/captions/XP5k0bHmhqc.en.asr.json3 --config config.toml --seconds 240
```

It dubs four minutes of the video in real time with the engines of that config, plays nothing, writes no metrics, and prints how many sentences were dubbed and dropped, what a second of speech took the translator and the voice, how far ahead the dub ended, and every hold. The translator and the voice work at the same time, so the dub keeps up when the larger of the two figures is under 1. A caption file does not say which video it is of, so with `voice = "auto"` add `--video XP5k0bHmhqc`, the video's YouTube id; without it nothing is looked at and the man's voice reads.

## First Turkish

The default settings do not translate. To hear a first Turkish dub, translate on the PC and read the result with Yelda, the Turkish system voice. In `config.toml`, change these two settings (the comment below `[voice]` in `config.example.toml` shows the same thing):

```toml
[translator]
engine = "pc"

[voice]
engine = "say"
voice = "Yelda"
```

Before you start the server:

- The PC is on and reachable over Tailscale. Its address is `base_url` in the `[pc]` table.
- Its llama.cpp server is in LLM mode with a model loaded. Before every sentence Dubber asks the PC which model is loaded and uses that one, so a model you load while it runs is picked up and none is swapped out. If none is loaded it asks for `preferred_model`, and the PC has to load it.
- The PC's API key is in `~/.qwen-pc/api-key`, a text file that holds only the key, on one line (`api_key_file` in `[pc]` changes the path). The server refuses a file that holds anything else, and it never prints the key.

Start the server as before. It asks the PC for its list of models, once, which is a read-only request and not a translation, and then prints `engines: llamacpp + say:Yelda`. If the PC is off, or Tailscale is down on this Mac or on the PC, it stops at once with `engine error: cannot reach the PC at <address>: <reason>`. Check both, then start it again. If the PC drops out in the middle of a session, each sentence that fails to translate appears as an English subtitle with the reason on the bar, and nothing is spoken for it. The metrics count as dropped both that sentence and the ones the video passes while the PC is away.

Yelda is the compact voice that comes with macOS. The enhanced one sounds better. Download it in System Settings, Accessibility, Spoken Content, System Voice, Manage Voices, then set `voice = "Yelda (Enhanced)"`. `say -v '?'` lists the exact names.

Expect rough edges. A Turkish sentence can take longer to say than the English one. The voice speeds up to 1.2x to fit. When that is not enough the translator is asked once for a shorter phrasing. If that still does not fit, the dub falls behind until a silence or a faster clip makes it up.

## Choose the voice

```
uv run dubber voice         # shows the voice in config.toml
uv run dubber voice auto    # kalin or ipek, by who presents the video; run pc-dublaj first
uv run dubber voice kalin   # a deep, steady male voice on the PC; run pc-dublaj first
uv run dubber voice ipek    # a calm female voice on the PC; run pc-dublaj first
uv run dubber voice owner   # the owner's cloned voice on the PC; run pc-dublaj first
uv run dubber voice yelda   # Yelda, the Turkish system voice on this Mac
```

`kalin` is nobody's own voice: VoxCPM2 made its reference clip from a description ("a man in his fifties with a very deep, low-pitched bass voice, calm, steady and authoritative, like a documentary narrator"), and the PC clones from that clip. On the same four sentences it speaks at about 104 Hz where the owner's voice is at 186 Hz, and takes 18 % longer. The clip and its transcript are `pc/voices/kalin.wav` and `kalin.txt`; the PC's voice service reads them from `C:\AI\tts\voices`, and lists a voice as soon as its two files are there.

`ipek` was made the same way, from "sıcak, tok ve oturaklı bir kadın sesi; orta yaşlı, sakin, güven veren bir belgesel anlatıcısı". It speaks at about 240 Hz and takes as long as `kalin`. Nobody has heard either voice: both were chosen by measuring pitch, steadiness and pace.

With `auto` the PC's model is asked who presents each video. It is shown what YouTube shows to anyone without playing the video: the title, the channel, the thumbnail and three frames. A video that a woman presents is read in `ipek`, every other video in `kalin`: also one that does not show who presents it, such as an animation. The look takes about a second and a half while the dub prepares, and a video keeps the voice it began with. The log says what was decided (`... is read in the voice pc-voxcpm2:ipek, a woman presents it`). On 46 test videos it found all 20 women and all 21 men. It can still be wrong, most easily on a video about somebody who does not speak in it; `uv run dubber voice kalin` goes back to one voice for everything. The two voices can be changed with `male` and `female` under `[voice]`. `auto` needs `engine = "pc"` under both `[voice]` and `[translator]`, since the translator's model does the looking, and that model needs its vision add-on (see `pc/README.md`). A model that cannot see, such as TranslateGemma, leaves every video in `kalin`, with a warning in the log for each video. Stopping the dub and starting it again looks once more when a look failed.

It rewrites the two values of `[voice]` in `config.toml` and nothing else. A running server keeps its voice, so restart `dubber serve` afterwards.

## How sentences are translated

- **Whole sentences.** The captions are cut at sentence ends, including one inside a human subtitle cue. A title, an initial, a dotted abbreviation or an ellipsis is not a sentence end, and on a punctuated track only a pause of 1.5 s cuts. A sentence is split only past 60 words or 18 s, after a comma near its middle when it has one, because Turkish puts the verb last and half a sentence translates badly. `[segmenter]` in `config.toml` changes these limits.
- **Captions without punctuation.** Some auto-captions have not a single full stop, and nothing else in them says where a sentence ends: their words have no gaps, and a pause guessed from the word times is a sentence end one time in four. For such a track the translator's model on the PC is given the words a stretch at a time, ahead of the video, and copies them with punctuation; only its marks are taken, never a word of its own. The first stretch is 60 words (under a second), the later ones 150 (2 s for about a minute of speech). A jump to a place that is not restored yet begins there again, 30 words before it. If the model cannot do it (TranslateGemma only translates; the stand-in has no model; a reply that is not a copy) the words are cut by length instead, never right after a word such as "and" or "the" and where possible before one such as "so" or "but", and the log says so. `restore_punctuation = false` in `[translator]` switches the model's part off.
- **Context.** Each sentence goes to the translator with the three before it, which are never translated themselves. `context_sentences` in `[translator]` changes the number.
- **Glossary.** A `[glossary]` table in `config.toml` fixes how a term is rendered, for example `spice = "baharat"`. `config.example.toml` shows the form.
- **Shorter phrasing.** A clip too long for its slot even at 1.2x is translated once more, in fewer words, when at least 30 s of the dub are ready ahead (`lead_min` in `[scheduler]`) and the clip is not late already. With less ready it plays as it is, at 1.2x and past its slot, because a second translation and clip cost time the dub does not have then. The metrics mark a shortened sentence `shortened`.
- **Two engines at once.** While a sentence is voiced, the next one is translated. The translator and the voice run on different cards of the PC, so a sentence costs the longer of the two, not their sum.
- **TranslateGemma.** Its fixed instruction takes no context, glossary or request to shorten, so with it each sentence is translated alone and a long clip simply runs over.

## When the dub falls behind

- **Holds.** When the video reaches a sentence that is not ready, the server pauses it (the bar says `holding`) until 12 s are ready ahead, for 20 s at most.
- **Catching up.** After a hold that ran its 20 s the video plays on. The server then stops making the sentence under the playhead, which would be over before its clip is ready. It aims at the first sentence whose clip can be ready when the sentence begins, judged by how long the last one took. The sentences it jumps over are not dubbed (the bar says `catching up: 2 sentences skipped`, and the metrics count them as dropped), and the clips it makes start with their sentences, within a second or so. One reply that hangs is not taken for the engines' speed: two slow ones in a row are. Holds come back once a clip is ready 30 s ahead. Engines that stay slower than the speech dub the share they can, about half of it in the simulation.
- **An engine that is down.** After two sentences in a row have failed, the server tries again every 5 s and the bar says why, for example that the PC is not in dubbing mode. The dub goes on when the engine answers.
- **Pause and seek.** Pausing pauses the dub. A seek, or a scrub while paused, moves it: where the dub is ready it plays on, elsewhere the video is held while the dub is made. A hold that began while you had paused ends paused, and the video stays paused when you then stop dubbing. After a seek back over sentences that were skipped, the dub is made again from the first of them, in order.
- **Captions.** YouTube is given up to 16 s to send the captions. An empty answer is asked for again, five times at most, and a download that fails is tried once more. A track that was fetched is kept for the next start on that page.

## Known limits

- The default voice is the English stand-in.
- After a start or a seek inside a sentence, the dub begins at the next sentence. So a start a couple of seconds into a video misses its first sentence.
- On a track without punctuation the sentences are the model's: it sets fewer full stops than a person would (about seven for every ten of YouTube's on the one track measured) and joins the rest with commas. Nothing of it is kept: after a jump back the stretch is restored again.
- After a seek the player can be held for up to 20 seconds while the dub is made.
- The dub is played on the output device that was the Mac's default when the first clip played (the log names it). It does not follow a later change, for example to headphones: stop and start dubbing after you switch.
- The dub stutters at playback rates other than 1.0, and the extension keeps the speed at 1.25x or below while dubbing. The 0.9x catch-up slow-down is off (`slow_player` in `[scheduler]`) until the mixer follows the rate: today it could only stutter, without making up any of the delay.
- There is no per-video cache yet. Every run fetches the captions again, then translates and speaks everything again.
- The extension asks for the video's human English subtitles first and its automatic captions second. A video with neither plays on as it is, and the bar says `no usable captions`.
- One tab dubs at a time, and live streams wait for Phase 3.

## Tests

```
uv run pytest -q                              # the fast tests: no audio device, no network
uv run pytest -m slow tests/test_e2e.py -v    # about 15 s: the server, a fake extension and Samantha, in real time
uv run pytest -m slow tests/test_dry_run.py   # about 7 s: the dry run tool, in real time
node --test extension/tests/                  # the extension's helpers (Node 22)
```

The slow test opens no audio device and says nothing out loud. It needs the Samantha voice and skips itself without it. Tests marked `live` talk to the PC and are skipped unless you ask for them with `-m live`.

## More

- `docs/decisions/2026-11-robust-dub.md` tells what was seen over a long-distance link on 2026-11-14, what was changed for it and what was measured.
- `docs/decisions/2026-11-phase1-shell.md` is the checklist for the browser run, with room for the results.
- `docs/decisions/2026-11-phase0-results.md` holds the Phase 0 bake-off and its checklist.
- `dubber/` is the server, `extension/` the Chrome side, `tools/` the helper scripts (the fake extension and the Phase 0 bake-off tools), and `tests/` the tests.

## The PC side

The PC part is optional. `pc/` holds what runs on a Windows PC with two NVIDIA GPUs: the voice
service (`tts_server.py`, VoxCPM2), the llama.cpp preset used while dubbing
(`models-dublaj.ini`) and a small mode service (`gpu_mod.py`) that shares the larger GPU between
the language model, ComfyUI and the voice. `pc-dublaj`, mentioned above, is the author's shell
helper on the Mac that asks that mode service to switch to dubbing (`POST /dublaj`). `pc/README.md` describes the setup.

## Author and license

Tarık Küçükdeniz ([ORCID 0000-0002-6670-1809](https://orcid.org/0000-0002-6670-1809)). MIT
license, see `LICENSE`. Dubber is a personal tool for watching videos with a Turkish voice; it
does not download or redistribute videos, and the captions it reads are fetched by the
browser for the video being watched.
