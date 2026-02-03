from __future__ import annotations

import os
from typing import List
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

# --- ROUTER IMPORTLARI ---
# 1. Mevcut Routerlar
from app.routers.translate import router as translate_router
from app.routers.tts import router as tts_router
from app.routers.ocr import router as ocr_router

# 2. Yeni Routerlar (Sohbet ve Sesli Asistan)
# NOT: Bu dosyaların (chat.py ve tts_openai.py) app/routers/ klasöründe olduğundan emin olun.
from app.routers.chat import router as chat_router
from app.routers.tts_openai import router as tts_openai_router

# --- AYARLAR ---
APP_VERSION = os.getenv("APP_VERSION", "italky-api-v2.0").strip()

app = FastAPI(
    title="Italky API", 
    version=APP_VERSION, 
    description="italkyAI Backend Services",
    redirect_slashes=False
)

# --- CORS (Erişim İzinleri) ---
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5500", # VS Code Live Server için
    "*" # Geliştirme aşamasında her yerden erişime izin ver (Canlıda kapatılabilir)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ROUTERLARI AKTİF ETME ---

# Eski Özellikler
app.include_router(translate_router, prefix="/api", tags=["Translate"])
app.include_router(tts_router, prefix="/api", tags=["Legacy TTS"])
app.include_router(ocr_router, prefix="/api", tags=["OCR"])

# Yeni Özellikler (Sohbet ve Ses)
app.include_router(chat_router, prefix="/api", tags=["Chat AI"])
app.include_router(tts_openai_router, prefix="/api", tags=["Voice AI"])

# --- TEMEL ENDPOINTLER ---

@app.get("/")
def root():
    return {"ok": True, "service": "italky-api", "version": APP_VERSION}

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204) # İçerik yok (Hata vermemesi için)

if __name__ == "__main__":
    import uvicorn
    # Local test için
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
