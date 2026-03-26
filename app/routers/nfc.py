from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client, Client

from routers.session import check_session

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


def log_nfc(
    card_uid: Optional[str],
    user_id: Optional[str],
    device_id: Optional[str],
    action: str,
    result: str,
    reason: str = "",
    meta: Optional[dict[str, Any]] = None,
):
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


def get_card_by_uid(uid: str):
    res = supabase.table("nfc_cards").select("*").eq("uid", uid).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None


def get_package_by_code(code: str):
    res = (
        supabase.table("nfc_packages")
        .select("*")
        .eq("code", code)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def get_active_entitlement(user_id: str):
    now_str = iso(utc_now())
    res = (
        supabase.table("nfc_entitlements")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .gte("expires_at", now_str)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def upsert_profile_access(
    user_id: str,
    card_uid: Optional[str],
    package_code: Optional[str],
    expires_at: Optional[str],
    mode: str
):
    supabase.table("profiles").update({
        "app_access_mode": mode,
        "nfc_card_uid": card_uid,
        "nfc_package_code": package_code,
        "nfc_expires_at": expires_at
    }).eq("id", user_id).execute()


@router.post("/activate")
def activate_nfc(body: ActivateNfcBody, x_session_id: str = Header(None)):
    check_session(body.user_id, x_session_id)

    user_id = body.user_id.strip()
    uid = body.uid.strip().upper()
    device_id = body.device_id.strip()

    if not user_id or not uid or not device_id:
        raise HTTPException(status_code=400, detail="user_id, uid, device_id zorunlu")

    card = get_card_by_uid(uid)
    if not card:
        log_nfc(uid, user_id, device_id, "activate", "fail", "card_not_found")
        raise HTTPException(status_code=404, detail="Kart bulunamadı")

    if not card.get("is_active", False):
        log_nfc(uid, user_id, device_id, "activate", "fail", "card_inactive")
        raise HTTPException(status_code=403, detail="Kart pasif")

    if str(card.get("status") or "").lower() in ("blocked", "tampered", "expired"):
        log_nfc(uid, user_id, device_id, "activate", "fail", f"card_status_{card.get('status')}")
        raise HTTPException(status_code=403, detail="Kart kullanılamaz durumda")

    bound_user_id = card.get("bound_user_id")
    current_device_id = str(card.get("current_device_id") or "").strip()

    if bound_user_id and str(bound_user_id) != user_id:
        log_nfc(uid, user_id, device_id, "activate", "fail", "bound_to_another_user")
        raise HTTPException(status_code=403, detail="Kart başka hesaba bağlı")

    if current_device_id and current_device_id != device_id:
        log_nfc(uid, user_id, device_id, "activate", "fail", "device_mismatch")
        raise HTTPException(status_code=403, detail="Kart farklı cihazda kullanıldığı için kullanılamıyor")

    package_code = str(card.get("package_code") or "").strip()
    package = get_package_by_code(package_code)
    if not package:
        log_nfc(uid, user_id, device_id, "activate", "fail", "package_not_found")
        raise HTTPException(status_code=404, detail="Kart paketi bulunamadı")

    first_bound_at = card.get("first_bound_at")
    expires_at = card.get("expires_at")

    now = utc_now()
    if not first_bound_at:
        start_dt = now
        exp_dt = now + timedelta(days=int(package.get("duration_days") or 30))
        expires_at = iso(exp_dt)

        supabase.table("nfc_cards").update({
            "is_bound": True,
            "bound_user_id": user_id,
            "first_bound_at": iso(start_dt),
            "last_seen_at": iso(now),
            "expires_at": expires_at,
            "current_device_id": device_id,
            "status": "active"
        }).eq("uid", uid).execute()

        existing = get_active_entitlement(user_id)
        if not existing:
            supabase.table("nfc_entitlements").insert({
                "user_id": user_id,
                "card_uid": uid,
                "package_code": package_code,
                "started_at": iso(start_dt),
                "expires_at": expires_at,
                "remaining_languages": int(package.get("language_limit") or 0),
                "remaining_jeton": int(package.get("jeton_amount") or 0),
                "can_use_text_to_text": bool(package.get("can_use_text_to_text")),
                "can_use_face_to_face": bool(package.get("can_use_face_to_face")),
                "can_use_side_to_side": bool(package.get("can_use_side_to_side")),
                "can_use_offline": bool(package.get("can_use_offline")),
                "can_use_clone_voice": bool(package.get("can_use_clone_voice")),
                "status": "active"
            }).execute()

            profile_res = (
                supabase.table("profiles")
                .select("jeton_balance")
                .eq("id", user_id)
                .maybe_single()
                .execute()
            )
            profile = profile_res.data or {}
            current_jeton = int(profile.get("jeton_balance") or 0)

            supabase.table("profiles").update({
                "jeton_balance": current_jeton + int(package.get("jeton_amount") or 0)
            }).eq("id", user_id).execute()

    else:
        exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) if expires_at else now
        if exp_dt < now:
            supabase.table("nfc_cards").update({
                "status": "expired",
                "is_active": False
            }).eq("uid", uid).execute()

            log_nfc(uid, user_id, device_id, "activate", "fail", "card_expired")
            raise HTTPException(status_code=403, detail="Kart süresi dolmuş")

        supabase.table("nfc_cards").update({
            "last_seen_at": iso(now),
            "current_device_id": device_id,
            "status": "active"
        }).eq("uid", uid).execute()

    ent = get_active_entitlement(user_id)
    if not ent:
        log_nfc(uid, user_id, device_id, "activate", "fail", "entitlement_missing")
        raise HTTPException(status_code=500, detail="Hak tanımı bulunamadı")

    upsert_profile_access(
        user_id=user_id,
        card_uid=uid,
        package_code=package_code,
        expires_at=ent["expires_at"],
        mode="nfc"
    )

    log_nfc(uid, user_id, device_id, "activate", "ok", "activated", {
        "package_code": package_code
    })

    return {
        "ok": True,
        "mode": "nfc",
        "uid": uid,
        "package_code": package_code,
        "expires_at": ent["expires_at"],
        "remaining_languages": ent["remaining_languages"],
        "remaining_jeton": ent["remaining_jeton"],
        "features": {
            "text_to_text": ent["can_use_text_to_text"],
            "face_to_face": ent["can_use_face_to_face"],
            "side_to_side": ent["can_use_side_to_side"],
            "offline": ent["can_use_offline"],
            "clone_voice": ent["can_use_clone_voice"]
        }
    }


@router.post("/check")
def check_nfc(body: CheckNfcBody, x_session_id: str = Header(None)):
    check_session(body.user_id, x_session_id)

    user_id = body.user_id.strip()
    uid = (body.uid or "").strip().upper()
    device_id = (body.device_id or "").strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id zorunlu")

    ent = get_active_entitlement(user_id)
    if ent:
        log_nfc(uid or ent.get("card_uid"), user_id, device_id, "check", "ok", "entitlement_active")
        return {
            "ok": True,
            "mode": "nfc",
            "package_code": ent["package_code"],
            "expires_at": ent["expires_at"],
            "remaining_languages": ent["remaining_languages"],
            "remaining_jeton": ent["remaining_jeton"],
            "features": {
                "text_to_text": ent["can_use_text_to_text"],
                "face_to_face": ent["can_use_face_to_face"],
                "side_to_side": ent["can_use_side_to_side"],
                "offline": ent["can_use_offline"],
                "clone_voice": ent["can_use_clone_voice"]
            }
        }

    upsert_profile_access(user_id, None, None, None, "basic")

    log_nfc(uid, user_id, device_id, "check", "basic", "no_active_entitlement")
    return {
        "ok": True,
        "mode": "basic",
        "package_code": None,
        "expires_at": None,
        "remaining_languages": 0,
        "remaining_jeton": 0,
        "features": {
            "text_to_text": True,
            "face_to_face": False,
            "side_to_side": False,
            "offline": False,
            "clone_voice": False
        }
    }


@router.post("/consume-jeton")
def consume_jeton(body: ConsumeJetonBody, x_session_id: str = Header(None)):
    check_session(body.user_id, x_session_id)

    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="amount pozitif olmalı")

    ent = get_active_entitlement(body.user_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Aktif hak bulunamadı")

    remaining = int(ent.get("remaining_jeton") or 0)
    if remaining < body.amount:
        raise HTTPException(status_code=403, detail="Yetersiz jeton")

    new_remaining = remaining - body.amount

    supabase.table("nfc_entitlements").update({
        "remaining_jeton": new_remaining
    }).eq("id", ent["id"]).execute()

    log_nfc(ent.get("card_uid"), body.user_id, None, "consume_jeton", "ok", body.reason, {
        "amount": body.amount,
        "remaining": new_remaining
    })

    return {
        "ok": True,
        "remaining_jeton": new_remaining
    }


@router.post("/consume-language")
def consume_language(body: ConsumeLanguageBody, x_session_id: str = Header(None)):
    check_session(body.user_id, x_session_id)

    if body.count <= 0:
        raise HTTPException(status_code=400, detail="count pozitif olmalı")

    ent = get_active_entitlement(body.user_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Aktif hak bulunamadı")

    remaining = int(ent.get("remaining_languages") or 0)
    if remaining < body.count:
        raise HTTPException(status_code=403, detail="Yetersiz dil hakkı")

    new_remaining = remaining - body.count

    supabase.table("nfc_entitlements").update({
        "remaining_languages": new_remaining
    }).eq("id", ent["id"]).execute()

    log_nfc(ent.get("card_uid"), body.user_id, None, "consume_language", "ok", body.reason, {
        "count": body.count,
        "remaining": new_remaining
    })

    return {
        "ok": True,
        "remaining_languages": new_remaining
    }
