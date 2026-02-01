# FILE: italky-api/app/models/translate.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

# ✅ Tek yerden anahtar oku (önce Translate key, yoksa Gemini key)
GOOGLE_TRANSLATE_API_KEY = (os.getenv("GOOGLE_TRANSLATE_API_KEY", "") or "").strip()
if not GOOGLE_TRANSLATE_API_KEY:
    GOOGLE_TRANSLATE_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TranslateReq(FlexibleModel):
    text: str
    source: Optional[str] = None   # "tr", "en"...
    target: str                    # "tr", "en"...


def _safe_str(x: Any) -> str:
    return str(x or "").strip()


@router.get("/translate/ping")
def translate_ping():
    # tarayıcıdan açınca görünsün diye
    return {"ok": True, "has_key": bool(GOOGLE_TRANSLATE_API_KEY)}


@router.get("/translate")
def translate_get_help():
    # ✅ Bu sayede adres çubuğundan açınca "Method Not Allowed" değil, açıklama görürsün
    return {
        "ok": False,
        "detail": "Bu endpoint POST ister. Örnek: POST /api/translate {text, source?, target}"
    }


@router.post("/translate")
async def translate_post(req: TranslateReq) -> Dict[str, Any]:
    text = _safe_str(req.text)
    target = _safe_str(req.target).lower()
    source = _safe_str(req.source).lower() or None

    if not text:
        return {"ok": False, "error": "empty_text"}

    if not target:
        return {"ok": False, "error": "missing_target"}

    if not GOOGLE_TRANSLATE_API_KEY:
        return {"ok": False, "error": "missing_google_translate_key"}

    # ✅ Google Translate v2 (basit ve hızlı)
    # https://cloud.google.com/translate/docs/reference/rest/v2/translate
    url = "https://translation.googleapis.com/language/translate/v2"
    params = {"key": GOOGLE_TRANSLATE_API_KEY}
    payload: Dict[str, Any] = {"q": text, "target": target, "format": "text"}
    if source:
        payload["source"] = source

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.post(url, params=params, json=payload)
            raw = await r.json()

        if r.status_code != 200:
            logger.error("TRANSLATE_V2_FAIL %s %s", r.status_code, raw)
            return {"ok": False, "error": "translate_failed", "raw": raw}

        data = (raw or {}).get("data", {}) or {}
        translations = data.get("translations", []) or []
        first = translations[0] if translations else {}
        translated = _safe_str(first.get("translatedText"))

        # Google v2 otomatik dil dönmez, source vermediysen detect gerekebilir:
        detected = _safe_str(first.get("detectedSourceLanguage")) or (source or "")

        return {
            "ok": True,
            "translated": translated or text,
            "detected_source": detected or None,
            "target": target,
        }

    except Exception as e:
        logger.error("TRANSLATE_V2_EXC: %s", str(e))
        return {"ok": False, "error": "exception", "message": str(e)}
