# FILE: italky-api/app/routers/voice_openai.py
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from openai import OpenAI

router = APIRouter()

# ENV
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    # Uygulama ayakta kalsın ama endpoint çağrılınca net hata versin
    client: Optional[OpenAI] = None
else:
    client = OpenAI(api_key=OPENAI_API_KEY)

TTS_MODEL = (os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts").strip()
TTS_VOICE = (os.getenv("OPENAI_TTS_VOICE") or "alloy").strip()
STT_MODEL = (os.getenv("OPENAI_STT_MODEL") or "whisper-1").strip()


@router.get("/voice/tts")
def voice_tts(
    text: str = Query(..., min_length=1),
    locale: str = Query("en"),
):
    """
    GET /api/voice/tts?text=apple&locale=en
    Response: audio/mpeg (mp3)
    """
    if client is None:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    try:
        # OpenAI TTS -> mp3 stream
        audio = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            format="mp3",
        )

        return StreamingResponse(
            audio.iter_bytes(),
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Italky-Locale": locale,
                "X-Italky-TTS-Model": TTS_MODEL,
                "X-Italky-TTS-Voice": TTS_VOICE,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS failed: {str(e)}")


@router.post("/voice/stt")
async def voice_stt(
    audio: UploadFile = File(...),
    locale: str = Query("en"),
):
    """
    POST /api/voice/stt?locale=en
    multipart/form-data:
      audio: file (webm/wav/mp3)
    Response: { "text": "apple" }
    """
    if client is None:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    try:
        data = await audio.read()
        if not data:
            return JSONResponse({"text": ""})

        # Whisper expects a file tuple (filename, bytes, content_type)
        transcription = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=(audio.filename or "speech.webm", data, audio.content_type or "audio/webm"),
        )

        text = (getattr(transcription, "text", "") or "").strip()
        return JSONResponse(
            {
                "text": text,
                "locale": locale,
                "model": STT_MODEL
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT failed: {str(e)}")
