from __future__ import annotations

import os
import json
import logging
import tempfile
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3

logger = logging.getLogger("italky-translate")
router = APIRouter(tags=["translate-ai"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()

_client: Optional[translate_v3.TranslationServiceClient] = None
_project_id: Optional[str] = None
_temp_json_path: Optional[str] = None


def _load_json_from_string(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Invalid GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")


def _load_json_from_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Could not read credentials file: {e}")


def _resolve_credentials_info() -> dict:
    if GOOGLE_CREDS_JSON:
        return _load_json_from_string(GOOGLE_CREDS_JSON)

    if GOOGLE_CREDS_PATH:
        if not os.path.exists(GOOGLE_CREDS_PATH):
            raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")
        return _load_json_from_file(GOOGLE_CREDS_PATH)

    raise RuntimeError(
        "Missing Google credentials. Set GOOGLE_APPLICATION_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS"
    )


def _ensure_temp_json_file(info: dict) -> str:
    global _temp_json_path
    if _temp_json_path and os.path.exists(_temp_json_path):
        return _temp_json_path

    fd, path = tempfile.mkstemp(prefix="gcp_translate_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    _temp_json_path = path
    return path


def _get_client_and_project():
    global _client, _project_id

    info = _resolve_credentials_info()

    if _client is None:
        creds = service_account.Credentials.from_service_account_info(info)
        _client = translate_v3.TranslationServiceClient(credentials=creds)

    if not _project_id:
        _project_id = str(info.get("project_id") or "").strip()
        if not _project_id:
            raise RuntimeError("project_id missing in Google credentials JSON")

    return _client, _project_id


class TranslateReq(BaseModel):
    text: str
    from_lang: Optional[str] = "auto"
    to_lang: str = "tr"


class TranslateResp(BaseModel):
    ok: bool
    provider: str
    translated: str


@router.get("/translate_ai/health")
async def translate_ai_health():
    try:
        info = _resolve_credentials_info()
        return {
            "ok": True,
            "project_id": str(info.get("project_id") or ""),
            "has_private_key": bool(info.get("private_key")),
            "mode": "json_env" if GOOGLE_CREDS_JSON else "file_path"
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


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

        payload = {
            "parent": parent,
            "contents": [text],
            "target_language_code": target,
            "mime_type": "text/plain",
        }

        if source and source != "auto":
            payload["source_language_code"] = source

        resp = client.translate_text(
    request=request,
    timeout=2.0
)

out = resp.translations[0].translated_text.strip()

return TranslateResp(
    ok=True,
    provider="google",
    translated=out
)

        if not out:
            raise HTTPException(status_code=502, detail="Google Translate returned empty response")

        return TranslateResp(ok=True, provider="google", translated=out)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("GOOGLE_TRANSLATE_V3_FAIL %s", e)
        raise HTTPException(status_code=502, detail=f"Google Translate v3 failed: {e}")
