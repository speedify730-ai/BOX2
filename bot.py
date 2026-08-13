# -*- coding: utf-8 -*-
import os
import re
import html
import json
import uuid
import logging
import asyncio

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters,
)

from processor import process_video, check_key_status, generate_position_preview
from voices import VOICES, DEFAULT_VOICE_ID, get_voice_by_id, voices_by_gender
from styles import (
    COLOR_OPTIONS, FONT_OPTIONS, FONT_SIZES,
    DEFAULT_COLOR_ID, DEFAULT_FONT_ID, DEFAULT_FONT_SIZE, DEFAULT_AUTO_BLUR,
    get_color_by_id, get_font_by_id,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot")

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")

# --- Local Bot API server support -------------------------------------
# Telegram's shared cloud Bot API (api.telegram.org) can only *download*
# files up to 20 MB via getFile, no matter what MAX_DOWNLOAD_BYTES below
# says - that ceiling is enforced by Telegram's servers. To support
# bigger files, self-host the Local Bot API server (see entrypoint.sh /
# Dockerfile / README) and set TELEGRAM_API_ID + TELEGRAM_API_HASH
# (from https://my.telegram.org/apps). When both are present we point
# python-telegram-bot at the local server instead of api.telegram.org.
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH")
LOCAL_BOT_API = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "http://127.0.0.1:8081")

_requested_max_mb = int(os.environ.get("MAX_DOWNLOAD_MB", "100" if LOCAL_BOT_API else "20"))
if not LOCAL_BOT_API and _requested_max_mb > 20:
    logger.warning(
        "MAX_DOWNLOAD_MB=%s ignored - the cloud Bot API caps downloads at 20MB. "
        "Set TELEGRAM_API_ID/TELEGRAM_API_HASH to self-host the Local Bot API "
        "server and unlock larger files (see README).",
        _requested_max_mb,
    )
    _requested_max_mb = 20
MAX_DOWNLOAD_MB = _requested_max_mb
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024

KEYS_FILE = "api_keys.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Per-user Logo image / BGM audio storage. Like api_keys.json, these live
# on local disk - on Railway that's ephemeral, so they'll be lost on
# redeploy unless a persistent volume is mounted (see README).
LOGO_DIR = os.path.join("assets", "logos")
BGM_DIR = os.path.join("assets", "bgm")
os.makedirs(LOGO_DIR, exist_ok=True)
os.makedirs(BGM_DIR, exist_ok=True)


def get_logo_path(user_id):
    path = os.path.join(LOGO_DIR, f"{user_id}.png")
    return path if os.path.exists(path) else None


def get_bgm_path(user_id):
    path = os.path.join(BGM_DIR, f"{user_id}.mp3")
    return path if os.path.exists(path) else None


def _remove_logo_file(user_id):
    path = os.path.join(LOGO_DIR, f"{user_id}.png")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _remove_bgm_file(user_id):
    path = os.path.join(BGM_DIR, f"{user_id}.mp3")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False

# Google AI Studio now issues two Gemini key formats: the legacy "AIza..."
# Standard key, and the newer "AQ...." Auth key (default for all new keys
# since ~June 2026; Standard keys stop working entirely in September 2026).
# Accept either so users with an old or a new key both work - see
# https://ai.google.dev/gemini-api/docs/api-key
GEMINI_KEY_PREFIXES = ("AIza", "AQ.")


def _split_keys(text, prefix):
    """Split a blob of text on commas/whitespace/newlines and keep only
    tokens that look like a real key for the given prefix (a single string
    like "gsk_", or a tuple of acceptable prefixes like GEMINI_KEY_PREFIXES).
    Handles both a single pasted key and many keys pasted at once (one per
    line, or comma/space separated)."""
    tokens = re.split(r"[,\s]+", text.strip())
    return [t for t in tokens if t.startswith(prefix)]


def load_keys():
    if os.path.exists(KEYS_FILE):
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"gemini": [], "groq": []}
    # Repair any previously-saved entries where multiple keys were
    # accidentally stored joined together as one string (old bug).
    fixed = {"gemini": [], "groq": []}
    for entry in data.get("gemini", []):
        for k in _split_keys(entry, GEMINI_KEY_PREFIXES):
            if k not in fixed["gemini"]:
                fixed["gemini"].append(k)
    for entry in data.get("groq", []):
        for k in _split_keys(entry, "gsk_"):
            if k not in fixed["groq"]:
                fixed["groq"].append(k)
    if fixed != data:
        save_keys(fixed)
    return fixed


def save_keys(keys):
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(keys, f)


api_keys = load_keys()
user_settings = {}


def get_settings(user_id):
    if user_id not in user_settings:
        user_settings[user_id] = {
            "autoPreview": True,
            "autoEdit": True,
            "flip": False,
            "multiVoice": False,
            "subtitles": True,
            "autoTrim": True,
            "size": "9:16",
            "voice": DEFAULT_VOICE_ID,
            "subColor": DEFAULT_COLOR_ID,
            "subFont": DEFAULT_FONT_ID,
            "subFontSize": DEFAULT_FONT_SIZE,
            "autoBlur": DEFAULT_AUTO_BLUR,
            "titleOverlay": True,
            "titleColor": DEFAULT_COLOR_ID,
            "titleFont": DEFAULT_FONT_ID,
            "titleFontSize": DEFAULT_FONT_SIZE,
            "resolution": "720",
            "watermark": "",
            "blurPos": 66,
            "blurHeight": 8,
            "subPos": 80,
        }
    return user_settings[user_id]


# Bounds + step size for the Blur/Subtitle position sliders on the preview
# screen - keeps the % in a sane range no matter how many times the user
# taps the up/down buttons.
POS_MIN, POS_MAX, POS_STEP = 2, 95, 2
BLUR_HEIGHT_MIN, BLUR_HEIGHT_MAX, BLUR_HEIGHT_STEP = 2, 30, 2


def clamp_pos(v):
    return max(POS_MIN, min(POS_MAX, v))


def clamp_blur_height(v):
    return max(BLUR_HEIGHT_MIN, min(BLUR_HEIGHT_MAX, v))


# Options offered by the "📱 Size" picker
SIZE_OPTIONS = [
    ("9:16", "📱 9:16 (Reels/Shorts)"),
    ("16:9", "🖥️ 16:9 (Landscape)"),
    ("1:1", "⬛ 1:1 (Square)"),
    ("4:5", "📷 4:5 (Portrait)"),
]

# Options offered by the "📺 Resolution" picker
RESOLUTION_OPTIONS = [
    ("720", "HD 720p (Default)"),
    ("1080", "FHD 1080p (High Quality)"),
]


def get_progress_bar(pct):
    done = int(pct / 10)
    return f"[ {'▓' * done}{'░' * (10 - done)} ] {pct}%"


def get_main_menu(user_id):
    s = get_settings(user_id)
    voice = get_voice_by_id(s["voice"]) or get_voice_by_id(DEFAULT_VOICE_ID)
    gender_icon = "👨" if voice["gender"] == "male" else "👩"
    color = get_color_by_id(s["subColor"]) or get_color_by_id(DEFAULT_COLOR_ID)
    font = get_font_by_id(s["subFont"]) or get_font_by_id(DEFAULT_FONT_ID)
    has_logo = get_logo_path(user_id) is not None
    has_bgm = get_bgm_path(user_id) is not None
    text = (
        "✨ **One Click Recap & Full Dub Bot** ✨\n\n"
        "⚙️ **Settings:**\n"
        f"• 👁️ Auto-Preview: `{'ON ✅' if s.get('autoPreview', True) else 'OFF ❌'}` | 🛡️ Auto Edit: `{'ON ✅' if s.get('autoEdit', True) else 'OFF ❌'}`\n"
        f"• ↔️ Flip: `{'ON ✅' if s['flip'] else 'OFF ❌'}` | 🗣️ Multi Voice: `{'ON ✅' if s['multiVoice'] else 'OFF ❌'}`\n"
        f"• 📝 Sub: `{'ON ✅' if s['subtitles'] else 'OFF ❌'}` | ✂️ Trim: `{'ON ✅' if s['autoTrim'] else 'OFF ❌'}`\n"
        f"• 📱 Size: `{s['size']}` | 🎙️ Voice: `{gender_icon} {voice['name']}`\n"
        f"• 🎨 Sub Color: `{color['name']}` | 🔤 Sub Font: `{font['name']}`\n"
        f"• 📏 Sub Size: `{s['subFontSize']}px` | 🌫️ Auto Blur: `{'ON ✅' if s['autoBlur'] else 'OFF ❌'}`\n"
        f"• 🏷️ Title Overlay: `{'ON ✅' if s['titleOverlay'] else 'OFF ❌'}` | 📺 Resolution: `{s['resolution']}p`\n"
        f"• 💧 Watermark: `{'ON ✅' if s['watermark'] else 'OFF ❌'}` | 📷 Logo: `{'ON ✅' if has_logo else 'OFF ❌'}` | 🎵 BGM: `{'ON ✅' if has_bgm else 'OFF ❌'}`\n"
        f"• ⬜ Blur နေရာ: `{s['blurPos']}%` | 📝 Subtitle နေရာ: `{s['subPos']}%` (ဗီဒီယို ပို့ပြီး Preview screen ပေါ်တွင် ချိန်ပါ)\n\n"
        f"🔑 **Keys:** Gemini: `{len(api_keys['gemini'])}` | Groq: `{len(api_keys['groq'])}`\n\n"
        "📥 Myanmar မဟုတ်တဲ့ mp4 ဗီဒီယို ပို့လိုက်ရုံနဲ့ မြန်မာအသံ dub ထည့်ပေးပါမယ်။"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                f"👁️ Auto-Preview: {'✅' if s.get('autoPreview', True) else '❌'}",
                callback_data="toggle_auto_preview",
            ),
            InlineKeyboardButton(
                f"🛡️ Auto Edit: {'✅' if s.get('autoEdit', True) else '❌'}",
                callback_data="toggle_auto_edit",
            ),
        ],
        [
            InlineKeyboardButton(f"Flip: {'✅' if s['flip'] else '❌'}", callback_data="toggle_flip"),
            InlineKeyboardButton(f"Multi Voice: {'✅' if s['multiVoice'] else '❌'}", callback_data="toggle_multi"),
        ],
        [
            InlineKeyboardButton(f"Subtitle: {'✅' if s['subtitles'] else '❌'}", callback_data="toggle_sub"),
            InlineKeyboardButton(f"Auto Trim: {'✅' if s['autoTrim'] else '❌'}", callback_data="toggle_trim"),
        ],
        [
            InlineKeyboardButton(f"📱 Size: {s['size']}", callback_data="change_size"),
            InlineKeyboardButton(f"🎙️ Voice: {voice['name']}", callback_data="change_voice"),
        ],
        [
            InlineKeyboardButton(f"🎨 Sub Color: {color['name']}", callback_data="change_subcolor"),
            InlineKeyboardButton(f"🔤 Sub Font: {font['name']}", callback_data="change_subfont"),
        ],
        [
            InlineKeyboardButton(f"📏 Sub Size: {s['subFontSize']}", callback_data="change_subsize"),
            InlineKeyboardButton(f"🌫️ Auto Blur: {'✅' if s['autoBlur'] else '❌'}", callback_data="toggle_blur"),
        ],
        [
            InlineKeyboardButton(f"🏷️ Title Overlay: {'✅' if s['titleOverlay'] else '❌'}", callback_data="open_title_menu"),
            InlineKeyboardButton(f"📺 Resolution: {s['resolution']}p", callback_data="open_resolution_menu"),
        ],
        [
            InlineKeyboardButton(f"💧 Watermark: {'✅' if s['watermark'] else '❌'}", callback_data="open_wm_menu"),
            InlineKeyboardButton(f"📷 Logo: {'✅' if has_logo else '❌'}", callback_data="open_logo_menu"),
        ],
        [
            InlineKeyboardButton(f"🎵 BGM: {'✅' if has_bgm else '❌'}", callback_data="open_bgm_menu"),
            InlineKeyboardButton("ℹ️ အသုံးပြုရန်အချက်များ", callback_data="help"),
        ],
        [
            InlineKeyboardButton("🔑 +Gemini", callback_data="add_gemini"),
            InlineKeyboardButton("🔑 +Groq", callback_data="add_groq"),
        ],
        [
            InlineKeyboardButton("🔍 Check Keys", callback_data="check_keys"),
            InlineKeyboardButton("🗑️ Clear Keys", callback_data="clear_keys"),
        ],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def get_voice_menu():
    keyboard = [[InlineKeyboardButton("👨 အမျိုးသား (Male)", callback_data="noop")]]
    keyboard += [
        [InlineKeyboardButton(f"{v['name']} — {v['desc']}", callback_data=f"set_voice_{v['id']}")]
        for v in voices_by_gender("male")
    ]
    keyboard.append([InlineKeyboardButton("👩 အမျိုးသမီး (Female)", callback_data="noop")])
    keyboard += [
        [InlineKeyboardButton(f"{v['name']} — {v['desc']}", callback_data=f"set_voice_{v['id']}")]
        for v in voices_by_gender("female")
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)


def title_overlay_menu(s):
    status = "✅ ဖွင့်ထားသည်" if s["titleOverlay"] else "❌ ပိတ်ထားသည်"
    color = get_color_by_id(s["titleColor"]) or get_color_by_id(DEFAULT_COLOR_ID)
    font = get_font_by_id(s["titleFont"]) or get_font_by_id(DEFAULT_FONT_ID)

    text = (
        "🏷️ <b>Video Title Overlay</b>\n\n"
        f"🏷️ Video Title Overlay: <b>{status}</b>\n"
        f"🎨 Color: <b>{color['name']}</b> | 🔤 Font: <b>{font['name']}</b> | 📏 Size: <b>{s['titleFontSize']}px</b>\n\n"
        "ဖွင့်ထားပါက AI ထုတ်ပေးတဲ့ Title ကို Video ထိပ်မှာ တစ်ခါတည်း အမြဲကပ်ပြပေးပါမည်။"
    )

    keyboard = [
        [InlineKeyboardButton(f"🏷️ Video Title Overlay: {status}", callback_data="toggle_title_overlay")],
        [
            InlineKeyboardButton("🎨 Title Color", callback_data="title_color"),
            InlineKeyboardButton("🔤 Title Font", callback_data="title_font"),
        ],
        [InlineKeyboardButton("📏 Title Size", callback_data="title_size")],
        [InlineKeyboardButton("⬅️ နောက်သို့", callback_data="back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def resolution_menu(s):
    text = (
        "📺 <b>Video Resolution</b>\n\n"
        f"လက်ရှိရွေးထားသည်: <b>{s['resolution']}p</b>\n\n"
        "Default = 720p ထားထားပြီး 1080p နှိပ်မှသာ 1080p အဖြစ် Export လုပ်သွားပါမယ်။"
    )
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"resolution_{val}")]
        for val, label in RESOLUTION_OPTIONS
    ]
    keyboard.append([InlineKeyboardButton("⬅️ နောက်သို့", callback_data="back")])
    return text, InlineKeyboardMarkup(keyboard)


def watermark_menu(s):
    status = html.escape(s["watermark"]) if s.get("watermark") else "❌ မထားရသေးပါ"
    text = (
        "💧 <b>Watermark</b>\n\n"
        f"လက်ရှိ Watermark - <b>{status}</b>\n\n"
        "Watermark ထည့်ရန် -\n"
        "<code>/setwm [စာသား]</code>\n"
        "ဥပမာ -\n"
        "<code>/setwm CMSM SHARE</code>\n\n"
        "Watermark ဖျက်ရန် စာသားမပါဘဲ <code>/setwm</code> ကို ရိုက်ပါ "
        "ဒါမှမဟုတ် အောက်က ခလုတ်ကို နှိပ်ပါ။"
    )
    keyboard = [
        [InlineKeyboardButton("🗑️ Watermark ဖျက်မည်", callback_data="remove_watermark")],
        [InlineKeyboardButton("⬅️ နောက်သို့", callback_data="back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def logo_menu(user_id):
    status = "✅ ထားရှိပြီး" if get_logo_path(user_id) else "❌ မထားရသေးပါ"
    text = (
        "📷 <b>Logo</b>\n\n"
        f"လက်ရှိ Logo - <b>{status}</b>\n\n"
        "Logo ထည့်ရန် -\n"
        "<code>/setlogo</code> ကို ရိုက်ပြီး Logo ပုံ Upload လုပ်ပါ။\n\n"
        "Logo ဖျက်ရန် -\n"
        "<code>/removelogo</code> ဒါမှမဟုတ် အောက်က ခလုတ်ကို နှိပ်ပါ။"
    )
    keyboard = [
        [InlineKeyboardButton("🗑️ Logo ဖျက်မည်", callback_data="remove_logo_btn")],
        [InlineKeyboardButton("⬅️ နောက်သို့", callback_data="back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


def bgm_menu(user_id):
    status = "✅ ထားရှိပြီး" if get_bgm_path(user_id) else "❌ မထားရသေးပါ"
    text = (
        "🎵 <b>BGM</b>\n\n"
        f"လက်ရှိ BGM - <b>{status}</b>\n\n"
        "BGM ထည့်ရန် -\n"
        "<code>/setbgm</code> ကို ရိုက်ပြီး Audio File Upload လုပ်ပါ။\n\n"
        "Video အရှည်အတိုင်း BGM ကို\n"
        "အလိုအလျောက် Loop ပြုလုပ်ပေးပါမည်။\n\n"
        "BGM ဖျက်ရန် -\n"
        "<code>/removebgm</code> ဒါမှမဟုတ် အောက်က ခလုတ်ကို နှိပ်ပါ။"
    )
    keyboard = [
        [InlineKeyboardButton("🗑️ BGM ဖျက်မည်", callback_data="remove_bgm_btn")],
        [InlineKeyboardButton("⬅️ နောက်သို့", callback_data="back")],
    ]
    return text, InlineKeyboardMarkup(keyboard)


HELP_TEXT = """
ℹ️ <b>အသုံးပြုရန်အချက်များ: တည်ဆောက်နည်း</b>

💧 <b>Watermark ထည့်ခြင်း</b>

<code>/setwm [စာသား]</code>
ကို ရိုက်လိုက်ပါ။

ဥပမာ -
<code>/setwm CMSM SHARE</code>

📷 <b>Logo ထည့်ခြင်း</b>

<code>/setlogo</code>
ကို ရိုက်ပြီး Logo ပုံ Upload လုပ်ပါ။

Logo ဖျက်ရန် -
<code>/removelogo</code>

🎵 <b>BGM ထည့်ခြင်း</b>

<code>/setbgm</code>
ကို ရိုက်ပြီး Audio File Upload လုပ်ပါ။

Video အရှည်အတိုင်း BGM ကို
အလိုအလျောက် Loop ပြုလုပ်ပေးပါမည်။

BGM ဖျက်ရန် -
<code>/removebgm</code>
"""


def help_menu():
    keyboard = [[InlineKeyboardButton("⬅️ နောက်သို့", callback_data="back")]]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, reply_markup = get_main_menu(update.effective_user.id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data

    await query.answer()

    if data == "noop":
        return  # section header, not a real button

    s = get_settings(user_id)

    if data == "toggle_auto_preview":
        s["autoPreview"] = not s.get("autoPreview", True)
    elif data == "toggle_auto_edit":
        s["autoEdit"] = not s.get("autoEdit", True)
    elif data == "toggle_flip":
        s["flip"] = not s["flip"]
    elif data == "toggle_multi":
        s["multiVoice"] = not s["multiVoice"]
    elif data == "toggle_sub":
        s["subtitles"] = not s["subtitles"]
    elif data == "toggle_trim":
        s["autoTrim"] = not s["autoTrim"]

    elif data == "change_size":
        keyboard = [[InlineKeyboardButton(label, callback_data=f"set_size_{i}")] for i, (val, label) in enumerate(SIZE_OPTIONS)]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await query.edit_message_text("📱 **Video Size ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    elif data == "change_voice":
        await query.edit_message_text("🎙️ **Myanmar အသံ ရွေးပါ**", reply_markup=get_voice_menu(), parse_mode="Markdown")
        return

    elif data.startswith("set_size_"):
        try:
            s["size"] = SIZE_OPTIONS[int(data[len("set_size_"):])][0]
        except (ValueError, IndexError):
            pass

    elif data.startswith("set_voice_"):
        voice_id = data[len("set_voice_"):]
        if get_voice_by_id(voice_id):
            s["voice"] = voice_id

    elif data == "toggle_blur":
        s["autoBlur"] = not s["autoBlur"]

    elif data == "change_subcolor":
        keyboard = [[InlineKeyboardButton(c["label"], callback_data=f"set_subcolor_{c['id']}")] for c in COLOR_OPTIONS]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await query.edit_message_text(
            "🎨 **Subtitle Color ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    elif data == "change_subfont":
        keyboard = [[InlineKeyboardButton(f["label"], callback_data=f"set_subfont_{f['id']}")] for f in FONT_OPTIONS]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await query.edit_message_text(
            "🔤 **Subtitle Font ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    elif data == "change_subsize":
        keyboard, row = [], []
        for size in FONT_SIZES:
            label = f"📏 {size}" + (" (Default)" if size == DEFAULT_FONT_SIZE else "")
            row.append(InlineKeyboardButton(label, callback_data=f"set_subsize_{size}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
        await query.edit_message_text(
            "📏 **Subtitle Font Size ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    elif data.startswith("set_subcolor_"):
        color_id = data[len("set_subcolor_"):]
        if get_color_by_id(color_id):
            s["subColor"] = color_id

    elif data.startswith("set_subfont_"):
        font_id = data[len("set_subfont_"):]
        if get_font_by_id(font_id):
            s["subFont"] = font_id

    elif data.startswith("set_subsize_"):
        try:
            s["subFontSize"] = int(data[len("set_subsize_"):])
        except ValueError:
            pass

    elif data == "open_title_menu":
        text, reply_markup = title_overlay_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "toggle_title_overlay":
        s["titleOverlay"] = not s["titleOverlay"]
        text, reply_markup = title_overlay_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "title_color":
        keyboard = [[InlineKeyboardButton(c["label"], callback_data=f"set_titlecolor_{c['id']}")] for c in COLOR_OPTIONS]
        keyboard.append([InlineKeyboardButton("⬅️ နောက်သို့", callback_data="open_title_menu")])
        await query.edit_message_text(
            "🎨 **Title Color ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    elif data == "title_font":
        keyboard = [[InlineKeyboardButton(f["label"], callback_data=f"set_titlefont_{f['id']}")] for f in FONT_OPTIONS]
        keyboard.append([InlineKeyboardButton("⬅️ နောက်သို့", callback_data="open_title_menu")])
        await query.edit_message_text(
            "🔤 **Title Font ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    elif data == "title_size":
        keyboard, row = [], []
        for size in FONT_SIZES:
            label = f"📏 {size}" + (" (Default)" if size == DEFAULT_FONT_SIZE else "")
            row.append(InlineKeyboardButton(label, callback_data=f"set_titlesize_{size}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("⬅️ နောက်သို့", callback_data="open_title_menu")])
        await query.edit_message_text(
            "📏 **Title Font Size ရွေးပါ**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return

    elif data.startswith("set_titlecolor_"):
        color_id = data[len("set_titlecolor_"):]
        if get_color_by_id(color_id):
            s["titleColor"] = color_id
        text, reply_markup = title_overlay_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data.startswith("set_titlefont_"):
        font_id = data[len("set_titlefont_"):]
        if get_font_by_id(font_id):
            s["titleFont"] = font_id
        text, reply_markup = title_overlay_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data.startswith("set_titlesize_"):
        try:
            s["titleFontSize"] = int(data[len("set_titlesize_"):])
        except ValueError:
            pass
        text, reply_markup = title_overlay_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "open_resolution_menu":
        text, reply_markup = resolution_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data.startswith("resolution_"):
        value = data[len("resolution_"):]
        if value in ("720", "1080"):
            s["resolution"] = value
        text, reply_markup = resolution_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "open_wm_menu":
        text, reply_markup = watermark_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "remove_watermark":
        s["watermark"] = ""
        text, reply_markup = watermark_menu(s)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "open_logo_menu":
        text, reply_markup = logo_menu(user_id)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "remove_logo_btn":
        _remove_logo_file(user_id)
        text, reply_markup = logo_menu(user_id)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "open_bgm_menu":
        text, reply_markup = bgm_menu(user_id)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "remove_bgm_btn":
        _remove_bgm_file(user_id)
        text, reply_markup = bgm_menu(user_id)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    elif data == "help":
        await query.edit_message_text(HELP_TEXT, parse_mode="HTML", reply_markup=help_menu())
        return

    elif data == "add_gemini":
        context.user_data["waiting_for"] = "gemini"
        await query.edit_message_text(
            "📥 **Gemini API Key** ပေးပို့ပါ။\n(key များစွာ ရှိရင် တစ်ကြောင်းစီ ဒါမှမဟုတ် comma ခြားပြီး တစ်ခါတည်း ပေးပို့နိုင်ပါတယ်)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode="Markdown",
        )
        return

    elif data == "add_groq":
        context.user_data["waiting_for"] = "groq"
        await query.edit_message_text(
            "📥 **Groq API Key** ပေးပို့ပါ။\n(key များစွာ ရှိရင် တစ်ကြောင်းစီ ဒါမှမဟုတ် comma ခြားပြီး တစ်ခါတည်း ပေးပို့နိုင်ပါတယ်)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode="Markdown",
        )
        return

    elif data == "check_keys":
        await query.edit_message_text("🔍 Key များကို စစ်ဆေးနေသည်... ခဏစောင့်ပါ။")
        # Runs several blocking HTTP calls - push to a worker thread so the
        # bot keeps responding to other users while this runs.
        status = await asyncio.to_thread(check_key_status, api_keys)
        lines = ["🔑 Key Status:", ""]
        for ktype in ["gemini", "groq"]:
            lines.append(f"{ktype.capitalize()}:")
            if not status[ktype]:
                lines.append("  (မရှိသေးပါ)")
            for item in status[ktype]:
                mark = "✅" if item["ok"] else f"❌ ({item.get('err', 'Error')})"
                lines.append(f"  - {item['key']}: {mark}")
        # Plain text (no Markdown) - error messages here can contain
        # characters like * or ` that would otherwise break Telegram's
        # Markdown parser and make this button silently fail.
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
        )
        return

    elif data == "clear_keys":
        api_keys["gemini"] = []
        api_keys["groq"] = []
        save_keys(api_keys)
        await query.edit_message_text(
            "🗑️ Keys cleared.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
        )
        return

    elif data in ("blurpos_up", "blurpos_down", "blurheight_up", "blurheight_down", "subpos_up", "subpos_down"):
        if data.startswith("blurheight_"):
            key = "blurHeight"
            step = BLUR_HEIGHT_STEP if data.endswith("_up") else -BLUR_HEIGHT_STEP
            s[key] = clamp_blur_height(s.get(key, 8) + step)
        else:
            key = "blurPos" if data.startswith("blurpos_") else "subPos"
            # blurPos/subPos = how far DOWN the frame the zone sits.
            step = -POS_STEP if data.endswith("_up") else POS_STEP
            s[key] = clamp_pos(s[key] + step)

        pending = context.user_data.get("pending_video")
        if pending and pending.get("input_path") and os.path.exists(pending["input_path"]):
            preview_img = pending.get("preview_img_path") or os.path.join(
                DOWNLOAD_DIR, f"preview_{user_id}_{uuid.uuid4().hex[:8]}.jpg"
            )
            await asyncio.to_thread(generate_position_preview, pending["input_path"], s, preview_img)
            pending["preview_img_path"] = preview_img
            try:
                with open(preview_img, "rb") as f:
                    await query.edit_message_media(
                        media=InputMediaPhoto(f, caption=preview_screen_text(s), parse_mode="Markdown"),
                        reply_markup=preview_screen_keyboard(s),
                    )
            except Exception:
                logger.exception("Failed to refresh position preview image")
        else:
            # No downloaded video left on disk (shouldn't normally happen) -
            # at least keep the caption/buttons in sync.
            try:
                await query.edit_message_caption(
                    caption=preview_screen_text(s), parse_mode="Markdown", reply_markup=preview_screen_keyboard(s)
                )
            except Exception:
                pass
        return

    elif data == "start_processing_confirmed":
        pending = context.user_data.get("pending_video")
        if not pending:
            try:
                await query.edit_message_caption(
                    caption="⚠️ Processing လုပ်ရန် Video မရှိတော့ပါ။ Video အသစ် ပြန်ပို့ပေးပါ။"
                )
            except Exception:
                await query.edit_message_text(
                    "⚠️ Processing လုပ်ရန် Video မရှိတော့ပါ။ Video အသစ် ပြန်ပို့ပေးပါ။"
                )
            return
        context.user_data["pending_video"] = None
        preview_img = pending.get("preview_img_path")
        if preview_img and os.path.exists(preview_img):
            try:
                os.remove(preview_img)
            except OSError:
                pass
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass
        status_msg = await context.bot.send_message(
            chat_id=chat_id, text="🔄 **Processing...**\n\nစတင်နေပါပြီ...", parse_mode="Markdown"
        )
        await run_processing(context, user_id, pending["input_path"], pending["output_path"], status_msg)
        return

    elif data == "cancel_processing":
        pending = context.user_data.get("pending_video")
        if pending:
            for p in (pending["input_path"], pending.get("output_path")):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            preview_img = pending.get("preview_img_path")
            if preview_img and os.path.exists(preview_img):
                try:
                    os.remove(preview_img)
                except OSError:
                    pass
        context.user_data["pending_video"] = None
        chat_id = query.message.chat_id
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat_id, text="❌ Processing ကို ပယ်ဖျက်လိုက်ပါပြီ။")
        return

    # Reaching this point means a toggle changed or "⬅️ Back" was pressed -
    # either way, cancel any pending "waiting for a pasted API key" state so
    # a stray text message afterwards isn't misread as a key.
    context.user_data["waiting_for"] = None

    text, reply_markup = get_main_menu(user_id)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    waiting_for = context.user_data.get("waiting_for")

    if waiting_for == "gemini":
        new_keys = [k for k in _split_keys(text, GEMINI_KEY_PREFIXES) if k not in api_keys["gemini"]]
        if new_keys:
            api_keys["gemini"].extend(new_keys)
            save_keys(api_keys)
            await update.message.reply_text(f"✅ Gemini Key {len(new_keys)} ခု ထည့်ပြီးပါပြီ။ (စုစုပေါင်း {len(api_keys['gemini'])} ခု)")
        else:
            await update.message.reply_text("⚠️ Valid Gemini key (AIza... သို့မဟုတ် AQ....) မတွေ့ပါ။")
        context.user_data["waiting_for"] = None
    elif waiting_for == "groq":
        new_keys = [k for k in _split_keys(text, "gsk_") if k not in api_keys["groq"]]
        if new_keys:
            api_keys["groq"].extend(new_keys)
            save_keys(api_keys)
            await update.message.reply_text(f"✅ Groq Key {len(new_keys)} ခု ထည့်ပြီးပါပြီ။ (စုစုပေါင်း {len(api_keys['groq'])} ခု)")
        else:
            await update.message.reply_text("⚠️ Valid Groq key (gsk_...) မတွေ့ပါ။")
        context.user_data["waiting_for"] = None
    else:
        # Not in a key-entry flow - just show the menu instead of ignoring them.
        pass

    text, reply_markup = get_main_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def set_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    parts = update.message.text.split(maxsplit=1)
    text = " ".join(parts[1].split()) if len(parts) > 1 else ""
    s["watermark"] = text
    if text:
        await update.message.reply_text(f"✅ Watermark သတ်မှတ်ပြီးပါပြီ - {text}")
    else:
        await update.message.reply_text("🗑️ Watermark ဖျက်လိုက်ပါပြီ။")


async def set_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for"] = "logo"
    await update.message.reply_text("📷 Logo အနေနဲ့ သုံးမည့် ပုံကို ပို့ပေးပါ။")


async def remove_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if _remove_logo_file(user_id):
        await update.message.reply_text("🗑️ Logo ဖျက်လိုက်ပါပြီ။")
    else:
        await update.message.reply_text("⚠️ Logo မထားရသေးပါ။")


async def set_bgm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for"] = "bgm"
    await update.message.reply_text("🎵 BGM အနေနဲ့ သုံးမည့် Audio File ကို ပို့ပေးပါ။")


async def remove_bgm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if _remove_bgm_file(user_id):
        await update.message.reply_text("🗑️ BGM ဖျက်လိုက်ပါပြီ။")
    else:
        await update.message.reply_text("⚠️ BGM မထားရသေးပါ။")


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only acts when a /setlogo command left the user in a 'waiting for a
    logo image' state - otherwise a stray photo is ignored rather than
    silently overwriting an existing logo."""
    if context.user_data.get("waiting_for") != "logo":
        return

    user_id = update.effective_user.id
    if update.message.photo:
        file_id = update.message.photo[-1].file_id  # highest resolution
    elif update.message.document:
        file_id = update.message.document.file_id
    else:
        return

    path = os.path.join(LOGO_DIR, f"{user_id}.png")
    try:
        file = await context.bot.get_file(file_id)
        await file.download_to_drive(path)
    except Exception as e:
        logger.exception("Logo download failed")
        await update.message.reply_text(f"❌ Logo ထည့်ရာတွင် အမှားရှိပါသည်: {str(e)[:200]}")
        return

    context.user_data["waiting_for"] = None
    await update.message.reply_text("✅ Logo သိမ်းဆည်းပြီးပါပြီ။")
    text, reply_markup = get_main_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only acts when a /setbgm command left the user in a 'waiting for a
    BGM file' state - otherwise a stray audio message is ignored."""
    if context.user_data.get("waiting_for") != "bgm":
        return

    user_id = update.effective_user.id
    file_id = None
    if update.message.audio:
        file_id = update.message.audio.file_id
    elif update.message.voice:
        file_id = update.message.voice.file_id
    elif update.message.document:
        mime = update.message.document.mime_type or ""
        if mime.startswith("audio/"):
            file_id = update.message.document.file_id
    if not file_id:
        await update.message.reply_text("❌ Audio file ပို့ပေးပါ။")
        return

    path = os.path.join(BGM_DIR, f"{user_id}.mp3")
    try:
        file = await context.bot.get_file(file_id)
        await file.download_to_drive(path)
    except Exception as e:
        logger.exception("BGM download failed")
        await update.message.reply_text(f"❌ BGM ထည့်ရာတွင် အမှားရှိပါသည်: {str(e)[:200]}")
        return

    context.user_data["waiting_for"] = None
    await update.message.reply_text("✅ BGM သိမ်းဆည်းပြီးပါပြီ။")
    text, reply_markup = get_main_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


def preview_screen_text(s):
    color = get_color_by_id(s["subColor"]) or get_color_by_id(DEFAULT_COLOR_ID)
    font = get_font_by_id(s["subFont"]) or get_font_by_id(DEFAULT_FONT_ID)
    return (
        "👁️ **Preview - Processing မလုပ်မီ လက်ရှိ Setting များ**\n\n"
        f"🛡️ Auto Edit (Metadata Cleanup): `{'ON ✅' if s.get('autoEdit', True) else 'OFF ❌'}`\n"
        f"🌫️ Auto Blur: `{'ON ✅' if s['autoBlur'] else 'OFF ❌'}`\n"
        f"📝 Subtitle: `{'ON ✅' if s['subtitles'] else 'OFF ❌'}` "
        f"(🎨 {color['name']} | 🔤 {font['name']} | 📏 {s['subFontSize']}px)\n"
        f"🏷️ Title Overlay: `{'ON ✅' if s['titleOverlay'] else 'OFF ❌'}`\n"
        f"📺 Resolution: `{s['resolution']}p`\n\n"
        f"🔵 Blur နေရာ: **{s['blurPos']}%** | အမြင့်: **{s.get('blurHeight', 8)}%** | 📝 Subtitle: **{s['subPos']}%**\n"
        "အနီရောင် ဇုန် = Blur ၊ အဝါရောင် ဇုန် = Subtitle\n"
        "⬆️⬇️ ခလုတ်များဖြင့် နေရာချိန်ပြီး 'မီဒီယို စတင်ရန်' နှိပ်ပါ။"
    )


def preview_screen_keyboard(s):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔵 Blur ⬆️ {s['blurPos']}%", callback_data="blurpos_up"),
            InlineKeyboardButton("🔵 Blur ⬇️", callback_data="blurpos_down"),
        ],
        [
            InlineKeyboardButton(f"🔹 Blur Size ➖ {s.get('blurHeight', 8)}%", callback_data="blurheight_down"),
            InlineKeyboardButton("Blur Size ➕", callback_data="blurheight_up"),
        ],
        [
            InlineKeyboardButton(f"📝 Sub ⬆️ {s['subPos']}%", callback_data="subpos_up"),
            InlineKeyboardButton("📝 Sub ⬇️", callback_data="subpos_down"),
        ],
        [InlineKeyboardButton("▶️ မီဒီယို စတင်ရန်", callback_data="start_processing_confirmed")],
        [InlineKeyboardButton("❌ ပယ်ဖျက်ရန်", callback_data="cancel_processing")],
    ])


async def send_preview_screen(context, user_id, status_msg, input_path):
    """Auto-Preview ON: show a frame grabbed from the video with the Blur
    (red) / Subtitle (yellow) zones marked on it, and wait for the user to
    confirm before actually processing the already-downloaded file. Replaces
    the plain-text status message with a photo message, since the zones are
    only meaningful as an image overlay."""
    s = get_settings(user_id)
    preview_img = os.path.join(DOWNLOAD_DIR, f"preview_{user_id}_{uuid.uuid4().hex[:8]}.jpg")
    await asyncio.to_thread(generate_position_preview, input_path, s, preview_img)

    chat_id = status_msg.chat_id
    try:
        await status_msg.delete()
    except Exception:
        pass

    with open(preview_img, "rb") as f:
        msg = await context.bot.send_photo(
            chat_id=chat_id,
            photo=f,
            caption=preview_screen_text(s),
            parse_mode="Markdown",
            reply_markup=preview_screen_keyboard(s),
        )

    pending = context.user_data.get("pending_video")
    if pending is not None:
        pending["preview_img_path"] = preview_img
        pending["preview_chat_id"] = msg.chat_id
        pending["preview_message_id"] = msg.message_id
    return msg


async def run_processing(context, user_id, input_path, output_path, status_msg):
    """Runs process_video on an already-downloaded file and reports progress
    on status_msg. Shared by the auto-queue path (video_handler) and the
    preview-confirmed path (button_handler), so it never touches
    update.message - only context.bot + status_msg's chat/message ids."""
    main_loop = asyncio.get_running_loop()

    def progress_callback(pct, text):
        # This runs inside a worker thread (asyncio.to_thread below), so we
        # can't create a task directly here - there's no event loop in this
        # thread. Schedule the coroutine back onto the bot's main loop instead.
        async def _update():
            try:
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text=f"🔄 **Processing...**\n\n{get_progress_bar(pct)}\n{text}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass  # e.g. message not modified - safe to ignore

        asyncio.run_coroutine_threadsafe(_update(), main_loop)

    try:
        success, message = await asyncio.to_thread(
            process_video, input_path, output_path, get_settings(user_id), api_keys, progress_callback,
            get_logo_path(user_id), get_bgm_path(user_id),
        )
    except Exception as e:
        logger.exception("process_video crashed")
        success, message = False, f"System Error: {str(e)[:200]}"

    if success:
        try:
            with open(output_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=status_msg.chat_id,
                    video=f,
                    caption="🎉 ဗီဒီယို အောင်မြင်စွာ ပြုပြင်ပြီးပါပြီ။",
                )
            await status_msg.delete()
        except Exception as e:
            logger.exception("Sending result video failed")
            await status_msg.edit_text(f"❌ ရလဒ်ဗီဒီယို ပို့ရာတွင် အမှားရှိပါသည်: {str(e)[:200]}")
    else:
        await status_msg.edit_text(f"❌ Error: {message}")

    # Cleanup
    for p in (input_path, output_path):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _pick_video(message):
    """Return (file_obj, file_size, ok, error_message)."""
    if message.video:
        return message.video, message.video.file_size, True, None
    if message.document:
        mime = message.document.mime_type or ""
        name = (message.document.file_name or "").lower()
        if mime.startswith("video/") or name.endswith((".mp4", ".mov", ".mkv", ".webm")):
            return message.document, message.document.file_size, True, None
        return None, None, False, "❌ Video file (mp4) ပို့ပေးပါ။ ဒီ file type ကို ပံ့ပိုးမထားပါ။"
    return None, None, False, "❌ Video file (mp4) ပို့ပေးပါ။"


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not api_keys["gemini"] or not api_keys["groq"]:
        await update.message.reply_text("❌ Keys အရင်ထည့်ပါ (Gemini + Groq)။ /start နှိပ်ပြီး 🔑 ခလုတ်များကို သုံးပါ။")
        return

    video, file_size, ok, err_msg = _pick_video(update.message)
    if not ok:
        await update.message.reply_text(err_msg)
        return

    if file_size and file_size > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"❌ ဗီဒီယို ဖိုင်က {MAX_DOWNLOAD_MB}MB ထက် ကြီးနေပါတယ်။ ဖိုင်ကို ချုံ့ပြီး ပြန်ပို့ပေးပါ။"
        )
        return

    status_msg = await update.message.reply_text(
        f"📥 **ဗီဒီယို လက်ခံရရှိပါပြီ**\n\n{get_progress_bar(10)}\n⏳ စောင့်ဆိုင်းပေးပါ...",
        parse_mode="Markdown",
    )

    run_id = uuid.uuid4().hex[:8]
    input_path = os.path.join(DOWNLOAD_DIR, f"input_{user_id}_{run_id}.mp4")
    output_path = os.path.join(DOWNLOAD_DIR, f"output_{user_id}_{run_id}.mp4")

    try:
        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(input_path)
    except Exception as e:
        logger.exception("Download failed")
        await status_msg.edit_text(f"❌ ဗီဒီယို download လုပ်ရာတွင် အမှားရှိပါသည်: {str(e)[:200]}")
        return

    if get_settings(user_id).get("autoPreview", True):
        # 👁️ Auto-Preview ON: hold the downloaded file and let the user
        # confirm the current settings before we actually process it.
        old_pending = context.user_data.get("pending_video")
        if old_pending:
            if old_pending.get("input_path") and os.path.exists(old_pending["input_path"]):
                try:
                    os.remove(old_pending["input_path"])
                except OSError:
                    pass
            old_img = old_pending.get("preview_img_path")
            if old_img and os.path.exists(old_img):
                try:
                    os.remove(old_img)
                except OSError:
                    pass
            if old_pending.get("preview_chat_id") and old_pending.get("preview_message_id"):
                try:
                    await context.bot.delete_message(
                        old_pending["preview_chat_id"], old_pending["preview_message_id"]
                    )
                except Exception:
                    pass
        context.user_data["pending_video"] = {
            "input_path": input_path,
            "output_path": output_path,
        }
        await send_preview_screen(context, user_id, status_msg, input_path)
        return

    # 👁️ Auto-Preview OFF: skip straight to processing with current settings.
    await run_processing(context, user_id, input_path, output_path, status_msg)


def main():
    if not TOKEN:
        print("Error: BOT_TOKEN not found in environment variables.")
        return

    builder = ApplicationBuilder().token(TOKEN)
    if LOCAL_BOT_API:
        builder = (
            builder
            .base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
        logger.info(
            "Using self-hosted Local Bot API server at %s (uploads/downloads up to %sMB).",
            LOCAL_BOT_API_URL, MAX_DOWNLOAD_MB,
        )
    else:
        logger.info(
            "Using Telegram's cloud Bot API (downloads capped at 20MB). "
            "Set TELEGRAM_API_ID/TELEGRAM_API_HASH to self-host the Local Bot API "
            "server and unlock larger files - see README."
        )
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setwm", set_watermark))
    app.add_handler(CommandHandler("setlogo", set_logo))
    app.add_handler(CommandHandler("removelogo", remove_logo))
    app.add_handler(CommandHandler("setbgm", set_bgm))
    app.add_handler(CommandHandler("removebgm", remove_bgm))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, photo_handler))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, audio_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))

    print("Bot started...")
    # close_loop=False: python-telegram-bot's default tries to close the
    # asyncio event loop when polling stops. Under nest_asyncio (Colab/
    # Jupyter), that loop is the notebook kernel's own already-running
    # loop, so closing it raises "RuntimeError: Cannot close a running
    # event loop" instead of shutting down cleanly. Leaving it open is
    # harmless when running as a plain script (main.py exits anyway).
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
