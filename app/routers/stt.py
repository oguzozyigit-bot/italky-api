from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import speech

router = APIRouter(tags=["stt"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()

_client: Optional[speech.SpeechClient] = None


def get_client() -> speech.SpeechClient:
    global _client

    if _client:
        return _client

    if not GOOGLE_CREDS_PATH:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS missing")

    if not os.path.exists(GOOGLE_CREDS_PATH):
        raise RuntimeError(f"Credential file not found: {GOOGLE_CREDS_PATH}")

    creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
    _client = speech.SpeechClient(credentials=creds)
    return _client


class STTResponse(BaseModel):
    text: str
    model_used: str


@router.post("/stt", response_model=STTResponse)
async def stt(
    file: UploadFile = File(...),
    lang: Optional[str] = Form(default="tr-TR"),
):
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="Empty audio")

    try:
        client = get_client()

        audio = speech.RecognitionAudio(content=audio_bytes)

        # Encoding zorlamıyoruz → Google otomatik algılasın
        config = speech.RecognitionConfig(
            language_code=lang or "tr-TR",
        )

        response = client.recognize(config=config, audio=audio)

        transcript = ""
        for result in response.results:
            transcript += result.alternatives[0].transcript + " "

        transcript = transcript.strip()

        if not transcript:
            raise HTTPException(status_code=502, detail="Empty transcription")

        return STTResponse(text=transcript, model_used="google-speech")

    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google STT failed: {e}")
