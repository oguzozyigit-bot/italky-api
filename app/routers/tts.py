# FILE: italky-api/app/routers/tts.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import base64
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

# =========================
# ENV
# =========================
GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
# Ucuz ve stabil seçenek: tts-1
OPENAI_TTS_MODEL = (os.getenv("OPENAI_TTS_MODEL", "tts-1") or "").strip()
OPENAI_TTS_VOICE = (os.getenv("OPENAI_TTS_VOICE", "alloy") or "").strip()

# Google Cloud TTS endpoint
GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
# OpenAI TTS endpoint
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"

# Basit dil->BCP47 map (Google voice languageCode için)
# FaceToFace'te çok dil olduğundan geniş tuttuk.
LANG_BCP47 = {
    "tr": "tr-TR",
    "en": "en-US",
    "en-gb": "en-GB",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "ru": "ru-RU",
    "pt": "pt-PT",
    "pt-br": "pt-BR",
    "nl": "nl-NL",
    "sv": "sv-SE",
    "no": "nb-NO",
    "nb": "nb-NO",
    "da": "da-DK",
    "fi": "fi-FI",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "sk": "sk-SK",
    "hu": "hu-HU",
    "ro": "ro-RO",
    "bg": "bg-BG",
    "el": "el-GR",
    "uk": "uk-UA",
    "ar": "ar-XA",
    "he": "he-IL",
    "fa": "fa-IR",
    "ur": "ur-PK",
    "hi": "hi-IN",
    "bn": "bn-BD",
    "id": "id-ID",
    "ms": "ms-MY",
    "vi": "vi-VN",
    "th": "th-TH",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "ka": "ka-GE",
}

def canon_lang(code: str) -> str:
    """
    input: "tr", "tr_TR", "tr-TR", "pt-br", "en-GB"...
    output: normalized lower-base + optional region (keeps pt-br special)
    """
    c = (code or "tr").strip().lower().replace("_", "-")
    if c == "pt-br":
        return "pt-br"
    # if "xx-YY" keep it (YY upper for BCP47 lookup)
    if "-" in c and len(c) >= 4:
        base = c.split("-")[0]
        region = c.split("-")[1].upper()
        return f"{base}-{region}"
    return c

def lang_to_bcp47(code: str) -> str:
    c = canon_lang(code)
    # If already looks like "xx-YY", accept as-is
    if "-" in c and len(c.split("-")[1]) == 2:
        return c
    return LANG_BCP47.get(c, "en-US")

# =========================
# SCHEMAS
# =========================
class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr"
    voice: Optional[str] = None
    speaking_rate: float = 1.0
    pitch: float = 0.0

class TTSResponse(FlexibleModel):
    ok: bool
    audio_base64: Optional[str] = None
    provider_used: Optional[str] = None
    error: Optional[str] = None

# =========================
# GOOGLE
# =========================
async def google_tts(
    text: str,
    lang: str,
    voice: Optional[str],
    speaking_rate: float,
    pitch: float
) -> Optional[str]:
    """
    Returns audio_base64 on success, None on failure.
    """
    if not GOOGLE_API_KEY:
        logger.warning("TTS_GOOGLE: GOOGLE_API_KEY missing -> skip")
        return None

    bcp47 = lang_to_bcp47(lang)

    payload: Dict[str, Any] = {
        "input": {"text": text},
        "voice": {"languageCode": bcp47},
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": float(speaking_rate or 1.0),
            "pitch": float(pitch or 0.0),
        },
    }
    if voice:
        payload["voice"]["name"] = voice

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{GOOGLE_TTS_URL}?key={GOOGLE_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if r.status_code >= 400:
            logger.error("TTS_FAIL_GOOGLE %s %s", r.status_code, r.text[:500])
            return None

        data = r.json()
        audio_b64 = (data.get("audioContent") or "").strip()
        return audio_b64 or None

    except Exception as e:
        logger.exception("TTS_GOOGLE_EXCEPTION: %s", e)
        return None

# =========================
# OPENAI FALLBACK
# =========================
async def openai_tts(text: str, voice: Optional[str]) -> Optional[str]:
    """
    OpenAI audio/speech -> returns base64 mp3
    """
    if not OPENAI_API_KEY:
        logger.warning("TTS_OPENAI: OPENAI_API_KEY missing -> skip")
        return None

    v = (voice or OPENAI_TTS_VOICE or "alloy").strip()
    model = (OPENAI_TTS_MODEL or "tts-1").strip()

    payload = {
        "model": model,
        "voice": v,
        "input": text,
        "format": "mp3",
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                OPENAI_TTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )

        if r.status_code >= 400:
            # OpenAI audio endpoint sometimes returns JSON error text
            try:
                err_txt = r.text
            except Exception:
                err_txt = "<no-text>"
            logger.error("TTS_FAIL_OPENAI %s %s", r.status_code, err_txt[:800])
            return None

        b = r.content or b""
        if not b:
            logger.error("TTS_OPENAI_EMPTY")
            return None

        return base64.b64encode(b).decode("utf-8")

    except Exception as e:
        logger.exception("TTS_OPENAI_EXCEPTION: %s", e)
        return None

# =========================
# ROUTE (GOOGLE -> OPENAI)
# =========================
@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest) -> TTSResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    # 1) Google
    g = await google_tts(text, req.lang, req.voice, req.speaking_rate, req.pitch)
    if g:
        return TTSResponse(ok=True, audio_base64=g, provider_used="google")

    # 2) OpenAI fallback
    o = await openai_tts(text, req.voice)
    if o:
        return TTSResponse(ok=True, audio_base64=o, provider_used="openai")

    # 3) none
    return TTSResponse(ok=False, provider_used="none", error="TTS_UNAVAILABLE")
