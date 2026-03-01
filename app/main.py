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
from app.routers import ocr_translate
from app.routers import offline

# ✅ EXAM PRO ROUTER (Foto çek çöz / solve_text / deneme motoru)
try:
    from app.routers import exam_pro
    has_exam_pro = True
except Exception:
    exam_pro = None
    has_exam_pro = False

# ✅ LEVEL TEST ROUTER
try:
    from app.routers import level_test
    has_level_test = True
except Exception:
    level_test = None
    has_level_test = False

# ✅ OPENAI VOICE ROUTER (varsa)
try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

# ✅ OCR ROUTER (varsa)
try:
    from app.routers import ocr
    has_ocr = True
except Exception:
    ocr = None
    has_ocr = False


APP_VERSION = os.getenv("APP_VERSION", "italky-api-v3.1").strip()

app = FastAPI(
    title="italky Academy API",
    version=APP_VERSION,
    description="Backend service for italky Academy",
    redirect_slashes=False,
)

# ===============================
# STATIC
# ===============================
os.makedirs("static", exist_ok=True)
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# ===============================
# CORS
# ===============================
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
    allow_origin_regex=None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# ===============================
# CORE ROUTERS
# ===============================
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
app.include_router(ocr_translate.router, prefix="/api")
app.include_router(offline.router, prefix="/api")
# ===============================
# OPTIONAL ROUTERS
# ===============================

# ✅ PRO exam engine
# exam_pro.py içinde route: @router.post("/exam/solve_text")
# Bu yüzden final endpoint: /api/exam/solve_text
if has_exam_pro:
    app.include_router(exam_pro.router, prefix="/api")

# ✅ Level test
if has_level_test:
    app.include_router(level_test.router, prefix="/api")

# ✅ Voice OpenAI
if has_voice_openai:
    app.include_router(voice_openai.router, prefix="/api")

# ✅ OCR
if has_ocr:
    app.include_router(ocr.router, prefix="/api")

# ===============================
# HEALTH
# ===============================
@app.get("/")
def root():
    return {
        "status": "online",
        "service": "italky-academy-api",
        "version": APP_VERSION
    }

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
