# -*- coding: utf-8 -*-
"""
Central definition of the subtitle styling options (colour / font / size)
offered to users, plus the Auto Blur Mask defaults.

Both bot.py (the Telegram pickers) and processor.py (the actual ffmpeg
subtitle burn-in) import from this one file, so the button labels and
what's actually rendered can never drift out of sync - same pattern as
voices.py for the TTS voices.
"""

# Subtitle text colour. `ass` is the libass/ASS PrimaryColour value
# (format: &HAABBGGRR - note blue/green/red order, not RGB. AA=00 is
# fully opaque).
COLOR_OPTIONS = [
    {"id": "yellow", "name": "Yellow", "label": "🎨 အဝါရောင် (Yellow)", "ass": "&H0000FFFF"},
    {"id": "white", "name": "White", "label": "🎨 အဖြူရောင် (White)", "ass": "&H00FFFFFF"},
    {"id": "cyan", "name": "Cyan", "label": "🎨 စိမ်းပြာ (Cyan)", "ass": "&H00FFFF00"},
    {"id": "lime", "name": "Lime", "label": "🎨 စိမ်းစို (Lime)", "ass": "&H0000FF00"},
    {"id": "hot_pink", "name": "Hot Pink", "label": "🎨 ပန်းရောင် (Hot Pink)", "ass": "&H00B469FF"},
    {"id": "orange", "name": "Orange", "label": "🎨 လိမ္မော်ရောင် (Orange)", "ass": "&H0000A5FF"},
    {"id": "red", "name": "Red", "label": "🎨 အနီရောင် (Red)", "ass": "&H000000FF"},
    {"id": "light_blue", "name": "Light Blue", "label": "🎨 ကောင်းကင်ပြာ (Light Blue)", "ass": "&H00E6D8AD"},
    {"id": "gold", "name": "Gold", "label": "🎨 ရွှေရောင် (Gold)", "ass": "&H0000D7FF"},
    {"id": "magenta", "name": "Magenta", "label": "🎨 ခရမ်းရောင် (Magenta)", "ass": "&H00FF00FF"},
]
DEFAULT_COLOR_ID = "white"

# Subtitle font. `family` is the FontName libass will look for (verified
# via `fc-scan --format "%{family[0]}\n" <file>` against the actual .ttf).
# `file` is the filename expected inside FONTS_DIR (see processor.py) -
# drop the matching .ttf there to activate each entry. "default" reuses
# the base Myanmar font pointed to by the SUBTITLE_FONT_PATH env var.
FONT_OPTIONS = [
    {
        "id": "default",
        "name": "Default",
        "label": "🔤 Default",
        "family": "Noto Sans Myanmar",
        "file": None,
    },
    {
        "id": "noto_bold",
        "name": "Noto Sans Myanmar UI Bold",
        "label": "🔤 Noto Sans Myanmar UI (Bold)",
        "family": "Noto Sans Myanmar UI",
        "file": "NotoSansMyanmarUI-Bold.ttf",
    },
    {
        "id": "padauk",
        "name": "Padauk",
        "label": "🔤 Padauk",
        "family": "Padauk",
        "file": "Padauk-Regular.ttf",
    },
    {
        "id": "padauk_book",
        "name": "Padauk Book",
        "label": "🔤 Padauk Book",
        "family": "Padauk Book",
        "file": "PadaukBook-Regular.ttf",
    },
    {
        "id": "padauk_book_bold",
        "name": "Padauk Book Bold",
        "label": "🔤 Padauk Book (Bold)",
        "family": "Padauk Book Bold",
        "file": "PadaukBook-Bold.ttf",
    },
    {
        "id": "phantee",
        "name": "Phantee Hand Written",
        "label": "🔤 Phantee (Hand Written)",
        "family": "PT13_Phantee Hand Written",
        "file": "Phantee-HandWritten.ttf",
    },
    {
        "id": "myanmar_universal",
        "name": "Myanmar Universal",
        "label": "🔤 Myanmar Universal",
        "family": "!MyMyanmar Universal",
        "file": "MyanmarFont-Universal.ttf",
    },
    {
        "id": "akkhayar",
        "name": "Akkhayar Robo",
        "label": "🔤 Akkhayar Robo",
        "family": "Akkhayar Robo",
        "file": "AkkhayarRobo.ttf",
    },
]
DEFAULT_FONT_ID = "padauk_book"

# Subtitle font size (px-ish, passed straight to libass FontSize).
FONT_SIZES = [8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 35, 40, 45, 50, 55, 60]
DEFAULT_FONT_SIZE = 35

# Auto Blur Mask: blurs the bottom strip of the ORIGINAL video (where
# baked-in captions from the source often sit) before the new Myanmar
# subtitles are drawn on top. On by default, same as the prototype.
DEFAULT_AUTO_BLUR = True

_BY_COLOR_ID = {c["id"]: c for c in COLOR_OPTIONS}
_BY_FONT_ID = {f["id"]: f for f in FONT_OPTIONS}


def get_color_by_id(color_id):
    return _BY_COLOR_ID.get(color_id)


def get_font_by_id(font_id):
    return _BY_FONT_ID.get(font_id)
