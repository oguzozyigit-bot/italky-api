# italky-api/app/routers/translate.py
from __future__ import annotations
import os
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

LIBRE_BASE = os.getenv("LIBRE_BASE", "").rstrip("/")
LIBRE_KEY  = os.getenv("LT_API_KEY", "").strip()  # şimdilik boş kalabilir

class TranslateIn(BaseModel):
    text: str
    source: str | None = None
    target: str | None = None
    from_lang: str | None = None
    to_lang: str | None = None

@router.post("/translate")
async def translate(payload: TranslateIn):
    if not LIBRE_BASE:
        raise HTTPException(status_code=500, detail="LIBRE_BASE not set")

    text = (payload.text or "").strip()
    if not text:
        return {"translated": ""}

    src = (payload.source or payload.from_lang or "auto").strip().lower()
    dst = (payload.target or payload.to_lang or "").strip().lower()
    if not dst:
        raise HTTPException(status_code=422, detail="to_lang/target is required")

    # LibreTranslate endpoint
    url = f"{LIBRE_BASE}/translate"

    data = {
        "q": text,
        "source": src,     # "auto" da olabilir
        "target": dst,
        "format": "text",
    }
    if LIBRE_KEY:
        data["api_key"] = LIBRE_KEY

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(url, data=data)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"libre_error {r.status_code}: {r.text[:300]}")
        j = r.json()
        out = (j.get("translatedText") or "").strip()
        return {"translated": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"libre_error: {str(e)}")
