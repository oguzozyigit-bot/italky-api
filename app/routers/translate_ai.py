from __future__ import annotations

import os
import logging
import html
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None  # type: ignore

logger = logging.getLogger("italky-translate-ai")
router = APIRouter(tags=["translate-ai"])

# =========================
# ENV
# =========================
GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("TRANSLATE_OPENAI_MODEL") or "gpt-4o-mini").strip()

GOOGLE_URL = "https://translation.googleapis.com/language/translate/v2"

# =========================
# GLOBAL CLIENTS (PERF)
# =========================
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=20.0, write=20.0, pool=8.0)
http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

openai_client = None
if OPENAI_API_KEY and AsyncOpenAI is not None:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# =========================
# SCHEMAS
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
        logger.warning("GOOGLE_API_KEY missing -> skip google")
        return None

    params = {
        "key": GOOGLE_API_KEY,
        "q": text,
        "target": target,
        "format": "text",
    }
    if source and source != "auto":
        params["source"] = source

    try:
        r = await http_client.post(GOOGLE_URL, params=params)
        if r.status_code != 200:
            logger.error("GOOGLE_FAIL %s %s", r.status_code, r.text[:300])
            return None

        data = r.json()
        out = (
            data.get("data", {})
            .get("translations", [{}])[0]
            .get("translatedText")
        )
        if not out:
            return None

        # Google bazen HTML escaped döndürür: &#39; vb.
        return html.unescape(str(out)).strip() or None

    except Exception as e:
        logger.exception("GOOGLE_EXCEPTION %s", e)
        return None

# =========================
# OPENAI TRANSLATE (FALLBACK)
# =========================
async def openai_translate(text: str, source: str, target: str) -> Optional[str]:
    if not openai_client:
        logger.warning("OPENAI client missing -> skip openai")
        return None

    # source auto ise algılasın
    lang_info = (
        f"Source language: {source}"
        if source and source != "auto"
        else "Detect the source language automatically."
    )

    prompt = f"""Task: Professional Translation
{lang_info}
Target language: {target}
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
        logger.exception("OPENAI_EXCEPTION %s", e)
        return None

# =========================
# ROUTE
# =========================
@router.post("/translate_ai", response_model=TranslateResp)
async def translate_ai(req: TranslateReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    source = (req.from_lang or "auto").strip().lower()
    target = (req.to_lang or "tr").strip().lower()

    # 1) Google
    g = await google_translate(text, source, target)
    if g:
        return TranslateResp(ok=True, provider="google", translated=g)

    # 2) OpenAI fallback
    o = await openai_translate(text, source, target)
    if o:
        return TranslateResp(ok=True, provider="openai", translated=o)

    raise HTTPException(status_code=500, detail="Translation unavailable (google+openai failed)")
