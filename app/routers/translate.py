# italky-api/app/routers/translate.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

# ✅ Önce Translate key (önerilen), yoksa Gemini key (fallback)
GOOGLE_TRANSLATE_API_KEY = (os.getenv("GOOGLE_TRANSLATE_API_KEY", "") or "").strip()
if not GOOGLE_TRANSLATE_API_KEY:
    GOOGLE_TRANSLATE_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TranslateReq(FlexibleModel):
    text: str
    target: str
    source: Optional[str] = None


def _s(x: Any) -> str:
    return str(x or "").strip()


@router.get("/translate/ping")
def translate_ping():
    return {"ok": True, "has_key": bool(GOOGLE_TRANSLATE_API_KEY)}


@router.get("/translate")
def translate_get_help():
    # ✅ Tarayıcıdan açınca “Method Not Allowed” yerine açıklama ver
    return {
        "ok": False,
        "detail": "Bu endpoint POST ister. Örnek: POST /api/translate {text, target, source?}",
        "example": {"text": "Merhaba", "target": "en", "source": "tr"},
    }


@router.post("/translate")
async def translate_post(req: TranslateReq) -> Dict[str, Any]:
    text = _s(req.text)
    target = _s(req.target).lower()
    source = _s(req.source).lower() or None

    if not text:
        return {"ok": False, "error": "empty_text"}
    if not target:
        return {"ok": False, "error": "missing_target"}
    if not GOOGLE_TRANSLATE_API_KEY:
        return {"ok": False, "error": "missing_google_translate_key"}

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
            logger.error("TRANSLATE_FAIL %s %s", r.status_code, raw)
            return {"ok": False, "error": "translate_failed", "raw": raw}

        data = (raw or {}).get("data", {}) or {}
        translations = data.get("translations", []) or []
        first = translations[0] if translations else {}

        translated = _s(first.get("translatedText")) or text
        detected = _s(first.get("detectedSourceLanguage")) or (source or None)

        return {
            "ok": True,
            "translated": translated,
            "detected_source": detected,
            "target": target,
        }

    except Exception as e:
        logger.error("TRANSLATE_EXC: %s", str(e))
        return {"ok": False, "error": "exception", "message": str(e)}
