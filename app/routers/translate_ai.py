from __future__ import annotations

import os
import json
import logging
from typing import Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3

import google.generativeai as genai

logger = logging.getLogger("italky-translate")
router = APIRouter(tags=["translate-ai"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()
GEMINI_API_KEY = (os.getenv("GOOGLE_API_KEY") or "").strip()

_client: Optional[translate_v3.TranslationServiceClient] = None
_project_id: Optional[str] = None


def _load_project_id_from_dict(data: dict) -> str:
    return str(data.get("project_id") or "").strip()


def _load_project_id_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _load_project_id_from_dict(data)
    except Exception:
        return ""


def _build_credentials() -> Tuple[service_account.Credentials, str]:
    if GOOGLE_CREDS_JSON:
        try:
            info = json.loads(GOOGLE_CREDS_JSON)
            creds = service_account.Credentials.from_service_account_info(info)
            project_id = _load_project_id_from_dict(info)
            if not project_id:
                raise RuntimeError("project_id missing in GOOGLE_APPLICATION_CREDENTIALS_JSON")
            return creds, project_id
        except Exception as e:
            raise RuntimeError(f"Invalid GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")

    if GOOGLE_CREDS_PATH:
        if not os.path.exists(GOOGLE_CREDS_PATH):
            raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")
        try:
            creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
            project_id = _load_project_id_from_file(GOOGLE_CREDS_PATH)
            if not project_id:
                raise RuntimeError("Could not read project_id from service account JSON file")
            return creds, project_id
        except Exception as e:
            raise RuntimeError(f"Invalid credentials file: {e}")

    raise RuntimeError(
        "Missing Google credentials. Set GOOGLE_APPLICATION_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS"
    )


def _get_client_and_project():
    global _client, _project_id

    if _client is None or not _project_id:
        creds, project_id = _build_credentials()
        _client = translate_v3.TranslationServiceClient(credentials=creds)
        _project_id = project_id

    return _client, _project_id


def _translate_with_google_v3(text: str, source: str, target: str) -> Optional[str]:
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

    return out or None


def _translate_with_gemini(text: str, source: str, target: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    src = source if source != "auto" else "auto-detect"
    prompt = (
        f"Translate the following text from {src} to {target}. "
        f"Return only the translated text, no explanation.\n\n{text}"
    )

    resp = model.generate_content(prompt)
    out = getattr(resp, "text", None)
    out = (out or "").strip()
    return out or None


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
        provider = None

        try:
            _, project_id = _get_client_and_project()
            provider = "google-translate-v3"
            return {"ok": True, "provider": provider, "project_id": project_id}
        except Exception as e:
            logger.warning("Google v3 health failed: %s", e)

        if GEMINI_API_KEY:
            provider = "gemini-fallback"
            return {"ok": True, "provider": provider, "project_id": None}

        raise RuntimeError("No translation provider is configured")

    except Exception as e:
        logger.exception("TRANSLATE_AI_HEALTH_FAIL %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/translate_ai", response_model=TranslateResp)
async def translate_ai(req: TranslateReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    source = (req.from_lang or "auto").strip().lower()
    target = (req.to_lang or "tr").strip().lower()

    # 1) Google Translate v3
    try:
        out = _translate_with_google_v3(text, source, target)
        if out:
            return TranslateResp(ok=True, provider="google-v3", translated=out)
    except Exception as e:
        logger.warning("GOOGLE_V3_TRANSLATE_FAIL %s", e)

    # 2) Gemini fallback
    try:
        out = _translate_with_gemini(text, source, target)
        if out:
            return TranslateResp(ok=True, provider="gemini-fallback", translated=out)
    except Exception as e:
        logger.warning("GEMINI_TRANSLATE_FAIL %s", e)

    raise HTTPException(status_code=502, detail="All translation providers failed")
