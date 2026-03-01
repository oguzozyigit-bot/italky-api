from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from typing import Optional
import os

router = APIRouter(tags=["ocr-translate"])

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

@router.post("/translate/ocr_translate")
async def ocr_translate(
    image: UploadFile = File(...),
    from_lang: str = Form("auto"),
    to_lang: str = Form("en")
):
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)

        img_bytes = await image.read()

        # 1️⃣ OCR
        ocr_resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all readable text from this image. Return only plain text."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img_bytes.hex()}"
                            }
                        }
                    ]
                }
            ]
        )

        extracted_text = ocr_resp.choices[0].message.content.strip()

        # 2️⃣ Çeviri
        translate_resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": f"Translate the following text from {from_lang} to {to_lang}. Only return translated text."
                },
                {
                    "role": "user",
                    "content": extracted_text
                }
            ]
        )

        translated = translate_resp.choices[0].message.content.strip()

        return {
            "ok": True,
            "extracted_text": extracted_text,
            "translated_text": translated
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
