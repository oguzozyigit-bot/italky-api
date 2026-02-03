# FILE: italky-api/app/routers/voice_chat.py
from __future__ import annotations

import os
import base64
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from openai import OpenAI

router = APIRouter()

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_CHAT_MODEL = (os.getenv("OPENAI_CHAT_MODEL", "") or "gpt-4o-mini").strip()
OPENAI_TTS_MODEL  = (os.getenv("OPENAI_TTS_MODEL", "") or "gpt-4o-mini-tts").strip()
DEFAULT_VOICE     = (os.getenv("OPENAI_TTS_VOICE", "") or "alloy").strip()

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class VoiceChatReq(FlexibleModel):
    user_id: str
    text: str
    voice: Optional[str] = None

class VoiceChatResp(FlexibleModel):
    ok: bool
    text: str
    audio_base64: str

@router.post("/voice/chat", response_model=VoiceChatResp)
def voice_chat(req: VoiceChatReq):
    if not client:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    text_in = (req.text or "").strip()
    if not text_in:
        raise HTTPException(400, "empty text")

    # ✅ Italky persona (Google vs demesin)
    system = (
        "Sen italkyAI'nin geliştirdiği dil asistanısın.\n"
        "Kısa, net, yardımsever cevap ver.\n"
        "Kendini 'Google' veya 'Gemini' diye tanıtma.\n"
        "Gereksiz uzun anlatma.\n"
    )

    try:
        chat = client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=[
                {"role":"system","content":system},
                {"role":"user","content":text_in}
            ],
            temperature=0.3,
        )
        reply = (chat.choices[0].message.content or "").strip() or "…"
    except Exception as e:
        raise HTTPException(502, f"openai_chat_error: {str(e)}")

    voice = (req.voice or DEFAULT_VOICE or "alloy").strip()

    try:
        audio = client.audio.speech.create(
            model=OPENAI_TTS_MODEL,
            voice=voice,
            input=reply[:3000],
            format="mp3",
        )
        audio_bytes = audio.read()
        b64 = base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as e:
        raise HTTPException(502, f"openai_tts_error: {str(e)}")

    return VoiceChatResp(ok=True, text=reply, audio_base64=b64)
