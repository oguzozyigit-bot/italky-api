# FILE: /app/routers/ui_translate.py

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import google.generativeai as genai

router = APIRouter(tags=["ui-translate"])

SUPPORTED_LANGS = {"en", "de", "fr", "it", "es"}


class UiTranslateIn(BaseModel):
    text: str
    target_lang: Literal["en", "de", "fr", "it", "es"]


class UiTranslateOut(BaseModel):
    translated_text: str


def _get_gemini_api_key() -> str:
    key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GOOGLE_AI_API_KEY")
        or ""
    ).strip()
    if not key:
      raise HTTPException(status_code=500, detail="Gemini API key bulunamadı.")
    return key


def _lang_name(code: str) -> str:
    return {
        "en": "English",
        "de": "German",
        "fr": "French",
        "it": "Italian",
        "es": "Spanish",
    }.get(code, "English")


@router.post("/api/ui-translate", response_model=UiTranslateOut)
async def ui_translate(payload: UiTranslateIn) -> UiTranslateOut:
    text = (payload.text or "").strip()
    target_lang = (payload.target_lang or "").strip().lower()

    if not text:
        return UiTranslateOut(translated_text="")

    if target_lang not in SUPPORTED_LANGS:
        return UiTranslateOut(translated_text=text)

    try:
        api_key = _get_gemini_api_key()
        genai.configure(api_key=api_key)

        model_name = (
            os.getenv("GEMINI_UI_TRANSLATE_MODEL")
            or os.getenv("GEMINI_MODEL")
            or "gemini-1.5-flash"
        ).strip()

        model = genai.GenerativeModel(model_name=model_name)

        prompt = f"""
You are translating Turkish mobile app UI text.

Rules:
- Translate from Turkish to {_lang_name(target_lang)}.
- Keep it short and natural for app UI.
- Do not explain anything.
- Do not add quotes.
- Do not add extra punctuation unless needed.
- Preserve brand/product names like italkyAI, FaceToFace, SideToSide, AllToAll, TextToText.
- Preserve numbers if present.
- Return only the translated text.

Text:
{text}
""".strip()

        response = model.generate_content(prompt)
        translated = ""

        try:
            translated = (response.text or "").strip()
        except Exception:
            translated = ""

        if not translated:
            translated = text

        return UiTranslateOut(translated_text=translated)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"UI translate failed: {e}")
