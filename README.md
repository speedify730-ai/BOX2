# Myanmar Video Dubbing Studio (Web App)

Upload an mp4 (any spoken language) on the web page and get back the
same video with **Myanmar (Burmese) dubbed audio**, subtitles, an
optional title overlay, watermark, logo and background music - all
configurable from the browser, no Telegram required.

```
mp4 in --> extract audio --> Groq Whisper (speech-to-text)
       --> Gemini (translate to natural spoken Myanmar)
       --> Gemini TTS (Myanmar speech, 6 selectable voices)
       --> ffmpeg (mux new audio onto the video; optional flip /
           aspect-ratio crop / burned-in Myanmar subtitles / auto-trim)
       --> mp4 out
```

## Deploy: GitHub → Railway → Web

1. **Push this project to a GitHub repo** (or fork/upload it there).
2. On [Railway](https://railway.app), **New Project → Deploy from GitHub
   repo**, and pick this repo. Railway builds it from the included
   `Dockerfile` automatically - no extra config needed.
3. Once deployed, open **Settings → Networking** on the Railway service
   and click **Generate Domain** to get a public `https://...up.railway.app`
   URL. That's the website.
4. That's it - no environment variables are required to deploy. Each
   visitor pastes their **own** Gemini/Groq API keys into the page
   before dubbing (get one free at
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and
   [console.groq.com/keys](https://console.groq.com/keys)); keys are
   only used for that one job and are never written to disk.

Optional environment variables (Railway → your service → **Variables**):

| Variable | Default | Purpose |
|---|---|---|
| `MAX_UPLOAD_MB` | `300` | Largest video the web form will accept |
| `MAX_CONCURRENT_JOBS` | `2` | How many dubbing jobs can run at once (raise only if your Railway plan has the CPU/RAM for it - ffmpeg + TTS rendering is heavy) |
| `JOB_TTL_SECONDS` | `21600` (6h) | How long a finished job's output file is kept before automatic cleanup |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Groq transcription model |
| `GEMINI_TEXT_MODEL` | `gemini-flash-latest` | Model used for translation |
| `GEMINI_TTS_MODEL` | `gemini-2.5-flash-preview-tts` | Model used for speech |
| `PROXY_BASE_URL` | *(unset)* | Only set this if Gemini/Groq are blocked from where Railway hosts the app - see "About the proxy" below |

To run it locally instead:

```bash
pip install -r requirements.txt
uvicorn web:app --reload
# open http://127.0.0.1:8000
```

## Still want the Telegram bot?

`bot.py` (the original Telegram front-end) is still in this repo and
untouched - it uses the exact same `processor.py` pipeline as the web
app. It's just no longer what `entrypoint.sh` starts by default. To
run it instead: set a `BOT_TOKEN` environment variable and run
`python3 bot.py` (locally, or by changing the Dockerfile `CMD` /
`entrypoint.sh` back to it). The rest of this README below (voices,
subtitle styling, Local Bot API server for >20MB files, etc.) was
written for that Telegram version, but every option it describes -
voice, subtitle color/font/size, watermark, logo, BGM, title overlay,
resolution - is exposed in the web form too.

---

# Telegram Video Recap & Myanmar Dub Bot

Send this bot an mp4 (any spoken language) and it sends back the same
video with **Myanmar (Burmese) dubbed audio**:

```
mp4 in --> extract audio --> Groq Whisper (speech-to-text)
       --> Gemini (translate to natural spoken Myanmar)
       --> Gemini TTS (Myanmar speech, 6 selectable voices)
       --> ffmpeg (mux new audio onto the video; optional flip /
           aspect-ratio crop / burned-in Myanmar subtitles / auto-trim)
       --> mp4 out
```

## Myanmar subtitle shaping and timing

Myanmar subtitles are burned with FFmpeg's **libass** subtitle renderer, not
`drawtext` or naive FFmpeg text rendering. libass handles complex Myanmar
script shaping through the HarfBuzz/Fribidi stack, while the bundled Myanmar
fonts are supplied through `fontsdir`.

The SRT is written as UTF-8 NFC-normalized text. TTS requests are also tracked
by generated-audio chunk duration, and subtitle sentences are timed inside
those measured chunks rather than distributing all sentences against one
global duration. This reduces timing drift on long recap videos.

The final mux step rebases both audio/video timestamps to zero, enforces the
target duration, and verifies the finished file before returning success.

## What was fixed in this version

- **Real dubbing pipeline.** The previous build's TTS step was a stub
  (`ok, err = True, None`) and the final ffmpeg command just copied the
  original audio through unchanged — so the output video never actually
  had Myanmar audio. This version calls Gemini's TTS model for real,
  builds a WAV file from the returned audio, and muxes it onto the video.
- **"Check Keys" / error messages no longer break buttons.** Error text
  from Google/Groq (which can contain `*`, `_`, or `` ` ``) was being
  inserted straight into a Telegram Markdown message. Any stray Markdown
  character there makes Telegram reject the message with a "can't parse
  entities" error, which looks like the button silently doing nothing.
  These messages now send as plain text.
- **Key-check no longer freezes the bot for other users** — it now runs
  in a background thread instead of blocking the bot's event loop.
- **Sending non-video files (PDFs, photos, etc.) no longer crashes the
  pipeline** — only actual video files are accepted now, with a friendly
  message otherwise.
- **Large files fail gracefully** — the bot checks the file size up
  front instead of crashing partway through, with a message that always
  matches the real limit currently in effect (20 MB on Telegram's cloud
  API, or `MAX_DOWNLOAD_MB` if you self-host — see "Bigger files (Local
  Bot API server)" below).
- **6 Myanmar TTS voices**, grouped male/female with Myanmar names, are
  defined once in `voices.py` so the button labels and the actual voice
  used for dubbing can never fall out of sync (see below).
- File names for temp downloads are now unique per run (`uuid4`), so two
  people processing videos in the same second can no longer collide.

## New in this version

- **Subtitle Color picker** — 10 colours (Yellow, White, Cyan, Lime, Hot
  Pink, Orange, Red, Light Blue, Gold, Magenta) for the burned-in
  Myanmar subtitles.
- **Subtitle Font picker** — 8 options defined in `styles.py`
  (`FONT_OPTIONS`): Default, Padauk, Padauk Book, Padauk Book Bold,
  Noto Sans Myanmar UI (Bold), Phantee (Hand Written), Myanmar
  Universal, and Akkhayar Robo. The 7 non-Default `.ttf` files all
  ship in this repo's `fonts/` folder already. "Default" (and the
  fallback for any font whose `.ttf` isn't found in `SUBTITLE_FONTS_DIR`)
  uses the bundled Padauk Book font unless you set `SUBTITLE_FONT_PATH`
  to point at a real font file of your own — see the environment
  variables table below. If a selected font's file genuinely isn't
  found, rendering falls back to that default instead of failing the
  render.
- **Subtitle Font Size picker** — 16 sizes from 8 to 60 (default 35).
- **Auto Blur Mask** toggle — blurs the bottom ~20% of the *original*
  video (where a source video's own baked-in captions often sit)
  before the new Myanmar subtitles are drawn on top. On by default;
  toggle it off from the main menu.
- All four are defined once in `styles.py`, the same pattern `voices.py`
  already used for the TTS voices, so the button labels and what
  actually gets rendered can't drift out of sync.
- **Video Title Overlay** toggle — Gemini generates a short Myanmar
  title from the translated narration and burns it onto the top of the
  video via ffmpeg `drawtext`. On by default; has its own submenu (⬅️
  from the main menu) with its own Color / Font / Size pickers, reusing
  the same palettes as the subtitles. If title generation fails for any
  reason, the job still completes — just without the overlay.
- **Video Resolution picker** — export at 720p (default) or 1080p.
  Scales the chosen aspect-ratio preset's dimensions accordingly.
- **Text Watermark** — `/setwm [text]` burns a small, semi-transparent
  watermark into the bottom-right corner of every video you export.
  Running `/setwm` with no text (or the "🗑️ Watermark ဖျက်မည်" button)
  clears it.
- **Logo overlay** — `/setlogo` then send an image; it's scaled to
  ~18% of the output width and pinned to the top-right corner of every
  export from then on. `/removelogo` (or the button) removes it.
- **Background music (BGM)** — `/setbgm` then send an audio file; it's
  looped automatically to match the dubbed narration's length and
  mixed in underneath it (turned down so the narration stays clearly
  audible). `/removebgm` (or the button) removes it.
- **Help button** — "ℹ️ အသုံးပြုရန်အချက်များ" on the main menu shows a
  quick reference for the Watermark / Logo / BGM commands above.
- Logo and BGM files are saved per-user under `assets/logos/` and
  `assets/bgm/` (like `api_keys.json`, this is local disk — ephemeral
  on Railway unless you attach a persistent volume; see "Deployment on
  Railway" below).

## The 6 voices

Google's Gemini TTS ships 30 prebuilt voices, documented by *style*
(Firm, Clear, Warm, ...) rather than gender. `voices.py` picks 6 clear
ones and groups them:

| Myanmar name | Gender | Gemini voice | Style |
|---|---|---|---|
| ကျော်ဇင် | 👨 Male | Charon | Informative / clear |
| မင်းသူ | 👨 Male | Orus | Firm |
| ဇေယျာ | 👨 Male | Iapetus | Clear |
| သီရိ | 👩 Female | Kore | Firm / clear |
| နှင်းဦး | 👩 Female | Leda | Youthful |
| ခင်ဇာ | 👩 Female | Erinome | Clear |

Want different names or voices? Edit `voices.py` — both the Telegram
menu and the TTS call read from that one file.

**Multi Voice** toggle: alternates the selected voice with a
complementary opposite-gender voice sentence-by-sentence, using
Gemini's built-in two-speaker TTS. It's a stylistic alternation, not
real speaker diarization (Groq's transcription doesn't return speaker
labels), so treat it as "more dynamic narration," not "detects who's
speaking."

## Deployment on Railway

1. **Create a new project** on Railway and connect this repo (or
   upload the zip).
2. **Set environment variables:**
   - `BOT_TOKEN` — your Telegram bot token (from @BotFather). Required.
   - `PROXY_BASE_URL` — optional, see "About the proxy" below. Leave
     unset unless you specifically need it.
3. Railway builds from the included `Dockerfile` and starts the bot
   automatically (see `railway.json`).
4. In the Telegram chat: `/start`, add at least one Gemini key and one
   Groq key via the 🔑 buttons, then send an mp4.

Keys are cached in `api_keys.json` on the container's local disk. On
Railway that disk is **ephemeral** — keys will be lost on redeploy
unless you attach a persistent volume mounted at the app's working
directory.

## Bigger files (Local Bot API server)

By default this bot talks to Telegram's shared cloud Bot API
(`api.telegram.org`), which can only **download** files up to 20 MB via
`getFile` — a limit Telegram enforces server-side, not something this
code controls. Videos just over that (e.g. a 20.9 MB forward) will fail
with a "File is too big" error from Telegram itself, even though
Telegram's own apps show the file as fine.

To support bigger files (up to 2000 MB), the container can run its own
copy of Telegram's [Local Bot API
server](https://github.com/tdlib/telegram-bot-api) alongside the bot
(`entrypoint.sh` starts it automatically before `bot.py` if configured):

1. Get an **api_id** and **api_hash** for your own Telegram application
   at <https://my.telegram.org/apps> (log in with any phone number —
   this is separate from your bot token, and free).
2. Set these environment variables on Railway (or wherever you deploy):
   - `TELEGRAM_API_ID`
   - `TELEGRAM_API_HASH`
   - `MAX_DOWNLOAD_MB` (optional, default `100` once the above two are
     set — raise this up to `2000` if you want)
3. Redeploy. On startup, `entrypoint.sh` launches
   `telegram-bot-api --local` on `127.0.0.1:8081` and waits for it to be
   ready, then points `bot.py` at it instead of `api.telegram.org`.

Leave `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` unset and everything works
exactly as before, capped at 20 MB.

## About the proxy

The earlier version hard-coded every API call to go through a
third-party Cloudflare Worker (`vpn-my-proxy.speedify730.workers.dev`)
that isn't yours. That means the operator of that worker could see and
log your Gemini/Groq API keys and the audio being translated, whether
or not you actually need the bypass.

This version calls Google and Groq **directly by default**. If Gemini
or Groq are genuinely blocked from where you host the bot, set
`PROXY_BASE_URL` to a proxy you personally run or fully trust, e.g.
`PROXY_BASE_URL=https://your-own-worker.example.com/?` (the code
appends the target URL right after it, same convention as before).

## Environment variables (all optional except `BOT_TOKEN`)

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token (required) |
| `TELEGRAM_API_ID` | *(unset)* | From my.telegram.org — enables the self-hosted Local Bot API server, see above |
| `TELEGRAM_API_HASH` | *(unset)* | From my.telegram.org — paired with `TELEGRAM_API_ID` above |
| `MAX_DOWNLOAD_MB` | `20`, or `100` once the two above are set | Max input video size the bot will accept. Values over 20 are ignored (and logged) unless the Local Bot API server is active |
| `PROXY_BASE_URL` | *(unset — direct calls)* | Prefix for API requests, see above |
| `GROQ_MODEL` | `whisper-large-v3-turbo` | Groq transcription model |
| `GEMINI_TEXT_MODEL` | `gemini-flash-latest` | Model used for translation |
| `GEMINI_TTS_MODEL` | `gemini-2.5-flash-preview-tts` | Model used for speech |
| `TTS_CHUNK_CHARS` | `1400` | Max Myanmar characters sent per TTS call |
| `SUBTITLE_FONT_PATH` | `fonts/PadaukBook-Regular.ttf` (bundled) | Font used for the "Default" picker option and as the fallback if a selected font's file is missing. Point this at a real font file of your own (e.g. a system-installed Myanmar font) to override it - if the path you set doesn't exist in the container, this silently falls back to the bundled default rather than failing |
| `SUBTITLE_FONTS_DIR` | `fonts` | Folder to look in for the two optional custom subtitle fonts |

## Known limitations

- Subtitle timing is estimated proportionally by sentence length
  against the dubbed audio's total duration (Groq's basic
  transcription response doesn't include word timestamps), so subtitle
  timing is approximate, not frame-accurate.
- "Multi Voice" alternates speakers by sentence turn, not by detecting
  who actually spoke in the source video.
- The standard Telegram Bot API limits downloads to 20 MB no matter what
  `MAX_DOWNLOAD_MB` is set to — that ceiling is enforced by Telegram's
  own servers on `getFile`, not by this code. See "Bigger files (Local
  Bot API server)" above to lift it.
- Title generation is a best-effort extra Gemini call — on failure it's
  skipped silently (logged, not shown to the user) rather than failing
  the whole dub.

## Files

- `bot.py` — Telegram bot: menus, buttons, key storage, upload/download.
- `processor.py` — the dubbing pipeline (transcribe → translate → TTS →
  ffmpeg render).
- `voices.py` — the 6-voice catalog shared by both files above.
- `styles.py` — subtitle colour/font/size options + Auto Blur default,
  shared the same way.
- `Dockerfile` — container image (ffmpeg + Myanmar font + deps + the
  optional Local Bot API server binary).
- `entrypoint.sh` — starts the Local Bot API server (if configured),
  then always starts `bot.py`.
- `requirements.txt` — Python dependencies.
- `railway.json` — Railway deploy config.


### PNG Burmese shaping

PNG subtitle/title rendering uses Pillow's RAQM layout engine when Pillow is
built with `libraqm-dev`, `libharfbuzz-dev` and `libfribidi-dev`. The helper
`render_burmese_png()` NFC-normalizes Myanmar text and renders it with RAQM
so e-vowels, medials, kinzi and stacked consonants are shaped before rasterization.
The final video subtitle burn-in still uses FFmpeg/libass, which also uses the
HarfBuzz/Fribidi shaping path.


## Railway Pillow build fix

The Docker image installs Pillow from a prebuilt manylinux wheel instead of forcing a source build. The previous `--no-binary=Pillow` setting could make Railway compile Pillow inside the small build environment and fail during the build. The image now verifies `PIL.features.check_feature("raqm")` during Docker build, so deployment stops immediately if complex-text shaping support is missing.


## Burmese subtitle rendering

Myanmar SRT subtitles are no longer passed to FFmpeg `drawtext`, `subtitles`,
or `ass`. Each SRT cue is normalized to Unicode NFC and rasterized by
PangoCairo using HarfBuzz/FriBidi into a transparent PNG. FFmpeg only overlays
those PNG layers at the original SRT timestamps. This avoids code-point-order
rendering problems with `ေ`, medials, kinzi and stacked consonants.

The Docker image installs Pango/PangoCairo, HarfBuzz, FriBidi and registers
the bundled Myanmar fonts with Fontconfig. Temporary subtitle PNGs are
removed automatically after every render.


## Railway fix: PangoCairo foreign Cairo Context

The Pango renderer uses Debian `/usr/bin/python3`. The image now installs `python3-gi-cairo` and the renderer explicitly registers the pycairo foreign type before importing PangoCairo. A Docker build-time smoke test verifies that `PangoCairo.create_layout(cairo.Context(...))` works, preventing the `KeyError: could not find foreign type Context` deployment/runtime error.
