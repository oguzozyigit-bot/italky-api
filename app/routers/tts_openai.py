from __future__ import annotations

import os
import base64
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_TTS_MODEL = (os.getenv("OPENAI_TTS_MODEL", "") or "gpt-4o-mini-tts").strip()
OPENAI_TTS_VOICE = (os.getenv("OPENAI_TTS_VOICE", "") or "alloy").strip()

# --------------------
# MODELS
# --------------------
class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TTSReq(FlexibleModel):
    text: str
    voice: str | None = None
    format: str | None = "mp3"
    speed: float | None = 1.0

class TTSRes(FlexibleModel):
    ok: bool
    audio_base64: str
    format: str

# --------------------
# HELPERS
# --------------------
def _ensure():
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

# --------------------
# POST /tts_openai
# --------------------
@router.post("/tts_openai", response_model=TTSRes)
def tts_openai(req: TTSReq):
    _ensure()

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")

    voice = (req.voice or OPENAI_TTS_VOICE).strip()
    fmt = (req.format or "mp3").lower()
    if fmt not in ("mp3", "wav", "aac", "flac", "opus"):
        fmt = "mp3"

    try:
        import requests
    except Exception:
        raise HTTPException(500, "requests missing")

    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_TTS_MODEL,
        "input": text[:4096],
        "voice": voice,
        "response_format": fmt,
        "speed": float(req.speed or 1.0),
    }

    try:
        r = requests.post(url, headers=headers, json=body, timeout=35)
        if r.status_code >= 400:
            raise HTTPException(502, r.text[:500])

        audio_bytes = r.content
        if not audio_bytes:
            raise HTTPException(502, "no audio returned")

        b64 = base64.b64encode(audio_bytes).decode("utf-8")
        return TTSRes(ok=True, audio_base64=b64, format=fmt)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("OPENAI_TTS_EXCEPTION %s", str(e))
        raise HTTPException(500, "tts_openai error")

# --------------------
# GET /tts_openai/_ping  ✅ EN ALTA
# --------------------
@router.get("/tts_openai/_ping")
def tts_openai_ping():
    return {
        "ok": True,
        "router": "tts_openai",
        "has_key": bool(OPENAI_API_KEY),
        "model": OPENAI_TTS_MODEL,
        "default_voice": OPENAI_TTS_VOICE,
    }
