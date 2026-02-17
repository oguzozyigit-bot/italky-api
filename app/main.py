# FILE: italky-api/app/main.py
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

# -------------------------------
# ROUTERS
# -------------------------------
from app.routers import chat              # (mevcut)
from app.routers import chat_openai       # (mevcut)
from app.routers import tts_openai        # (mevcut)
from app.routers import lang_pool         # (mevcut)
from app.routers import teacher_chat      # ✅ (mevcut)

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
    title="italky Academy API",
    version=APP_VERSION,
    description="Backend service for italky Academy (Chat, Voice, Course, Lang Pool)",
    redirect_slashes=False,
)

# -------------------------------
# STATIC ASSETS
# -------------------------------
# /static altında: /static/tests/*.json vs yayınlamak için
os.makedirs("static", exist_ok=True)
app.mount("/assets", StaticFiles(directory="static"), name="assets")

# -------------------------------
# CORS
# -------------------------------
# Not: Render domainini de eklersen front testlerinde rahat edersin.
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5500",
]

# İstersen environment ile genişlet:
# EXTRA_ORIGINS="https://italky-api.onrender.com,https://xyz.vercel.app"
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

# -------------------------------
# ROUTER REGISTRATION
# -------------------------------
# ⚠️ Dışarıya “OpenAI/Gemini” etiketi göstermiyoruz.
# Tag isimlerini italky Academy olarak tutuyoruz.

app.include_router(chat.router, prefix="/api", tags=["Academy Chat"])
app.include_router(chat_openai.router, prefix="/api", tags=["Academy Chat"])
app.include_router(tts_openai.router, prefix="/api", tags=["Academy Voice"])
app.include_router(lang_pool.router, tags=["Academy Lang Pool"])

# ✅ Real-time course teacher endpoint
app.include_router(teacher_chat.router, prefix="/api", tags=["Academy Teacher"])

if has_voice_openai and voice_openai is not None:
    app.include_router(voice_openai.router, prefix="/api", tags=["Academy Voice"])

if has_legacy_modules:
    app.include_router(translate.router, prefix="/api", tags=["Legacy Translate"])
    app.include_router(tts.router, prefix="/api", tags=["Legacy TTS"])
    app.include_router(ocr.router, prefix="/api", tags=["Legacy OCR"])

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
            "academy_chat": True,
            "academy_teacher": True,
            "academy_voice": True,
            "lang_pool": True,
            "voice_optional": bool(has_voice_openai),
            "legacy_modules": bool(has_legacy_modules),
        },
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
