# FILE: italky-api/app/routers/translate_ai.py
from __future__ import annotations

import os
from typing import Optional, Dict, Any, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["translate-ai"])

# --- KEYS / ENDPOINTS ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Gemini endpoint (1.5 flash hızlı/ucuz)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent"

# --- Language map (prompt için) ---
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

# --- Schemas ---
class TranslateAIRequest(BaseModel):
    text: str = Field(..., min_length=1)
    from_lang: str = Field(..., min_length=1)
    to_lang: str = Field(..., min_length=1)

    # fast: literal / chat: daha doğal
    style: Optional[str] = Field(default="chat")

    # "auto" => önce Gemini sonra OpenAI
    # "gemini" => sadece Gemini (fail olursa 502)
    # "openai" => sadece OpenAI (fail olursa 502)
    provider: Optional[str] = Field(default="auto")

class TranslateAIResponse(BaseModel):
    translated: str
    provider_used: str
    tokens_used: Optional[int] = None

# --- Provider calls ---
async def _call_gemini(text: str, instructions: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    # Gemini tarafında “sadece çeviri” disiplinini korumak için prompt’u tek parçada veriyoruz
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": instructions + "\n\nUser text:\n" + text}
                ]
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if r.status_code >= 400:
            return None

        data = r.json()
        # candidates[0].content.parts[0].text
        out = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        out = (out or "").strip()
        return out or None
    except Exception:
        return None

async def _call_openai(text: str, instructions: str) -> Tuple[Optional[str], Optional[int]]:
    if not OPENAI_API_KEY:
        return None, None

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
            return None, None

        data = r.json()

        translated = (data.get("output_text") or "").strip()

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

        return (translated or None), tokens_used
    except Exception:
        return None, None

# --- Main endpoint ---
@router.post("/translate_ai", response_model=TranslateAIResponse)
async def translate_ai(req: TranslateAIRequest) -> TranslateAIResponse:
    text = (req.text or "").strip()
    if not text:
        return TranslateAIResponse(translated="", provider_used="none", tokens_used=None)

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

    provider = (req.provider or "auto").strip().lower()

    # --- AUTO: first Gemini then OpenAI ---
    if provider == "auto":
        g = await _call_gemini(text, instructions)
        if g:
            return TranslateAIResponse(translated=g, provider_used="gemini", tokens_used=None)

        o, tok = await _call_openai(text, instructions)
        if o:
            return TranslateAIResponse(translated=o, provider_used="openai", tokens_used=tok)

        raise HTTPException(status_code=502, detail="All AI providers failed (gemini->openai)")

    # --- Force Gemini only ---
    if provider == "gemini":
        g = await _call_gemini(text, instructions)
        if g:
            return TranslateAIResponse(translated=g, provider_used="gemini", tokens_used=None)
        raise HTTPException(status_code=502, detail="Gemini failed")

    # --- Force OpenAI only ---
    if provider == "openai":
        o, tok = await _call_openai(text, instructions)
        if o:
            return TranslateAIResponse(translated=o, provider_used="openai", tokens_used=tok)
        raise HTTPException(status_code=502, detail="OpenAI failed")

    raise HTTPException(status_code=400, detail="Invalid provider. Use: auto | gemini | openai")
