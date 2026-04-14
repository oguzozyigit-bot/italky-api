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


@router.get("/access-state")
def get_access_state(authorization: str | None = Header(default=None)):
    user_id = get_current_user_id(authorization)

    res = (
        supabase.table("profiles")
        .select(
            "id,tokens,"
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

    membership_status = str(row.get("membership_status") or "").strip().lower()
    membership_ends_at = row.get("membership_ends_at")
    end_dt = _parse_dt(membership_ends_at)
    now_dt = datetime.now(timezone.utc)

    access_open = membership_status == "active" and end_dt is not None and end_dt > now_dt

    return {
        "ok": True,
        "user_id": row.get("id"),
        "access_open": access_open,
        "trial_started_at": None,
        "trial_ends_at": None,
        "trial_used": False,
        "trial_days_left": 0,
        "membership_status": row.get("membership_status"),
        "membership_source": row.get("membership_source"),
        "membership_product_id": row.get("membership_product_id"),
        "membership_started_at": row.get("membership_started_at"),
        "membership_ends_at": row.get("membership_ends_at"),
        "membership_last_checked_at": row.get("membership_last_checked_at"),
        "tokens": int(row.get("tokens") or 0),
    }
