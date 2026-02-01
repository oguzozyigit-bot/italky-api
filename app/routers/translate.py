# italky-api/app/routers/translate.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()

class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TranslateRequest(FlexibleModel):
    text: str
    target: str = "tr"      # hedef dil
    source: Optional[str] = None  # kaynak dil (None => auto)
    format: str = "text"    # text | html

class TranslateResponse(FlexibleModel):
    ok: bool
    translated: str
    detected_source: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

@router.get("/translate/ping")
def ping():
    return {"ok": True, "has_key": bool(GOOGLE_API_KEY)}

@router.post("/translate", response_model=TranslateResponse)
async def translate(req: TranslateRequest):
    if not req.text.strip():
        raise HTTPException(400, "text is required")

    if not GOOGLE_API_KEY:
        raise HTTPException(500, "GOOGLE_API_KEY missing")

    url = "https://translation.googleapis.com/language/translate/v2"
    payload: Dict[str, Any] = {
        "q": req.text,
        "target": req.target,
        "format": req.format,
        "key": GOOGLE_API_KEY,
    }
    if req.source and req.source.strip():
        payload["source"] = req.source.strip()

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, data=payload)
        if r.status_code >= 400:
            logger.error("TRANSLATE_FAIL %s %s", r.status_code, r.text[:400])
            raise HTTPException(r.status_code, "translate failed")

        data = r.json()
        tr0 = (((data.get("data") or {}).get("translations") or [])[0] or {})
        translated = (tr0.get("translatedText") or "").strip()
        detected = (tr0.get("detectedSourceLanguage") or None)

        return TranslateResponse(ok=True, translated=translated or req.text, detected_source=detected, raw=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("TRANSLATE_EXCEPTION: %s", str(e))
        raise HTTPException(500, "translate exception")
