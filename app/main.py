# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.routers import chat, chat_openai, tts_openai, lang_pool, teacher_chat
from app.routers import translate
from app.routers import admin  # ✅

try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

try:
    from app.routers import tts, ocr
    has_legacy_modules = True
except ImportError:
    has_legacy_modules = False

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v3.0").strip()

app = FastAPI(
    title="italky Academy API",
    version=APP_VERSION,
    description="Backend service for italky Academy",
    redirect_slashes=False,
)

os.makedirs("static", exist_ok=True)
app.mount("/assets", StaticFiles(directory="static"), name="assets")

ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
]

extra = os.getenv("EXTRA_ORIGINS", "").strip()
if extra:
    for o in [x.strip() for x in extra.split(",") if x.strip()]:
        if o not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(o)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ROUTERS
app.include_router(chat.router, prefix="/api", tags=["Academy Chat"])
app.include_router(chat_openai.router, prefix="/api", tags=["Academy Chat"])
app.include_router(tts_openai.router, prefix="/api", tags=["Academy Voice"])
app.include_router(lang_pool.router, tags=["Academy Lang Pool"])
app.include_router(teacher_chat.router, prefix="/api", tags=["Academy Teacher"])

app.include_router(translate.router, prefix="/api", tags=["Academy Translate"])

# ✅ ADMIN API
app.include_router(admin.router, prefix="/api")  # /api/admin/...

if has_voice_openai and voice_openai is not None:
    app.include_router(voice_openai.router, prefix="/api", tags=["Academy Voice"])

if has_legacy_modules:
    app.include_router(tts.router, prefix="/api", tags=["Legacy TTS"])
    app.include_router(ocr.router, prefix="/api", tags=["Legacy OCR"])

@app.get("/")
def root():
    return {"status": "online", "service": "italky-academy-api", "version": APP_VERSION}

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
