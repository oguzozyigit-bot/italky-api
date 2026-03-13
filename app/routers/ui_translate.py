from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
import google.generativeai as genai

router = APIRouter()
logger = logging.getLogger("ui-translate")

SUPPORTED_LANGS = {"en", "de", "fr", "it", "es"}


class UiTranslateIn(BaseModel):
    text: str
    target_lang: Literal["en", "de", "fr", "it", "es"]


class UiTranslateOut(BaseModel):
    translated_text: str


def _get_gemini_key() -> str:
    return (
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GOOGLE_AI_API_KEY")
        or ""
    ).strip()


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
    text = (payload.text or "").strip()
    lang = (payload.target_lang or "").strip().lower()

    if not text:
        return UiTranslateOut(translated_text="")

    if lang not in SUPPORTED_LANGS:
        return UiTranslateOut(translated_text=text)

    # Çok kısa / anlamsız parçalar için API'ye gitme
    if len(text) < 3:
        return UiTranslateOut(translated_text=text)

    api_key = _get_gemini_key()
    if not api_key:
        logger.warning("ui_translate: Gemini key yok, fallback text dönüldü.")
        return UiTranslateOut(translated_text=text)

    try:
        genai.configure(api_key=api_key)

        model_name = (
            os.getenv("GEMINI_UI_TRANSLATE_MODEL")
            or os.getenv("GEMINI_MODEL")
            or "gemini-1.5-flash"
        ).strip()

        model = genai.GenerativeModel(model_name=model_name)

        prompt = f"""
Translate the following Turkish mobile app UI text into {_lang_name(lang)}.

Rules:
- Keep it short and natural.
- Return only the translation.
- Do not add quotes.
- Do not explain anything.
- Keep brand names unchanged: italkyAI, FaceToFace, SideToSide, AllToAll, TextToText.

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
            logger.warning("ui_translate: boş Gemini cevabı, fallback text dönüldü. text=%r", text)
            return UiTranslateOut(translated_text=text)

        return UiTranslateOut(translated_text=translated)

    except Exception as e:
        logger.exception("ui_translate failed. text=%r lang=%r error=%s", text, lang, e)
        return UiTranslateOut(translated_text=text)
