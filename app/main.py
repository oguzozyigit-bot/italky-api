from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.routers.auth import router as auth_router
from app.routers.nfc import router as nfc_router
from app.routers.session import router as session_router
from app.routers.practice_ai import router as practice_ai_router
from app.routers.license import router as license_router
from app.routers.delete_account import router as delete_account_router
from app.routers.nfc_tokens import router as nfc_tokens_router
from app.routers.italkyai_chat import router as italkyai_chat_router

# ROUTER IMPORTS
from app.routers.ui_translate import router as ui_translate_router
from app.routers.onetoall_ws import router as onetoall_ws_router

# CORE ROUTERS
from app.routers.chat_ai import router as chat_ai_router
from app.routers.translate_ai import router as translate_ai_router
from app.routers.command_parse import router as command_parse_router
from app.routers.admin import router as admin_router
from app.routers.f2f_ws import router as f2f_ws_router
from app.routers.tts import router as tts_router
from app.routers.interpreter import router as interpreter_router
from app.routers.voice_enroll import router as voice_enroll_router

# BILLING ROUTERS
from app.routers.billing_google import router as billing_google_router
from app.routers.offline_billing import router as offline_billing_router
from app.routers.usage_billing import router as usage_billing_router
from app.routers.interpreter_billing import router as interpreter_billing_router
from app.routers.meeting_billing import router as meeting_billing_router

# OPTIONAL ROUTERS
try:
    from app.routers.exam_pro import router as exam_pro_router
    has_exam_pro = True
except Exception:
    exam_pro_router = None
    has_exam_pro = False

try:
    from app.routers.level_test import router as level_test_router
    has_level_test = True
except Exception:
    level_test_router = None
    has_level_test = False

try:
    from app.routers.ocr import router as ocr_router
    has_ocr = True
except Exception:
    ocr_router = None
    has_ocr = False

try:
    from app.routers.offline import router as offline_router
    has_offline = True
except Exception:
    offline_router = None
    has_offline = False

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v3.3").strip()

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
# ROUTERS
# ===============================
app.include_router(translate_ai_router, prefix="/api")
app.include_router(command_parse_router, prefix="/api")
app.include_router(tts_router, prefix="/api")
app.include_router(f2f_ws_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(interpreter_router, prefix="/api")
app.include_router(voice_enroll_router, prefix="/api")
app.include_router(chat_ai_router, prefix="/api")
app.include_router(onetoall_ws_router, prefix="/api")
app.include_router(ui_translate_router, prefix="/api")
app.include_router(nfc_tokens_router, prefix="/api")
app.include_router(italkyai_chat_router)

app.include_router(nfc_router)
app.include_router(session_router)
app.include_router(practice_ai_router)
app.include_router(license_router)
app.include_router(delete_account_router)

# AUTH
app.include_router(auth_router)

# BILLING
app.include_router(billing_google_router)
app.include_router(offline_billing_router)
app.include_router(usage_billing_router)
app.include_router(interpreter_billing_router)
app.include_router(meeting_billing_router)

# OPTIONAL
if has_offline and offline_router:
    app.include_router(offline_router, prefix="/api")

if has_level_test and level_test_router:
    app.include_router(level_test_router, prefix="/api")

if has_exam_pro and exam_pro_router:
    app.include_router(exam_pro_router, prefix="/api")

if has_ocr and ocr_router:
    app.include_router(ocr_router, prefix="/api")

# ===============================
# HEALTH
# ===============================
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

@app.get("/api/healthz")
def api_healthz():
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)
