# FILE: italky-api/app/routers/tts.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

# =========================
# ENV (GOOGLE ONLY)
# =========================
GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()

# Google Cloud TTS endpoint
GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

# Basit dil->BCP47 map (Google voice languageCode için)
LANG_BCP47 = {
    "tr": "tr-TR",
    "en": "en-US",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "pt": "pt-PT",
    "pt-br": "pt-BR",
    "nl": "nl-NL",
    "sv": "sv-SE",
    "no": "nb-NO",
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
    "ru": "ru-RU",
    "ar": "ar-XA",   # Google genelde ar-XA kullanır
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
    "ja": "ja-JP",
    "ko": "ko-KR",
    "ka": "ka-GE",
}

def canon_lang(code: str) -> str:
    c = (code or "tr").strip().lower()
    c = c.replace("_", "-")
    # "tr-TR" gibi gelirse direkt kullan
    if "-" in c and len(c) >= 4:
        base = c.split("-")[0]
        region = c.split("-")[1].upper()
        return f"{base}-{region}"
    if c == "pt-br":
        return "pt-br"
    return c

def lang_to_bcp47(code: str) -> str:
    c = canon_lang(code)
    if "-" in c:
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
# PROVIDER (GOOGLE ONLY)
# =========================
async def google_tts(text: str, lang: str, voice: Optional[str], speaking_rate: float, pitch: float) -> Optional[str]:
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
# ROUTE (NO OPENAI FALLBACK)
# =========================
@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest) -> TTSResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    g = await google_tts(text, req.lang, req.voice, req.speaking_rate, req.pitch)
    if g:
        return TTSResponse(ok=True, audio_base64=g, provider_used="google")

    # ✅ Kural: asla OpenAI TTS fallback yok
    # Frontend bozulmasın diye 200 + ok:false dönüyoruz
    return TTSResponse(ok=False, provider_used="google", error="TTS_UNAVAILABLE")
