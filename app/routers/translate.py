# FILE: app/routers/translate.py
from __future__ import annotations

import os
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3  # ✅ v3 client

router = APIRouter()

GOOGLE_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

_client: Optional[translate_v3.TranslationServiceClient] = None
_project_id: Optional[str] = None


def _load_project_id_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("project_id") or "").strip()
    except Exception:
        return ""


def get_client_and_project():
    global _client, _project_id

    if not GOOGLE_CREDS_PATH:
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS env var")

    if not os.path.exists(GOOGLE_CREDS_PATH):
        raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")

    if _client is None:
        creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
        _client = translate_v3.TranslationServiceClient(credentials=creds)

    if not _project_id:
        _project_id = _load_project_id_from_file(GOOGLE_CREDS_PATH)
        if not _project_id:
            raise RuntimeError("Could not read project_id from service account JSON")

    return _client, _project_id


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
        client, project_id = get_client_and_project()
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
