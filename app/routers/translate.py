# FILE: app/routers/translate.py
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate_v2 as translate

router = APIRouter()

# =========================
# CONFIG
# =========================
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "").strip()  # optional

_translate_client: Optional[translate.Client] = None


def get_translate_client() -> translate.Client:
    """
    Force Service Account credentials (no API key).
    Uses GOOGLE_APPLICATION_CREDENTIALS file path.
    """
    global _translate_client

    if _translate_client is not None:
        return _translate_client

    if not GOOGLE_CREDS_PATH:
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS env var")

    if not os.path.exists(GOOGLE_CREDS_PATH):
        raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")

    creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)

    # project_id optional; client can infer from creds
    _translate_client = translate.Client(credentials=creds, project=GOOGLE_PROJECT_ID or None)
    return _translate_client


class TranslateIn(BaseModel):
    text: str
    source: Optional[str] = None   # e.g. "tr"
    target: str                    # e.g. "en"
    format: str = "text"           # "text" or "html"


class TranslateOut(BaseModel):
    translated: str
    detected_source: Optional[str] = None


@router.post("/translate", response_model=TranslateOut)
def translate_text(payload: TranslateIn):
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    target = (payload.target or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="target is required")

    source = (payload.source or "").strip() or None
    fmt = (payload.format or "text").strip()

    try:
        client = get_translate_client()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google credentials error: {e}")

    try:
        # translate_v2 returns dict with translatedText, detectedSourceLanguage, etc.
        res = client.translate(
            text,
            target_language=target,
            source_language=source,   # None => auto detect
            format_=fmt
        )

        translated = res.get("translatedText", "")
        detected = res.get("detectedSourceLanguage")

        return TranslateOut(translated=translated, detected_source=detected)

    except Exception as e:
        # This is where you previously got: "TranslateText are blocked"
        raise HTTPException(status_code=502, detail=f"Google Translate failed: {e}")
