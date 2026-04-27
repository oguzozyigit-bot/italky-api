# FILE: backend session route dosyan / örn: session.py

from __future__ import annotations

import os
import requests
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from supabase import create_client

router = APIRouter(prefix="/api/session", tags=["session"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE ENV missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def check_session(user_id: str, session_key: str | None):
    user_id = str(user_id or "").strip()
    session_key = str(session_key or "").strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id_missing")

    if not session_key:
        raise HTTPException(status_code=401, detail="SESSION_MISSING")

    res = (
        supabase.table("profiles")
        .select("active_session_key")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )

    profile = res.data or {}
    live_key = str(profile.get("active_session_key") or "").strip()

    if not live_key:
        raise HTTPException(status_code=403, detail="SESSION_EXPIRED")

    if live_key != session_key:
        raise HTTPException(status_code=403, detail="SESSION_EXPIRED")

    return True


def _get_bearer(auth_header: str | None) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    return parts[1].strip()


def get_current_user_id(auth_header: str | None) -> str:
    token = _get_bearer(auth_header)
    url = f"{SUPABASE_URL}/auth/v1/user"

    resp = requests.get(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {token}",
        },
        timeout=20,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Geçersiz oturum")

    data = resp.json() or {}
    uid = data.get("id")

    if not uid:
        raise HTTPException(status_code=401, detail="Kullanıcı bulunamadı")

    return uid


def _parse_dt(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _utc_now():
    return datetime.now(timezone.utc)


def _is_future(value) -> bool:
    dt = _parse_dt(value)
    if not dt:
        return False

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt > _utc_now()


def _clean_lower(value) -> str:
    return str(value or "").strip().lower()


def _is_admin_role(role: str) -> bool:
    clean = _clean_lower(role)
    return clean in {"admin", "superadmin"}


def _is_reklamsiz_product(product_id: str) -> bool:
    clean = _clean_lower(product_id)
    return (
        clean == "reklamsiz"
        or "reklamsiz" in clean
        or "no_ads" in clean
        or "ads_free" in clean
    )


@router.get("/access-state")
def get_access_state(authorization: str | None = Header(default=None)):
    user_id = get_current_user_id(authorization)

    res = (
        supabase.table("profiles")
        .select(
            "id,tokens,role,"
            "membership_status,membership_source,membership_product_id,"
            "membership_started_at,membership_ends_at,membership_last_checked_at"
        )
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )

    row = res.data or {}
    if not row:
        raise HTTPException(status_code=404, detail="Access state bulunamadı")

    membership_status = _clean_lower(row.get("membership_status"))
    membership_product_id = _clean_lower(row.get("membership_product_id"))
    membership_source = _clean_lower(row.get("membership_source"))
    role = _clean_lower(row.get("role"))

    membership_started_at = row.get("membership_started_at")
    membership_ends_at = row.get("membership_ends_at")
    membership_last_checked_at = row.get("membership_last_checked_at")

    membership_date_valid = _is_future(membership_ends_at)
    membership_status_active = membership_status == "active"

    is_admin = _is_admin_role(role)
    is_reklamsiz = _is_reklamsiz_product(membership_product_id)

    has_active_membership = bool(
        is_admin
        or (
            membership_status_active
            and membership_date_valid
        )
    )

    subscription_active = bool(
        has_active_membership
        and is_reklamsiz
    )

    ads_disabled = bool(
        is_admin
        or subscription_active
    )

    access_open = bool(has_active_membership)

    tokens = 0
    try:
        tokens = int(row.get("tokens") or 0)
    except Exception:
        tokens = 0

    return {
        "ok": True,
        "user_id": row.get("id"),

        # Genel erişim
        "access_open": access_open,
        "is_logged_in": True,

        # Eski trial alanları, eski front kırılmasın diye kalıyor
        "trial_started_at": None,
        "trial_ends_at": None,
        "trial_used": False,
        "trial_days_left": 0,

        # Üyelik temel alanları
        "membership_status": row.get("membership_status"),
        "membership_source": row.get("membership_source"),
        "membership_product_id": row.get("membership_product_id"),
        "membership_started_at": membership_started_at,
        "membership_ends_at": membership_ends_at,
        "membership_last_checked_at": membership_last_checked_at,

        # Frontend uyum alanları
        "package_active": has_active_membership,
        "package_code": membership_product_id,
        "selected_package_code": membership_product_id,
        "package_started_at": membership_started_at,
        "package_ends_at": membership_ends_at,

        "subscription_active": subscription_active,
        "subscription_product_id": membership_product_id,
        "subscription_started_at": membership_started_at,
        "subscription_ends_at": membership_ends_at,

        "is_member": has_active_membership,
        "has_active_membership": has_active_membership,

        # Reklam kilidi için kritik alanlar
        "no_ads": ads_disabled,
        "ads_disabled": ads_disabled,
        "is_no_ads_member": ads_disabled,

        # Yetki
        "role": role,
        "is_admin": role == "admin",
        "is_superadmin": role == "superadmin",

        # Jeton
        "tokens": tokens,

        # Debug/izleme
        "membership_date_valid": membership_date_valid,
        "membership_status_active": membership_status_active,
        "is_reklamsiz_product": is_reklamsiz,
        "server_time": _utc_now().isoformat(),
    }
