# FILE: italky-api/app/routers/voice_openai.py
from __future__ import annotations

import os
from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse
from openai import OpenAI

router = APIRouter()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy")

# STT için default: whisper-1 (stabil)
STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1")


@router.get("/voice/tts")
def tts(text: str = Query(..., min_length=1), locale: str = Query("en")):
    """
    GET /api/voice/tts?text=apple&locale=en
    Return: audio/mpeg stream
    """
    try:
        # OpenAI TTS -> mp3 stream
        audio = client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            format="mp3",
        )
        # audio is a stream-like object in recent SDKs
        return StreamingResponse(audio.iter_bytes(), media_type="audio/mpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS failed: {str(e)}")


@router.post("/voice/stt")
async def stt(
    audio: UploadFile = File(...),
    locale: str = Query("en"),
):
    """
    POST /api/voice/stt?locale=en  (multipart/form-data)
    FormData: audio=<file/webm|wav|mp3>
    Return: { "text": "apple" }
    """
    try:
        data = await audio.read()
        if not data:
            return JSONResponse({"text": ""})

        # Whisper expects a file-like; SDK accepts (filename, bytes, content_type)
        transcription = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=(audio.filename or "audio.webm", data, audio.content_type or "audio/webm"),
        )

        text = (getattr(transcription, "text", "") or "").strip()
        return JSONResponse({"text": text})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT failed: {str(e)}")
