# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

# ROUTERS
from app.routers import chat
from app.routers import chat_openai
from app.routers import tts_openai
from app.routers import lang_pool
from app.routers import teacher_chat
from app.routers import translate
from app.routers import admin   # ✅ ADMIN

try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

try:
    from app.routers import tts
    from app.routers import ocr
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

# STATIC
os.makedirs("static", exist_ok=True)
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# CORS
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ROUTER REGISTER
app.include_router(chat.router, prefix="/api")
app.include_router(chat_openai.router, prefix="/api")
app.include_router(tts_openai.router, prefix="/api")
app.include_router(lang_pool.router)
app.include_router(teacher_chat.router, prefix="/api")
app.include_router(translate.router, prefix="/api")

# ✅ ADMIN ROUTER
app.include_router(admin.router, prefix="/api")

if has_voice_openai:
    app.include_router(voice_openai.router, prefix="/api")

if has_legacy_modules:
    app.include_router(tts.router, prefix="/api")
    app.include_router(ocr.router, prefix="/api")

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "italky-academy-api",
        "version": APP_VERSION,
    }

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
