# FILE: italky-api/app/routers/tts_openai.py
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

ALLOWED_VOICES = {"alloy", "ash", "nova", "shimmer", "echo", "fable", "onyx"}
ALLOWED_FORMATS = {"mp3", "wav", "aac", "flac", "opus", "pcm"}


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


def _ensure():
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing (Render ENV)")


@router.get("/tts_openai/_ping")
def tts_openai_ping():
    return {
        "ok": True,
        "router": "tts_openai",
        "has_key": bool(OPENAI_API_KEY),
        "model": OPENAI_TTS_MODEL,
        "default_voice": OPENAI_TTS_VOICE,
    }


@router.post("/tts_openai", response_model=TTSRes)
def tts_openai(req: TTSReq):
    """
    OpenAI TTS: POST https://api.openai.com/v1/audio/speech
    """
    _ensure()

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")

    voice = (req.voice or OPENAI_TTS_VOICE or "alloy").strip().lower()
    if voice not in ALLOWED_VOICES:
        voice = (OPENAI_TTS_VOICE or "alloy").strip().lower()
        if voice not in ALLOWED_VOICES:
            voice = "alloy"

    fmt = (req.format or "mp3").strip().lower()
    if fmt not in ALLOWED_FORMATS:
        fmt = "mp3"

    speed = float(req.speed or 1.0)
    if speed < 0.25:
        speed = 0.25
    if speed > 2.0:
        speed = 2.0

    try:
        import requests  # type: ignore
    except Exception:
        raise HTTPException(500, "requests missing on server")

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
        "speed": speed,
    }

    try:
        r = requests.post(url, headers=headers, json=body, timeout=45)

        if r.status_code >= 400:
            b = (r.text or "")[:1600]
            logger.error("OPENAI_TTS_FAIL status=%s body=%s", r.status_code, b)
            raise HTTPException(502, f"openai_tts_error status={r.status_code} body={b}")

        audio_bytes = r.content or b""
        if not audio_bytes:
            raise HTTPException(502, "openai_tts_no_audio")

        b64 = base64.b64encode(audio_bytes).decode("utf-8")
        return TTSRes(ok=True, audio_base64=b64, format=fmt)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("OPENAI_TTS_EXCEPTION %s", str(e))
        raise HTTPException(500, "tts_openai error")
