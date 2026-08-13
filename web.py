# -*- coding: utf-8 -*-
"""
Web front-end for the Myanmar video dubbing pipeline.

This replaces bot.py (Telegram) as the primary interface: same
processor.py pipeline underneath (transcribe -> translate -> TTS ->
ffmpeg render), just driven from a browser form instead of Telegram
button menus. Every visitor supplies their own Gemini/Groq API keys
in the form - nothing is stored server-side beyond the lifetime of
their dubbing job.

Run locally:   uvicorn web:app --reload
Run in prod:   uvicorn web:app --host 0.0.0.0 --port $PORT
"""
import os
import re
import time
import uuid
import shutil
import asyncio
import logging
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from processor import process_video, check_key_status, _ass_color_to_rgb, DOWNLOAD_DIR
from voices import VOICES, DEFAULT_VOICE_ID, get_voice_by_id
from styles import (
    COLOR_OPTIONS, FONT_OPTIONS, FONT_SIZES,
    DEFAULT_COLOR_ID, DEFAULT_FONT_ID, DEFAULT_FONT_SIZE, DEFAULT_AUTO_BLUR,
    get_color_by_id, get_font_by_id,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("web")

app = FastAPI(title="Myanmar Video Dubbing Studio")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "300"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(6 * 3600)))  # cleanup after 6h

GEMINI_KEY_PREFIXES = ("AIza", "AQ.")
GROQ_KEY_PREFIX = "gsk_"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)

JOBS = {}
JOBS_LOCK = threading.Lock()


class KeyCheckRequest(BaseModel):
    gemini: str = ""
    groq: str = ""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _split_keys(text, prefix):
    tokens = re.split(r"[,\s]+", (text or "").strip())
    return [t for t in tokens if t.startswith(prefix)]


def _flag(value):
    return value is not None and str(value).lower() not in ("", "false", "0", "off", "no")


def _clamp(value, lo, hi, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _safe_ext(filename, allowed, fallback):
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in allowed else fallback


def _job_dir(job_id):
    d = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


async def _save_upload(upload: UploadFile, dest_path, max_bytes=None):
    size = 0
    with open(dest_path, "wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if max_bytes and size > max_bytes:
                f.close()
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"ဖိုင်အရွယ်အစား {max_bytes // (1024*1024)}MB ထက်ကြီးနေပါသည်",
                )
            f.write(chunk)
    return size


def _cleanup_old_jobs():
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if j.get("status") in ("done", "error") and now - j.get("created", now) > JOB_TTL_SECONDS]
        for jid in stale:
            job = JOBS.pop(jid, None)
            if not job:
                continue
            for p in (job.get("output_path"), job.get("dir")):
                if not p:
                    continue
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    elif os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Background job runner
# ---------------------------------------------------------------------------
def _run_job(job_id, video_path, output_path, settings, keys, logo_path, bgm_path, job_dir):
    def progress_cb(pct, msg):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["progress"] = pct
                JOBS[job_id]["message"] = msg

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["progress"] = 5
        JOBS[job_id]["message"] = "စတင်နေသည်..."

    try:
        ok, msg = process_video(
            video_path, output_path, settings, keys,
            progress_callback=progress_cb, logo_path=logo_path, bgm_path=bgm_path,
        )
        with JOBS_LOCK:
            if job_id not in JOBS:
                return
            if ok:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["progress"] = 100
                JOBS[job_id]["message"] = "ပြီးပါပြီ!"
                JOBS[job_id]["output_path"] = output_path
            else:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["message"] = msg or "အမှားတစ်ခု ဖြစ်သွားပါသည်"
    except Exception as e:  # noqa: BLE001
        logger.exception("Job %s failed", job_id)
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["message"] = f"System Error: {e}"
    finally:
        # Uploaded input files are no longer needed once rendering is done
        # (success or failure) - only the rendered output is kept.
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    colors = [dict(c, css=_ass_color_to_rgb(c["ass"])) for c in COLOR_OPTIONS]
    ctx = {
        "request": request,
        "voices": VOICES,
        "default_voice": DEFAULT_VOICE_ID,
        "colors": colors,
        "fonts": FONT_OPTIONS,
        "font_sizes": FONT_SIZES,
        "default_color": DEFAULT_COLOR_ID,
        "default_font": DEFAULT_FONT_ID,
        "default_font_size": DEFAULT_FONT_SIZE,
        "default_auto_blur": DEFAULT_AUTO_BLUR,
        "max_upload_mb": MAX_UPLOAD_MB,
    }
    return templates.TemplateResponse("index.html", ctx)


@app.post("/api/check-keys")
async def api_check_keys(payload: KeyCheckRequest):
    keys = {
        "gemini": _split_keys(payload.gemini, GEMINI_KEY_PREFIXES),
        "groq": _split_keys(payload.groq, GROQ_KEY_PREFIX),
    }
    if not keys["gemini"] and not keys["groq"]:
        raise HTTPException(400, "Key မထည့်ရသေးပါ")
    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(EXECUTOR, check_key_status, keys)
    return JSONResponse(status)


@app.post("/api/jobs")
async def create_job(
    request: Request,
    video: UploadFile = File(...),
    gemini_keys: str = Form(""),
    groq_keys: str = Form(""),
    voice: str = Form(DEFAULT_VOICE_ID),
    multiVoice: Optional[str] = Form(None),
    size: str = Form("9:16"),
    resolution: str = Form("720"),
    subtitles: Optional[str] = Form(None),
    subColor: str = Form(DEFAULT_COLOR_ID),
    subFont: str = Form(DEFAULT_FONT_ID),
    subFontSize: str = Form(str(DEFAULT_FONT_SIZE)),
    autoBlur: Optional[str] = Form(None),
    blurPos: str = Form("66"),
    blurHeight: str = Form("8"),
    subPos: str = Form("80"),
    titleOverlay: Optional[str] = Form(None),
    titleColor: str = Form(DEFAULT_COLOR_ID),
    titleFont: str = Form(DEFAULT_FONT_ID),
    titleFontSize: str = Form(str(DEFAULT_FONT_SIZE)),
    flip: Optional[str] = Form(None),
    autoTrim: Optional[str] = Form(None),
    autoEdit: Optional[str] = Form(None),
    watermark: str = Form(""),
    logo: Optional[UploadFile] = File(None),
    bgm: Optional[UploadFile] = File(None),
):
    _cleanup_old_jobs()

    gemini_list = _split_keys(gemini_keys, GEMINI_KEY_PREFIXES)
    groq_list = _split_keys(groq_keys, GROQ_KEY_PREFIX)
    if not gemini_list:
        raise HTTPException(400, "Gemini API key အနည်းဆုံး တစ်ခု ထည့်ပါ")
    if not groq_list:
        raise HTTPException(400, "Groq API key အနည်းဆုံး တစ်ခု ထည့်ပါ")

    ext = _safe_ext(video.filename, VIDEO_EXTS, None)
    if ext is None:
        raise HTTPException(400, "Video file (mp4/mov/mkv/webm/m4v/avi) ပို့ပေးပါ")

    voice_id = voice if get_voice_by_id(voice) else DEFAULT_VOICE_ID
    color_id = subColor if get_color_by_id(subColor) else DEFAULT_COLOR_ID
    font_id = subFont if get_font_by_id(subFont) else DEFAULT_FONT_ID
    title_color_id = titleColor if get_color_by_id(titleColor) else DEFAULT_COLOR_ID
    title_font_id = titleFont if get_font_by_id(titleFont) else DEFAULT_FONT_ID
    size_id = size if size in ("9:16", "16:9", "1:1", "4:5") else "9:16"
    resolution_id = resolution if resolution in ("720", "1080") else "720"

    job_id = uuid.uuid4().hex
    jdir = _job_dir(job_id)

    video_path = os.path.join(jdir, f"input{ext}")
    await _save_upload(video, video_path, MAX_UPLOAD_BYTES)

    logo_path = None
    if logo is not None and getattr(logo, "filename", ""):
        logo_ext = _safe_ext(logo.filename, {".png", ".jpg", ".jpeg", ".webp"}, ".png")
        logo_path = os.path.join(jdir, f"logo{logo_ext}")
        await _save_upload(logo, logo_path, 15 * 1024 * 1024)

    bgm_path = None
    if bgm is not None and getattr(bgm, "filename", ""):
        bgm_ext = _safe_ext(bgm.filename, {".mp3", ".wav", ".m4a", ".aac", ".ogg"}, ".mp3")
        bgm_path = os.path.join(jdir, f"bgm{bgm_ext}")
        await _save_upload(bgm, bgm_path, 40 * 1024 * 1024)

    settings = {
        "voice": voice_id,
        "multiVoice": _flag(multiVoice),
        "size": size_id,
        "resolution": resolution_id,
        "subtitles": _flag(subtitles),
        "subColor": color_id,
        "subFont": font_id,
        "subFontSize": _clamp(subFontSize, 8, 60, DEFAULT_FONT_SIZE),
        "autoBlur": _flag(autoBlur),
        "blurPos": _clamp(blurPos, 2, 95, 66),
        "blurHeight": _clamp(blurHeight, 2, 30, 8),
        "subPos": _clamp(subPos, 2, 95, 80),
        "titleOverlay": _flag(titleOverlay),
        "titleColor": title_color_id,
        "titleFont": title_font_id,
        "titleFontSize": _clamp(titleFontSize, 8, 60, DEFAULT_FONT_SIZE),
        "flip": _flag(flip),
        "autoTrim": _flag(autoTrim),
        "autoEdit": _flag(autoEdit),
        "watermark": (watermark or "").strip()[:60],
    }
    keys = {"gemini": gemini_list, "groq": groq_list}
    output_path = os.path.join(DOWNLOAD_DIR, f"out_{job_id}.mp4")

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "စီစဉ်နေသည်...",
            "created": time.time(),
            "output_path": None,
            "dir": jdir,
        }

    EXECUTOR.submit(_run_job, job_id, video_path, output_path, settings, keys, logo_path, bgm_path, jdir)

    return JSONResponse({"job_id": job_id})


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        data = {
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
        }
        if job["status"] == "done":
            data["download_url"] = f"/download/{job_id}"
    return JSONResponse(data)


@app.get("/download/{job_id}")
async def download(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("output_path"):
        raise HTTPException(404, "File not ready")
    path = job["output_path"]
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="video/mp4", filename="myanmar_dub.mp4")


@app.get("/healthz")
async def healthz():
    return {"ok": True}
