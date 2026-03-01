from __future__ import annotations

import os
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("translate-ai")
router = APIRouter(tags=["translate-ai"])

# ENV
GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("TRANSLATE_OPENAI_MODEL") or "gpt-4o-mini").strip()

GOOGLE_URL = "https://translation.googleapis.com/language/translate/v2"


# =========================
# SCHEMA
# =========================
class TranslateReq(BaseModel):
    text: str
    from_lang: Optional[str] = "auto"
    to_lang: str = "tr"


class TranslateResp(BaseModel):
    ok: bool
    provider: str
    translated: str


# =========================
# GOOGLE TRANSLATE
# =========================
async def google_translate(text: str, source: str, target: str) -> Optional[str]:
    if not GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY missing")
        return None

    payload = {
        "q": text,
        "target": target,
        "format": "text",
    }

    if source and source != "auto":
        payload["source"] = source

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{GOOGLE_URL}?key={GOOGLE_API_KEY}",
                data=payload,
            )

        if r.status_code >= 400:
            logger.error("GOOGLE FAIL %s %s", r.status_code, r.text[:400])
            return None

        data = r.json()
        return (
            data.get("data", {})
            .get("translations", [{}])[0]
            .get("translatedText")
        )

    except Exception as e:
        logger.exception("GOOGLE EXCEPTION: %s", e)
        return None


# =========================
# OPENAI FALLBACK
# =========================
async def openai_translate(text: str, source: str, target: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY missing")
        return None

    prompt = f"""
Translate the following text strictly.
Source language: {source}
Target language: {target}

Return ONLY the translated text.

TEXT:
{text}
"""

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": "You are a professional translator."},
                {"role": "user", "content": prompt},
            ],
        )

        return (resp.choices[0].message.content or "").strip()

    except Exception as e:
        logger.exception("OPENAI EXCEPTION: %s", e)
        return None


# =========================
# ROUTE
# =========================
@router.post("/translate_ai", response_model=TranslateResp)
async def translate_ai(req: TranslateReq):

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    source = (req.from_lang or "auto").strip()
    target = (req.to_lang or "tr").strip()

    # 1️⃣ Google
    g = await google_translate(text, source, target)
    if g:
        return TranslateResp(ok=True, provider="google", translated=g)

    # 2️⃣ OpenAI fallback
    o = await openai_translate(text, source, target)
    if o:
        return TranslateResp(ok=True, provider="openai", translated=o)

    # 3️⃣ FAIL
    raise HTTPException(status_code=500, detail="Translation unavailable")
