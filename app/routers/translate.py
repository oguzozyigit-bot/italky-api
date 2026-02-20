# FILE: italky-api/app/routers/translate.py
from __future__ import annotations

import os
import html
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# Google Translate v2 (API key ile)
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"


class TranslateIn(BaseModel):
    text: str
    # eski frontend uyumluluğu:
    source: str | None = None
    target: str | None = None
    from_lang: str | None = None
    to_lang: str | None = None


def _canon(code: str | None) -> str:
    c = (code or "").strip().lower()
    if not c:
        return ""
    # pt-br / zh-tw gibi gelirse -> pt, zh
    return c.split("-")[0]


@router.post("/translate")
async def translate(payload: TranslateIn):
    if not GOOGLE_TRANSLATE_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_TRANSLATE_API_KEY not set")

    text = (payload.text or "").strip()
    if not text:
        return {"translated": ""}

    src = _canon(payload.source) or _canon(payload.from_lang) or "auto"
    dst = _canon(payload.target) or _canon(payload.to_lang)

    if not dst:
        raise HTTPException(status_code=422, detail="to_lang/target is required")

    params = {"key": GOOGLE_TRANSLATE_API_KEY}

    body = {
        "q": text,
        "target": dst,
        "format": "text",
    }
    # Google v2: source="auto" gönderme; yoksa auto algılar
    if src and src != "auto":
        body["source"] = src

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(GOOGLE_TRANSLATE_URL, params=params, json=body)

        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"google_translate_error {r.status_code}: {r.text[:300]}",
            )

        j = r.json() or {}
        data = (j.get("data") or {})
        translations = data.get("translations") or []

        translated = ""
        if translations and isinstance(translations, list):
            translated = (translations[0].get("translatedText") or "").strip()

        # Google bazen HTML entity döndürür (&quot; vb.)
        translated = html.unescape(translated)

        return {"translated": translated}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"google_translate_error: {str(e)}")
