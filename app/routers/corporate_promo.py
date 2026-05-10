from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(prefix="/api/promo/corporate", tags=["Corporate Promo Activation"])

CORPORATE_PROMO_TABLE = "corporate_promo_codes"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SMS_PROVIDER_ENABLED = os.getenv("SMS_PROVIDER_ENABLED", "").strip().lower() in {"1", "true", "yes"}

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class PromoOtpSendIn(BaseModel):
    code: str = Field(..., min_length=8, max_length=8)
    phone: str = Field(..., min_length=8, max_length=24)
    sms_consent: bool = False
    email_consent: bool = False


class PromoOtpVerifyIn(BaseModel):
    code: str = Field(..., min_length=8, max_length=8)
    phone: str = Field(..., min_length=8, max_length=24)
    otp: str = Field(..., min_length=4, max_length=4)
    sms_consent: bool = False
    email_consent: bool = False


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def normalize_code(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())[:8]


def normalize_phone(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "").strip() if ch.isdigit() or ch == "+")
    if len(cleaned) < 8:
        raise HTTPException(status_code=400, detail="PHONE_REQUIRED")
    return cleaned[:24]


def require_user(authorization: Optional[str]) -> Dict[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
    token = authorization.split(" ", 1)[1].strip()
    try:
        res = supabase.auth.get_user(token)
        user = getattr(res, "user", None) or (res.get("user") if isinstance(res, dict) else None)
        user_id = (getattr(user, "id", None) if user else None) or (user.get("id") if isinstance(user, dict) and user else None)
        email = (getattr(user, "email", None) if user else None) or (user.get("email") if isinstance(user, dict) and user else None)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"AUTH_INVALID: {e}")
    if not user_id:
        raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
    return {"id": str(user_id), "email": str(email or "")}


def get_active_code(code: str) -> dict:
    res = supabase.table(CORPORATE_PROMO_TABLE).select("*").eq("code", code).limit(1).execute()
    rows = safe_data(res) or []
    if not rows:
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
    row = rows[0]
    if row.get("status") != "active" or row.get("activated_at"):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")
    valid_until = parse_dt(row.get("valid_until"))
    if valid_until and valid_until < now_utc():
        try:
            supabase.table(CORPORATE_PROMO_TABLE).update({"status": "expired"}).eq("id", row["id"]).execute()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="PROMO_EXPIRED")
    return row


def otp_code() -> str:
    return f"{random.randint(0, 9999):04d}"


@router.post("/otp/send")
def send_otp(payload: PromoOtpSendIn, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    code = normalize_code(payload.code)
    phone = normalize_phone(payload.phone)
    if not payload.sms_consent or not payload.email_consent:
        raise HTTPException(status_code=400, detail="CONSENT_REQUIRED")

    get_active_code(code)
    otp = otp_code()
    expires_at = now_utc() + timedelta(minutes=10)

    try:
        supabase.table("promo_phone_otps").insert({
            "code": code,
            "phone": phone,
            "otp_code": otp,
            "expires_at": iso(expires_at),
            "verified_at": None,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OTP_STORE_FAILED: {e}")

    # TODO: gerçek SMS provider bağlanınca burada SMS gönderilecek.
    # Geçici taslak: SMS_PROVIDER_ENABLED false ise debug_otp döner.
    return {
        "ok": True,
        "sms_sent": SMS_PROVIDER_ENABLED,
        "debug_otp": None if SMS_PROVIDER_ENABLED else otp,
        "message": "OTP hazır" if not SMS_PROVIDER_ENABLED else "SMS gönderildi",
    }


@router.post("/otp/verify")
def verify_otp(payload: PromoOtpVerifyIn, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    code = normalize_code(payload.code)
    phone = normalize_phone(payload.phone)
    otp = str(payload.otp or "").strip()
    if not payload.sms_consent or not payload.email_consent:
        raise HTTPException(status_code=400, detail="CONSENT_REQUIRED")

    code_row = get_active_code(code)

    otp_res = (
        supabase.table("promo_phone_otps")
        .select("*")
        .eq("code", code)
        .eq("phone", phone)
        .eq("otp_code", otp)
        .is_("verified_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    otp_rows = safe_data(otp_res) or []
    if not otp_rows:
        raise HTTPException(status_code=400, detail="OTP_INVALID")

    otp_row = otp_rows[0]
    expires_at = parse_dt(otp_row.get("expires_at"))
    if expires_at and expires_at < now_utc():
        raise HTTPException(status_code=400, detail="OTP_EXPIRED")

    start = now_utc()
    duration_days = int(code_row.get("duration_days") or 0)
    if duration_days <= 0:
        raise HTTPException(status_code=400, detail="PROMO_DURATION_INVALID")
    end = start + timedelta(days=duration_days)
    consent_at = start

    try:
        supabase.table("promo_phone_otps").update({"verified_at": iso(start)}).eq("id", otp_row["id"]).execute()

        updated = (
            supabase.table(CORPORATE_PROMO_TABLE)
            .update({
                "status": "activated",
                "activated_by": user["id"],
                "activated_email": user.get("email"),
                "activated_phone": phone,
                "phone_verified": True,
                "sms_consent": True,
                "email_consent": True,
                "consent_at": iso(consent_at),
                "activated_at": iso(start),
                "membership_starts_at": iso(start),
                "membership_ends_at": iso(end),
            })
            .eq("id", code_row["id"])
            .eq("status", "active")
            .execute()
        )
        if not safe_data(updated):
            raise HTTPException(status_code=409, detail="PROMO_ALREADY_USED")

        profile_patch = {
            "package_active": True,
            "package_started_at": iso(start),
            "package_ends_at": iso(end),
            "selected_package_code": code,
            "app_access_mode": "premium",
            "promo_used_at": iso(start),
            "promo_code_used": code,
        }
        supabase.table("profiles").update(profile_patch).eq("id", user["id"]).execute()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PROMO_ACTIVATION_FAILED: {e}")

    return {
        "ok": True,
        "membership_starts_at": iso(start),
        "membership_ends_at": iso(end),
        "duration_days": duration_days,
    }
