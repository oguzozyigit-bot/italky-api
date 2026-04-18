from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(prefix="/api/promo", tags=["promo"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# =========================================================
# MODELS
# =========================================================

class PromoRedeemRequest(BaseModel):
    source: Literal["manual", "nfc"] = "manual"
    user_id: str = Field(..., min_length=1)
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


# =========================================================
# HELPERS
# =========================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def add_months_safe(dt: datetime, months: int) -> datetime:
    if months <= 0:
        return dt

    year = dt.year + ((dt.month - 1 + months) // 12)
    month = ((dt.month - 1 + months) % 12) + 1

    month_days = [
        31,
        29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31
    ]
    day = min(dt.day, month_days[month - 1])

    return dt.replace(year=year, month=month, day=day)


def require_auth(auth_header: Optional[str]) -> None:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="MISSING_AUTH")


def get_profile(user_id: str) -> dict:
    res = supabase.table("profiles").select(
        "id, email, tokens, selected_package_code, package_started_at, package_ends_at, "
        "promo_used_at, promo_code_used, has_ever_paid"
    ).eq("id", user_id).limit(1).execute()

    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="PROFILE_NOT_FOUND")
    return rows[0]


def get_code_record(source: str, code: Optional[str], uid: Optional[str]) -> dict:
    if source == "manual":
        final_code = str(code or "").strip().upper()
        if not final_code:
            raise HTTPException(status_code=400, detail="CODE_REQUIRED")

        res = supabase.table("promo_codes").select(
            "id, campaign_id, code_value, delivery_type, nfc_uid, is_active, is_used, used_by, used_at, bound_user_id"
        ).eq("code_value", final_code).eq("delivery_type", "manual").limit(1).execute()

        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
        return rows[0]

    if source == "nfc":
        final_uid = str(uid or "").strip()
        if not final_uid:
            raise HTTPException(status_code=400, detail="UID_REQUIRED")

        res = supabase.table("promo_codes").select(
            "id, campaign_id, code_value, delivery_type, nfc_uid, is_active, is_used, used_by, used_at, bound_user_id"
        ).eq("delivery_type", "nfc").eq("nfc_uid", final_uid).limit(1).execute()

        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
        return rows[0]

    raise HTTPException(status_code=400, detail="INVALID_SOURCE")


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
    res = supabase.table("promo_redemptions").select(
        "id", count="exact"
    ).eq("campaign_id", campaign_id).eq("user_id", user_id).execute()
    return int(res.count or 0)


def count_total_redemptions(campaign_id: str) -> int:
    res = supabase.table("promo_redemptions").select(
        "id", count="exact"
    ).eq("campaign_id", campaign_id).execute()
    return int(res.count or 0)


def is_active_membership(profile: dict) -> bool:
    end_dt = parse_dt(profile.get("package_ends_at"))
    if not end_dt:
        return False
    return end_dt > now_utc()


def validate_user_eligibility(profile: dict) -> None:
    if bool(profile.get("has_ever_paid")):
        raise HTTPException(status_code=400, detail="PROMO_NOT_ELIGIBLE_FOR_PAID_MEMBER")

    if str(profile.get("promo_used_at") or "").strip():
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED_BY_USER")

    if is_active_membership(profile):
        raise HTTPException(status_code=400, detail="PROMO_NOT_ALLOWED_WHILE_MEMBERSHIP_ACTIVE")


def validate_code_and_campaign(code_rec: dict, campaign: dict, user_id: str) -> None:
    if not bool(code_rec.get("is_active", False)):
        raise HTTPException(status_code=400, detail="PROMO_INACTIVE")

    if bool(code_rec.get("is_used", False)):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")

    bound_user_id = str(code_rec.get("bound_user_id") or "").strip()
    if bound_user_id and bound_user_id != user_id:
        raise HTTPException(status_code=400, detail="PROMO_ACCOUNT_MISMATCH")

    if not bool(campaign.get("is_active", False)):
        raise HTTPException(status_code=400, detail="PROMO_INACTIVE")

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


def apply_membership(profile: dict, campaign: dict, promo_code: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    months = int(campaign.get("membership_months") or 0)
    package_code = str(campaign.get("package_code") or "member").strip() or "member"

    if months <= 0:
        return profile.get("selected_package_code"), profile.get("package_started_at"), profile.get("package_ends_at")

    current = now_utc()
    new_start = current
    new_end = add_months_safe(new_start, months)

    payload = {
        "selected_package_code": package_code,
        "package_started_at": new_start.isoformat(),
        "package_ends_at": new_end.isoformat(),
        "promo_used_at": iso_now(),
        "promo_code_used": promo_code,
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
        upd = supabase.table("profiles").update({
            "tokens": next_tokens
        }).eq("id", profile["id"]).execute()

        if getattr(upd, "data", None) is None and getattr(upd, "error", None):
            raise HTTPException(status_code=500, detail="PROFILE_TOKENS_UPDATE_FAILED")

    return token_amount, next_tokens


def mark_code_used(code_rec: dict, user_id: str) -> None:
    upd = supabase.table("promo_codes").update({
        "is_used": True,
        "used_by": user_id,
        "used_at": iso_now(),
        "bound_user_id": user_id
    }).eq("id", code_rec["id"]).execute()

    if getattr(upd, "data", None) is None and getattr(upd, "error", None):
        raise HTTPException(status_code=500, detail="PROMO_MARK_USED_FAILED")


def get_profile_after(user_id: str) -> dict:
    return get_profile(user_id)


def log_redemption(
    code_rec: dict,
    campaign: dict,
    profile_before: dict,
    profile_after: dict,
    user_id: str,
    source: str,
    membership_months: int,
    granted_tokens: int,
    package_code: Optional[str],
) -> None:
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


def build_success_message(
    grant_type: str,
    membership_months: int,
    tokens_loaded: int,
) -> str:
    parts: list[str] = []

    if grant_type in ("membership", "bundle") and membership_months > 0:
        parts.append(f"{membership_months} aylık üyelik tanımlandı")

    if grant_type in ("tokens", "bundle") and tokens_loaded > 0:
        parts.append(f"{tokens_loaded} jeton yüklendi")

    return " ve ".join(parts) if parts else "Promosyon başarıyla uygulandı."


# =========================================================
# ROUTES
# =========================================================

@router.post("/redeem", response_model=PromoRedeemResponse)
def redeem_promo(payload: PromoRedeemRequest, authorization: Optional[str] = Header(None)):
    require_auth(authorization)

    profile_before = get_profile(payload.user_id)
    validate_user_eligibility(profile_before)

    code_rec = get_code_record(payload.source, payload.code, payload.uid)
    campaign = get_campaign(code_rec["campaign_id"])
    validate_code_and_campaign(code_rec, campaign, payload.user_id)

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

    profile_mid = get_profile(payload.user_id)

    if grant_type in ("tokens", "bundle"):
        tokens_loaded, _ = apply_tokens(profile_mid, campaign)

    mark_code_used(code_rec, payload.user_id)

    profile_after = get_profile_after(payload.user_id)

    log_redemption(
        code_rec=code_rec,
        campaign=campaign,
        profile_before=profile_before,
        profile_after=profile_after,
        user_id=payload.user_id,
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
