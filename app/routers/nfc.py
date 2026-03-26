from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client, Client

from app.routers.session import check_session

router = APIRouter(prefix="/api/nfc", tags=["nfc"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ===============================
# MODELS
# ===============================
class ActivateNfcBody(BaseModel):
    user_id: str
    uid: str
    device_id: str
    platform: str = "android"


class CheckNfcBody(BaseModel):
    user_id: str
    uid: Optional[str] = None
    device_id: Optional[str] = None


class ConsumeJetonBody(BaseModel):
    user_id: str
    amount: int
    reason: str = "usage"


class ConsumeLanguageBody(BaseModel):
    user_id: str
    count: int = 1
    reason: str = "language_install"


# ===============================
# HELPERS
# ===============================
def log_nfc(card_uid, user_id, device_id, action, result, reason="", meta=None):
    try:
        supabase.table("nfc_logs").insert({
            "card_uid": card_uid,
            "user_id": user_id,
            "device_id": device_id,
            "action": action,
            "result": result,
            "reason": reason,
            "meta": meta or {}
        }).execute()
    except Exception:
        pass


def get_card_by_uid(uid):
    res = supabase.table("nfc_cards").select("*").eq("uid", uid).limit(1).execute()
    return (res.data or [None])[0]


def get_package_by_code(code):
    res = supabase.table("nfc_packages") \
        .select("*") \
        .eq("code", code) \
        .eq("is_active", True) \
        .limit(1) \
        .execute()
    return (res.data or [None])[0]


def get_active_entitlement(user_id):
    now_str = iso(utc_now())
    res = supabase.table("nfc_entitlements") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("status", "active") \
        .gte("expires_at", now_str) \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()
    return (res.data or [None])[0]


def upsert_profile_access(user_id, card_uid, package_code, expires_at, mode):
    supabase.table("profiles").update({
        "app_access_mode": mode,
        "nfc_card_uid": card_uid,
        "nfc_package_code": package_code,
        "nfc_expires_at": expires_at
    }).eq("id", user_id).execute()


# ===============================
# ACTIVATE
# ===============================
@router.post("/activate")
def activate_nfc(body: ActivateNfcBody, x_session_key: str = Header(None)):
    check_session(body.user_id, x_session_key)

    user_id = body.user_id.strip()
    uid = body.uid.strip().upper()
    device_id = body.device_id.strip()

    if not user_id or not uid or not device_id:
        raise HTTPException(status_code=400, detail="Eksik parametre")

    card = get_card_by_uid(uid)
    if not card:
        raise HTTPException(status_code=404, detail="Kart bulunamadı")

    package = get_package_by_code(card.get("package_code"))
    if not package:
        raise HTTPException(status_code=404, detail="Paket bulunamadı")

    now = utc_now()

    if not card.get("first_bound_at"):
        expires_at = iso(now + timedelta(days=package.get("duration_days", 30)))

        supabase.table("nfc_cards").update({
            "is_bound": True,
            "bound_user_id": user_id,
            "first_bound_at": iso(now),
            "expires_at": expires_at,
            "current_device_id": device_id,
            "status": "active"
        }).eq("uid", uid).execute()

        supabase.table("nfc_entitlements").insert({
            "user_id": user_id,
            "card_uid": uid,
            "package_code": package.get("code"),
            "started_at": iso(now),
            "expires_at": expires_at,
            "remaining_languages": package.get("language_limit", 0),
            "remaining_jeton": package.get("jeton_amount", 0),
            "can_use_text_to_text": True,
            "can_use_face_to_face": True,
            "can_use_side_to_side": True,
            "can_use_offline": False,
            "can_use_clone_voice": False,
            "status": "active"
        }).execute()

    ent = get_active_entitlement(user_id)

    if not ent:
        raise HTTPException(status_code=500, detail="Entitlement yok")

    upsert_profile_access(
        user_id,
        uid,
        ent["package_code"],
        ent["expires_at"],
        "nfc"
    )

    return {"ok": True}


# ===============================
# CHECK
# ===============================
@router.post("/check")
def check_nfc(body: CheckNfcBody, x_session_key: str = Header(None)):
    check_session(body.user_id, x_session_key)

    ent = get_active_entitlement(body.user_id)

    if ent:
        return {"ok": True, "mode": "nfc"}

    return {"ok": True, "mode": "basic"}


# ===============================
# JETON
# ===============================
@router.post("/consume-jeton")
def consume_jeton(body: ConsumeJetonBody, x_session_key: str = Header(None)):
    check_session(body.user_id, x_session_key)

    ent = get_active_entitlement(body.user_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Entitlement yok")

    remaining = ent.get("remaining_jeton", 0)

    if remaining < body.amount:
        raise HTTPException(status_code=403, detail="Yetersiz jeton")

    new_remaining = remaining - body.amount

    supabase.table("nfc_entitlements").update({
        "remaining_jeton": new_remaining
    }).eq("id", ent["id"]).execute()

    return {"ok": True}


# ===============================
# LANGUAGE
# ===============================
@router.post("/consume-language")
def consume_language(body: ConsumeLanguageBody, x_session_key: str = Header(None)):
    check_session(body.user_id, x_session_key)

    ent = get_active_entitlement(body.user_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Entitlement yok")

    remaining = ent.get("remaining_languages", 0)

    if remaining < body.count:
        raise HTTPException(status_code=403, detail="Dil hakkı yok")

    new_remaining = remaining - body.count

    supabase.table("nfc_entitlements").update({
        "remaining_languages": new_remaining
    }).eq("id", ent["id"]).execute()

    return {"ok": True}
