# italky-api/app/routers/translate.py
from __future__ import annotations

import os
import httpx
import hashlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# ===== ENV CONFIG =====
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"

LIBRE_BASE = os.getenv("LIBRE_BASE", "").rstrip("/")
LIBRE_KEY = os.getenv("LT_API_KEY", "").strip()

# ===== SIMPLE MEMORY CACHE (Production'da Redis'e geçilecek) =====
LOCAL_CACHE: dict[str, str] = {}

class TranslateIn(BaseModel):
    text: str
    source: str | None = None
    target: str | None = None
    from_lang: str | None = None
    to_lang: str | None = None


# ==============================
# Helpers
# ==============================
def normalize_lang(code: str | None) -> str:
    if not code:
        return ""
    return code.lower().split("-")[0].strip()


def cache_key(text: str, src: str, dst: str) -> str:
    raw = f"{text}|{src}|{dst}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ==============================
# GEMINI TRANSLATE (PRIMARY)
# ==============================
async def translate_gemini(text: str, src: str, dst: str) -> str | None:
    if not GEMINI_API_KEY:
        return None

    prompt = f"""
Translate the following text from {src} to {dst}.
Only return the translated text. No explanation.

Text:
{text}
"""

    body = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }

    url = f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=body)

    if r.status_code != 200:
        return None

    try:
        j = r.json()
        out = j["candidates"][0]["content"]["parts"][0]["text"]
        return out.strip()
    except Exception:
        return None


# ==============================
# LIBRE TRANSLATE (FALLBACK)
# ==============================
async def translate_libre(text: str, src: str, dst: str) -> str | None:
    if not LIBRE_BASE:
        return None

    url = f"{LIBRE_BASE}/translate"

    data = {
        "q": text,
        "source": src or "auto",
        "target": dst,
        "format": "text",
    }

    if LIBRE_KEY:
        data["api_key"] = LIBRE_KEY

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, data=data)

    if r.status_code != 200:
        return None

    try:
        j = r.json()
        return j.get("translatedText", "").strip()
    except Exception:
        return None


# ==============================
# MAIN ROUTE
# ==============================
@router.post("/translate")
async def translate(payload: TranslateIn):

    text = (payload.text or "").strip()
    if not text:
        return {"translated": ""}

    src = normalize_lang(payload.source or payload.from_lang or "auto")
    dst = normalize_lang(payload.target or payload.to_lang)

    if not dst:
        raise HTTPException(status_code=422, detail="target language required")

    if src == dst:
        return {"translated": text}

    key = cache_key(text, src, dst)

    # ===== CACHE CHECK =====
    if key in LOCAL_CACHE:
        return {"translated": LOCAL_CACHE[key], "cached": True}

    # ===== PRIMARY: GEMINI =====
    translated = await translate_gemini(text, src, dst)

    # ===== FALLBACK: LIBRE =====
    if not translated:
        translated = await translate_libre(text, src, dst)

    if not translated:
        raise HTTPException(status_code=502, detail="translation_failed")

    # ===== SAVE CACHE =====
    LOCAL_CACHE[key] = translated

    return {
        "translated": translated,
        "engine": "gemini" if GEMINI_API_KEY else "libre"
    }
