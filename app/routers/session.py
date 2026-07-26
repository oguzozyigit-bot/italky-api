# FILE: backend session route dosyan / örn: session.py

from __future__ import annotations

import os
import requests
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from supabase import create_client

router = APIRouter(prefix="/api/session", tags=["session"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

TOKEN_DAY_PASS_SOURCE = "token_day_pass"
TOKEN_DAY_PASS_FIRST_COST = 6
TOKEN_DAY_PASS_RENEWAL_COST = 5

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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _is_future(value) -> bool:
    dt = _parse_dt(value)
    return bool(dt and dt > _utc_now())


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


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return _clean_lower(value) in {"1", "true", "yes", "active", "premium"}


def _max_dt(*values):
    dates = [dt for dt in (_parse_dt(v) for v in values) if dt]
    return max(dates) if dates else None


def _remaining_seconds(active_until: datetime | None) -> int:
    if not active_until:
        return 0
    return max(0, int((active_until - _utc_now()).total_seconds()))


def _safe_int(value, fallback: int = 0) -> int:
    try:
        return int(value or 0)
    except Exception:
        return fallback


def _insert_wallet_tx(user_id: str, cost: int, tokens_after: int):
    try:
        supabase.table("wallet_tx").insert({
            "user_id": user_id,
            "type": "debit",
            "amount": cost,
            "delta": -cost,
            "reason": TOKEN_DAY_PASS_SOURCE,
            "note": "24 saatlik kullanım hakkı",
            "tokens_after": tokens_after,
            "created_at": _iso(_utc_now()),
        }).execute()
    except Exception:
        # wallet_tx şeması farklıysa erişim açma işlemini engelleme.
        pass


def _grant_token_day_if_needed(row: dict) -> dict:
    """Günü olmayan kullanıcıya giriş anında jeton karşılığı 24 saat kullanım açar.

    Eski gün/üyelik hakları korunur. Aktif süre varsa jetona dokunulmaz.
    İlk token günlük geçişi 6 jeton, sonraki token günlük geçişleri 5 jetondur.
    Kullanıcı 24 saatten sonra giriş yapmazsa yeni istek gelmediği için jeton düşmez.
    """
    role = _clean_lower(row.get("role"))
    if _is_admin_role(role):
        return row

    active_until = _max_dt(row.get("package_ends_at"), row.get("membership_ends_at"), row.get("trial_ends_at"))
    if active_until and active_until > _utc_now():
        return row

    tokens = _safe_int(row.get("tokens"), 0)
    previous_source = _clean_lower(row.get("membership_source"))
    cost = TOKEN_DAY_PASS_RENEWAL_COST if previous_source == TOKEN_DAY_PASS_SOURCE else TOKEN_DAY_PASS_FIRST_COST

    row["token_day_pass_required_cost"] = cost
    row["token_day_pass_granted"] = False

    if tokens < cost:
        return row

    now = _utc_now()
    end = now + timedelta(days=1)
    tokens_after = max(0, tokens - cost)
    product_id = f"{TOKEN_DAY_PASS_SOURCE}_{cost}_tokens"

    payload = {
        "tokens": tokens_after,
        "package_active": True,
        "package_started_at": _iso(now),
        "package_ends_at": _iso(end),
        "membership_status": "active",
        "membership_source": TOKEN_DAY_PASS_SOURCE,
        "membership_product_id": product_id,
        "membership_started_at": _iso(now),
        "membership_ends_at": _iso(end),
        "membership_last_checked_at": _iso(now),
        "plan": "token",
        "app_access_mode": "token",
    }

    supabase.table("profiles").update(payload).eq("id", row["id"]).execute()
    _insert_wallet_tx(str(row["id"]), cost, tokens_after)

    row.update(payload)
    row["token_day_pass_required_cost"] = cost
    row["token_day_pass_cost_charged"] = cost
    row["token_day_pass_granted"] = True
    return row


@router.get("/access-state")
def get_access_state(authorization: str | None = Header(default=None)):
    user_id = get_current_user_id(authorization)

    res = (
        supabase.table("profiles")
        .select(
            "id,tokens,role,plan,app_access_mode,"
            "trial_started_at,trial_ends_at,trial_used,"
            "package_active,package_started_at,package_ends_at,selected_package_code,"
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

    row = _grant_token_day_if_needed(row)

    membership_status = _clean_lower(row.get("membership_status"))
    membership_product_id = _clean_lower(row.get("membership_product_id"))
    membership_source = _clean_lower(row.get("membership_source"))
    role = _clean_lower(row.get("role"))

    package_active = _is_truthy(row.get("package_active"))
    package_started_at = row.get("package_started_at")
    package_ends_at = row.get("package_ends_at")
    selected_package_code = row.get("selected_package_code")

    trial_started_at = row.get("trial_started_at")
    trial_ends_at = row.get("trial_ends_at")
    trial_used = _is_truthy(row.get("trial_used"))

    membership_started_at = row.get("membership_started_at") or package_started_at or trial_started_at
    membership_ends_at = row.get("membership_ends_at") or package_ends_at or trial_ends_at
    membership_last_checked_at = row.get("membership_last_checked_at")

    active_until_dt = _max_dt(package_ends_at, membership_ends_at, trial_ends_at)
    remaining_seconds = _remaining_seconds(active_until_dt)

    membership_date_valid = _is_future(membership_ends_at)
    package_date_valid = _is_future(package_ends_at)
    trial_date_valid = _is_future(trial_ends_at)
    membership_status_active = membership_status == "active"
    package_access_active = bool(package_active and package_date_valid)

    is_admin = _is_admin_role(role)
    is_reklamsiz = _is_reklamsiz_product(membership_product_id)
    is_corporate_promo = membership_source == "corporate_promo" or bool(selected_package_code)
    is_ios_iap = membership_source == "ios_iap"
    is_google_inapp = membership_source == "google_play_inapp"
    is_token_day_pass = membership_source == TOKEN_DAY_PASS_SOURCE and membership_date_valid
    is_trial = membership_source == "free_trial_1day" or trial_date_valid

    has_active_membership = bool(
        is_admin
        or package_access_active
        or trial_date_valid
        or is_token_day_pass
        or (active_until_dt and active_until_dt > _utc_now())
        or (
            membership_status_active
            and membership_date_valid
        )
        or (is_ios_iap and membership_status_active)
    )

    subscription_active = bool(
        has_active_membership
        and (is_reklamsiz or is_corporate_promo or is_ios_iap or is_google_inapp)
    )

    ads_disabled = bool(
        is_admin
        or subscription_active
        or has_active_membership
    )

    access_open = bool(has_active_membership)

    tokens = _safe_int(row.get("tokens"), 0)
    package_code = selected_package_code or membership_product_id

    return {
        "ok": True,
        "user_id": row.get("id"),
        "access_open": access_open,
        "is_logged_in": True,
        "trial_started_at": trial_started_at,
        "trial_ends_at": trial_ends_at,
        "trial_used": trial_used,
        "trial_days_left": max(0, remaining_seconds // 86400) if trial_date_valid else 0,
        "membership_status": row.get("membership_status"),
        "membership_source": row.get("membership_source"),
        "membership_product_id": row.get("membership_product_id"),
        "membership_started_at": membership_started_at,
        "membership_ends_at": membership_ends_at,
        "membership_last_checked_at": membership_last_checked_at,
        "package_active": has_active_membership,
        "package_code": package_code,
        "selected_package_code": package_code,
        "package_started_at": package_started_at or membership_started_at,
        "package_ends_at": package_ends_at or membership_ends_at,
        "active_until": _iso(active_until_dt) if active_until_dt else None,
        "remaining_seconds": remaining_seconds,
        "subscription_active": subscription_active,
        "subscription_product_id": membership_product_id,
        "subscription_started_at": membership_started_at,
        "subscription_ends_at": membership_ends_at,
        "is_member": has_active_membership,
        "has_active_membership": has_active_membership,
        "no_ads": ads_disabled,
        "ads_disabled": ads_disabled,
        "is_no_ads_member": ads_disabled,
        "role": role,
        "is_admin": role == "admin",
        "is_superadmin": role == "superadmin",
        "tokens": tokens,
        "token_day_pass_required_cost": row.get("token_day_pass_required_cost"),
        "token_day_pass_cost_charged": row.get("token_day_pass_cost_charged", 0),
        "token_day_pass_granted": bool(row.get("token_day_pass_granted")),
        "plan": row.get("plan"),
        "app_access_mode": row.get("app_access_mode"),
        "membership_date_valid": membership_date_valid,
        "membership_status_active": membership_status_active,
        "package_date_valid": package_date_valid,
        "package_access_active": package_access_active,
        "trial_date_valid": trial_date_valid,
        "is_trial": is_trial,
        "is_token_day_pass": is_token_day_pass,
        "is_reklamsiz_product": is_reklamsiz,
        "server_time": _utc_now().isoformat(),
    }
