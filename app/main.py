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
from app.routers import tts_openai
from app.routers import lang_pool
from app.routers import teacher_chat

# ✅ Translate (Google)
from app.routers import translate
from app.routers import translate_langs

# Optional voice router
try:
    from app.routers import voice_openai
    has_voice_openai = True
except Exception:
    voice_openai = None
    has_voice_openai = False

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v3.0").strip()

app = FastAPI(
    title="italky Academy API",
    version=APP_VERSION,
    description="Backend service for italky Academy (Chat, Voice, Course, Lang Pool)",
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

# ✅ Google Translate endpoints
app.include_router(translate.router, prefix="/api", tags=["Academy Translate"])
app.include_router(translate_langs.router, prefix="/api", tags=["Academy Translate"])

if has_voice_openai and voice_openai is not None:
    app.include_router(voice_openai.router, prefix="/api", tags=["Academy Voice"])

# HEALTH & ROOT
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
            "translate_google": True,
            "translate_languages_google": True,
            "voice_optional": bool(has_voice_openai),
            "legacy_modules": False,  # ✅ kapattık
        },
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
