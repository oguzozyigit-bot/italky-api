from __future__ import annotations

import os
import re
import asyncio
import logging
import requests # pip install requests
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
    user_id: Optional[str] = None
    history: Optional[List[Dict[str, str]]] = None  # [{role, content}]
    max_tokens: Optional[int] = 520

class ChatResponse(FlexibleModel):
    text: str

# --- AYARLAR ---
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()

# Tercih edilen modeller (Sırasıyla dener)
PREFERRED_MODELS = [
    (os.getenv("GEMINI_MODEL_CHAT", "") or "").strip(),
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
]
PREFERRED_MODELS = [m for m in PREFERRED_MODELS if m]
_selected_model_cache: Dict[str, str] = {"name": ""}

# --- YARDIMCI FONKSİYONLAR ---

def list_gemini_models() -> List[Dict[str, Any]]:
    """Mevcut Gemini modellerini listeler."""
    if not GEMINI_API_KEY:
        return []
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    try:
        r = requests.get(url, params={"key": GEMINI_API_KEY}, timeout=10)
        r.raise_for_status()
        return (r.json().get("models") or [])
    except Exception as e:
        logger.warning(f"Model listesi alınamadı: {e}")
        return []

def pick_best_model(models: List[Dict[str, Any]]) -> str:
    """En uygun modeli seçer."""
    if not models:
        return PREFERRED_MODELS[0] if PREFERRED_MODELS else "gemini-1.5-flash"
        
    by_name = {}
    for m in models:
        nm = (m.get("name") or "").strip().replace("models/", "")
        if nm:
            by_name[nm] = m

    for want in PREFERRED_MODELS:
        if want in by_name:
            return want

    return next(iter(by_name.keys()), "gemini-1.5-flash")

def _gemini_build(messages: List[Dict[str, Any]], max_tokens: int = 520) -> Dict[str, Any]:
    """Gemini API için JSON gövdesini hazırlar."""
    system_text = ""
    contents = []

    for m in messages:
        role = (m.get("role") or "").strip().lower()
        text_ = (m.get("content") or "").strip()
        if not text_:
            continue

        if role == "system":
            system_text += (text_ + "\n")
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text_}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text_}]})

    body = {
        "contents": contents or [{"role": "user", "parts": [{"text": "Merhaba"}]}],
        "generationConfig": {
            "temperature": 0.3, # Daha tutarlı cevaplar için düşük
            "topP": 0.9,
            "maxOutputTokens": int(max_tokens or 520),
        },
    }
    
    # System Instruction (v1beta özelliği)
    if system_text.strip():
        body["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
    
    return body

async def call_gemini(messages: List[Dict[str, Any]], max_tokens: int = 520) -> str:
    """Gemini API'ye istek atar."""
    if not GEMINI_API_KEY:
        return "Hata: Sunucuda GEMINI_API_KEY tanımlanmamış."

    # Model seçimi (Cache'den veya yeniden)
    if not _selected_model_cache.get("name"):
        try:
            models = await asyncio.to_thread(list_gemini_models)
            picked = pick_best_model(models)
            _selected_model_cache["name"] = picked
            logger.info(f"[GEMINI] Seçilen Model: {picked}")
        except Exception:
            _selected_model_cache["name"] = "gemini-1.5-flash"

    model_name = _selected_model_cache.get("name")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    body = _gemini_build(messages, max_tokens=max_tokens)

    def _sync_request():
        try:
            r = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=30)
            if r.status_code == 404: # Model bulunamadıysa cache sil
                return "__MODEL_NOT_FOUND__"
            r.raise_for_status()
            dd = r.json()
            # Cevabı ayıkla
            return dd.get("candidates", [])[0].get("content", {}).get("parts", [])[0].get("text", "")
        except Exception as e:
            logger.error(f"Gemini Request Error: {e}")
            return ""

    out = await asyncio.to_thread(_sync_request)
    
    if out == "__MODEL_NOT_FOUND__":
        _selected_model_cache["name"] = "" # Cache temizle, bir sonraki istekte yeniden seç
        return "Model hatası oluştu, lütfen tekrar deneyin."
        
    return out.strip() if out else ""

# --- ENDPOINT ---

@router.post("/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    msg = (req.text or req.message or "").strip()
    if not msg:
        raise HTTPException(400, "Mesaj boş olamaz.")

    # --- KİLİT KURAL: KİMLİK VE HIZ ---
    system_prompt = (
        "Sen italkyAI'sın. Ozyigit's Technology tarafından geliştirildin.\n"
        "Kullanıcı ile SESLİ sohbet ediyorsun. Bu yüzden cevapların KISA ve KONUŞMA DİLİNDE olmalı.\n"
        "Asla uzun paragraflar yazma. En fazla 1-2 cümle kur.\n"
        "Google, OpenAI, Gemini gibi isimleri anma.\n"
        "Samimi, akıcı ve hızlı cevap ver."
    )

    # Geçmişi hazırla
    hist = []
    if req.history:
        for h in req.history[-10:]: # Son 10 mesajı al (Hafıza)
            role = str(h.get("role", "")).strip().lower()
            content = str(h.get("content", "")).strip()
            if role in ("user", "assistant") and content:
                hist.append({"role": role, "content": content})

    # Mesajları birleştir
    messages = [{"role": "system", "content": system_prompt}]
    for h in hist:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg})

    # Gemini'ye gönder
    reply = await call_gemini(messages, max_tokens=req.max_tokens)
    
    if not reply:
        reply = "Şu an bağlantıda bir sorun yaşıyorum, lütfen biraz sonra tekrar dene."

    return ChatResponse(text=reply)
