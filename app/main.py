# italky-api/app/main.py
from __future__ import annotations

import os
import logging
from typing import List, Optional, Dict, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app.routers.translate import router as translate_router
from app.routers.ocr import router as ocr_router
from app.routers.tts import router as tts_router

logger = logging.getLogger("uvicorn.error")

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v1.0").strip()

app = FastAPI(title="Italky API", version=APP_VERSION, redirect_slashes=False)

# ============================================================
# CORS
# ============================================================
ALLOWED_ORIGINS: List[str] = [
    "https://italky.ai",
    "https://www.italky.ai",
    "https://italky-web.vercel.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(translate_router, prefix="/api")
app.include_router(ocr_router, prefix="/api")
app.include_router(tts_router, prefix="/api")

@app.get("/")
def root():
    return {"ok": True, "service": "italky-api", "version": APP_VERSION}

@app.get("/healthz")
def healthz():
    # DB şu an yok; ileride eklenir
    return {"ok": True, "version": APP_VERSION}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
