# FILE: italky-api/app/routers/translate_ai.py
from __future__ import annotations

import os
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["translate-ai"])

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

LANG_MAP = {
    "tr": "Turkish",
    "en": "English",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "es": "Spanish",
    "pt": "Portuguese",
    "pt-br": "Brazilian Portuguese",
    "nl": "Dutch",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "el": "Greek",
    "uk": "Ukrainian",
    "ru": "Russian",
    "ar": "Arabic",
    "he": "Hebrew",
    "fa": "Persian",
    "ur": "Urdu",
    "hi": "Hindi",
    "bn": "Bengali",
    "id": "Indonesian",
    "ms": "Malay",
    "vi": "Vietnamese",
    "th": "Thai",
    "zh": "Chinese (Simplified)",
    "ja": "Japanese",
    "ko": "Korean",
}


def _canon_lang(code: str) -> str:
    return (code or "").strip().lower()


def _lang_name(code: str) -> str:
    c = _canon_lang(code)
    return LANG_MAP.get(c, c or "English")


class TranslateAIRequest(BaseModel):
    text: str = Field(..., min_length=1)
    from_lang: str = Field(..., min_length=1)
    to_lang: str = Field(..., min_length=1)
    style: Optional[str] = Field(default="chat")       # "fast" | "chat"
    provider: Optional[str] = Field(default="openai")  # ileride: "auto"


class TranslateAIResponse(BaseModel):
    translated: str
    provider_used: str = "openai"
    tokens_used: Optional[int] = None


@router.post("/translate_ai", response_model=TranslateAIResponse)
async def translate_ai(req: TranslateAIRequest) -> TranslateAIResponse:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    text = (req.text or "").strip()
    if not text:
        return TranslateAIResponse(translated="", provider_used="openai")

    src = _lang_name(req.from_lang)
    dst = _lang_name(req.to_lang)

    style = (req.style or "chat").strip().lower()
    tone_line = (
        "Keep it natural, like real spoken dialogue. Avoid overly formal wording."
        if style == "chat"
        else "Keep it direct and faithful. Prefer literal translation when possible."
    )

    instructions = f"""
You are a real-time translation engine for a face-to-face interpreter app.

Source language: {src}
Target language: {dst}

Rules:
- Output ONLY the translated text in the target language.
- No explanations, no notes, no extra commentary.
- Preserve meaning, politeness level, and emojis.
- {tone_line}
- Do NOT switch languages. Always respond in {dst}.
""".strip()

    # ✅ Responses API payload (text.format yok)
    payload: Dict[str, Any] = {
        "model": "gpt-4o-mini",
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": text},
        ],
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(OPENAI_RESPONSES_URL, headers=headers, json=payload)

        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=r.text)

        data = r.json()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenAI request failed: {e}")

    translated = (data.get("output_text") or "").strip()

    # yedek parse
    if not translated:
        out = data.get("output") or []
        buf = []
        for item in out:
            for c in (item.get("content") or []):
                if c.get("type") == "output_text":
                    buf.append(c.get("text", ""))
        translated = "".join(buf).strip()

    usage = data.get("usage") or {}
    tokens_used = usage.get("total_tokens")

    return TranslateAIResponse(
        translated=translated,
        provider_used="openai",
        tokens_used=tokens_used,
    )
