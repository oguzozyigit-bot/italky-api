# FILE: italky-api/app/routers/ocr.py
from __future__ import annotations

import os
import json
import logging
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from google.oauth2 import service_account
from google.cloud import vision
from google.cloud import translate as translate_v3

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["ocr"])

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


def get_vision_client() -> vision.ImageAnnotatorClient:
    global _vision_client
    if _vision_client is not None:
        return _vision_client
    _ensure_creds()
    creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
    _vision_client = vision.ImageAnnotatorClient(credentials=creds)
    return _vision_client


def get_translate_client_and_project():
    global _translate_client, _project_id
    if _translate_client is not None and _project_id:
        return _translate_client, _project_id
    _ensure_creds()
    creds = service_account.Credentials.from_service_account_file(GOOGLE_CREDS_PATH)
    _translate_client = translate_v3.TranslationServiceClient(credentials=creds)
    _project_id = _load_project_id_from_file(GOOGLE_CREDS_PATH)
    if not _project_id:
        raise RuntimeError("Could not read project_id from service account JSON")
    return _translate_client, _project_id


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OCRRequest(FlexibleModel):
    image_base64: str
    language_hints: Optional[List[str]] = None


class OCRResponse(FlexibleModel):
    ok: bool
    text: str


class OCRTranslateRequest(FlexibleModel):
    image_base64: str
    target: str = "tr"
    source: Optional[str] = "auto"


class OCRTranslateResponse(FlexibleModel):
    ok: bool
    extracted_text: str
    translated: str


def _strip_data_url(s: str) -> str:
    s = (s or "").strip()
    if s.lower().startswith("data:") and "," in s:
        return s.split(",", 1)[1].strip()
    return s


@router.get("/ocr/ping")
def ping():
    # Service account var mı?
    ok = bool(GOOGLE_CREDS_PATH)
    return {"ok": True, "has_credentials": ok, "creds_path": GOOGLE_CREDS_PATH or ""}


@router.post("/ocr", response_model=OCRResponse)
async def ocr(req: OCRRequest):
    b64 = _strip_data_url(req.image_base64)
    if not b64:
        raise HTTPException(400, "image_base64 required")

    try:
        client = get_vision_client()
        image = vision.Image(content=__import__("base64").b64decode(b64))
        ctx = vision.ImageContext(language_hints=req.language_hints or [])

        resp = client.document_text_detection(image=image, image_context=ctx)
        if resp.error and resp.error.message:
            logger.error("OCR_VISION_ERROR %s", resp.error.message)
            raise HTTPException(502, "ocr failed")

        txt = ""
        if resp.full_text_annotation and resp.full_text_annotation.text:
            txt = resp.full_text_annotation.text.strip()

        return OCRResponse(ok=True, text=txt)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("OCR_EXCEPTION %s", e)
        raise HTTPException(500, "ocr exception")


@router.post("/ocr/translate", response_model=OCRTranslateResponse)
async def ocr_translate(req: OCRTranslateRequest):
    # 1) OCR
    o = await ocr(OCRRequest(image_base64=req.image_base64, language_hints=["tr","en","de","fr","es","it"]))
    extracted = (o.text or "").strip()
    if not extracted:
        return OCRTranslateResponse(ok=True, extracted_text="", translated="Metin bulamadım.")

    # 2) Translate v3
    target = (req.target or "tr").strip().lower()
    source = (req.source or "auto").strip().lower()

    try:
        tclient, pid = get_translate_client_and_project()
        parent = f"projects/{pid}/locations/global"

        request: Dict[str, Any] = {
            "parent": parent,
            "contents": [extracted],
            "target_language_code": target,
            "mime_type": "text/plain",
        }
        if source and source != "auto":
            request["source_language_code"] = source

        r = tclient.translate_text(request=request)
        out = ""
        if r.translations:
            out = (r.translations[0].translated_text or "").strip()

        return OCRTranslateResponse(ok=True, extracted_text=extracted, translated=(out or extracted))

    except Exception as e:
        logger.exception("OCR_TRANSLATE_EXCEPTION %s", e)
        raise HTTPException(500, "ocr translate exception")
