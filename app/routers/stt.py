from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["stt"])

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"

class STTResponse(BaseModel):
    text: str
    model_used: str

@router.post("/stt", response_model=STTResponse)
async def stt(
    file: UploadFile = File(...),
    lang: Optional[str] = Form(default=None),      # "tr", "en" vb (opsiyonel)
    model: Optional[str] = Form(default=None),     # opsiyonel override
):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="Empty audio")

    # hızlı model -> fallback whisper
    model_try = [model] if model else ["gpt-4o-mini-transcribe", "whisper-1"]

    headers = { "Authorization": f"Bearer {OPENAI_API_KEY}" }

    last_err = None
    for m in model_try:
        try:
            data = {
                "model": m,
                "response_format": "json",
            }
            if lang:
                data["language"] = lang

            files = {
                "file": (file.filename or "audio.webm", audio_bytes, file.content_type or "audio/webm")
            }

            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(OPENAI_TRANSCRIPTIONS_URL, headers=headers, data=data, files=files)

            if r.status_code >= 400:
                last_err = r.text
                continue

            j = r.json()
            text = (j.get("text") or "").strip()
            if not text:
                last_err = "empty transcription"
                continue

            return STTResponse(text=text, model_used=m)

        except Exception as e:
            last_err = str(e)
            continue

    raise HTTPException(status_code=502, detail=f"stt failed: {last_err}")
