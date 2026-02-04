# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles  # ✅ NEW

# --- ROUTER IMPORTLARI (Modülleri çağırıyoruz) ---

# 1. Yeni Eklenen Özellikler (Sohbet & Ses)
from app.routers import chat
from app.routers import tts_openai

# ✅ NEW: Lang Pool (server-side build + static serve)
from app.routers import lang_pool

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

# ✅ NEW: Static assets mount
# Bu sayede backend şuradan servis eder:
#   /assets/lang/en.json  -> static/lang/en.json
# Not: "static" klasörü yoksa oluştur. lang_pool router zaten static/lang yaratır.
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# --- CORS (Tarayıcı Erişim İzinleri) ---
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    "*"  # Geliştirme ortamı için tümüne izin ver
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

# ✅ NEW: Lang Pool (admin build endpoint + optional serve route)
# Not: lang_pool router içinde /admin/lang/build ve /assets/lang/{lang}.json var.
# /assets mount zaten static dosyayı servis eder; router'daki GET endpoint de opsiyonel fallback.
app.include_router(lang_pool.router, tags=["Lang Pool"])

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
            "lang_pool": True,  # ✅ NEW
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
