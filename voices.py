# -*- coding: utf-8 -*-
"""
Central definition of the Myanmar dubbing voices offered to users.

Both bot.py (Telegram voice-picker menu) and processor.py (the actual
Gemini TTS calls) import from this one file, so the button labels and
the voice actually used for dubbing can never drift out of sync.

Each Gemini TTS "voice_name" is one of Google's 30 prebuilt voices.
Google documents each by a *style* characteristic (Firm, Clear, Warm,
etc.), not by gender, so the male/female grouping below is our own
practical choice based on how each voice generally sounds - pick
freely and adjust names/voices to taste.
"""

VOICES = [
    {
        "id": "kyaw_zin",
        "name": "ကျော်ဇင်",
        "gender": "male",
        "google_voice": "Charon",
        "desc": "ရှင်းလင်းပြတ်သားသော အသံ",
    },
    {
        "id": "min_thu",
        "name": "မင်းသူ",
        "gender": "male",
        "google_voice": "Orus",
        "desc": "ခိုင်မာကျယ်လောင်သော အသံ",
    },
    {
        "id": "zeyar",
        "name": "ဇေယျာ",
        "gender": "male",
        "google_voice": "Iapetus",
        "desc": "ကြည်လင်နူးညံ့သော အသံ",
    },
    {
        "id": "thiri",
        "name": "သီရိ",
        "gender": "female",
        "google_voice": "Kore",
        "desc": "ခိုင်မာကြည်လင်သော အသံ",
    },
    {
        "id": "hnin_oo",
        "name": "နှင်းဦး",
        "gender": "female",
        "google_voice": "Leda",
        "desc": "လူငယ်ဆန်သွက်လက်သော အသံ",
    },
    {
        "id": "khin_za",
        "name": "ခင်ဇာ",
        "gender": "female",
        "google_voice": "Erinome",
        "desc": "ကြည်လင်နူးညံ့သော အသံ",
    },
]

DEFAULT_VOICE_ID = "kyaw_zin"

_BY_ID = {v["id"]: v for v in VOICES}


def get_voice_by_id(voice_id):
    return _BY_ID.get(voice_id)


def voices_by_gender(gender):
    return [v for v in VOICES if v["gender"] == gender]


def pair_voice(voice_id):
    """For Multi-Voice mode: pair the selected voice with the first voice
    of the opposite gender, so a two-speaker narration has real contrast."""
    v = get_voice_by_id(voice_id)
    if not v:
        return VOICES[0]
    opposite = "female" if v["gender"] == "male" else "male"
    candidates = voices_by_gender(opposite)
    return candidates[0] if candidates else v
