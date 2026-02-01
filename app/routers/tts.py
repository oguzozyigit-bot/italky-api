# italky-api/app/routers/tts.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()

class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr-TR"   # tr-TR, en-US ...
    voice: Optional[str] = None
    speaking_rate: float = 1.0
    pitch: float = 0.0

class TTSResponse(FlexibleModel):
    ok: bool
    audio_base64: str

@router.get("/tts/ping")
def ping():
    return {"ok": True, "has_key": bool(GOOGLE_API_KEY)}

@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest):
    if not GOOGLE_API_KEY:
        raise HTTPException(500, "GOOGLE_API_KEY missing")
    if not (req.text or "").strip():
        raise HTTPException(400, "text required")

    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={GOOGLE_API_KEY}"

    body: Dict[str, Any] = {
        "input": {"text": req.text},
        "voice": {
            "languageCode": req.lang,
        },
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": float(req.speaking_rate),
            "pitch": float(req.pitch),
        },
    }
    if req.voice:
        body["voice"]["name"] = req.voice

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(url, json=body)
        if r.status_code >= 400:
            logger.error("TTS_FAIL %s %s", r.status_code, r.text[:400])
            raise HTTPException(r.status_code, "tts failed")

        data = r.json()
        audio = (data.get("audioContent") or "").strip()
        return TTSResponse(ok=True, audio_base64=audio)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("TTS_EXCEPTION: %s", str(e))
        raise HTTPException(500, "tts exception")
