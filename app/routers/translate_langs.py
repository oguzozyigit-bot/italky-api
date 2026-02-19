from __future__ import annotations

import os
from typing import Any, List, Dict

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()

# LibreTranslate base (Render env’den yönet)
LT_BASE = os.getenv("LT_BASE_URL", "https://italky-libretranslate.onrender.com").rstrip("/")

@router.get("/api/translate/languages")
async def translate_languages() -> List[Dict[str, Any]]:
    """
    UI dil listesini dinamik tutmak için.
    LibreTranslate /languages endpoint'ini proxyler.
    """
    url = f"{LT_BASE}/languages"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"LT languages failed: HTTP {r.status_code}")
        data = r.json()
        # Beklenen format: [{"code":"en","name":"English"}, ...]
        if not isinstance(data, list):
            raise HTTPException(status_code=502, detail="LT languages invalid response")
        return data
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="LT languages timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LT languages error: {str(e)}")
