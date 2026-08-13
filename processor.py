# -*- coding: utf-8 -*-
"""
Core video-dubbing pipeline:

    mp4 (any language) --> extract audio --> Groq Whisper (transcribe)
    --> Gemini (translate to Myanmar) --> Gemini TTS (Myanmar speech)
    --> ffmpeg (mux new audio back onto the video, optional flip / size /
        subtitles / auto-trim) --> mp4 (Myanmar dub)

Every network call rotates through all saved API keys and falls through
to the next one on a rate limit or error, so one dead/limited key never
blocks a whole run.
"""

import os
import re
import time
import uuid
import wave
import base64
import logging
import tempfile
import subprocess
import unicodedata
import shutil

import requests
from PIL import Image, ImageDraw, ImageFont

from voices import get_voice_by_id, pair_voice
from styles import (
    DEFAULT_COLOR_ID, DEFAULT_FONT_ID, DEFAULT_FONT_SIZE, DEFAULT_AUTO_BLUR,
    get_color_by_id, get_font_by_id,
)

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------

# Off by default. Only set PROXY_BASE_URL if Google/Groq are unreachable
# from where the bot is hosted. NOTE: whatever you put here can see your
# API keys and the audio/video content passing through it - only point
# this at a proxy you run yourself or fully trust.
PROXY_BASE_URL = os.environ.get("PROXY_BASE_URL", "").strip()

GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-flash-latest")
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")

# Roughly how much Myanmar text (in characters) to send per TTS call.
# Keeps each request well under the model's context window and avoids
# the audio-quality drift Google notes for very long single TTS outputs.
TTS_CHUNK_CHARS = int(os.environ.get("TTS_CHUNK_CHARS", "1400"))

# FFmpeg is intentionally kept inside a predictable process/thread budget.
# Some small hosting plans terminate a process with SIGKILL when ffmpeg
# creates too many worker threads at once. This does not change the video
# features or encoding settings; it prevents the host from killing a valid
# render before it can finish. Override only when the host has more memory.
FFMPEG_THREADS = max(1, int(os.environ.get("FFMPEG_THREADS", "2")))
FFMPEG_FILTER_THREADS = max(1, int(os.environ.get("FFMPEG_FILTER_THREADS", "1")))

# Directory the optional custom subtitle/title fonts live in. Every font
# listed in styles.FONT_OPTIONS that has a "file" is expected to be a real
# .ttf dropped in here - this repo ships 7 of them already (Padauk,
# Padauk Book (+Bold), Noto Sans Myanmar UI Bold, Phantee, Akkhayar Robo,
# Myanmar Universal). If the file for a selected font isn't found here,
# rendering falls back to the default font below instead of failing the
# whole job.
FONTS_DIR = os.path.abspath(os.environ.get("SUBTITLE_FONTS_DIR", "fonts"))

# Font used when the user picks "Default" (styles.FONT_OPTIONS id="default",
# file=None), or as the safety-net fallback everywhere else. SUBTITLE_FONT_PATH
# lets you point this at a real system-installed Myanmar font (e.g. if your
# base image already has "Noto Sans Myanmar" via fontconfig) - but if that env
# var isn't set, or is set to a path that doesn't actually exist in this
# container, fall back to the Padauk Book font bundled in FONTS_DIR (same
# font as DEFAULT_FONT_ID in styles.py) rather than assuming a system font
# that may not be installed. This is what actually prevents the "square
# box" / tofu failure mode for the default option: it only ever points at a
# font file we've verified is really there.
_env_font_path = os.environ.get("SUBTITLE_FONT_PATH", "").strip()
if _env_font_path and os.path.exists(_env_font_path):
    FONT_PATH = _env_font_path
    DEFAULT_FONT_FAMILY = "Noto Sans Myanmar"
else:
    FONT_PATH = os.path.join(FONTS_DIR, "PadaukBook-Regular.ttf")
    DEFAULT_FONT_FAMILY = "Padauk Book"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

proc_logger = logging.getLogger("processor")
proc_logger.setLevel(logging.INFO)
if not proc_logger.handlers:
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler("logs/processor.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    proc_logger.addHandler(fh)


def _proxied(url):
    if not PROXY_BASE_URL:
        return url
    return f"{PROXY_BASE_URL}{url}"


# ---------------------------------------------------------------------------
# Key rotation - spreads load evenly across saved keys instead of always
# hammering the first one until it hits a rate limit.
# ---------------------------------------------------------------------------
_rotation_state = {"gemini": 0, "groq": 0}


def _rotated(keys, kind):
    if not keys:
        return keys
    n = len(keys)
    start = _rotation_state[kind] % n
    _rotation_state[kind] = (start + 1) % n
    return keys[start:] + keys[:start]


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe helpers
# ---------------------------------------------------------------------------

def _run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        proc_logger.error(
            "Command failed (exit %s): %s\n%s", result.returncode, " ".join(cmd), stderr[-2000:]
        )
        if result.returncode < 0:
            # Negative returncode = killed by a signal (SIGKILL from an
            # out-of-memory kill, SIGSEGV from a decoder/filter crash,
            # etc). In this case ffmpeg never gets a chance to print its
            # own error line, so the tail of stderr is just whatever
            # routine metadata/progress output happened to be last -
            # not the actual cause. Surface that distinction instead of
            # showing the misleading boilerplate.
            import signal
            try:
                sig_name = signal.Signals(-result.returncode).name
            except ValueError:
                sig_name = str(-result.returncode)
            raise RuntimeError(
                f"ffmpeg ကို process kill ခံရသည် (signal {sig_name}). "
                "အများအားဖြင့် server memory ကုန်သွားလို့ ဖြစ်တတ်ပါသည် - "
                "video ကို ပိုသေးအောင်/တိုအောင် လုပ်ကြည့်ပါ၊ "
                "ဒါမှမဟုတ် hosting service ရဲ့ memory limit ကို တိုးပေးပါ။"
            )
        raise RuntimeError(stderr[-500:] or "ffmpeg error")
    return result


def get_duration(path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def has_audio_stream(path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())


def _probe_stream(path, selector):
    """Return stable ffprobe stream metadata used by the final sync check.

    The renderer deliberately does not trust container-level duration alone:
    MP4/MOV files can have different stream durations, non-zero start times,
    VFR video, or encoder delay. The sync fix below therefore verifies the
    actual output video/audio streams independently.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", selector,
        "-show_entries",
        "stream=index,codec_type,codec_name,duration,start_time,time_base,"
        "avg_frame_rate,r_frame_rate,sample_rate,channels",
        "-of", "json", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {(result.stderr or '').strip()[-500:]}")
    try:
        data = __import__("json").loads(result.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"ffprobe returned invalid metadata: {exc}") from exc
    streams = data.get("streams") or []
    if not streams:
        return None
    return streams[0]


def _float_or_zero(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fps_from_probe(stream):
    if not stream:
        return 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "N/A"):
            continue
        try:
            num, den = raw.split("/", 1)
            if float(den):
                return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            pass
    return 0.0


def _verify_audio_video_sync(output_path, expected_duration=None):
    """Verify the rendered file before it is sent back to Telegram.

    A successful ffmpeg exit is not enough for a dubbing job: a mux can still
    contain a non-zero stream start, mismatched stream durations, or malformed
    timing metadata. Treat anything outside one video frame (or 20 ms when
    FPS metadata is unavailable) as a real sync failure.
    """
    video = _probe_stream(output_path, "v:0")
    audio = _probe_stream(output_path, "a:0")
    if not video:
        raise RuntimeError("Sync verification failed: output video stream is missing")
    if not audio:
        raise RuntimeError("Sync verification failed: output audio stream is missing")

    video_duration = _float_or_zero(video.get("duration"))
    audio_duration = _float_or_zero(audio.get("duration"))
    video_start = _float_or_zero(video.get("start_time"))
    audio_start = _float_or_zero(audio.get("start_time"))
    fps = _fps_from_probe(video)

    # AAC mux/encoder timing can differ by a few milliseconds. Allow one
    # complete video frame plus a small codec timestamp margin, but never
    # accept multi-frame drift.
    frame_tolerance = (1.0 / fps) if fps > 0 else 0.020
    tolerance = max(frame_tolerance + 0.005, 0.020)

    duration_delta = abs(video_duration - audio_duration)
    expected_delta = (
        abs(video_duration - expected_duration)
        if expected_duration and expected_duration > 0
        else 0.0
    )

    proc_logger.info(
        "Sync verification: video=%.6fs audio=%.6fs delta=%.6fs "
        "video_start=%.6fs audio_start=%.6fs fps=%.6f timebase=%s "
        "audio_rate=%s codec_v=%s codec_a=%s",
        video_duration, audio_duration, duration_delta,
        video_start, audio_start, fps,
        video.get("time_base"), audio.get("sample_rate"),
        video.get("codec_name"), audio.get("codec_name"),
    )

    if abs(video_start) > 0.001 or abs(audio_start) > 0.001:
        raise RuntimeError(
            "Sync verification failed: output stream does not start at PTS 0 "
            f"(video={video_start:.6f}s, audio={audio_start:.6f}s)"
        )

    if duration_delta > tolerance:
        raise RuntimeError(
            "Sync verification failed: audio/video duration drift is "
            f"{duration_delta:.6f}s (allowed {tolerance:.6f}s)"
        )

    if expected_duration and expected_duration > 0 and expected_delta > tolerance:
        raise RuntimeError(
            "Sync verification failed: output duration differs from target by "
            f"{expected_delta:.6f}s (target {expected_duration:.6f}s)"
        )

    if not audio.get("sample_rate"):
        raise RuntimeError("Sync verification failed: audio sample rate is missing")
    if not video.get("time_base"):
        raise RuntimeError("Sync verification failed: video timebase is missing")

    return {
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "duration_delta": duration_delta,
        "fps": fps,
        "video_timebase": video.get("time_base"),
        "audio_sample_rate": audio.get("sample_rate"),
    }


def extract_audio(video_path, audio_path):
    _run(["ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-y", audio_path])


# ---------------------------------------------------------------------------
# 1) Transcribe original audio -> text (Groq Whisper)
# ---------------------------------------------------------------------------

def transcribe_audio(audio_path, groq_keys):
    if not groq_keys:
        return None, "Groq API Key မထည့်ရသေးပါ"
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    last_err = "No keys provided"
    for key in _rotated(groq_keys, "groq"):
        try:
            headers = {"Authorization": f"Bearer {key}"}
            with open(audio_path, "rb") as f:
                files = {
                    "file": (os.path.basename(audio_path), f),
                    "model": (None, GROQ_TRANSCRIBE_MODEL),
                }
                response = requests.post(_proxied(url), headers=headers, files=files, timeout=120)
            if response.status_code == 429:
                last_err = "Groq Rate Limit (429) - နောက် key ဖြင့် ပြန်စမ်းနေသည်"
                continue
            if response.status_code != 200:
                last_err = f"Groq Error ({response.status_code}): {response.text[:300]}"
                continue
            text = response.json().get("text", "")
            return text, None
        except Exception as e:
            last_err = f"Groq Error: {str(e)}"
            continue
    return None, last_err


# ---------------------------------------------------------------------------
# 2) Translate -> natural spoken Myanmar (Gemini)
# ---------------------------------------------------------------------------

def translate_to_myanmar(text, gemini_keys):
    if not gemini_keys:
        return None, "Gemini API Key မထည့်ရသေးပါ"

    prompt = (
        "You are a professional dubbing translator. Translate the following transcript "
        "into natural, spoken Myanmar (Burmese) suitable for voice dubbing. Keep the "
        "meaning faithful, use natural everyday spoken Myanmar phrasing (not overly "
        "literary), and keep sentence lengths close to the original so the dubbed audio "
        "pacing matches. Reply with ONLY the Myanmar translation text - no notes, no "
        "quotation marks, no explanations.\n\nTranscript:\n" + text
    )

    last_err = "No keys provided"
    for key in _rotated(gemini_keys, "gemini"):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            response = requests.post(_proxied(url), json=payload, timeout=60)
            if response.status_code == 429:
                last_err = "Gemini Rate Limit (429) - နောက် key ဖြင့် ပြန်စမ်းနေသည်"
                continue
            if response.status_code != 200:
                last_err = f"Gemini Translation Error ({response.status_code}): {response.text[:300]}"
                continue
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                last_err = f"Gemini did not return a translation ({data.get('promptFeedback', '')})"
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            myanmar_text = "".join(p.get("text", "") for p in parts).strip()
            if not myanmar_text:
                last_err = "Gemini returned an empty translation"
                continue
            return myanmar_text, None
        except Exception as e:
            last_err = f"Gemini Translation Error: {str(e)}"
            continue
    return None, last_err


# ---------------------------------------------------------------------------
# 2b) Generate a short on-video title overlay (Gemini)
# ---------------------------------------------------------------------------

def generate_title(text_myan, gemini_keys):
    """Ask Gemini for a short, punchy Myanmar title to burn onto the top
    of the video, based on the already-translated dub text. Best-effort:
    on any failure the caller should just skip the title overlay rather
    than fail the whole dubbing job over it."""
    if not gemini_keys:
        return None, "Gemini API Key မထည့်ရသေးပါ"

    prompt = (
        "You are writing a short, catchy on-screen title overlay for a "
        "short-form Myanmar-dubbed video, based on its narration below. "
        "Reply with ONLY the Myanmar title text - a single short line "
        "(ideally under 12 words), no quotation marks, no hashtags, no "
        "explanations.\n\nNarration:\n" + text_myan
    )

    last_err = "No keys provided"
    for key in _rotated(gemini_keys, "gemini"):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TEXT_MODEL}:generateContent?key={key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            response = requests.post(_proxied(url), json=payload, timeout=60)
            if response.status_code == 429:
                last_err = "Gemini Rate Limit (429) - နောက် key ဖြင့် ပြန်စမ်းနေသည်"
                continue
            if response.status_code != 200:
                last_err = f"Gemini Title Error ({response.status_code}): {response.text[:300]}"
                continue
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                last_err = f"Gemini did not return a title ({data.get('promptFeedback', '')})"
                continue
            parts = candidates[0].get("content", {}).get("parts", [])
            title = "".join(p.get("text", "") for p in parts).strip().strip('"').strip("'")
            if not title:
                last_err = "Gemini returned an empty title"
                continue
            return title, None
        except Exception as e:
            last_err = f"Gemini Title Error: {str(e)}"
            continue
    return None, last_err


# ---------------------------------------------------------------------------
# 3) Text -> Myanmar speech (Gemini TTS)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[။!?\.])\s+")


def split_sentences(text):
    text = unicodedata.normalize("NFC", text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts if parts else [text]


def _chunk_sentences(sentences, max_chars=TTS_CHUNK_CHARS):
    """Group sentences into chunks under max_chars, remembering each
    chunk's starting sentence index (needed to keep Speaker1/Speaker2
    alternation continuous across chunk boundaries in multi-voice mode)."""
    chunks, starts = [], []
    current, current_len, start_idx = [], 0, 0
    for i, s in enumerate(sentences):
        if current and current_len + len(s) > max_chars:
            chunks.append(current)
            starts.append(start_idx)
            current, current_len, start_idx = [], 0, i
        current.append(s)
        current_len += len(s)
    if current:
        chunks.append(current)
        starts.append(start_idx)
    return chunks, starts


def _decode_tts_response(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return None, None, f"Gemini TTS did not return audio ({data.get('promptFeedback', '')})"
    parts = candidates[0].get("content", {}).get("parts", [])
    for p in parts:
        inline = p.get("inlineData") or p.get("inline_data")
        if inline and inline.get("data"):
            mime = inline.get("mimeType", "audio/L16;rate=24000")
            m = re.search(r"rate=(\d+)", mime)
            rate = int(m.group(1)) if m else 24000
            pcm = base64.b64decode(inline["data"])
            return pcm, rate, None
    return None, None, "Gemini TTS response did not contain audio data"


def _write_wav(path, pcm, rate):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def generate_myanmar_tts(text, voice_id, gemini_keys, output_path, multi_voice=False, progress_cb=None, return_timings=False):
    if not gemini_keys:
        return (False, "Gemini API Key မထည့်ရသေးပါ", []) if return_timings else (False, "Gemini API Key မထည့်ရသေးပါ")

    voice = get_voice_by_id(voice_id)
    if not voice:
        err = f"မသိသော အသံရွေးချယ်မှု: {voice_id}"
        return (False, err, []) if return_timings else (False, err)
    partner = pair_voice(voice_id) if multi_voice else None

    sentences = split_sentences(text)
    if not sentences:
        err = "ဘာသာပြန်ထားသော စာသား ဗလာဖြစ်နေသည်"
        return (False, err, []) if return_timings else (False, err)
    chunks, starts = _chunk_sentences(sentences)

    url_base = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TTS_MODEL}:generateContent"
    all_pcm = b""
    final_rate = 24000
    last_err = "No keys provided"
    chunk_timings = []

    for ci, (chunk_sentences, start_idx) in enumerate(zip(chunks, starts)):
        if multi_voice:
            lines = []
            for j, s in enumerate(chunk_sentences):
                speaker = "Speaker1" if (start_idx + j) % 2 == 0 else "Speaker2"
                lines.append(f"{speaker}: {s}")
            transcript = (
                "TTS the following Myanmar narration, alternating speakers naturally:\n"
                + "\n".join(lines)
            )
            payload = {
                "contents": [{"parts": [{"text": transcript}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "multiSpeakerVoiceConfig": {
                            "speakerVoiceConfigs": [
                                {"speaker": "Speaker1", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice["google_voice"]}}},
                                {"speaker": "Speaker2", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": partner["google_voice"]}}},
                            ]
                        }
                    },
                },
            }
        else:
            chunk_text = " ".join(chunk_sentences)
            payload = {
                "contents": [{"parts": [{"text": f"Say in natural spoken Myanmar: {chunk_text}"}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice["google_voice"]}}
                    },
                },
            }

        chunk_ok = False
        for key in _rotated(gemini_keys, "gemini"):
            try:
                url = f"{url_base}?key={key}"
                response = requests.post(_proxied(url), json=payload, timeout=120)
                if response.status_code == 429:
                    last_err = "Gemini TTS Rate Limit (429) - နောက် key ဖြင့် ပြန်စမ်းနေသည်"
                    continue
                if response.status_code != 200:
                    last_err = f"Gemini TTS Error ({response.status_code}): {response.text[:300]}"
                    continue
                pcm, rate, err = _decode_tts_response(response.json())
                if err:
                    last_err = err
                    continue
                chunk_duration = len(pcm) / float(rate * 2) if rate > 0 else 0.0
                all_pcm += pcm
                final_rate = rate
                chunk_timings.append((start_idx, start_idx + len(chunk_sentences), chunk_duration))
                chunk_ok = True
                break
            except Exception as e:
                last_err = f"Gemini TTS Error: {str(e)}"
                continue

        if not chunk_ok:
            if return_timings:
                return False, last_err, []
            return False, last_err
        if progress_cb:
            progress_cb(ci + 1, len(chunks))

    _write_wav(output_path, all_pcm, final_rate)
    if return_timings:
        return True, None, chunk_timings
    return True, None


# ---------------------------------------------------------------------------
# 4) Subtitles (best-effort timing, proportional to sentence length)
# ---------------------------------------------------------------------------

def _format_srt_time(seconds):
    seconds = max(0.0, seconds)
    ms_total = int(round(seconds * 1000))
    h, ms_total = divmod(ms_total, 3600000)
    m, ms_total = divmod(ms_total, 60000)
    s, ms = divmod(ms_total, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(sentences, total_duration, srt_path, chunk_timings=None):
    """Create UTF-8 SRT timings anchored to the actual generated TTS audio.

    Gemini TTS returns audio per request chunk, so chunk_timings contains the
    measured duration of each generated PCM chunk. Sentence timings are then
    distributed only inside their real audio chunk instead of spreading every
    sentence over the whole file. This reduces drift on long recaps.
    """
    if not sentences or total_duration <= 0:
        return False

    sentences = [unicodedata.normalize("NFC", s).strip() for s in sentences if s.strip()]
    lines = []
    t = 0.0

    if chunk_timings:
        cursor = 0
        for chunk_index, (start_idx, end_idx, chunk_duration) in enumerate(chunk_timings):
            chunk_sentences = sentences[start_idx:end_idx]
            if not chunk_sentences:
                continue
            chunk_duration = max(0.001, float(chunk_duration))
            weights = [max(len(s), 1) for s in chunk_sentences]
            total_weight = sum(weights)
            local_t = t
            for s, weight in zip(chunk_sentences, weights):
                dur = chunk_duration * (weight / total_weight)
                start, end = local_t, min(local_t + dur, total_duration)
                lines.extend([
                    str(cursor + 1),
                    f"{_format_srt_time(start)} --> {_format_srt_time(end)}",
                    s,
                    "",
                ])
                cursor += 1
                local_t = end
            t = min(t + chunk_duration, total_duration)

        # Guard against a tiny final rounding gap.
        if lines and t < total_duration:
            t = total_duration
    else:
        weights = [max(len(s), 1) for s in sentences]
        total_weight = sum(weights)
        for i, (s, w) in enumerate(zip(sentences, weights), start=1):
            dur = total_duration * (w / total_weight)
            start, end = t, min(t + dur, total_duration)
            lines.extend([str(i), f"{_format_srt_time(start)} --> {_format_srt_time(end)}", s, ""])
            t = end

    with open(srt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return bool(lines)


# ---------------------------------------------------------------------------
# 5) Final render: mux new audio onto the video with flip / size / subtitles
# ---------------------------------------------------------------------------

SIZE_DIMENSIONS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


# ---------------------------------------------------------------------------
# Position preview image - a small JPEG snapshot of the source video with
# the Blur zone (red) and Subtitle zone (yellow) painted on top, so the
# user can see where blurPos%/subPos% (bot.py preview screen, Blur/Sub
# ⬆️⬇️ buttons) will actually land before committing to a full render.
# Regenerated on every button tap, so it's deliberately small/cheap:
# one ffmpeg frame grab + a couple of PIL rectangles, not a real render.
# ---------------------------------------------------------------------------

PREVIEW_MAX_WIDTH = 480


def _extract_preview_frame(video_path, w, h, out_jpg):
    """Grab one representative frame (30% into the clip) from video_path,
    scaled/cropped to w x h - the same crop the final render uses - so the
    zone markers drawn on top line up with where they'll really be."""
    duration = get_duration(video_path)
    ss = max(0.0, min(duration * 0.3, max(duration - 0.1, 0.0))) if duration > 0 else 0.0
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    cmd = [
        "ffmpeg", "-y", "-ss", f"{ss:.2f}", "-i", video_path,
        "-vf", vf, "-vframes", "1", "-q:v", "3", out_jpg,
    ]
    subprocess.run(cmd, capture_output=True)


def _load_myanmar_font(size, font_path=None):
    """Load a Myanmar font with Pillow's RAQM layout engine.

    Pillow without libraqm falls back to naive code-point placement, which
    is exactly the failure mode where kinzi, medials and the e-vowel marker
    appear in the wrong visual order.  RAQM delegates shaping to HarfBuzz +
    FriBidi before FreeType rasterizes the glyphs.
    """
    candidates = [
        font_path,
        FONT_PATH,
        os.path.join(FONTS_DIR, "PadaukBook-Regular.ttf"),
        os.path.join(FONTS_DIR, "Padauk-Regular.ttf"),
    ]
    raqm = getattr(ImageFont, "Layout", None)
    raqm_layout = getattr(raqm, "RAQM", None) if raqm else None
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            kwargs = {}
            if raqm_layout is not None:
                kwargs["layout_engine"] = raqm_layout
            return ImageFont.truetype(candidate, size, **kwargs)
        except Exception as exc:
            proc_logger.warning("Myanmar font load failed for %s: %s", candidate, exc)
    return ImageFont.load_default()


def render_burmese_png(text, out_path, font_size=64, font_path=None,
                       fill=(255, 255, 255, 255), stroke_fill=(0, 0, 0, 255),
                       stroke_width=2, padding=24, max_width=None):
    """Render Burmese to a transparent PNG using HarfBuzz/RAQM shaping.

    This helper is intentionally separate from FFmpeg subtitle rendering.
    It is safe for title cards, PNG subtitle overlays and preview images.
    The text is NFC-normalized and Pillow is forced onto the RAQM layout
    path whenever the installed Pillow build provides it.
    """
    text = unicodedata.normalize("NFC", str(text or ""))
    font = _load_myanmar_font(font_size, font_path)

    def measure(line):
        box = font.getbbox(line, stroke_width=stroke_width)
        return max(1, box[2] - box[0]), max(1, box[3] - box[1])

    lines = []
    if max_width and max_width > 0:
        current = ""
        for word in re.split(r"(\s+)", text):
            candidate = current + word
            if current and measure(candidate)[0] > max_width:
                lines.append(current.rstrip())
                current = word.lstrip()
            else:
                current = candidate
        if current.strip():
            lines.append(current.strip())
    else:
        lines = text.splitlines() or [""]

    metrics = [measure(line) for line in lines]
    width = max(1, max(w for w, _ in metrics) + padding * 2)
    ascent, descent = font.getmetrics()
    line_h = max(font_size, ascent + descent) + stroke_width * 2
    height = max(1, line_h * len(lines) + padding * 2)

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    y = padding
    for line in lines:
        draw.text(
            (padding, y), line, font=font, fill=fill,
            stroke_width=stroke_width, stroke_fill=stroke_fill,
            direction="ltr", language="my",
        )
        y += line_h

    image.save(out_path, "PNG")
    return out_path


def _preview_label_font(size):
    return _load_myanmar_font(size)


def generate_position_preview(video_path, settings, out_path):
    """Build the small annotated JPEG shown on the preview screen. Always
    produces a file at out_path, even if the source frame grab fails (a
    plain dark placeholder is used instead), so callers never have to
    special-case a missing preview image."""
    out_w, out_h = SIZE_DIMENSIONS.get(settings.get("size", "9:16"), (1080, 1920))
    scale = PREVIEW_MAX_WIDTH / out_w
    pw, ph = PREVIEW_MAX_WIDTH, max(2, int(round(out_h * scale)))

    fd, raw_frame = tempfile.mkstemp(suffix=".jpg", dir=DOWNLOAD_DIR)
    os.close(fd)
    img = None
    try:
        _extract_preview_frame(video_path, pw, ph, raw_frame)
        if os.path.exists(raw_frame) and os.path.getsize(raw_frame) > 0:
            img = Image.open(raw_frame).convert("RGB")
    except Exception:
        img = None
    finally:
        if os.path.exists(raw_frame):
            try:
                os.remove(raw_frame)
            except OSError:
                pass

    if img is None:
        img = Image.new("RGB", (pw, ph), (30, 34, 42))
    img = img.convert("RGBA")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    blur_pos = settings.get("blurPos", 66) / 100.0
    blur_height = max(0.02, min(0.30, settings.get("blurHeight", 8) / 100.0))
    # Keep the selected blur zone fully inside the frame. blurPos is the
    # top edge of the zone; users can move it independently from its height.
    blur_pos = min(blur_pos, max(0.0, 1.0 - blur_height))
    sub_pos = settings.get("subPos", 80) / 100.0
    font = _preview_label_font(max(13, pw // 24))

    if settings.get("autoBlur"):
        y0 = int(ph * blur_pos)
        y1 = int(ph * min(1.0, blur_pos + blur_height))
        # Blue = the exact region that will be blurred. It is intentionally
        # a small, bounded rectangle rather than the whole bottom strip.
        draw.rectangle([0, y0, pw, y1], fill=(40, 130, 255, 105))
        draw.line([0, y0, pw, y0], fill=(40, 130, 255, 235), width=3)
        draw.line([0, y1, pw, y1], fill=(40, 130, 255, 235), width=3)
        draw.text((8, y0 + 4), f"Blur {settings.get('blurPos', 66)}% / {settings.get('blurHeight', 8)}%", fill=(255, 255, 255, 255), font=font)

    if settings.get("subtitles"):
        bar_h = max(20, int(ph * 0.07))
        y0 = max(0, min(ph - bar_h, int(ph * sub_pos) - bar_h // 2))
        draw.rectangle([0, y0, pw, y0 + bar_h], fill=(255, 214, 0, 140))
        draw.text((8, y0 + max(2, bar_h // 2 - 8)), f"Subtitle zone {settings.get('subPos', 80)}%", fill=(20, 20, 20, 255), font=font)

    composed = Image.alpha_composite(img, overlay).convert("RGB")
    composed.save(out_path, "JPEG", quality=85)
    return out_path


def _parse_srt_entries(srt_path):
    """Read UTF-8 SRT entries as (start, end, text)."""
    if not srt_path or not os.path.exists(srt_path):
        return []
    raw = open(srt_path, "r", encoding="utf-8-sig").read()
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n"))
    entries = []
    ts_re = re.compile(
        r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    for block in blocks:
        lines = [line.rstrip() for line in block.split("\n")]
        if not lines:
            continue
        match = None
        time_line_index = -1
        for i, line in enumerate(lines[:3]):
            match = ts_re.match(line)
            if match:
                time_line_index = i
                break
        if not match:
            continue
        g = [int(x) for x in match.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text_lines = lines[time_line_index + 1:]
        text = "\n".join(text_lines).strip()
        text = unicodedata.normalize("NFC", text)
        text = re.sub(r"<[^>]+>", "", text)
        if text and end > start:
            entries.append((start, end, text))
    return entries


def _ass_color_to_rgb(ass_color):
    """Convert ASS &HAABBGGRR / &HBBGGRR to RGB hex for Pango."""
    raw = str(ass_color or "").strip().lstrip("&Hh")
    raw = raw[-6:].rjust(6, "0")
    bb, gg, rr = raw[0:2], raw[2:4], raw[4:6]
    return f"#{rr}{gg}{bb}"


def _render_pango_subtitle(text, out_path, font_family, font_size_px,
                           fill_hex, stroke_hex="#000000", stroke_width_px=2,
                           max_width=1600):
    """Render one subtitle with Pango/HarfBuzz/FriBidi into a transparent PNG.

    This deliberately does not use PIL text rendering or FFmpeg text filters.
    PangoCairo shapes Myanmar through HarfBuzz/FriBidi first, then Cairo
    rasterizes the already-shaped glyphs. The result is a normal RGBA PNG,
    so FFmpeg only performs image overlay and never touches Myanmar glyph
    shaping.
    """
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pango_render.py")
    if not os.path.exists(helper):
        raise RuntimeError("Pango renderer helper is missing: pango_render.py")

    cmd = [
        "/usr/bin/python3", helper,
        "--text", text,
        "--output", out_path,
        "--font", font_family,
        "--size", str(max(8, int(font_size_px))),
        "--fill", fill_hex,
        "--stroke", stroke_hex,
        "--stroke-width", str(max(0, int(stroke_width_px))),
        "--max-width", str(max(200, int(max_width))),
        "--padding", "14",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Pango Myanmar subtitle render failed: {detail[-1200:]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Pango subtitle PNG was not created: {out_path}")
    return out_path


def _build_pango_subtitle_overlays(settings, srt_path, w, h, target_dir):
    """Create shaped PNGs for every SRT entry and return overlay metadata.

    Each PNG is a small transparent image containing only the shaped Myanmar
    glyphs. FFmpeg then overlays those images at the SRT timestamps. This
    completely removes Myanmar text shaping from FFmpeg/libass/drawtext.
    """
    if not (settings.get("subtitles") and srt_path and os.path.exists(srt_path)):
        return []

    entries = _parse_srt_entries(srt_path)
    if not entries:
        return []

    color = get_color_by_id(settings.get("subColor", DEFAULT_COLOR_ID)) or get_color_by_id(DEFAULT_COLOR_ID)
    font = get_font_by_id(settings.get("subFont", DEFAULT_FONT_ID)) or get_font_by_id(DEFAULT_FONT_ID)
    font_size = max(8, int(settings.get("subFontSize", DEFAULT_FONT_SIZE)))

    # Keep the subtitle within the central 92% of the output width.
    max_width = max(300, int(w * 0.92))
    family = font.get("family") if font else DEFAULT_FONT_FAMILY
    if not family:
        family = DEFAULT_FONT_FAMILY
    fill_hex = _ass_color_to_rgb(color.get("ass") if color else "&H00FFFFFF")

    os.makedirs(target_dir, exist_ok=True)
    overlays = []
    for i, (start, end, text) in enumerate(entries):
        png_path = os.path.join(target_dir, f"sub_{i:05d}.png")
        _render_pango_subtitle(
            text, png_path, family, font_size, fill_hex,
            stroke_hex="#000000", stroke_width_px=2, max_width=max_width,
        )
        overlays.append((png_path, start, end))
    return overlays


def _build_subtitle_filter(settings, srt_path, h=1920):
    """Legacy compatibility shim.

    Actual Burmese subtitle rendering is now done by Pango/HarfBuzz PNG
    overlays in render_final_video. Returning None here prevents FFmpeg from
    ever invoking drawtext/subtitles/ass for the Myanmar SRT.
    """
    return None


# ---------------------------------------------------------------------------
# 5b) Title overlay + 5c) Watermark - both burned in via libass (the `ass`
# filter) instead of ffmpeg's `drawtext`.
#
# Why: ffmpeg's drawtext filter shapes Myanmar text incorrectly for some
# fonts (reordered medials / wrong stacking) even when built with
# harfbuzz+fribidi, because its shaping code path is separate from
# libass's. The `subtitles` filter (used for the actual subtitles) goes
# through libass, which shapes the exact same font files correctly. So
# title/watermark now write a tiny auto-generated .ass file (one
# Dialogue line, positioned via the style's Alignment/Margin fields) and
# burn it in with the `ass` filter - same rendering path as subtitles,
# same correct shaping, for any font.
# ---------------------------------------------------------------------------

def _escape_ass_path(path):
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _escape_ass_text(text):
    """Collapse whitespace and strip characters that have special meaning
    inside an ASS Dialogue Text field ({}=override tags, backslash=escape)."""
    text = unicodedata.normalize("NFC", text or "")
    text = " ".join(text.split())
    text = text.replace("\\", "").replace("{", "").replace("}", "")
    return text


def _seconds_to_ass_ts(seconds):
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _resolve_font(font_setting_id):
    """Same family/fontsdir resolution rule used for subtitles: only trust
    a custom font's family name if its .ttf actually exists in FONTS_DIR,
    otherwise fall back to the bundled default Myanmar font so libass
    always finds a real match instead of rendering tofu."""
    font = get_font_by_id(font_setting_id) or get_font_by_id(DEFAULT_FONT_ID)
    fontsdir = os.path.dirname(FONT_PATH) if os.path.exists(FONT_PATH) else None
    font_family = DEFAULT_FONT_FAMILY
    if font and font.get("file"):
        custom_path = os.path.join(FONTS_DIR, font["file"])
        if os.path.exists(custom_path):
            fontsdir = FONTS_DIR
            font_family = font["family"]
    return font_family, fontsdir


def _write_ass_overlay(text, duration, play_res_x, play_res_y, font_family,
                        font_size, primary_ass_color, outline_ass_color,
                        alignment, margin_l=20, margin_r=20, margin_v=20,
                        outline=2, bold=0):
    """Write a minimal one-line .ass file burning `text` on screen for the
    whole clip. Returns the temp file path, or None if there's nothing to
    show. Caller is responsible for deleting the file after the ffmpeg run."""
    escaped = _escape_ass_text(text)
    if not escaped:
        return None

    end_ts = _seconds_to_ass_ts(duration)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Overlay,{font_family},{font_size},{primary_ass_color},&H000000FF,{outline_ass_color},"
        f"&H64000000,{bold},0,0,0,100,100,0,0,1,{outline},0,{alignment},{margin_l},{margin_r},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,{end_ts},Overlay,,0,0,0,,{escaped}\n"
    )

    fd, path = tempfile.mkstemp(suffix=".ass", dir=DOWNLOAD_DIR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(header)
    return path


def _build_title_filter(settings, title_text, w, h, overlay_duration):
    """Build the ffmpeg `ass=...` filter fragment that burns the
    AI-generated title onto the top-center of the video, or (None, None)
    if the title overlay is off / there's no title to show."""
    if not (settings.get("titleOverlay", True) and title_text):
        return None, None

    color = get_color_by_id(settings.get("titleColor", DEFAULT_COLOR_ID)) or get_color_by_id(DEFAULT_COLOR_ID)
    font_family, fontsdir = _resolve_font(settings.get("titleFont", DEFAULT_FONT_ID))
    font_size = settings.get("titleFontSize", DEFAULT_FONT_SIZE)

    ass_path = _write_ass_overlay(
        title_text, overlay_duration, w, h, font_family, font_size,
        primary_ass_color=(color["ass"] if color else "&H00FFFFFF"),
        outline_ass_color="&H30000000",  # black, ~80% opaque outline
        alignment=8,  # top-center
        margin_v=35,
    )
    if not ass_path:
        return None, None

    filt = f"ass={_escape_ass_path(ass_path)}"
    if fontsdir:
        filt += f":fontsdir={_escape_ass_path(fontsdir)}"
    return filt, ass_path


def _build_watermark_filter(watermark_text, w, h, overlay_duration):
    """Build the ffmpeg `ass=...` filter fragment for a small
    semi-transparent text watermark in the bottom-right corner, or
    (None, None) if no watermark text is set."""
    if not watermark_text:
        return None, None

    font_family, fontsdir = _resolve_font(DEFAULT_FONT_ID)
    ass_path = _write_ass_overlay(
        watermark_text, overlay_duration, w, h, font_family, font_size=22,
        primary_ass_color="&H59FFFFFF",   # white, ~65% opaque (matches old drawtext@0.65)
        outline_ass_color="&H80000000",   # black, ~50% opaque
        alignment=3,  # bottom-right
        margin_l=16, margin_r=16, margin_v=16,
        outline=2,
    )
    if not ass_path:
        return None, None

    filt = f"ass={_escape_ass_path(ass_path)}"
    if fontsdir:
        filt += f":fontsdir={_escape_ass_path(fontsdir)}"
    return filt, ass_path


# Output resolution: scales the size-preset dimensions below (which are
# already at "1080" scale on their primary axis) down for 720p, or keeps
# them as-is for 1080p.
RESOLUTION_SCALE = {"720": 720 / 1080, "1080": 1.0}


def _safe_boxblur_radius(min_plane_dim_luma):
    """ffmpeg's boxblur filter caps the chroma-plane radius based on that
    plane's smallest dimension (half the luma dimension under yuv420p
    subsampling) - a hardcoded boxblur=20:10 can exceed that cap once the
    blurred strip (the blurPos% crop) gets thin, throwing 'Invalid
    chroma_param radius value ... must be >= 0 and <= N' and aborting the
    whole render. Scale the radius down to whatever's safe for this
    specific crop instead of always assuming 20 fits."""
    chroma_dim = max(0, min_plane_dim_luma // 2)
    max_radius = max(0, (chroma_dim - 1) // 2)
    return max(0, min(20, max_radius))


def render_final_video(
    video_path, audio_path, output_path, settings, srt_path=None, title_text=None,
    watermark_text=None, logo_path=None, bgm_path=None,
):
    video_stream = _probe_stream(video_path, "v:0")
    audio_stream = _probe_stream(audio_path, "a:0")
    video_duration = _float_or_zero(video_stream.get("duration")) if video_stream else 0.0
    audio_duration = _float_or_zero(audio_stream.get("duration")) if audio_stream else 0.0
    source_fps = _fps_from_probe(video_stream)
    source_r_fps = 0.0
    if video_stream:
        raw_r = video_stream.get("r_frame_rate")
        try:
            if raw_r and raw_r != "0/0":
                n, d = raw_r.split("/", 1)
                source_r_fps = float(n) / float(d)
        except (ValueError, ZeroDivisionError):
            source_r_fps = 0.0
    source_is_cfr = (
        source_fps > 0
        and source_r_fps > 0
        and abs(source_fps - source_r_fps) < 0.01
    )
    if video_duration <= 0:
        video_duration = get_duration(video_path)
    if audio_duration <= 0:
        audio_duration = get_duration(audio_path)
    if video_duration <= 0 or audio_duration <= 0:
        raise RuntimeError(
            f"Cannot determine media duration (video={video_duration}, audio={audio_duration})"
        )

    # The dubbed WAV is the timing master when Auto Trim is enabled, matching
    # the existing feature semantics. Both streams are explicitly rebased to
    # PTS 0 below so a non-zero input start_time can never become an A/V offset.
    if settings.get("autoTrim", True):
        target_duration = audio_duration
    else:
        target_duration = video_duration

    w, h = SIZE_DIMENSIONS.get(settings.get("size", "9:16"), (1080, 1920))
    scale = RESOLUTION_SCALE.get(str(settings.get("resolution", "720")), RESOLUTION_SCALE["720"])
    # Round to the nearest even number - libx264's default yuv420p pixel
    # format requires even width/height, and an odd value here would make
    # ffmpeg fail the whole render.
    w = max(2, int(round(w * scale / 2)) * 2)
    h = max(2, int(round(h * scale / 2)) * 2)
    # Always reset video timestamps before any crop/overlay operation. This
    # prevents source container start_time/PTS offsets from becoming a visible
    # audio lead/lag after the new audio is muxed.
    vf = [
        "setpts=PTS-STARTPTS",
        f"scale={w}:{h}:force_original_aspect_ratio=increase",
        f"crop={w}:{h}",
    ]
    if settings.get("flip"):
        vf.append("hflip")

    af = []
    extra_args = []

    if settings.get("autoTrim", True):
        # Preserve the existing Auto Trim behaviour: the final video follows
        # the generated Myanmar narration. If the narration is longer, clone
        # the final video frame; if it is shorter, the exact target duration
        # trims the video without changing the audio timing.
        if audio_duration > video_duration:
            extra_pad = audio_duration - video_duration
            if source_is_cfr:
                try:
                    source_frames = int(video_stream.get("nb_frames") or 0)
                except (TypeError, ValueError):
                    source_frames = 0
                if source_frames <= 0:
                    source_frames = max(1, int(round(video_duration * source_fps)))
                target_frames = max(source_frames, int(round(target_duration * source_fps)))
                extra_frames = max(0, target_frames - source_frames)
                if extra_frames:
                    vf.append(f"tpad=stop_mode=clone:stop={extra_frames}")
            else:
                # VFR sources retain their native timestamps. Duration-based
                # padding is used only where a frame count is not meaningful.
                vf.append(f"tpad=stop_mode=clone:stop_duration={extra_pad:.6f}")
    else:
        # Preserve the original-video-length behaviour, but make the audio
        # duration deterministic instead of relying on -shortest. The audio
        # graph below pads/trims it to this exact target.
        if audio_duration < video_duration:
            af.append(f"apad=whole_dur={video_duration:.6f}")

    if source_is_cfr:
        # `tpad=stop` clones the final frame with identical timestamps. Rebase
        # the CFR stream once more so those cloned frames occupy real frame
        # intervals instead of being dropped by the output CFR muxer.
        vf.append(f"setpts=N/({source_fps:.12g}*TB)")

    # Exact output duration is enforced after both streams have been rebased
    # to zero. Millisecond rounding here was one source of cumulative timing
    # error on longer clips, so keep the measured duration at microsecond
    # precision until ffmpeg receives it.
    extra_args += ["-t", f"{target_duration:.6f}"]

    # Overlays need to stay burned in for as long as the *final* output
    # runs, which may be longer than either source track (autoTrim can
    # pad the video to match a longer dub) - pad a couple seconds for
    # safety rather than compute the exact post-trim length twice.
    overlay_duration = max(video_duration, audio_duration) + 2.0

    subtitle_dir = tempfile.mkdtemp(prefix="mm_sub_", dir=DOWNLOAD_DIR)
    subtitle_overlays = []
    try:
        subtitle_overlays = _build_pango_subtitle_overlays(
            settings, srt_path, w, h, subtitle_dir
        )
    except Exception:
        shutil.rmtree(subtitle_dir, ignore_errors=True)
        raise

    # Title/watermark may still use ASS, but the Myanmar SRT itself is never
    # passed to libass. It is already shaped into transparent PNGs by Pango.
    title_filter, _title_ass_path = _build_title_filter(settings, title_text, w, h, overlay_duration)
    watermark_filter, _watermark_ass_path = _build_watermark_filter(watermark_text, w, h, overlay_duration)
    _temp_ass_paths = [p for p in (_title_ass_path, _watermark_ass_path) if p]
    auto_blur = settings.get("autoBlur", DEFAULT_AUTO_BLUR)

    has_logo = bool(logo_path and os.path.exists(logo_path))
    has_bgm = bool(bgm_path and os.path.exists(bgm_path))

    # Audio is also rebased to sample 0. `aresample=async=0` intentionally
    # disables time-stretching: this fix must correct timestamps, not alter
    # the TTS speech speed. The final atrim/apad below establishes the exact
    # output duration.
    audio_base = "[1:a]asetpts=N/SR/TB,aresample=async=0:first_pts=0"
    if af:
        audio_base += "," + ",".join(af)
    audio_base += f",atrim=duration={target_duration:.6f},asetpts=N/SR/TB[aout]"

    cmd = ["ffmpeg", "-fflags", "+genpts", "-i", video_path, "-i", audio_path]

    # Extra optional inputs (logo image / BGM audio) get appended after the
    # two required inputs, and we remember their index so the filter graph
    # below can refer back to them (e.g. "[2:v]").
    logo_input_idx = None
    bgm_input_idx = None
    next_idx = 2
    if has_logo:
        cmd += ["-i", logo_path]
        logo_input_idx = next_idx
        next_idx += 1
    if has_bgm:
        # -stream_loop -1 loops the BGM file indefinitely; the amix
        # "duration=first" option below trims that infinite loop down to
        # the dubbed narration's actual length.
        cmd += ["-stream_loop", "-1", "-i", bgm_path]
        bgm_input_idx = next_idx
        next_idx += 1

    # Each Burmese subtitle is already shaped by Pango/HarfBuzz into a
    # transparent PNG. FFmpeg receives these as image inputs only; it never
    # parses or shapes the Myanmar Unicode string.
    subtitle_input_indices = []
    for png_path, _start, _end in subtitle_overlays:
        cmd += ["-loop", "1", "-framerate", "1", "-i", png_path]
        subtitle_input_indices.append(next_idx)
        next_idx += 1

    # Auto Blur Mask splits the video stream (blur one copy of the
    # bottom strip, overlay it back onto the other); a logo needs a
    # second video input overlaid on top; BGM needs a second audio input
    # mixed in. All of these need -filter_complex rather than a simple
    # linear -vf chain. Build the graph as a labelled chain whenever any
    # of these are in play, so title -> blur -> logo -> watermark ->
    # subtitles compose correctly regardless of which combination is on.
    needs_complex = bool(title_filter or auto_blur or af or has_logo or has_bgm or subtitle_overlays)

    if needs_complex:
        base_chain = ",".join(vf)
        parts = [f"[0:v]{base_chain}[vbase]"]
        last_label = "vbase"

        if title_filter:
            parts.append(f"[{last_label}]{title_filter}[vtitle]")
            last_label = "vtitle"

        if auto_blur:
            # blurPos% (bot.py preview screen, Blur ⬆️/⬇️ buttons) is how far
            # down the frame the blurred strip starts - e.g. 80 means the
            # bottom 20% of the video gets blurred.
            blur_pos = max(0.0, min(0.98, settings.get("blurPos", 66) / 100.0))
            blur_height = max(0.02, min(0.30, settings.get("blurHeight", 8) / 100.0))
            blur_pos = min(blur_pos, max(0.0, 1.0 - blur_height))
            crop_h_px = max(2, int(round(h * blur_height)))
            radius = _safe_boxblur_radius(min(w, crop_h_px))
            if radius >= 1:
                parts.append(
                    f"[{last_label}]split=2[vmain][vblursrc];"
                    f"[vblursrc]crop=iw:ih*{blur_height:.4f}:0:ih*{blur_pos:.4f},boxblur={radius}:10[vblurred];"
                    f"[vmain][vblurred]overlay=0:H*{blur_pos:.4f}[vblur]"
                )
                last_label = "vblur"
            else:
                # Strip is too thin (a few px) for any blur radius ffmpeg will
                # accept on this video's resolution - skip rather than crash
                # the whole render over a sliver the user likely can't see.
                proc_logger.warning(
                    "Auto Blur strip too thin (%dpx) at blurPos=%s%% - skipping blur for this render",
                    crop_h_px, settings.get("blurPos", 66),
                )

        if has_logo:
            # Scale the logo to ~18% of the output width (even pixels,
            # aspect ratio preserved via -2) and pin it to the top-right
            # corner with a small margin.
            logo_w = max(2, int(w * 0.18 / 2) * 2)
            parts.append(f"[{logo_input_idx}:v]scale={logo_w}:-2[vlogosrc]")
            parts.append(f"[{last_label}][vlogosrc]overlay=W-w-16:16[vlogo]")
            last_label = "vlogo"

        if watermark_filter:
            parts.append(f"[{last_label}]{watermark_filter}[vwm]")
            last_label = "vwm"

        if subtitle_overlays:
            sub_pos = max(0.02, min(0.98, settings.get("subPos", 80) / 100.0))
            for i, ((_png_path, start, end), input_idx) in enumerate(
                zip(subtitle_overlays, subtitle_input_indices)
            ):
                # A 1fps looped PNG is only a static shaped glyph layer.
                # `enable` is evaluated against the main video's timestamps,
                # so the SRT start/end times remain the actual visibility
                # window. The overlay is centered at the user's subPos%.
                img_label = f"subimg{i}"
                out_label = f"vsub{i}"
                parts.append(f"[{input_idx}:v]format=rgba[{img_label}]")
                parts.append(
                    f"[{last_label}][{img_label}]"
                    f"overlay=x=(W-w)/2:y=H*{sub_pos:.6f}-h/2:"
                    f"eof_action=pass:"
                    f"enable='between(t,{start:.6f},{end:.6f})'"
                    f"[{out_label}]"
                )
                last_label = out_label
            parts.append(f"[{last_label}]copy[v]")
        else:
            parts.append(f"[{last_label}]copy[v]")

        video_graph = ";".join(parts)

        audio_parts = [audio_base]
        if has_bgm:
            # Normalize both tracks to the same sample rate/layout before
            # amix - it expects matching formats to mix correctly. BGM is
            # turned down first so it sits behind the narration, then the
            # mixed result is boosted back up since amix's default
            # normalize behaviour halves the combined level.
            audio_parts.append("[aout]aformat=sample_rates=44100:channel_layouts=stereo[adubn]")
            audio_parts.append(
                f"[{bgm_input_idx}:a]asetpts=N/SR/TB,aresample=async=0:first_pts=0,"
                f"volume=0.18,aformat=sample_rates=44100:channel_layouts=stereo,"
                f"atrim=duration={target_duration:.6f},asetpts=N/SR/TB[bgmvol]"
            )
            audio_parts.append("[adubn][bgmvol]amix=inputs=2:duration=first:dropout_transition=0[amixed]")
            audio_parts.append(
                f"[amixed]volume=2.0,atrim=duration={target_duration:.6f},"
                "asetpts=N/SR/TB[a]"
            )
        else:
            audio_parts.append("[aout]anull[a]")

        full_graph = video_graph + ";" + ";".join(audio_parts)
        cmd += ["-filter_complex", full_graph, "-map", "[v]", "-map", "[a]"]
    else:
        if watermark_filter:
            vf.append(watermark_filter)
        # Burmese SRTs are handled above as Pango PNG overlay inputs.
        video_filter = ",".join(vf)
        # Keep the simple render path simple, but still use the same explicit
        # PTS-zero audio chain so sync cannot depend on ffmpeg's implicit
        # timestamp handling.
        simple_graph = f"[0:v]{video_filter}[v];{audio_base};[aout]anull[a]"
        cmd += ["-filter_complex", simple_graph, "-map", "[v]", "-map", "[a]"]

    cmd += extra_args

    # 🛡️ Auto Edit: strip metadata carried over from the source file
    # (device/app tags, GPS, timestamps, etc.) as part of the encode we're
    # already doing - no need for a separate cleanup pass. When turned off,
    # ffmpeg keeps its default behaviour of copying the first input's
    # global metadata through to the output.
    if settings.get("autoEdit", True):
        cmd += ["-map_metadata", "-1"]

    # For a constant-frame-rate source, explicitly request the exact number
    # of output frames for the target duration. `-t` alone can stop between
    # frame boundaries and leave a one-frame A/V mismatch. Variable-frame-rate
    # sources are left in their native timing instead of being silently
    # converted to CFR.
    if source_is_cfr:
        output_fps = source_fps
        output_frames = max(1, int(round(target_duration * output_fps)))
        cmd += [
            "-fps_mode", "cfr",
            "-r", f"{output_fps:.12g}",
            "-frames:v", str(output_frames),
        ]

    cmd += [
        # Keep the existing encoder/settings intact. These are not feature
        # changes; the sync fix above only controls timestamps/frame count.
        "-filter_threads", str(FFMPEG_FILTER_THREADS),
        "-filter_complex_threads", str(FFMPEG_FILTER_THREADS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-threads", str(FFMPEG_THREADS),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-y", output_path,
    ]
    try:
        _run(cmd)
        # Never return an apparently successful file without checking the
        # actual muxed streams. This catches duration drift, non-zero PTS
        # starts, missing audio, or broken timebase/sample-rate metadata.
        _verify_audio_video_sync(output_path, expected_duration=target_duration)
    finally:
        # Clean up all temporary ASS and Pango PNG subtitle layers regardless
        # of success/failure. This is important on Railway where the ephemeral
        # disk is shared by concurrent Telegram jobs.
        for p in _temp_ass_paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        shutil.rmtree(subtitle_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Key status check (used by the "🔍 Check Keys" button)
# ---------------------------------------------------------------------------

def check_key_status(keys):
    status = {"gemini": [], "groq": []}
    for k in keys.get("gemini", []):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={k}"
            resp = requests.get(_proxied(url), timeout=15)
            if resp.status_code == 200:
                status["gemini"].append({"key": k[:8] + "...", "ok": True})
            else:
                status["gemini"].append({"key": k[:8] + "...", "ok": False, "err": f"HTTP {resp.status_code}"})
        except Exception as e:
            status["gemini"].append({"key": k[:8] + "...", "ok": False, "err": str(e)[:120]})
    for k in keys.get("groq", []):
        try:
            url = "https://api.groq.com/openai/v1/models"
            resp = requests.get(_proxied(url), headers={"Authorization": f"Bearer {k}"}, timeout=15)
            if resp.status_code == 200:
                status["groq"].append({"key": k[:8] + "...", "ok": True})
            else:
                status["groq"].append({"key": k[:8] + "...", "ok": False, "err": f"HTTP {resp.status_code}"})
        except Exception as e:
            status["groq"].append({"key": k[:8] + "...", "ok": False, "err": str(e)[:120]})
    return status


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_video(video_path, output_path, settings, keys, progress_callback=None, logo_path=None, bgm_path=None):
    work_id = uuid.uuid4().hex[:8]
    audio_orig = os.path.join(DOWNLOAD_DIR, f"orig_{work_id}.mp3")
    audio_new = os.path.join(DOWNLOAD_DIR, f"new_{work_id}.wav")
    srt_path = os.path.join(DOWNLOAD_DIR, f"subs_{work_id}.srt")

    try:
        if not has_audio_stream(video_path):
            return False, "ဗီဒီယိုတွင် အသံလိုင်း မတွေ့ပါ (video has no audio track)"

        if progress_callback:
            progress_callback(20, "🔍 မူရင်းအသံ ထုတ်ယူနေသည်...")
        extract_audio(video_path, audio_orig)

        if progress_callback:
            progress_callback(35, "🎙️ အသံမှ စာသားပြောင်းနေသည် (Groq)...")
        text_src, err = transcribe_audio(audio_orig, keys.get("groq", []))
        if err:
            return False, err
        if not text_src or not text_src.strip():
            return False, "အသံထဲမှ စာသား ရှာမတွေ့ပါ"

        if progress_callback:
            progress_callback(50, "🧠 မြန်မာဘာသာ ပြန်ဆိုနေသည် (Gemini)...")
        text_myan, err = translate_to_myanmar(text_src, keys.get("gemini", []))
        if err:
            return False, err

        title_text = None
        if settings.get("titleOverlay", True):
            if progress_callback:
                progress_callback(58, "🏷️ ဗီဒီယို ခေါင်းစဉ် ဖန်တီးနေသည် (Gemini)...")
            title_text, title_err = generate_title(text_myan, keys.get("gemini", []))
            if title_err:
                # Best-effort only - the title overlay is a nice-to-have,
                # so a failure here shouldn't fail the whole dubbing job.
                proc_logger.warning("Title generation failed, continuing without overlay: %s", title_err)
                title_text = None

        if progress_callback:
            progress_callback(65, "🔊 မြန်မာအသံ ထုတ်လုပ်နေသည် (Gemini TTS)...")
        voice_id = settings.get("voice")
        multi_voice = bool(settings.get("multiVoice"))

        def _tts_progress(done, total):
            if progress_callback:
                pct = 65 + int(15 * done / max(total, 1))
                progress_callback(pct, f"🔊 မြန်မာအသံ ထုတ်လုပ်နေသည်... ({done}/{total})")

        ok, err, tts_chunk_timings = generate_myanmar_tts(
            text_myan, voice_id, keys.get("gemini", []), audio_new,
            multi_voice=multi_voice, progress_cb=_tts_progress,
            return_timings=True,
        )
        if not ok:
            return False, err

        srt_file = None
        if settings.get("subtitles"):
            audio_dur = get_duration(audio_new)
            sentences = split_sentences(text_myan)
            if build_srt(sentences, audio_dur, srt_path, chunk_timings=tts_chunk_timings):
                srt_file = srt_path

        if progress_callback:
            progress_callback(90, "🎬 ဗီဒီယို နောက်ဆုံးအဆင့် ပေါင်းစပ်နေသည်...")
        render_final_video(
            video_path, audio_new, output_path, settings,
            srt_path=srt_file, title_text=title_text,
            watermark_text=settings.get("watermark"), logo_path=logo_path, bgm_path=bgm_path,
        )

        return True, "Success"
    except RuntimeError as e:
        return False, f"Video Processing Error: {str(e)}"
    except Exception as e:
        proc_logger.exception("process_video failed")
        return False, f"System Error: {str(e)}"
    finally:
        for f in [audio_orig, audio_new, srt_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
