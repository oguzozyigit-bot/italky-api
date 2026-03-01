# FILE: italky-api/app/routers/ocr_translate.py
from __future__ import annotations

import os
import base64
import logging
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("ocr-translate")
router = APIRouter(tags=["ocr-translate"])

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL_OCR = (os.getenv("OCR_OPENAI_MODEL") or "gpt-4o-mini").strip()
OPENAI_MODEL_TRANSLATE = (os.getenv("TRANSLATE_OPENAI_MODEL") or "gpt-4o-mini").strip()

# ---- helpers ----
def _canon_lang(code: str) -> str:
    c = (code or "").strip().lower().replace("_", "-")
    if not c:
        return "auto"
    return c

def _guess_mime(filename: str, content_type: str | None) -> str:
    ct = (content_type or "").strip().lower()
    if ct.startswith("image/"):
        return ct
    fn = (filename or "").lower()
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith(".webp"):
        return "image/webp"
    if fn.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"

class OcrTranslateResp(BaseModel):
    ok: bool = True
    extracted_text: str = ""
    translated_text: str = ""
    provider: str = "openai"
    error: Optional[str] = None

# ---- route ----
@router.post("/translate/ocr_translate", response_model=OcrTranslateResp)
async def ocr_translate(
    image: UploadFile = File(...),
    from_lang: str = Form("auto"),
    to_lang: str = Form("tr"),
):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    src = _canon_lang(from_lang)
    dst = _canon_lang(to_lang)
    if not dst or dst == "auto":
        dst = "tr"

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        img_bytes = await image.read()
        if not img_bytes:
            raise HTTPException(status_code=422, detail="Empty image")

        mime = _guess_mime(image.filename or "", image.content_type)
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        data_url = f"data:{mime};base64,{b64}"

        # 1) OCR (text extraction)
        ocr_prompt = (
            "Extract ALL readable text from this image.\n"
            "Rules:\n"
            "- Return ONLY plain text.\n"
            "- Keep line breaks.\n"
            "- If there are multiple questions, keep them in order.\n"
            "- Do NOT add any explanation.\n"
        )

        ocr_resp = await client.chat.completions.create(
            model=OPENAI_MODEL_OCR,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ocr_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )

        extracted_text = (ocr_resp.choices[0].message.content or "").strip()
        if not extracted_text:
            return OcrTranslateResp(
                ok=False,
                extracted_text="",
                translated_text="",
                provider="openai",
                error="OCR_EMPTY",
            )

        # 2) Translate (strict)
        # If src == "auto", let model detect. Otherwise constrain.
        if src == "auto":
            sys = (
                f"Translate the following text to {dst}.\n"
                "Detect the source language automatically.\n"
                "Return ONLY the translated text. No explanations."
            )
        else:
            sys = (
                f"Translate the following text from {src} to {dst}.\n"
                "Return ONLY the translated text. No explanations."
            )

        tr_resp = await client.chat.completions.create(
            model=OPENAI_MODEL_TRANSLATE,
            temperature=0,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": extracted_text},
            ],
        )

        translated = (tr_resp.choices[0].message.content or "").strip()

        return OcrTranslateResp(
            ok=True,
            extracted_text=extracted_text,
            translated_text=translated,
            provider="openai",
            error=None,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("OCR_TRANSLATE_FAIL: %s", e)
        raise HTTPException(status_code=500, detail=f"ocr_translate failed: {e}")
