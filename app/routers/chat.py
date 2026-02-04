# FILE: italky-api/app/routers/chat.py
from __future__ import annotations

import os
import logging
import httpx  # ✅ DEĞİŞTİ: Asenkron istekler için requests yerine httpx
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

# Loglama Ayarları
logger = logging.getLogger("uvicorn.error")
router = APIRouter()

# --- MODELLER ---
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

# --- AYARLAR ---
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()
# Not: Hız ve maliyet dengesi için flash model
MODEL_NAME = "gemini-2.0-flash" 

# --- YARDIMCI FONKSİYON ---
async def call_gemini(messages: List[Dict[str, Any]], system_instruction: str, max_tokens: int = 200) -> str:
    """Gemini API çağrısını asenkron olarak yapar (Sunucuyu kilitlemez)."""
    
    if not GEMINI_API_KEY:
        logger.error("Gemini API Key eksik! Lütfen .env dosyasını kontrol et.")
        return "Sistem ayarlarında bir eksiklik var."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"
    
    # Mesajları Google Gemini formatına çeviriyoruz
    contents = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        
        # System mesajını buraya eklemiyoruz, ayrı parametre olarak gönderiyoruz
        if role == "system": 
            continue 
            
        # OpenAI formatından Google formatına basit çeviri
        if role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})
        else:
            contents.append({"role": "user", "parts": [{"text": content}]})

    # İstek gövdesi
    body = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "generationConfig": {
            "temperature": 0.3, # Daha tutarlı ve saçmalamayan cevaplar için düşük tutuyoruz
            "maxOutputTokens": max_tokens,
        }
    }
    
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}

    try:
        # ✅ ASENKRON ÇAĞRI: Burası sunucuyu kilitlemez, arkada bekler.
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(url, params=params, json=body, headers=headers)
            
        if response.status_code != 200:
            logger.error(f"Gemini API Hatası ({response.status_code}): {response.text}")
            return "Şu an cevap veremiyorum, lütfen biraz sonra tekrar dene."
        
        data = response.json()
        
        # Cevabı güvenli bir şekilde alıp temizliyoruz
        if "candidates" in data and data["candidates"]:
            raw_text = data["candidates"][0].get("content", {}).get("parts", [])[0].get("text", "")
            return raw_text.strip()
        else:
            logger.warning(f"Gemini boş cevap döndü: {data}")
            return "..."

    except httpx.RequestError as e:
        logger.error(f"Bağlantı Hatası (Network): {e}")
        return "İnternet bağlantısında bir sorun var."
    except Exception as e:
        logger.error(f"Genel Hata: {e}")
        return "Bir hata oluştu."

# --- ENDPOINT ---
@router.post("/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    # Gelen mesajı al (text veya message alanından)
    msg = (req.text or req.message or "").strip()
    if not msg: 
        raise HTTPException(status_code=400, detail="Boş mesaj gönderilemez.")

    # ✅ DİNAMİK KİMLİK (PERSONA)
    name = req.persona_name or "italkyAI"
    
    # System Prompt: Sesli asistan olduğu için cevapların okunabilirliğine vurgu yaptık
    system_prompt = (
        f"Senin adın {name}. "
        f"Sen italkyAI tarafından geliştirilen, insanlarla sesli sohbet eden yardımsever bir asistansın. "
        f"Kullanıcı sana adını sorarsa gururla {name} olduğunu söyle. "
        "Yanıtların kesinlikle KISA, SAMİMİ ve SOHBET TARZINDA olmalı. "
        "Robotik konuşma. Uzun paragraflar yazma, en fazla 1-2 cümle kur. "
        "Asla emojileri abartma çünkü bu metin sese çevrilecek (TTS). "
        "Google, OpenAI veya teknik altyapıdan asla bahsetme. Sadece yardımcı ol."
    )

    # Sohbet geçmişini hazırla
    messages = []
    if req.history:
        # Sadece son 6 mesajı alıyoruz ki token limiti dolmasın ve bağlam kopmasın
        messages.extend(req.history[-6:])
    
    # Yeni kullanıcı mesajını ekle
    messages.append({"role": "user", "content": msg})

    # Gemini'yi çağır
    reply = await call_gemini(messages, system_instruction=system_prompt, max_tokens=req.max_tokens)
    
    return ChatResponse(text=reply)
