# FILE: italky-api/app/routers/ocr_translate.py
from __future__ import annotations

import os
import json
import base64
import logging
from typing import Optional, Dict, Any

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import vision
from google.cloud import translate as translate_v3

logger = logging.getLogger("ocr-translate")
router = APIRouter(tags=["ocr-translate"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()

_vision_client: Optional[vision.ImageAnnotatorClient] = None
_translate_client: Optional[translate_v3.TranslationServiceClient] = None
_project_id: Optional[str] = None


def _load_project_id_from_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("project_id") or "").strip()
    except Exception:
        return ""


def _ensure_creds():
    if not GOOGLE_CREDS_PATH:
        raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS")
    if not os.path.exists(GOOGLE_CREDS_PATH):
        raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")


def get_clients():
    global _vision_client, _translate_client, _project_id
    _ensure_creds()
    creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)

    if _vision_client is None:
        _vision_client = vision.ImageAnnotatorClient(credentials=creds)

    if _translate_client is None:
        _translate_client = translate_v3.TranslationServiceClient(credentials=creds)

    if not _project_id:
        _project_id = _load_project_id_from_file(GOOGLE_CREDS_PATH)
        if not _project_id:
            raise RuntimeError("Could not read project_id from service account JSON")

    return _vision_client, _translate_client, _project_id


def _canon_lang(code: str) -> str:
    c = (code or "").strip().lower().replace("_", "-")
    return c or "auto"


def _guess_mime(filename: str, content_type: str | None) -> str:
    ct = (content_type or "").strip().lower()
    if ct.startswith("image/"):
        return ct
    fn = (filename or "").lower()
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith(".webp"):
        return "image/webp"
    if fn.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


class OcrTranslateResp(BaseModel):
    ok: bool = True
    extracted_text: str = ""
    translated_text: str = ""
    provider: str = "google"
    error: Optional[str] = None


@router.post("/translate/ocr_translate", response_model=OcrTranslateResp)
async def ocr_translate(
    image: UploadFile = File(...),
    from_lang: str = Form("auto"),
    to_lang: str = Form("tr"),
):
    src = _canon_lang(from_lang)
    dst = _canon_lang(to_lang)
    if not dst or dst == "auto":
        dst = "tr"

    try:
        vclient, tclient, pid = get_clients()

        img_bytes = await image.read()
        if not img_bytes:
            raise HTTPException(status_code=422, detail="Empty image")

        # OCR
        vimg = vision.Image(content=img_bytes)
        ctx = vision.ImageContext(language_hints=[src] if src != "auto" else [])
        vresp = vclient.document_text_detection(image=vimg, image_context=ctx)

        if vresp.error and vresp.error.message:
            raise HTTPException(status_code=502, detail=f"Vision error: {vresp.error.message}")

        extracted_text = (vresp.full_text_annotation.text or "").strip() if vresp.full_text_annotation else ""
        if not extracted_text:
            return OcrTranslateResp(ok=True, extracted_text="", translated_text="Metin bulamadım.", provider="google", error="OCR_EMPTY")

        # Translate v3
        parent = f"projects/{pid}/locations/global"
        req: Dict[str, Any] = {
            "parent": parent,
            "contents": [extracted_text],
            "target_language_code": dst,
            "mime_type": "text/plain",
        }
        if src != "auto":
            req["source_language_code"] = src

        tresp = tclient.translate_text(request=req)
        translated = (tresp.translations[0].translated_text or "").strip() if tresp.translations else ""

        return OcrTranslateResp(
            ok=True,
            extracted_text=extracted_text,
            translated_text=(translated or extracted_text),
            provider="google",
            error=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("OCR_TRANSLATE_FAIL: %s", e)
        raise HTTPException(status_code=500, detail=f"ocr_translate failed: {e}")
