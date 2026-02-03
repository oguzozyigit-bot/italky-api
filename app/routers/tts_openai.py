# FILE: italky-api/app/routers/tts_openai.py
from __future__ import annotations

import os
import base64
import logging
import requests  # pip install requests

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

# Log ayarları
logger = logging.getLogger("uvicorn.error")
router = APIRouter()

# --- AYARLAR ---
# Canlı sohbet için en hızlı model 'tts-1'dir.
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_TTS_MODEL = (os.getenv("OPENAI_TTS_MODEL", "") or "tts-1").strip()
OPENAI_TTS_VOICE = (os.getenv("OPENAI_TTS_VOICE", "") or "alloy").strip()

ALLOWED_VOICES = {"alloy", "ash", "nova", "shimmer", "echo", "fable", "onyx"}
ALLOWED_FORMATS = {"mp3", "wav", "aac", "flac", "opus", "pcm"}

# --- MODELLER ---
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

# --- YARDIMCI FONKSİYONLAR ---
def _ensure():
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY eksik. Lütfen Render/Env ayarlarını kontrol et.")
        raise HTTPException(500, "Sunucu yapılandırma hatası: API Key eksik.")

# --- ENDPOINTLER ---

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
    OpenAI TTS: Metni sese çevirir ve Base64 döner.
    Canlı sohbet için optimize edilmiştir.
    """
    _ensure()

    # 1. Validasyon
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "Metin boş olamaz.")

    # Ses seçimi (Varsayılan: Alloy)
    voice = (req.voice or OPENAI_TTS_VOICE or "alloy").strip().lower()
    if voice not in ALLOWED_VOICES:
        voice = "alloy"

    # Format (Varsayılan: mp3)
    fmt = (req.format or "mp3").strip().lower()
    if fmt not in ALLOWED_FORMATS:
        fmt = "mp3"

    # Hız (0.25 - 4.0 arası, biz güvenli aralıkta tutalım)
    speed = float(req.speed or 1.0)
    speed = max(0.25, min(speed, 4.0))

    # 2. OpenAI İsteği
    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_TTS_MODEL, # tts-1 (Hızlı)
        "input": text[:4096],      # OpenAI limiti
        "voice": voice,
        "response_format": fmt,
        "speed": speed,
    }

    try:
        # Timeout'u 10 saniye yaptım, canlı sohbette daha fazla beklememeli
        r = requests.post(url, headers=headers, json=body, timeout=10)

        if r.status_code >= 400:
            error_msg = r.text[:200]
            logger.error(f"OPENAI TTS ERROR: {r.status_code} - {error_msg}")
            raise HTTPException(502, f"OpenAI Hatası: {error_msg}")

        audio_bytes = r.content
        if not audio_bytes:
            raise HTTPException(502, "OpenAI ses verisi döndürmedi.")

        # 3. Base64 Çevrimi
        b64 = base64.b64encode(audio_bytes).decode("utf-8")
        return TTSRes(ok=True, audio_base64=b64, format=fmt)

    except requests.Timeout:
        logger.error("OpenAI TTS zaman aşımına uğradı.")
        raise HTTPException(504, "Ses üretimi çok uzun sürdü.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS EXCEPTION: {str(e)}")
        raise HTTPException(500, "Sunucu içi ses hatası.")
