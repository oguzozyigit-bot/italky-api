# italky-api/app/routers/ocr.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any, List

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()

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
    return {"ok": True, "has_key": bool(GOOGLE_API_KEY)}

@router.post("/ocr", response_model=OCRResponse)
async def ocr(req: OCRRequest):
    if not GOOGLE_API_KEY:
        raise HTTPException(500, "GOOGLE_API_KEY missing")

    b64 = _strip_data_url(req.image_base64)
    if not b64:
        raise HTTPException(400, "image_base64 required")

    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_API_KEY}"
    body: Dict[str, Any] = {
        "requests": [{
            "image": {"content": b64},
            "features": [{"type":"TEXT_DETECTION","maxResults":1}],
            "imageContext": {"languageHints": req.language_hints or []}
        }]
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=body)
        if r.status_code >= 400:
            logger.error("OCR_FAIL %s %s", r.status_code, (r.text or "")[:400])
            raise HTTPException(r.status_code, "ocr failed")

        data = r.json()
        res0 = (data.get("responses") or [])[0] or {}
        txt = ""
        if "fullTextAnnotation" in res0:
            txt = (res0["fullTextAnnotation"].get("text") or "").strip()
        elif "textAnnotations" in res0 and res0["textAnnotations"]:
            txt = (res0["textAnnotations"][0].get("description") or "").strip()

        return OCRResponse(ok=True, text=txt)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("OCR_EXCEPTION: %s", str(e))
        raise HTTPException(500, "ocr exception")

@router.post("/ocr/translate", response_model=OCRTranslateResponse)
async def ocr_translate(req: OCRTranslateRequest):
    # 1) OCR
    o = await ocr(OCRRequest(image_base64=req.image_base64, language_hints=["it","en","de","fr","es"]))
    extracted = (o.text or "").strip()
    if not extracted:
        return OCRTranslateResponse(ok=True, extracted_text="", translated="Metin bulamadım.")

    # 2) Translate (same API key)
    url = "https://translation.googleapis.com/language/translate/v2"
    payload = {"q": extracted, "target": req.target, "format":"text", "key": GOOGLE_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, data=payload)
        if r.status_code >= 400:
            logger.error("OCR_TRANSLATE_FAIL %s %s", r.status_code, (r.text or "")[:400])
            raise HTTPException(r.status_code, "ocr translate failed")

        data = r.json()
        tr0 = (((data.get("data") or {}).get("translations") or [])[0] or {})
        out = (tr0.get("translatedText") or "").strip()

        return OCRTranslateResponse(ok=True, extracted_text=extracted, translated=(out or extracted))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("OCR_TRANSLATE_EXCEPTION: %s", str(e))
        raise HTTPException(500, "ocr translate exception")
