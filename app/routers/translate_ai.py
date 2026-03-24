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
    mode: Optional[str] = "normal"   # normal | cultural


def canonical(code: str) -> str:
    return str(code or "").strip().lower().split("-")[0]


def normalize_text(text: str) -> str:
    return str(text or "").strip()


def google_translate_free(text: str, source: str, target: str) -> str:
    """
    Google public translate endpoint.
    API key gerektirmez.
    Stabilite %100 garanti değil ama fallback için çok işe yarar.
    """
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

    # Beklenen format:
    # [[["Merhaba","Hello",...], ...], ...]
    translated = ""
    if isinstance(data, list) and data and isinstance(data[0], list):
        for item in data[0]:
            if isinstance(item, list) and item:
                translated += str(item[0] or "")
    return translated.strip()


def google_translate_official(text: str, source: str, target: str) -> str:
    """
    Cloud Translation API key varsa onu kullan.
    """
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


def gemini_cultural_translate(text: str, source: str, target: str) -> str:
    """
    Kültürel / doğal çeviri için Gemini.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    prompt = f"""
You are a high quality translator.

Task:
Translate the following text from "{source}" to "{target}".

Rules:
- Preserve meaning exactly.
- Make the output natural, fluent and culturally appropriate.
- Keep the emotional tone.
- Do not explain anything.
- Do not add quotation marks.
- Return only the translated text.

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
            "temperature": 0.35,
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

    parts = (
        candidates[0]
        .get("content", {})
        .get("parts", [])
    )

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

    if not text:
        return {"ok": False, "error": "empty_text"}

    if not source or not target:
        return {"ok": False, "error": "missing_lang"}

    if source == target:
        return {
            "ok": True,
            "translated": text,
            "engine": "identity"
        }

    # 1) NORMAL MOD = direkt Google Translate
    if mode == "normal":
        try:
            translated = google_translate_official(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated,
                    "engine": "google_official"
                }
        except Exception as e1:
            print("[translate_ai] google_official failed:", e1)

        try:
            translated = google_translate_free(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated,
                    "engine": "google_free"
                }
        except Exception as e2:
            print("[translate_ai] google_free failed:", e2)

        return {
            "ok": False,
            "error": "normal_translate_failed"
        }

    # 2) CULTURAL MOD = önce Gemini, patlarsa Google Translate
    if mode == "cultural":
        try:
            translated = gemini_cultural_translate(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated,
                    "engine": "gemini"
                }
        except Exception as e1:
            print("[translate_ai] gemini failed:", e1)

        try:
            translated = google_translate_official(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated,
                    "engine": "google_official_fallback"
                }
        except Exception as e2:
            print("[translate_ai] google_official fallback failed:", e2)

        try:
            translated = google_translate_free(text, source, target)
            if translated:
                return {
                    "ok": True,
                    "translated": translated,
                    "engine": "google_free_fallback"
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
