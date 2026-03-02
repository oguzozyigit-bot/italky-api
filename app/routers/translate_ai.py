# FILE: app/routers/translate_ai.py
from __future__ import annotations

import os
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3

logger = logging.getLogger("italky-translate")
router = APIRouter(tags=["translate-ai"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()

_client: Optional[translate_v3.TranslationServiceClient] = None
_project_id: Optional[str] = None


def _load_project_id_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("project_id") or "").strip()
    except Exception:
        return ""


def _get_client_and_project():
    global _client, _project_id

    if not GOOGLE_CREDS_PATH:
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS")

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


class TranslateReq(BaseModel):
    text: str
    from_lang: Optional[str] = "auto"
    to_lang: str = "tr"


class TranslateResp(BaseModel):
    ok: bool
    provider: str
    translated: str


@router.post("/translate_ai", response_model=TranslateResp)
async def translate_ai(req: TranslateReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    source = (req.from_lang or "auto").strip().lower()
    target = (req.to_lang or "tr").strip().lower()

    try:
        client, project_id = _get_client_and_project()
        parent = f"projects/{project_id}/locations/global"

        request = {
            "parent": parent,
            "contents": [text],
            "target_language_code": target,
            "mime_type": "text/plain",
        }
        if source and source != "auto":
            request["source_language_code"] = source

        resp = client.translate_text(request=request)
        out = ""
        if resp.translations:
            out = (resp.translations[0].translated_text or "").strip()

        if not out:
            raise HTTPException(status_code=502, detail="Google Translate returned empty response")

        return TranslateResp(ok=True, provider="google", translated=out)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("GOOGLE_TRANSLATE_V3_FAIL %s", e)
        raise HTTPException(status_code=502, detail=f"Google Translate v3 failed: {e}")
