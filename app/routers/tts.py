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
OPENAI_TTS_MODEL = (os.getenv("OPENAI_TTS_MODEL", "tts-1") or "").strip()
OPENAI_TTS_VOICE = (os.getenv("OPENAI_TTS_VOICE", "alloy") or "").strip()

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

# Google Cloud TTS endpoint
GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
# OpenAI TTS endpoint
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"

# =========================
# LANG MAP
# =========================
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
    c = (code or "tr").strip().lower().replace("_", "-")
    if c == "pt-br":
        return "pt-br"
    if "-" in c and len(c) >= 4:
        base = c.split("-")[0]
        region = c.split("-")[1].upper()
        return f"{base}-{region}"
    return c


def lang_to_bcp47(code: str) -> str:
    c = canon_lang(code)
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
    user_id: Optional[str] = None  # ✅ yeni


class TTSResponse(FlexibleModel):
    ok: bool
    audio_base64: Optional[str] = None
    provider_used: Optional[str] = None
    error: Optional[str] = None


# =========================
# PROFILE LOOKUP
# =========================
async def get_user_tts_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id:
        return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        return None

    url = (
        f"{SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}"
        f"&select=id,tts_voice_provider,tts_voice_id,tts_voice_ready"
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                url,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                },
            )

        if r.status_code >= 400:
            logger.error("TTS_PROFILE_FETCH_FAIL %s %s", r.status_code, r.text[:400])
            return None

        arr = r.json()
        if not arr:
            return None

        row = arr[0] or {}
        if row.get("tts_voice_ready") and row.get("tts_voice_id"):
            return row

        return None

    except Exception as e:
        logger.exception("TTS_PROFILE_FETCH_EXCEPTION: %s", e)
        return None


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
# VOICE SELECTION
# =========================
def choose_default_openai_voice(req_voice: Optional[str], lang: str) -> str:
    if req_voice:
        return req_voice.strip()

    c = canon_lang(lang)

    # Türkçe için erkek fallback biraz daha doğal hissettirsin
    if c == "tr":
        return "alloy"

    return OPENAI_TTS_VOICE or "alloy"


# =========================
# ROUTE
# =========================
@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest) -> TTSResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    custom_profile = await get_user_tts_profile(req.user_id)

    # ✅ Şimdilik gerçek clone provider yok.
    # Ama custom voice hazırsa bunu işaretleyelim.
    provider_prefix = "custom-ready" if custom_profile else None

    # 1) Google
    g = await google_tts(text, req.lang, req.voice, req.speaking_rate, req.pitch)
    if g:
        return TTSResponse(
            ok=True,
            audio_base64=g,
            provider_used=f"{provider_prefix}+google" if provider_prefix else "google"
        )

    # 2) OpenAI fallback
    fallback_voice = choose_default_openai_voice(req.voice, req.lang)
    o = await openai_tts(text, fallback_voice)
    if o:
        return TTSResponse(
            ok=True,
            audio_base64=o,
            provider_used=f"{provider_prefix}+openai" if provider_prefix else "openai"
        )

    # 3) none
    return TTSResponse(ok=False, provider_used="none", error="TTS_UNAVAILABLE")
