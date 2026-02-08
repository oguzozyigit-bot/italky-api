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
    max_tokens: Optional[int] = 200

class ChatResponse(FlexibleModel):
    text: str

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()

# ✅ env'den model seç (yoksa fallback)
MODEL_NAME = (os.getenv("GEMINI_MODEL_CHAT", "") or "").strip() or "gemini-2.0-flash"

# ✅ keep-alive client (speed + stability)
_CLIENT: Optional[httpx.AsyncClient] = None

def _get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
        _CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=6.0),
            limits=limits,
            http2=True,
            headers={"Content-Type": "application/json"},
        )
    return _CLIENT

def _to_gemini_contents(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    contents: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            continue
        if role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})
        else:
            contents.append({"role": "user", "parts": [{"text": content}]})
    return contents

async def call_gemini(messages: List[Dict[str, Any]], system_instruction: str, max_tokens: int = 200) -> str:
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY missing/empty in runtime env")
        return "Şu an cevap veremiyorum."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

    body = {
        "contents": _to_gemini_contents(messages),
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": int(max_tokens or 200),
        },
    }

    try:
        client = _get_client()
        resp = await client.post(url, params={"key": GEMINI_API_KEY}, json=body)

        if resp.status_code != 200:
            # ✅ burada gerçek nedeni log'a basıyoruz
            logger.error("Gemini API error %s: %s", resp.status_code, resp.text[:1200])
            # kullanıcıya kısa mesaj
            if resp.status_code == 401 or resp.status_code == 403:
                return "AI anahtar yetkisi sorunu var."
            if resp.status_code == 429:
                return "AI şu an çok yoğun. Birazdan dene."
            return "AI şu an cevap veremiyor."

        data = resp.json()

        if data.get("candidates"):
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            if parts:
                txt = str(parts[0].get("text", "")).strip()
                return txt or "..."
        logger.warning("Gemini empty candidates: %s", str(data)[:800])
        return "..."

    except httpx.RequestError as e:
        logger.error("Gemini network error: %s", e)
        return "Bağlantı sorunu var."
    except Exception as e:
        logger.error("Gemini unexpected error: %s", e)
        return "Bir hata oluştu."

@router.post("/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    msg = (req.text or req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Boş mesaj gönderilemez.")

    name = req.persona_name or "italkyAI"

    system_prompt = (
        f"Senin adın {name}. "
        f"Sen italkyAI tarafından geliştirilen, insanlarla sesli sohbet eden yardımsever bir asistansın. "
        "Yanıtların kısa ve sohbet tarzında olmalı (1-2 cümle). "
        "Teknik altyapıdan bahsetme."
    )

    messages: List[Dict[str, Any]] = []
    if req.history:
        messages.extend(req.history[-6:])
    messages.append({"role": "user", "content": msg})

    reply = await call_gemini(messages, system_instruction=system_prompt, max_tokens=req.max_tokens or 200)
    return ChatResponse(text=reply)
