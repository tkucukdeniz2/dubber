# dubber-tts: the voice on the PC

`tts_server.py` runs VoxCPM2 on qwen-pc's RTX 4080 SUPER with a cloned voice and answers
the Mac's dubber with one audio file per sentence: Ogg Opus when the Mac asks for it
(`"format": "opus"`, which it does), else WAV. Opus is about a twelfth of the size; from far
away that is most of a second for every sentence. A Mac from before Opus gets WAV as before,
and a service from before Opus answers WAV to a Mac that asks for Opus: both still work. It needs about 6.2 GB on the 4080 SUPER, so it
runs only in dubbing mode, when Qwen and ComfyUI are stopped and the translator runs on the 4070.

## Layout on the PC

| Path | What |
|---|---|
| `C:\AI\tts\venv` | Python 3.12 with PyTorch 2.11 (CUDA 12.8), `voxcpm` 2.0.3 and `triton-windows` 3.7 |
| `C:\AI\tts\models\VoxCPM2` | the model, from `openbmb/VoxCPM2` on Hugging Face |
| `C:\AI\tts\voices\owner.wav` and `owner.txt` | the owner's voice clip and its transcript |
| `C:\AI\tts\voices\kalin.wav`, `ipek.wav` and their `.txt` | the two designed voices, a man's and a woman's (`pc/voices` here); a voice is listed as soon as its two files are there |
| `C:\AI\tts\tts_server.py`, `start-tts.cmd` | this folder's two files |
| `C:\AI\tts\inductor-cache` | compiled GPU code; it makes later starts faster |
| `C:\AI\logs\tts.log` | the service's log |

Without `triton-windows`, PyTorch cannot compile the model on Windows and a second of speech
takes 1.4 to 2.6 s to make; compiled, it takes about 0.42 s.

## Start and stop

Do not start `start-tts.cmd` by hand. The mode service (`gpu_mod.py`, the `Qwen38-GpuMod` task)
starts and stops the voice service, so that it never shares the 4080 SUPER with Qwen or ComfyUI:

- `pc-dublaj` on the Mac (POST /dublaj): Qwen and ComfyUI stop, the translator in
  `C:\AI\config\models-dublaj.ini` starts on the RTX 4070 and dubber-tts on the 4080 SUPER.
  30 to 80 s with the compile cache, about 2.5 minutes without it. `pc-dublaj --zorla` restarts
  both even in dubbing mode, which is how a failed voice service is replaced. The switch gives up
  and returns to LLM mode when the translator does not start or exits within 15 s, the voice
  service reports itself failed or its process is gone after 30 s, or the two are not ready
  within 10 minutes.
- `pc-llm` or `pc-video`: the voice service stops first, then Qwen or ComfyUI start.
- 45 minutes without a sentence, or a voice service that died or reports itself failed (three
  sentences in a row that failed or got no audio within 55 s, such as after a driver reset): the
  mode service returns to LLM mode.
  A translator that died while the voice lives is restarted.

`tailscale serve --bg --http=8191 http://127.0.0.1:8191` (set up once) makes the service reachable
from the tailnet, as the other services are.

## Updating the service

The service's code is `pc/tts_server.py` in this repository. To put a new version on the PC:
copy the file in place of `C:\AI\tts\tts_server.py` (keep the old one beside it, as
`tts_server.py.yedek-<date>`), then run `pc-dublaj --zorla` on the Mac, which restarts the
service with the new file. Dubbing stops for the 30 to 80 s the model takes to load, and Qwen
is stopped if the PC was in LLM mode, so do it when neither is in use. Afterwards
`GET /status` lists what the service can answer in: `"formats": ["wav", "opus"]` once the
version with Opus runs.

## The translator looks at the video

With `voice = "auto"` the Mac asks the translator's model who presents each video, and shows it
the video's thumbnail and three frames. Gemma sees pictures only with its vision add-on, the line

    mmproj = C:\AI\models\mmproj-gemma-4-12B-it-F16.gguf

in the `[gemma4-12b]` table of `C:\AI\config\models-dublaj.ini` (`pc/models-dublaj.ini` here;
`models-video.ini` has had it all along). It was added on 2026-11-15; the file from before is
`models-dublaj.ini.yedek-20261115-012535`. The add-on costs about 0.2 GB on the RTX 4070, which the
fitted context pays for (71680 tokens instead of 92928). A picture is at most 256 tokens to the model,
because `ubatch-size = 256`; it sees coarsely. The preset is read when the translator starts: after a
change, `pc-dublaj --zorla` restarts it together with the voice service. Without the line the model
refuses pictures and every video is read in the man's voice.

## On the Mac

In `config.toml`: `[voice] engine = "pc"` and `voice = "owner"`, `"kalin"`, `"ipek"` or `"auto"`
(`uv run dubber voice <name>` writes it). `[pc] tts_url` holds the service's address; the key is the
PC key the translator uses.
