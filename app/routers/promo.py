from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(prefix="/api/promo", tags=["promo"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class PromoRedeemRequest(BaseModel):
    source: Literal["manual", "nfc"] = "manual"
    user_id: Optional[str] = None
    code: Optional[str] = None
    uid: Optional[str] = None


class PromoRedeemResponse(BaseModel):
    ok: bool
    reason: Optional[str] = None
    grant_type: Optional[str] = None
    membership_months: int = 0
    package_code: Optional[str] = None
    membership_started_at: Optional[str] = None
    membership_ends_at: Optional[str] = None
    tokens_loaded: int = 0
    tokens_after: Optional[int] = None
    message: Optional[str] = None


def promo_log(label: str, data: dict | None = None) -> None:
    try:
        print(f"[Promo] {label} {data or {}}")
    except Exception:
        pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def clean(value: object) -> str:
    return str(value or "").strip()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def add_months_safe(dt: datetime, months: int) -> datetime:
    if months <= 0:
        return dt
    year = dt.year + ((dt.month - 1 + months) // 12)
    month = ((dt.month - 1 + months) % 12) + 1
    month_days = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(dt.day, month_days[month - 1])
    return dt.replace(year=year, month=month, day=day)


def require_auth_user_id(auth_header: Optional[str]) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="MISSING_AUTH")
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="MISSING_AUTH")
    try:
        user_res = supabase.auth.get_user(token)
        user = getattr(user_res, "user", None)
        user_id = clean(getattr(user, "id", "")) if user else ""
    except Exception as exc:
        promo_log("auth invalid", {"message": str(exc)})
        raise HTTPException(status_code=401, detail="INVALID_AUTH")
    if not user_id:
        raise HTTPException(status_code=401, detail="INVALID_AUTH")
    return user_id


def resolve_redeem_user_id(payload_user_id: Optional[str], auth_header: Optional[str]) -> str:
    auth_user_id = require_auth_user_id(auth_header)
    requested_user_id = clean(payload_user_id)
    if requested_user_id and requested_user_id != auth_user_id:
        promo_log("user id mismatch", {"auth_user_id": auth_user_id, "payload_user_id": requested_user_id})
        raise HTTPException(status_code=403, detail="user_id_mismatch")
    return auth_user_id


def get_profile(user_id: str) -> dict:
    res = supabase.table("profiles").select(
        "id, email, tokens, selected_package_code, package_started_at, package_ends_at, "
        "promo_used_at, promo_code_used, has_ever_paid, "
        "membership_status, membership_source, membership_product_id, membership_started_at, "
        "membership_ends_at, membership_last_checked_at, trial_started_at, trial_ends_at, "
        "trial_used, plan, app_access_mode"
    ).eq("id", user_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="PROFILE_NOT_FOUND")
    return rows[0]


def get_code_record(source: str, code: Optional[str], uid: Optional[str]) -> Optional[dict]:
    if source == "manual":
        final_code = str(code or "").strip().upper()
        if not final_code:
            raise HTTPException(status_code=400, detail="CODE_REQUIRED")
        try:
            res = supabase.table("promo_codes").select(
                "id, campaign_id, code_value, delivery_type, nfc_uid, is_active, is_used, used_by, used_at, bound_user_id, marketplace"
            ).eq("code_value", final_code).eq("delivery_type", "manual").limit(1).execute()
        except Exception as exc:
            promo_log("campaign promo lookup failed; simple fallback will be tried", {"message": str(exc)})
            return None
        rows = res.data or []
        return rows[0] if rows else None

    if source == "nfc":
        final_uid = str(uid or "").strip()
        if not final_uid:
            raise HTTPException(status_code=400, detail="UID_REQUIRED")
        res = supabase.table("promo_codes").select(
            "id, campaign_id, code_value, delivery_type, nfc_uid, is_active, is_used, used_by, used_at, bound_user_id, marketplace"
        ).eq("delivery_type", "nfc").eq("nfc_uid", final_uid).limit(1).execute()
        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
        return rows[0]

    raise HTTPException(status_code=400, detail="INVALID_SOURCE")


def get_simple_code_record(source: str, code: Optional[str]) -> dict:
    if source != "manual":
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")

    final_code = str(code or "").strip().upper()
    if not final_code:
        raise HTTPException(status_code=400, detail="CODE_REQUIRED")

    try:
        res = supabase.table("promo_codes").select(
            "id, code, duration_days, status, used_by, used_at, expires_at, marketplace"
        ).eq("code", final_code).limit(1).execute()
    except Exception as exc:
        promo_log("simple promo lookup failed", {"message": str(exc)})
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")

    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
    return rows[0]


def get_campaign(campaign_id: str) -> dict:
    res = supabase.table("promo_campaigns").select(
        "id, code, name, description, is_active, starts_at, ends_at, grant_type, "
        "membership_months, token_amount, package_code, stack_mode, per_user_limit, max_total_redemptions"
    ).eq("id", campaign_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="CAMPAIGN_NOT_FOUND")
    return rows[0]


def count_user_redemptions(campaign_id: str, user_id: str) -> int:
    res = supabase.table("promo_redemptions").select("id", count="exact").eq("campaign_id", campaign_id).eq("user_id", user_id).execute()
    return int(res.count or 0)


def count_total_redemptions(campaign_id: str) -> int:
    res = supabase.table("promo_redemptions").select("id", count="exact").eq("campaign_id", campaign_id).execute()
    return int(res.count or 0)


def active_base_date(profile: dict) -> datetime:
    current = now_utc()
    dates = [
        parse_dt(profile.get("package_ends_at")),
        parse_dt(profile.get("membership_ends_at")),
        parse_dt(profile.get("trial_ends_at")),
    ]
    active_dates = [dt for dt in dates if dt and dt > current]
    return max(active_dates) if active_dates else current


def current_membership_start(profile: dict) -> datetime:
    current = now_utc()
    starts = [
        parse_dt(profile.get("membership_started_at")),
        parse_dt(profile.get("package_started_at")),
        parse_dt(profile.get("trial_started_at")),
    ]
    valid = [dt for dt in starts if dt]
    return min(valid) if valid else current


def validate_user_eligibility(profile: dict) -> None:
    # New access model: paid users and active members can still extend time with a valid promo.
    return None


def validate_code_unused(code_rec: dict) -> None:
    if bool(code_rec.get("is_used", False)):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")


def validate_code_and_campaign(code_rec: dict, campaign: dict, user_id: str) -> None:
    if not bool(code_rec.get("is_active", False)):
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")
    validate_code_unused(code_rec)
    bound_user_id = str(code_rec.get("bound_user_id") or "").strip()
    if bound_user_id and bound_user_id != user_id:
        raise HTTPException(status_code=400, detail="PROMO_ACCOUNT_MISMATCH")
    if not bool(campaign.get("is_active", False)):
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    current = now_utc()
    starts_at = parse_dt(campaign.get("starts_at"))
    ends_at = parse_dt(campaign.get("ends_at"))
    if starts_at and current < starts_at:
        raise HTTPException(status_code=400, detail="PROMO_NOT_STARTED")
    if ends_at and current > ends_at:
        raise HTTPException(status_code=400, detail="PROMO_EXPIRED")

    per_user_limit = int(campaign.get("per_user_limit") or 1)
    if count_user_redemptions(campaign["id"], user_id) >= per_user_limit:
        raise HTTPException(status_code=400, detail="PROMO_USER_LIMIT_REACHED")
    max_total = campaign.get("max_total_redemptions")
    if max_total is not None and count_total_redemptions(campaign["id"]) >= int(max_total):
        raise HTTPException(status_code=400, detail="PROMO_GLOBAL_LIMIT_REACHED")


def validate_simple_code(code_rec: dict, user_id: str) -> int:
    status = str(code_rec.get("status") or "").strip().lower()
    used_by = str(code_rec.get("used_by") or "").strip()

    if status == "used" or used_by or code_rec.get("used_at"):
        if used_by and used_by == user_id:
            raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED_BY_USER")
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")

    if status not in {"active", "aktif"}:
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    expires_at = parse_dt(code_rec.get("expires_at"))
    if expires_at and expires_at <= now_utc():
        raise HTTPException(status_code=400, detail="PROMO_EXPIRED")

    try:
        duration_days = int(code_rec.get("duration_days") or 0)
    except Exception:
        duration_days = 0
    if duration_days <= 0:
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    return duration_days


def apply_membership(profile: dict, campaign: dict, promo_code: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    months = int(campaign.get("membership_months") or 0)
    package_code = str(campaign.get("package_code") or "member").strip() or "member"
    if months <= 0:
        return profile.get("selected_package_code"), profile.get("package_started_at"), profile.get("package_ends_at")

    current = now_utc()
    base = active_base_date(profile)
    new_start = current_membership_start(profile)
    new_end = add_months_safe(base, months)
    current_iso = current.isoformat()
    payload = {
        "package_active": True,
        "selected_package_code": package_code,
        "package_started_at": new_start.isoformat(),
        "package_ends_at": new_end.isoformat(),
        "promo_used_at": current_iso,
        "promo_code_used": promo_code,
        "membership_status": "active",
        "membership_source": "promo_code",
        "membership_product_id": package_code,
        "membership_started_at": new_start.isoformat(),
        "membership_ends_at": new_end.isoformat(),
        "membership_last_checked_at": current_iso,
        "plan": "member",
        "app_access_mode": "member",
    }
    upd = supabase.table("profiles").update(payload).eq("id", profile["id"]).execute()
    if getattr(upd, "data", None) is None and getattr(upd, "error", None):
        raise HTTPException(status_code=500, detail="PROFILE_UPDATE_FAILED")
    return package_code, new_start.isoformat(), new_end.isoformat()


def apply_simple_membership(profile: dict, promo_code: str, duration_days: int) -> tuple[str, str, str]:
    current = now_utc()
    base = active_base_date(profile)
    new_start = current_membership_start(profile)
    new_end = base + timedelta(days=duration_days)
    package_code = "promo_code"
    current_iso = current.isoformat()
    payload = {
        "package_active": True,
        "selected_package_code": package_code,
        "package_started_at": new_start.isoformat(),
        "package_ends_at": new_end.isoformat(),
        "promo_used_at": current_iso,
        "promo_code_used": promo_code,
        "membership_status": "active",
        "membership_source": "promo_code",
        "membership_product_id": package_code,
        "membership_started_at": new_start.isoformat(),
        "membership_ends_at": new_end.isoformat(),
        "membership_last_checked_at": current_iso,
        "plan": "member",
        "app_access_mode": "member",
    }
    upd = supabase.table("profiles").update(payload).eq("id", profile["id"]).execute()
    if getattr(upd, "data", None) is None and getattr(upd, "error", None):
        raise HTTPException(status_code=500, detail="PROFILE_UPDATE_FAILED")
    return package_code, new_start.isoformat(), new_end.isoformat()


def apply_tokens(profile: dict, campaign: dict) -> tuple[int, int]:
    token_amount = int(campaign.get("token_amount") or 0)
    current_tokens = int(profile.get("tokens") or 0)
    next_tokens = current_tokens + token_amount
    if token_amount > 0:
        upd = supabase.table("profiles").update({"tokens": next_tokens}).eq("id", profile["id"]).execute()
        if getattr(upd, "data", None) is None and getattr(upd, "error", None):
            raise HTTPException(status_code=500, detail="PROFILE_TOKENS_UPDATE_FAILED")
    return token_amount, next_tokens


def code_used_payload(code_rec: dict, user_id: str) -> dict:
    payload = {
        "is_used": True,
        "used_by": user_id,
        "used_at": iso_now(),
        "activated_at": iso_now(),
        "bound_user_id": user_id,
        "activated_by": user_id,
    }
    if str(code_rec.get("marketplace") or "").strip().lower() == "trendyol":
        payload["invoice_status"] = "handled_by_trendyol"
    return payload


def mark_code_used(code_rec: dict, user_id: str) -> None:
    payload = code_used_payload(code_rec, user_id)
    try:
        upd = supabase.table("promo_codes").update(payload).eq("id", code_rec["id"]).execute()
    except Exception as exc:
        promo_log("activated_by update retry without optional column", {"message": str(exc), "promo_code_id": code_rec.get("id")})
        payload.pop("activated_by", None)
        upd = supabase.table("promo_codes").update(payload).eq("id", code_rec["id"]).execute()

    if getattr(upd, "data", None) is None and getattr(upd, "error", None):
        raise HTTPException(status_code=500, detail="PROMO_MARK_USED_FAILED")


def mark_simple_code_used(code_rec: dict, user_id: str) -> None:
    payload = {
        "status": "used",
        "used_by": user_id,
        "used_at": iso_now(),
        "activated_at": iso_now(),
    }
    if str(code_rec.get("marketplace") or "").strip().lower() == "trendyol":
        payload["invoice_status"] = "handled_by_trendyol"
    upd = supabase.table("promo_codes").update(payload).eq("id", code_rec["id"]).execute()
    if getattr(upd, "data", None) is None and getattr(upd, "error", None):
        raise HTTPException(status_code=500, detail="PROMO_MARK_USED_FAILED")


def get_profile_after(user_id: str) -> dict:
    return get_profile(user_id)


def log_redemption(code_rec: dict, campaign: dict, profile_before: dict, profile_after: dict, user_id: str, source: str, membership_months: int, granted_tokens: int, package_code: Optional[str]) -> None:
    ins = supabase.table("promo_redemptions").insert({
        "promo_code_id": code_rec["id"],
        "campaign_id": campaign["id"],
        "user_id": user_id,
        "source": source,
        "grant_type": campaign["grant_type"],
        "granted_membership_months": membership_months,
        "granted_tokens": granted_tokens,
        "granted_package_code": package_code,
        "before_package_code": profile_before.get("selected_package_code"),
        "after_package_code": profile_after.get("selected_package_code"),
        "before_membership_end": profile_before.get("package_ends_at"),
        "after_membership_end": profile_after.get("package_ends_at"),
        "before_tokens": int(profile_before.get("tokens") or 0),
        "after_tokens": int(profile_after.get("tokens") or 0),
        "created_at": iso_now(),
    }).execute()
    if getattr(ins, "data", None) is None and getattr(ins, "error", None):
        raise HTTPException(status_code=500, detail="PROMO_LOG_FAILED")


def build_success_message(grant_type: str, membership_months: int, tokens_loaded: int) -> str:
    parts: list[str] = []
    if grant_type in ("membership", "bundle") and membership_months > 0:
        parts.append("kullanım süreniz uzatıldı")
    if grant_type in ("tokens", "bundle") and tokens_loaded > 0:
        parts.append(f"{tokens_loaded} jeton yüklendi")
    return " ve ".join(parts) if parts else "Promosyon başarıyla uygulandı."


def redeem_simple_promo(payload: PromoRedeemRequest, redeem_user_id: str, profile_before: dict) -> PromoRedeemResponse:
    simple_code = get_simple_code_record(payload.source, payload.code)
    duration_days = validate_simple_code(simple_code, redeem_user_id)
    package_code, membership_started_at, membership_ends_at = apply_simple_membership(
        profile_before,
        str(simple_code.get("code") or payload.code or "").strip().upper(),
        duration_days,
    )
    mark_simple_code_used(simple_code, redeem_user_id)
    profile_after = get_profile_after(redeem_user_id)

    return PromoRedeemResponse(
        ok=True,
        grant_type="membership",
        membership_months=0,
        package_code=package_code,
        membership_started_at=membership_started_at,
        membership_ends_at=membership_ends_at,
        tokens_loaded=0,
        tokens_after=int(profile_after.get("tokens") or 0),
        message=f"{duration_days} günlük kullanım süresi eklendi",
    )


@router.post("/redeem", response_model=PromoRedeemResponse)
def redeem_promo(payload: PromoRedeemRequest, authorization: Optional[str] = Header(None)):
    redeem_user_id = resolve_redeem_user_id(payload.user_id, authorization)

    profile_before = get_profile(redeem_user_id)

    code_rec = get_code_record(payload.source, payload.code, payload.uid)
    if not code_rec:
        validate_user_eligibility(profile_before)
        return redeem_simple_promo(payload, redeem_user_id, profile_before)

    validate_code_unused(code_rec)
    validate_user_eligibility(profile_before)

    campaign = get_campaign(code_rec["campaign_id"])
    validate_code_and_campaign(code_rec, campaign, redeem_user_id)

    membership_months = int(campaign.get("membership_months") or 0)
    package_code = str(campaign.get("package_code") or "member").strip() or "member"
    grant_type = str(campaign.get("grant_type") or "").strip()

    membership_started_at = None
    membership_ends_at = None
    tokens_loaded = 0

    if grant_type in ("membership", "bundle"):
        package_code, membership_started_at, membership_ends_at = apply_membership(
            profile_before,
            campaign,
            str(code_rec.get("code_value") or "").strip()
        )

    profile_mid = get_profile(redeem_user_id)
    if grant_type in ("tokens", "bundle"):
        tokens_loaded, _ = apply_tokens(profile_mid, campaign)

    mark_code_used(code_rec, redeem_user_id)
    profile_after = get_profile_after(redeem_user_id)

    log_redemption(
        code_rec=code_rec,
        campaign=campaign,
        profile_before=profile_before,
        profile_after=profile_after,
        user_id=redeem_user_id,
        source=payload.source,
        membership_months=membership_months,
        granted_tokens=tokens_loaded,
        package_code=package_code,
    )

    return PromoRedeemResponse(
        ok=True,
        grant_type=grant_type,
        membership_months=membership_months,
        package_code=package_code,
        membership_started_at=membership_started_at,
        membership_ends_at=membership_ends_at,
        tokens_loaded=tokens_loaded,
        tokens_after=int(profile_after.get("tokens") or 0),
        message=build_success_message(grant_type, membership_months, tokens_loaded),
    )