# FILE: app/routers/tts.py

from __future__ import annotations

import os
import logging
import base64
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"

# 🔥 BURASI KRİTİK
CARTESIA_MODEL_ID = "sonic-3"

def canon_lang(code: str) -> str:
    return (code or "tr").strip().lower().replace("_", "-")

def lang_base(code: str) -> str:
    return canon_lang(code).split("-")[0]

def canon_voice(value: Optional[str]) -> str:
    v = (value or "auto").strip().lower()
    if v in ("own", "my"):
        return "clone"
    if v in ("female", "male", "clone"):
        return v
    return "auto"

def canon_tone(value: Optional[str]) -> str:
    v = (value or "neutral").strip().lower()
    if v in ("happy", "angry", "sad", "excited", "neutral"):
        return v
    return "neutral"


# 🔥 TON GÜÇLENDİRİLDİ
def tone_config(tone: str):
    t = canon_tone(tone)

    if t == "happy":
        return {"speed": 1.15, "volume": 1.1, "emotion": "positivity:high"}

    if t == "angry":
        return {"speed": 1.25, "volume": 1.15, "emotion": "anger:high"}

    if t == "sad":
        return {"speed": 0.85, "volume": 0.9, "emotion": "sadness:high"}

    if t == "excited":
        return {"speed": 1.3, "volume": 1.15, "emotion": "excitement:high"}

    return {"speed": 1.0, "volume": 1.0, "emotion": "neutral"}


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr"
    voice: Optional[str] = None
    tone: Optional[str] = "neutral"
    user_id: Optional[str] = None
    module: str = "facetoface"

class TTSResponse(FlexibleModel):
    ok: bool
    audio_base64: Optional[str] = None
    provider_used: Optional[str] = None
    error: Optional[str] = None


async def get_user_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        return None

    url = f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=tts_voice_id,tts_voice_ready"

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
            },
        )
        if r.status_code != 200:
            return None

        data = r.json()
        return data[0] if data else None


# 🔥 CLONE + TON
async def cartesia_tts(text: str, lang: str, voice_id: str, tone: str, use_tone=True):
    if not CARTESIA_API_KEY:
        return None

    payload = {
        "model_id": CARTESIA_MODEL_ID,
        "transcript": text,
        "voice": {
            "mode": "id",
            "id": voice_id,
        },
        "output_format": {
            "container": "mp3",
            "bit_rate": 128000,
            "sample_rate": 44100,
        },
        "language": lang_base(lang),
    }

    # 🔥 TON BURADA
    if use_tone:
        payload["generation_config"] = tone_config(tone)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                CARTESIA_TTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {CARTESIA_API_KEY}",
                    "Cartesia-Version": CARTESIA_VERSION,
                    "Content-Type": "application/json",
                },
            )

        if r.status_code >= 400:
            return None

        return base64.b64encode(r.content).decode("utf-8")

    except Exception:
        return None


@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest):

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    tone = canon_tone(req.tone)
    voice = canon_voice(req.voice)

    profile = await get_user_profile(req.user_id)

    voice_ready = bool(profile and profile.get("tts_voice_ready"))
    voice_id = (profile or {}).get("tts_voice_id")

    # 🔥 1. CLONE + TON
    if voice_ready and voice_id:
        audio = await cartesia_tts(text, req.lang, voice_id, tone, True)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="clone-tone")

        # 🔥 2. CLONE NORMAL (fallback)
        audio = await cartesia_tts(text, req.lang, voice_id, tone, False)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="clone")

    # 🔥 3. GOOGLE FALLBACK
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{GOOGLE_TTS_URL}?key={GOOGLE_API_KEY}",
                json={
                    "input": {"text": text},
                    "voice": {"languageCode": "en-US"},
                    "audioConfig": {"audioEncoding": "MP3"},
                },
            )

        if r.status_code == 200:
            audio = r.json().get("audioContent")
            return TTSResponse(ok=True, audio_base64=audio, provider_used="google")

    except Exception:
        pass

    return TTSResponse(ok=False, error="TTS_UNAVAILABLE")
