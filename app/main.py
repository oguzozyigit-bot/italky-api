# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

# --- ROUTER IMPORTLARI ---
from app.routers import chat
from app.routers import tts_openai
from app.routers import lang_pool

# ✅ YENİ: OpenAI STT/TTS (voice) router
# (Bu dosyayı bir sonraki adımda oluşturacağız: app/routers/voice_openai.py)
try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

try:
    from app.routers import translate
    from app.routers import tts
    from app.routers import ocr
    has_legacy_modules = True
except ImportError:
    has_legacy_modules = False

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v2.0").strip()

app = FastAPI(
    title="Italky AI API",
    version=APP_VERSION,
    description="Backend for Italky (Chat, TTS, Voice AI)",
    redirect_slashes=False
)

# ✅ KRİTİK: static/lang klasörü garanti (mount'tan ÖNCE)
os.makedirs("static/lang", exist_ok=True)

# ✅ Static assets mount
# /assets/lang/en.json -> static/lang/en.json
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# ✅ CORS: allow_credentials=True iken "*" KULLANILMAZ (tarayıcıda patlar)
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ROUTERLAR ---
app.include_router(chat.router, prefix="/api", tags=["Chat AI"])
app.include_router(tts_openai.router, prefix="/api", tags=["Voice AI"])
app.include_router(lang_pool.router, tags=["Lang Pool"])

# ✅ YENİ: voice_openai router (dosya varsa devreye girer)
if has_voice_openai and voice_openai is not None:
    app.include_router(voice_openai.router, prefix="/api", tags=["Voice OpenAI"])

if has_legacy_modules:
    app.include_router(translate.router, prefix="/api", tags=["Translate"])
    app.include_router(tts.router, prefix="/api", tags=["Legacy TTS"])
    app.include_router(ocr.router, prefix="/api", tags=["OCR"])

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "italky-api",
        "version": APP_VERSION,
        "modules": {
            "chat": True,
            "voice_ai": True,
            "voice_openai": bool(has_voice_openai),
            "lang_pool": True,
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
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
