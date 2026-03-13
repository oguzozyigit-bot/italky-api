from __future__ import annotations

import os
from typing import List

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from app.routers.ui_translate import router as ui_translate_router

# CORE ROUTERS
from app.routers import chat_ai
from app.routers import translate, translate_ai, command_parse
from app.routers import admin, f2f_ws, tts, stt, ocr_translate
from app.routers import interpreter
from app.routers import voice_enroll

# BILLING ROUTERS
from app.routers.billing_google import router as billing_google_router
from app.routers.offline_billing import router as offline_billing_router
from app.routers.usage_billing import router as usage_billing_router
from app.routers.interpreter_billing import router as interpreter_billing_router
from app.routers.meeting_billing import router as meeting_billing_router

# OPTIONAL ROUTERS
try:
    from app.routers import exam_pro
    has_exam_pro = True
except Exception:
    exam_pro = None
    has_exam_pro = False

try:
    from app.routers import level_test
    has_level_test = True
except Exception:
    level_test = None
    has_level_test = False

try:
    from app.routers import ocr
    has_ocr = True
except Exception:
    ocr = None
    has_ocr = False

try:
    from app.routers import offline
    has_offline = True
except Exception:
    offline = None
    has_offline = False

APP_VERSION = os.getenv("APP_VERSION", "italky-api-v3.2").strip()

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
app.include_router(translate.router, prefix="/api")
app.include_router(translate_ai.router, prefix="/api")
app.include_router(command_parse.router, prefix="/api")
app.include_router(tts.router, prefix="/api")
app.include_router(stt.router, prefix="/api")
app.include_router(f2f_ws.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(ocr_translate.router, prefix="/api")
app.include_router(interpreter.router, prefix="/api")
app.include_router(voice_enroll.router, prefix="/api")
app.include_router(chat_ai.router, prefix="/api")
app.include_router(ui_translate_router)

# ===============================
# BILLING ROUTERS
# ===============================
app.include_router(billing_google_router)
app.include_router(offline_billing_router)
app.include_router(usage_billing_router)
app.include_router(interpreter_billing_router)
app.include_router(meeting_billing_router)

# ===============================
# OPTIONAL ROUTERS
# ===============================
if has_offline:
    app.include_router(offline.router, prefix="/api")

if has_exam_pro:
    app.include_router(exam_pro.router, prefix="/api")

if has_level_test:
    app.include_router(level_test.router, prefix="/api")

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


# ===============================
# HARD ACCOUNT DELETE
# ===============================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


def _get_bearer(auth_header: str | None) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split(" ", 1)

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = parts[1].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Empty token")

    return token


@app.post("/api/account/delete")
def delete_account(authorization: str | None = Header(default=None)):

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise HTTPException(status_code=500, detail="Supabase not configured")

    access_token = _get_bearer(authorization)

    try:
        user_resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "apikey": SUPABASE_SERVICE_ROLE,
            },
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if user_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid session")

    user_data = user_resp.json()
    user_id = user_data.get("id")

    if not user_id:
        raise HTTPException(status_code=401, detail="User id missing")

    try:
        del_resp = requests.delete(
            f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                "apikey": SUPABASE_SERVICE_ROLE,
                "Content-Type": "application/json",
            },
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if del_resp.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail="Delete failed")

    return {"ok": True}
