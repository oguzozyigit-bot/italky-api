from __future__ import annotations

import os
from typing import Optional

import requests
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["translate_ai"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()


class TranslateBody(BaseModel):
    text: str
    from_lang: str
    to_lang: str
    mode: Optional[str] = "normal"      # normal | cultural
    tone: Optional[str] = "neutral"     # neutral | happy | angry | sad | excited


def canonical(code: str) -> str:
    return str(code or "").strip().lower().split("-")[0]


def normalize_text(text: str) -> str:
    return str(text or "").strip()


def canonical_tone(tone: str) -> str:
    v = str(tone or "neutral").strip().lower()
    if v in {"neutral", "happy", "angry", "sad", "excited"}:
        return v
    return "neutral"


def detect_register_hint(text: str) -> str:
    s = normalize_text(text).lower()

    formal_markers = [
        "sayın", "saygılarımla", "arz ederim", "rica ederim", "teşekkür ederim",
        "lütfen", "bilginize", "tarafınıza", "konu hakkında", "gerekmektedir",
        "uygundur", "hususunda", "talep ediyorum", "değerlendirmenizi"
    ]

    casual_markers = [
        "ya", "abi", "abla", "kanka", "bence", "hadi", "vallahi", "valla",
        "tamam", "olur", "aynen", "şöyle", "şoyle", "yani", "off", "wow"
    ]

    formal_score = sum(1 for x in formal_markers if x in s)
    casual_score = sum(1 for x in casual_markers if x in s)

    if formal_score >= casual_score + 2:
        return "formal"
    if casual_score >= formal_score + 1:
        return "casual"
    return "neutral"


def google_translate_free(text: str, source: str, target: str) -> str:
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }

    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()

    translated = ""
    if isinstance(data, list) and data and isinstance(data[0], list):
        for item in data[0]:
            if isinstance(item, list) and item:
                translated += str(item[0] or "")
    return translated.strip()


def google_translate_official(text: str, source: str, target: str) -> str:
    if not GOOGLE_TRANSLATE_API_KEY:
        raise RuntimeError("GOOGLE_TRANSLATE_API_KEY missing")

    url = "https://translation.googleapis.com/language/translate/v2"
    payload = {
        "q": text,
        "source": source,
        "target": target,
        "format": "text",
        "key": GOOGLE_TRANSLATE_API_KEY,
    }

    r = requests.post(url, data=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    translated = (
        data.get("data", {})
        .get("translations", [{}])[0]
        .get("translatedText", "")
    )
    return str(translated).strip()


def gemini_cultural_translate(text: str, source: str, target: str, tone: str = "neutral") -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    tone = canonical_tone(tone)
    register_hint = detect_register_hint(text)

    tone_map = {
        "neutral": "Keep the tone natural, clear, smooth and human.",
        "happy": "Keep the tone warm, friendly, positive and lively.",
        "angry": "Keep the emotional intensity, but make it sound natural, socially appropriate and human.",
        "sad": "Keep the tone soft, sincere, empathetic and emotionally gentle.",
        "excited": "Keep the tone energetic, vivid, enthusiastic and natural."
    }

    register_map = {
        "formal": "If the source sounds formal, keep it respectful but do not make it stiff or robotic.",
        "casual": "If the source sounds casual, keep it relaxed, natural and conversational.",
        "neutral": "Prefer natural daily phrasing over overly formal or rigid wording."
    }

    tone_rule = tone_map.get(tone, tone_map["neutral"])
    register_rule = register_map.get(register_hint, register_map["neutral"])

    prompt = f"""
You are a highly natural conversational translator.

Translate the following text from "{source}" to "{target}".

Core rules:
- Preserve the exact meaning.
- Do not sound robotic, stiff, or overly literal.
- Make the result feel like something a real person would naturally say.
- Keep the speaker's emotional tone.
- Keep the social intent and politeness level.
- Prefer fluent, human, culturally appropriate phrasing.
- If a sentence sounds too formal in the target language, soften it slightly into natural spoken language without losing meaning.
- If the source is already casual, keep it casual and natural.
- Do not add extra explanation.
- Do not add quotation marks.
- Do not mention translation systems, AI tools, model names, brands, or technology.
- Return only the translated text.

Tone guidance:
{tone_rule}

Register guidance:
{register_rule}

Text:
{text}
""".strip()

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.5,
            "topP": 0.9,
            "maxOutputTokens": 1024
        }
    }

    r = requests.post(url, json=payload, timeout=40)
    r.raise_for_status()
    data = r.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini empty candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    out = "".join(str(p.get("text", "")) for p in parts).strip()

    if not out:
        raise RuntimeError("Gemini empty text")

    return out


@router.get("/api/translate_ai/health")
def translate_ai_health():
    return {"ok": True, "service": "translate_ai"}


@router.post("/api/translate_ai")
def translate_ai(body: TranslateBody):
    text = normalize_text(body.text)
    source = canonical(body.from_lang)
    target = canonical(body.to_lang)
    mode = str(body.mode or "normal").strip().lower()
    tone = canonical_tone(body.tone or "neutral")

    if not text:
        return {"ok": False, "error": "empty_text"}

    if not source or not target:
        return {"ok": False, "error": "missing_lang"}

    if source == target:
        return {
            "ok": True,
            "translated": text
        }

    if mode == "normal":
        try:
            translated = google_translate_official(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated
                }
        except Exception as e1:
            print("[translate_ai] google_official failed:", e1)

        try:
            translated = google_translate_free(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated
                }
        except Exception as e2:
            print("[translate_ai] google_free failed:", e2)

        return {
            "ok": False,
            "error": "normal_translate_failed"
        }

    if mode == "cultural":
        try:
            translated = gemini_cultural_translate(text, source, target, tone)
            if translated:
                return {
                    "ok": True,
                    "translated": translated
                }
        except Exception as e1:
            print("[translate_ai] gemini failed:", e1)

        try:
            translated = google_translate_official(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated
                }
        except Exception as e2:
            print("[translate_ai] google_official fallback failed:", e2)

        try:
            translated = google_translate_free(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated
                }
        except Exception as e3:
            print("[translate_ai] google_free fallback failed:", e3)

        return {
            "ok": False,
            "error": "cultural_translate_failed"
        }

    return {
        "ok": False,
        "error": "invalid_mode"
    }
