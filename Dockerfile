FROM python:3.11-slim

# FFmpeg is used for video/audio processing and image overlays. Myanmar SRT
# text is shaped by Pango + HarfBuzz + FriBidi before FFmpeg sees it. Myanmar script coverage
# for burned-in text comes from the fonts/ directory bundled in this repo
# (Padauk, Padauk Book, Noto Sans Myanmar UI Bold, Phantee, Akkhayar Robo,
# Myanmar Universal - see styles.py FONT_OPTIONS), not from a system
# package, so subtitles render correctly even if fonts-noto-core doesn't
# happen to include Myanmar on a given base image. fonts-noto-core /
# fontconfig here are just for general Unicode coverage elsewhere.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libass9 \
    libharfbuzz0b \
    libharfbuzz-dev \
    libfribidi0 \
    libfribidi-dev \
    libraqm0 \
    libraqm-dev \
    libfreetype6-dev \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    pango1.0-tools \
    python3-gi \
    python3-cairo \
    python3-gi-cairo \
    gir1.2-pango-1.0 \
    gir1.2-pangocairo-1.0 \
    fonts-noto-core \
    fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
# Build Pillow with libraqm enabled. A normal Pillow wheel may omit RAQM;
# without RAQM, Burmese combining marks are placed by code-point order and
# the PNG shaping test can show the exact broken layout seen on Android.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && python -c "from PIL import features; import PIL; assert features.check_feature('raqm'), 'Pillow RAQM support is unavailable'; print('Pillow', PIL.__version__, 'RAQM=OK')"

# Copy the rest of the code
COPY . .

# Register the bundled Myanmar fonts with Fontconfig so Pango/HarfBuzz can
# resolve the exact family selected in styles.py.
RUN mkdir -p /usr/local/share/fonts/myanmar \
    && cp -f fonts/*.ttf /usr/local/share/fonts/myanmar/ \
    && fc-cache -f -v >/dev/null

# Smoke-test the actual system Python + PyGObject + Cairo + PangoCairo bridge
# used by processor.py. This catches the exact foreign-type error at build time.
RUN /usr/bin/python3 - <<'PY'
import cairo
import gi
gi.require_foreign("cairo")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo
s = cairo.ImageSurface(cairo.FORMAT_ARGB32, 32, 32)
ctx = cairo.Context(s)
layout = PangoCairo.create_layout(ctx)
layout.set_text("မြန်မာစာ", -1)
print("PangoCairo foreign Cairo Context: OK")
PY

# Create necessary directories. `fonts/` already ships with this repo's
# 7 bundled Myanmar subtitle/title fonts (see styles.py FONT_OPTIONS) -
# this mkdir just makes sure the directory (and SUBTITLE_FONTS_DIR) exists
# even if you volume-mount over it or add more fonts of your own. Any
# font file that's missing at runtime falls back to the bundled default
# (Padauk Book) instead of failing the render. `uploads/` holds temporary
# per-job video/logo/bgm uploads for the web app (web.py), cleaned up
# automatically after each job finishes.
RUN ffmpeg -hide_banner -filters 2>/dev/null | grep -Eq '[[:space:]](ass|subtitles)[[:space:]]' >/dev/null \
    || (echo "ERROR: FFmpeg libass subtitle filters are unavailable" >&2; exit 1)

RUN mkdir -p downloads uploads logs fonts

RUN chmod +x entrypoint.sh

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
EXPOSE 8000

# Starts the web app (web.py, served with uvicorn) - see entrypoint.sh.
CMD ["./entrypoint.sh"]
