# FILE: italky-api/app/routers/chat.py
from __future__ import annotations

import os
import logging
from typing import Optional, List, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class ChatRequest(FlexibleModel):
    text: Optional[str] = None
    message: Optional[str] = None
    persona_name: Optional[str] = "italkyAI"
    history: Optional[List[Dict[str, str]]] = None
    max_tokens: Optional[int] = 120  # ✅ faster default

class ChatResponse(FlexibleModel):
    text: str

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()
MODEL_NAME = "gemini-2.0-flash"

# ✅ Reuse one AsyncClient for keep-alive (faster per message)
_GEMINI_CLIENT: Optional[httpx.AsyncClient] = None

def _get_client() -> httpx.AsyncClient:
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
        _GEMINI_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=5.0),
            limits=limits,
            http2=True
        )
    return _GEMINI_CLIENT

async def call_gemini(messages: List[Dict[str, Any]], system_instruction: str, max_tokens: int = 120) -> str:
    if not GEMINI_API_KEY:
        logger.error("Gemini API Key eksik! .env kontrol et.")
        return "Sistem ayarı eksik."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

    contents = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            continue
        if role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})
        else:
            contents.append({"role": "user", "parts": [{"text": content}]})

    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": max_tokens,
        },
    }

    try:
        client = _get_client()
        resp = await client.post(url, params={"key": GEMINI_API_KEY}, json=body, headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            logger.error(f"Gemini error {resp.status_code}: {resp.text}")
            return "Şu an cevap veremiyorum."
        data = resp.json()
        if data.get("candidates"):
            raw = data["candidates"][0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return str(raw).strip() or "..."
        return "..."
    except Exception as e:
        logger.error(f"Gemini call failed: {e}")
        return "Bir hata oluştu."

@router.post("/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    msg = (req.text or req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Boş mesaj gönderilemez.")

    name = req.persona_name or "italkyAI"

    # ✅ shorter system prompt (faster)
    system_prompt = (
        f"Adın {name}. italkyAI asistanısın. "
        "Kısa ve samimi konuş. 1-2 cümle. "
        "Teknik altyapıdan bahsetme."
    )

    messages: List[Dict[str, Any]] = []
    if req.history:
        # ✅ fewer history items (faster)
        messages.extend(req.history[-4:])

    messages.append({"role": "user", "content": msg})

    reply = await call_gemini(messages, system_instruction=system_prompt, max_tokens=int(req.max_tokens or 120))
    return ChatResponse(text=reply)
