# FILE: italky-api/app/main.py

from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

# -------------------------------
# ROUTER IMPORTS
# -------------------------------
from app.routers import chat
from app.routers import chat_openai
from app.routers import tts_openai
from app.routers import lang_pool
from app.routers import teacher_chat   # ✅ NEW

# Optional voice router
try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

# Optional legacy modules
try:
    from app.routers import translate
    from app.routers import tts
    from app.routers import ocr
    has_legacy_modules = True
except ImportError:
    has_legacy_modules = False

# -------------------------------
# APP CONFIG
# -------------------------------

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v3.0").strip()

app = FastAPI(
    title="Italky Academy API",
    version=APP_VERSION,
    description="Backend service for italky Academy (Chat, Voice, Course, Lang Pool)",
    redirect_slashes=False
)

# -------------------------------
# STATIC ASSETS
# -------------------------------

os.makedirs("static/lang", exist_ok=True)
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# -------------------------------
# CORS
# -------------------------------

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

# -------------------------------
# ROUTER REGISTRATION
# -------------------------------

# Gemini site chat (legacy site chat)
app.include_router(chat.router, prefix="/api", tags=["Chat AI"])

# OpenAI text chat (voice text / modern chat)
app.include_router(chat_openai.router, prefix="/api", tags=["Chat OpenAI"])

# Voice (TTS base64)
app.include_router(tts_openai.router, prefix="/api", tags=["Voice AI"])

# Language pool
app.include_router(lang_pool.router, tags=["Lang Pool"])

# ✅ Real-time course teacher endpoint
app.include_router(teacher_chat.router, prefix="/api", tags=["Teacher Course"])

# Optional voice router
if has_voice_openai and voice_openai is not None:
    app.include_router(voice_openai.router, prefix="/api", tags=["Voice OpenAI"])

# Optional legacy modules
if has_legacy_modules:
    app.include_router(translate.router, prefix="/api", tags=["Translate"])
    app.include_router(tts.router, prefix="/api", tags=["Legacy TTS"])
    app.include_router(ocr.router, prefix="/api", tags=["OCR"])

# -------------------------------
# HEALTH & ROOT
# -------------------------------

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "italky-academy-api",
        "version": APP_VERSION,
        "modules": {
            "chat": True,
            "chat_openai": True,
            "teacher_course": True,
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

# -------------------------------
# LOCAL DEV
# -------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
