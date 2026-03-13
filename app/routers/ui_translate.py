# FILE: app/routers/ui_translate.py

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import google.generativeai as genai

router = APIRouter()

SUPPORTED_LANGS = {"en", "de", "fr", "it", "es"}


class UiTranslateIn(BaseModel):
    text: str
    target_lang: Literal["en", "de", "fr", "it", "es"]


class UiTranslateOut(BaseModel):
    translated_text: str


def _get_gemini_key():
    key = (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GOOGLE_AI_API_KEY")
    )
    if not key:
        raise HTTPException(500, "Gemini API key bulunamadı")
    return key


def _lang_name(code: str) -> str:
    return {
        "en": "English",
        "de": "German",
        "fr": "French",
        "it": "Italian",
        "es": "Spanish",
    }.get(code, "English")


@router.post("/ui-translate", response_model=UiTranslateOut)
async def ui_translate(payload: UiTranslateIn):

    text = payload.text.strip()
    lang = payload.target_lang.lower()

    if not text:
        return {"translated_text": ""}

    if lang not in SUPPORTED_LANGS:
        return {"translated_text": text}

    try:

        genai.configure(api_key=_get_gemini_key())

        model = genai.GenerativeModel(
            os.getenv("GEMINI_UI_TRANSLATE_MODEL", "gemini-1.5-flash")
        )

        prompt = f"""
Translate the following Turkish UI text to {_lang_name(lang)}.

Rules:
- keep it short
- mobile UI style
- no explanation
- return only translation
- keep product names unchanged (italkyAI, FaceToFace, SideToSide, AllToAll)

Text:
{text}
"""

        r = model.generate_content(prompt)

        try:
            translated = r.text.strip()
        except:
            translated = text

        if not translated:
            translated = text

        return {"translated_text": translated}

    except Exception as e:
        raise HTTPException(500, f"Translate failed: {e}")
