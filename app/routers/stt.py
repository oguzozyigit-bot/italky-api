from __future__ import annotations

import io
import json
import os
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import speech

router = APIRouter(tags=["stt"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()

_client: Optional[speech.SpeechClient] = None


def _load_credentials():
    if GOOGLE_CREDS_JSON:
        try:
            info = json.loads(GOOGLE_CREDS_JSON)
            return service_account.Credentials.from_service_account_info(info)
        except Exception as e:
            raise RuntimeError(f"Invalid GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")

    if GOOGLE_CREDS_PATH:
        if not os.path.exists(GOOGLE_CREDS_PATH):
            raise RuntimeError(f"Credential file not found: {GOOGLE_CREDS_PATH}")
        try:
            return service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
        except Exception as e:
            raise RuntimeError(f"Could not load credentials file: {e}")

    raise RuntimeError("Google credentials missing")


def get_client() -> speech.SpeechClient:
    global _client

    if _client is not None:
        return _client

    creds = _load_credentials()
    _client = speech.SpeechClient(credentials=creds)
    return _client


class STTResponse(BaseModel):
    text: str
    model_used: str


def guess_encoding(content_type: str) -> Optional[speech.RecognitionConfig.AudioEncoding]:
    ct = str(content_type or "").lower()

    if "wav" in ct or "wave" in ct:
        return speech.RecognitionConfig.AudioEncoding.LINEAR16
    if "ogg" in ct:
        return speech.RecognitionConfig.AudioEncoding.OGG_OPUS
    if "webm" in ct:
        return speech.RecognitionConfig.AudioEncoding.WEBM_OPUS
    if "mp3" in ct or "mpeg" in ct:
        return speech.RecognitionConfig.AudioEncoding.MP3

    return None


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
        encoding = guess_encoding(file.content_type or "")

        config_kwargs = {
            "language_code": (lang or "tr-TR").strip() or "tr-TR",
            "enable_automatic_punctuation": True,
        }

        if encoding is not None:
            config_kwargs["encoding"] = encoding

        config = speech.RecognitionConfig(**config_kwargs)

        # Kısa sesler için sync recognize yeterli
        response = client.recognize(config=config, audio=audio)

        transcript_parts = []
        for result in response.results:
            if result.alternatives:
                transcript_parts.append(result.alternatives[0].transcript)

        transcript = " ".join(transcript_parts).strip()

        if not transcript:
            raise HTTPException(status_code=502, detail="Empty transcription")

        return STTResponse(
            text=transcript,
            model_used="google-speech"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google STT failed: {e}")
