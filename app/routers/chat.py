# FILE: italky-api/app/routers/chat.py
from __future__ import annotations

import os
import re
import asyncio
import logging
import requests
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

# --- MODELLER ---
class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class ChatRequest(FlexibleModel):
    text: Optional[str] = None
    message: Optional[str] = None
    persona_name: Optional[str] = "italkyAI" # ✅ YENİ: Karakter İsmi
    history: Optional[List[Dict[str, str]]] = None
    max_tokens: Optional[int] = 200

class ChatResponse(FlexibleModel):
    text: str

# --- AYARLAR ---
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()
MODEL_NAME = "gemini-2.0-flash" # Hız için sabitliyoruz

# --- YARDIMCI ---
async def call_gemini(messages: List[Dict[str, Any]], max_tokens: int = 200) -> str:
    if not GEMINI_API_KEY: return "API Key eksik."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
    
    # Mesaj formatı
    contents = []
    system_instruction = ""

    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        if role == "system":
            system_instruction += text + "\n"
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})

    body = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens,
        }
    }
    
    # System Prompt (Kimlik)
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=10)
        if r.status_code != 200:
            logger.error(f"Gemini Error: {r.text}")
            return "Şu an cevap veremiyorum."
        
        data = r.json()
        return data.get("candidates", [])[0].get("content", {}).get("parts", [])[0].get("text", "")
    except Exception as e:
        logger.error(f"Gemini Exception: {e}")
        return "Bağlantı hatası."

# --- ENDPOINT ---
@router.post("/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    msg = (req.text or req.message or "").strip()
    if not msg: raise HTTPException(400, "Boş mesaj.")

    # ✅ DİNAMİK KİMLİK
    name = req.persona_name or "italkyAI"
    
    system = (
        f"Senin adın {name}.\n"
        "Sen italkyAI tarafından geliştirilen, insanlarla sesli sohbet eden yardımsever bir asistansın.\n"
        "Kullanıcı sana adını sorarsa kendi adını ({name}) söyle.\n"
        "Cevapların KISA, SAMİMİ ve KONUŞMA DİLİNDE olsun.\n"
        "Asla uzun cümleler kurma. 1-2 cümle yeterli.\n"
        "Google, OpenAI gibi firmalardan bahsetme."
    )

    messages = [{"role": "system", "content": system}]
    if req.history:
        for h in req.history[-5:]:
            messages.append(h)
    messages.append({"role": "user", "content": msg})

    reply = await call_gemini(messages, max_tokens=req.max_tokens)
    return ChatResponse(text=reply)
