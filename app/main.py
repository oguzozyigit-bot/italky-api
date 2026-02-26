# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.routers import chat
from app.routers import chat_openai
from app.routers import lang_pool
from app.routers import teacher_chat
from app.routers import translate
from app.routers import translate_ai
from app.routers import command_parse
from app.routers import admin
from app.routers import f2f_ws
from app.routers import tts
from app.routers import stt

# ✅ LEVEL TEST ROUTER
from app.routers import level_test

try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

try:
    from app.routers import ocr
    has_ocr = True
except Exception:
    ocr = None
    has_ocr = False

APP_VERSION = (os.getenv("APP_VERSION", "italky-api-v3.0") or "").strip()

app = FastAPI(
    title="italky Academy API",
    version=APP_VERSION,
    description="Backend service for italky Academy",
    redirect_slashes=False,
)

os.makedirs("static", exist_ok=True)
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# ✅ CORS: Regex ile (italky.ai + www + preview subdomain vs.)
# - allow_credentials True kalsın (auth cookie kullanan endpointler olabilir)
# - regex, listeye göre daha az sürpriz çıkarır
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
    allow_origin_regex=r"^https:\/\/([a-z0-9-]+\.)*italky\.ai$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# ROUTERS
app.include_router(chat.router, prefix="/api")
app.include_router(chat_openai.router, prefix="/api")

app.include_router(lang_pool.router, prefix="/api")
app.include_router(teacher_chat.router, prefix="/api")

app.include_router(translate.router, prefix="/api")
app.include_router(translate_ai.router, prefix="/api")
app.include_router(command_parse.router, prefix="/api")

app.include_router(tts.router, prefix="/api")
app.include_router(stt.router, prefix="/api")
app.include_router(f2f_ws.router, prefix="/api")
app.include_router(admin.router, prefix="/api")

# ✅ level test endpoints: /api/level_test/...
app.include_router(level_test.router, prefix="/api")

if has_voice_openai:
    app.include_router(voice_openai.router, prefix="/api")

if has_ocr:
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
