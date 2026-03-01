# italky-api/app/routers/translate.py
from __future__ import annotations

import os
import html
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None  # type: ignore

router = APIRouter()
logger = logging.getLogger("italky-translate")

# =========================
# ENV
# =========================
# Google Translate v2 (API key)
# (Senin projede farklı isimler geçmişti; ikisini de destekleyelim)
GOOGLE_TRANSLATE_API_KEY = (os.getenv("GOOGLE_TRANSLATE_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"

# OpenAI fallback
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or os.getenv("ITALKY_LLM_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("TRANSLATE_OPENAI_MODEL") or os.getenv("ITALKY_LLM_MODEL") or "gpt-4o-mini").strip()

# =========================
# GLOBAL CLIENTS
# =========================
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=25.0, write=25.0, pool=8.0)
http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

openai_client = None
if OPENAI_API_KEY and AsyncOpenAI is not None:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# =========================
# SCHEMA
# =========================
class TranslateIn(BaseModel):
    text: str
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

# =========================
# PROVIDERS
# =========================
async def google_translate(text: str, src: str, dst: str) -> Optional[str]:
    if not GOOGLE_TRANSLATE_API_KEY:
        return None

    params = {"key": GOOGLE_TRANSLATE_API_KEY}
    body: dict = {"q": text, "target": dst, "format": "text"}

    # Google v2: source="auto" göndermeyin; boş bırakırsanız auto algılar
    if src and src != "auto":
        body["source"] = src

    try:
        r = await http_client.post(GOOGLE_TRANSLATE_URL, params=params, json=body)
        if r.status_code != 200:
            logger.error("GOOGLE_TRANSLATE_FAIL %s %s", r.status_code, r.text[:300])
            return None

        j = r.json() or {}
        data = (j.get("data") or {})
        translations = data.get("translations") or []
        if not (translations and isinstance(translations, list)):
            return None

        translated = (translations[0].get("translatedText") or "").strip()
        if not translated:
            return None

        # Google bazen HTML escape döndürür
        return html.unescape(translated).strip() or None

    except Exception as e:
        logger.exception("GOOGLE_TRANSLATE_EXCEPTION %s", e)
        return None

async def openai_translate(text: str, src: str, dst: str) -> Optional[str]:
    if not openai_client:
        return None

    lang_info = f"Source language: {src}" if (src and src != "auto") else "Detect the source language automatically."

    prompt = f"""Task: Professional Translation
{lang_info}
Target language: {dst}
Strict Rule: Return ONLY the translated text. No explanations. No quotes.

TEXT:
{text}
"""

    try:
        resp = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": "You are a neutral professional translation system for italkyAI."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or None

    except Exception as e:
        logger.exception("OPENAI_TRANSLATE_EXCEPTION %s", e)
        return None

# =========================
# ROUTE (GOOGLE -> OPENAI)
# =========================
@router.post("/translate")
async def translate(payload: TranslateIn):
    text = (payload.text or "").strip()
    if not text:
        return {"translated": ""}

    src = _canon(payload.source) or _canon(payload.from_lang) or "auto"
    dst = _canon(payload.target) or _canon(payload.to_lang)

    if not dst:
        raise HTTPException(status_code=422, detail="to_lang/target is required")

    # 1) Google
    g = await google_translate(text, src, dst)
    if g is not None:
        return {"translated": g, "provider": "google"}

    # 2) OpenAI fallback
    o = await openai_translate(text, src, dst)
    if o is not None:
        return {"translated": o, "provider": "openai"}

    # 3) fail
    raise HTTPException(status_code=502, detail="translate failed (google+openai)")
