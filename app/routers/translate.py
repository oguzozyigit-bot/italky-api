# FILE: app/routers/translate.py
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3  # ✅ v3 client

router = APIRouter()

GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
GOOGLE_PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID", "").strip()  # opsiyonel ama öneririm

_client: Optional[translate_v3.TranslationServiceClient] = None


def get_client() -> translate_v3.TranslationServiceClient:
    global _client

    if _client is not None:
        return _client

    if not GOOGLE_CREDS_PATH:
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS env var")

    if not os.path.exists(GOOGLE_CREDS_PATH):
        raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")

    creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
    _client = translate_v3.TranslationServiceClient(credentials=creds)
    return _client


def infer_project_id_from_creds() -> str:
    # GOOGLE_PROJECT_ID yoksa creds içinden okumayı dener
    if GOOGLE_PROJECT_ID:
        return GOOGLE_PROJECT_ID
    try:
        creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
        pid = getattr(creds, "project_id", "") or ""
        if pid:
            return pid
    except Exception:
        pass
    return ""


class TranslateIn(BaseModel):
    text: str
    source: Optional[str] = None  # "tr"
    target: str                   # "en"
    mime_type: str = "text/plain" # "text/plain" or "text/html"


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
    mime_type = (payload.mime_type or "text/plain").strip()

    try:
        client = get_client()
        project_id = infer_project_id_from_creds()
        if not project_id:
            raise RuntimeError("Missing GOOGLE_PROJECT_ID (and could not infer from credentials)")

        parent = f"projects/{project_id}/locations/global"

        req = {
            "parent": parent,
            "contents": [text],
            "target_language_code": target,
            "mime_type": mime_type,
        }
        if source:
            req["source_language_code"] = source

        resp = client.translate_text(request=req)

        translated = ""
        detected = None
        if resp.translations:
            translated = resp.translations[0].translated_text or ""
            detected = resp.translations[0].detected_language_code or None

        return TranslateOut(translated=translated, detected_source=detected)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google Translate v3 failed: {e}")
