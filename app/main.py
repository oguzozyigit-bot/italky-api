# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

# --- ROUTER IMPORTLARI (Modülleri çağırıyoruz) ---

# 1. Yeni Eklenen Özellikler (Sohbet & Ses)
from app.routers import chat
from app.routers import tts_openai

# 2. Mevcut Özellikler (Eğer bu dosyalar yoksa bu satırları silebilirsin)
try:
    from app.routers import translate
    from app.routers import tts
    from app.routers import ocr
    has_legacy_modules = True
except ImportError:
    has_legacy_modules = False

# --- UYGULAMA AYARLARI ---
APP_VERSION = os.getenv("APP_VERSION", "italky-api-v2.0").strip()

app = FastAPI(
    title="Italky AI API",
    version=APP_VERSION,
    description="Backend for Italky (Chat, TTS, Voice AI)",
    redirect_slashes=False
)

# --- CORS (Tarayıcı Erişim İzinleri) ---
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    "*" # Geliştirme ortamı için tümüne izin ver
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ROUTERLARI BAĞLAMA (INCLUDE) ---

# 1. Voice AI (Konuşkan Mod)
app.include_router(chat.router, prefix="/api", tags=["Chat AI"])
app.include_router(tts_openai.router, prefix="/api", tags=["Voice AI"])

# 2. Eski Modüller (Varsa ekle)
if has_legacy_modules:
    app.include_router(translate.router, prefix="/api", tags=["Translate"])
    app.include_router(tts.router, prefix="/api", tags=["Legacy TTS"])
    app.include_router(ocr.router, prefix="/api", tags=["OCR"])

# --- TEMEL ENDPOINTLER ---

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "italky-api",
        "version": APP_VERSION,
        "modules": {
            "chat": True,
            "voice_ai": True,
            "legacy_modules": has_legacy_modules
        }
    }

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

if __name__ == "__main__":
    import uvicorn
    # Localde çalıştırmak için: python app/main.py
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
