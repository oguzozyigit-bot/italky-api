# FILE: italky-api/app/routers/translate_langs.py
from __future__ import annotations

import os
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
GOOGLE_LANGS_URL = "https://translation.googleapis.com/language/translate/v2/languages"


class LangsOut(BaseModel):
    languages: list[dict]


@router.get("/translate/languages", response_model=LangsOut)
async def translate_languages(target: str = "tr"):
    """
    Frontend dil seçimi için:
    GET /api/translate/languages?target=tr
    - target: dönen dil isimlerinin hangi dilde olacağı (tr/en vs)
    """
    if not GOOGLE_TRANSLATE_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_TRANSLATE_API_KEY not set")

    params = {"key": GOOGLE_TRANSLATE_API_KEY, "target": target}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(GOOGLE_LANGS_URL, params=params)

        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"google_languages_error {r.status_code}: {r.text[:300]}",
            )

        j = r.json() or {}
        data = (j.get("data") or {})
        langs = data.get("languages") or []

        # Format: [{"language":"en","name":"İngilizce"}, ...]
        out = [{"code": x.get("language"), "name": x.get("name")} for x in langs if x.get("language")]
        return {"languages": out}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"google_languages_error: {str(e)}")
