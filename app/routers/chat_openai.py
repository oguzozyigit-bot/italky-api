# FILE: italky-api/app/routers/chat_openai.py
from __future__ import annotations

import os
import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from openai import OpenAI

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
client: Optional[OpenAI] = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

CHAT_MODEL = (os.getenv("OPENAI_MODEL_CHAT") or "gpt-4o-mini").strip()

class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class ChatOpenAIRequest(FlexibleModel):
    text: Optional[str] = None
    message: Optional[str] = None
    persona_name: Optional[str] = "italkyAI"
    history: Optional[List[Dict[str, str]]] = None
    max_tokens: Optional[int] = 140

class ChatOpenAIResponse(FlexibleModel):
    text: str
    model: str

@router.post("/chat_openai", response_model=ChatOpenAIResponse)
def chat_openai(req: ChatOpenAIRequest):
    if client is None:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")

    msg = (req.text or req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Boş mesaj gönderilemez.")

    name = req.persona_name or "italkyAI"

    system_prompt = (
        f"Adın {name}. italkyAI tarafından geliştirilen sesli asistansın. "
        "Cevapların kısa ve sohbet gibi olsun (1-2 cümle). "
        "Teknik altyapıdan bahsetme."
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if req.history:
        # role/content beklenir
        for h in req.history[-6:]:
            r = str(h.get("role", "user"))
            c = str(h.get("content", ""))
            if r in ("user", "assistant") and c:
                messages.append({"role": r, "content": c})

    messages.append({"role": "user", "content": msg})

    try:
        out = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=messages,
            max_tokens=int(req.max_tokens or 140),
            temperature=0.3,
        )

        text_out = (out.choices[0].message.content or "").strip()
        return ChatOpenAIResponse(text=text_out or "...", model=CHAT_MODEL)

    except Exception as e:
        logger.error("OpenAI chat failed: %s", e)
        raise HTTPException(status_code=500, detail=f"OpenAI chat failed: {str(e)}")
