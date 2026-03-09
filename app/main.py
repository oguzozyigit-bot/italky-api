from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from app.routers import chat_ai
from __future__ import annotations
from routers.billing_google import router as billing_google_router

import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

router = APIRouter(tags=["billing-google"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class GoogleBillingConfirmReq(BaseModel):
    user_id: str
    product_id: str
    amount: int
    purchase_token: str


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()
    amount = int(req.amount or 0)

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")
    if amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be > 0")

    # 1) Aynı token daha önce işlendi mi?
    existing = (
        supabase.table("billing_purchases")
        .select("id")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )

    if existing.data:
        return {"ok": True, "already_processed": True}

    # 2) Mevcut token sayısını çek
    prof = (
        supabase.table("profiles")
        .select("tokens")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(status_code=404, detail="profile not found")

    current_tokens = int((prof.data[0] or {}).get("tokens") or 0)
    next_tokens = current_tokens + amount

    # 3) Profili güncelle
    supabase.table("profiles").update(
        {"tokens": next_tokens}
    ).eq("id", user_id).execute()

    # 4) İşlenmiş satın almayı kaydet
    supabase.table("billing_purchases").insert({
        "user_id": user_id,
        "product_id": product_id,
        "amount": amount,
        "purchase_token": purchase_token,
        "provider": "google_play"
    }).execute()

    return {"ok": True, "tokens": next_tokens}

import requests

# ✅ CORE ROUTERS (OpenAI yok)
from app.routers import translate, translate_ai, command_parse
from app.routers import admin, f2f_ws, tts, stt, ocr_translate
from app.routers import interpreter  # ✅ YENİ
from app.routers import voice_enroll

# ✅ OPTIONAL ROUTERS (OpenAI yok)
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
app.include_router(translate.router, prefix="/api")
app.include_router(translate_ai.router, prefix="/api")
app.include_router(command_parse.router, prefix="/api")
app.include_router(tts.router, prefix="/api")
app.include_router(stt.router, prefix="/api")
app.include_router(f2f_ws.router, prefix="/api")
app.include_router(admin.router, prefix="/api")
app.include_router(ocr_translate.router, prefix="/api")
app.include_router(interpreter.router, prefix="/api")  # ✅ YENİ
app.include_router(voice_enroll.router, prefix="/api")
app.include_router(chat_ai.router, prefix="/api")
app.include_router(billing_google_router)

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
    return {"status": "online", "service": "italky-academy-api", "version": APP_VERSION}

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

# ===============================
# HARD ACCOUNT DELETE (PRODUCTION SAFE)
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
        raise HTTPException(status_code=500, detail="Supabase not configured (missing env)")

    access_token = _get_bearer(authorization)

    # 1) Verify session + get user id
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
        raise HTTPException(status_code=502, detail=f"Supabase auth check failed: {e}")

    if user_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    try:
        user_data = user_resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Supabase returned invalid JSON")

    user_id = user_data.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in session")

    # 2) Admin delete user (hard delete)
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
        raise HTTPException(status_code=502, detail=f"Supabase delete failed: {e}")

    if del_resp.status_code not in (200, 204):
        try:
            err = del_resp.json()
        except Exception:
            err = {"error": del_resp.text}
        raise HTTPException(status_code=500, detail=f"Delete failed: {err}")

    return {"ok": True}
