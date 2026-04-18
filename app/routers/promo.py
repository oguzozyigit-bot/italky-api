from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional, Literal

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from supabase import create_client, Client

router = APIRouter(prefix="/api/promo", tags=["promo"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# =========================
# MODELS
# =========================

class PromoRedeemRequest(BaseModel):
    source: Literal["manual", "nfc"]
    user_id: str = Field(..., min_length=1)
    code: Optional[str] = None
    uid: Optional[str] = None


class PromoRedeemResponse(BaseModel):
    ok: bool
    grant_type: Optional[str] = None
    membership_months: int = 0
    package_code: Optional[str] = None
    membership_started_at: Optional[str] = None
    membership_ends_at: Optional[str] = None
    tokens_loaded: int = 0
    tokens_after: Optional[int] = None
    message: Optional[str] = None
    reason: Optional[str] = None


# =========================
# HELPERS
# =========================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def add_months_safe(dt: datetime, months: int) -> datetime:
    # Basit ve güvenli yaklaşım:
    year = dt.year + ((dt.month - 1 + months) // 12)
    month = ((dt.month - 1 + months) % 12) + 1

    # gün taşmalarını engelle
    day = min(dt.day, [31,
                       29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])

    return dt.replace(year=year, month=month, day=day)


def get_profile(user_id: str) -> dict:
    res = supabase.table("profiles").select(
        "id,tokens,selected_package_code,package_started_at,package_ends_at"
    ).eq("id", user_id).limit(1).execute()

    data = res.data or []
    if not data:
        raise HTTPException(status_code=404, detail="PROFILE_NOT_FOUND")
    return data[0]


def find_code_record(source: str, code: Optional[str], uid: Optional[str]) -> dict:
    if source == "manual":
        if not code:
            raise HTTPException(status_code=400, detail="CODE_REQUIRED")

        res = supabase.table("promo_codes").select(
            "id,campaign_id,code_value,delivery_type,nfc_uid,is_active,is_used,used_by,used_at,bound_user_id"
        ).eq("code_value", code).eq("delivery_type", "manual").limit(1).execute()

    elif source == "nfc":
        if not uid:
            raise HTTPException(status_code=400, detail="UID_REQUIRED")

        # önce nfc_uid ile dene
        res = supabase.table("promo_codes").select(
            "id,campaign_id,code_value,delivery_type,nfc_uid,is_active,is_used,used_by,used_at,bound_user_id"
        ).eq("delivery_type", "nfc").eq("nfc_uid", uid).limit(1).execute()

        data = res.data or []
        if data:
            return data[0]

        # fallback: uid aslında code_value gibi de gelebilir
        res = supabase.table("promo_codes").select(
            "id,campaign_id,code_value,delivery_type,nfc_uid,is_active,is_used,used_by,used_at,bound_user_id"
        ).eq("delivery_type", "nfc").eq("code_value", uid).limit(1).execute()
    else:
        raise HTTPException(status_code=400, detail="INVALID_SOURCE")

    data = res.data or []
    if not data:
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
    return data[0]


def get_campaign(campaign_id: str) -> dict:
    res = supabase.table("promo_campaigns").select(
        "id,code,name,description,is_active,starts_at,ends_at,grant_type,membership_months,token_amount,package_code,stack_mode,per_user_limit,max_total_redemptions"
    ).eq("id", campaign_id).limit(1).execute()

    data = res.data or []
    if not data:
        raise HTTPException(status_code=404, detail="CAMPAIGN_NOT_FOUND")
    return data[0]


def count_user_redemptions(campaign_id: str, user_id: str) -> int:
    res = supabase.table("promo_redemptions").select(
        "id", count="exact"
    ).eq("campaign_id", campaign_id).eq("user_id", user_id).execute()

    return res.count or 0


def count_total_redemptions(campaign_id: str) -> int:
    res = supabase.table("promo_redemptions").select(
        "id", count="exact"
    ).eq("campaign_id", campaign_id).execute()

    return res.count or 0


def validate_campaign_and_code(code_rec: dict, campaign: dict, user_id: str) -> None:
    if not code_rec.get("is_active", False):
        raise HTTPException(status_code=400, detail="PROMO_INACTIVE")

    if code_rec.get("is_used", False):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")

    bound_user_id = code_rec.get("bound_user_id")
    if bound_user_id and bound_user_id != user_id:
        raise HTTPException(status_code=400, detail="PROMO_ACCOUNT_MISMATCH")

    if not campaign.get("is_active", False):
        raise HTTPException(status_code=400, detail="CAMPAIGN_INACTIVE")

    current = now_utc()
    starts_at = campaign.get("starts_at")
    ends_at = campaign.get("ends_at")

    if starts_at:
        starts_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
        if current < starts_dt:
            raise HTTPException(status_code=400, detail="PROMO_NOT_STARTED")

    if ends_at:
        ends_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
        if current > ends_dt:
            raise HTTPException(status_code=400, detail="PROMO_EXPIRED")

    per_user_limit = int(campaign.get("per_user_limit") or 1)
    if count_user_redemptions(campaign["id"], user_id) >= per_user_limit:
        raise HTTPException(status_code=400, detail="PROMO_USER_LIMIT_REACHED")

    max_total = campaign.get("max_total_redemptions")
    if max_total is not None:
        if count_total_redemptions(campaign["id"]) >= int(max_total):
            raise HTTPException(status_code=400, detail="PROMO_GLOBAL_LIMIT_REACHED")


def apply_membership(profile: dict, campaign: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    membership_months = int(campaign.get("membership_months") or 0)
    package_code = campaign.get("package_code") or "member"

    if membership_months <= 0:
        return profile.get("selected_package_code"), profile.get("package_started_at"), profile.get("package_ends_at")

    current = now_utc()

    old_end_raw = profile.get("package_ends_at")
    old_end = None
    if old_end_raw:
        old_end = datetime.fromisoformat(old_end_raw.replace("Z", "+00:00"))

    stack_mode = campaign.get("stack_mode") or "extend"

    if stack_mode == "ignore_if_active" and old_end and old_end > current:
        raise HTTPException(status_code=400, detail="PROMO_SKIPPED_ACTIVE_MEMBERSHIP")

    if stack_mode == "replace":
        new_start = current
    else:
        if old_end and old_end > current:
            new_start = old_end
        else:
            new_start = current

    new_end = add_months_safe(new_start, membership_months)

    update_payload = {
        "selected_package_code": package_code,
        "package_started_at": current.isoformat(),
        "package_ends_at": new_end.isoformat(),
    }
    supabase.table("profiles").update(update_payload).eq("id", profile["id"]).execute()

    return package_code, current.isoformat(), new_end.isoformat()


def apply_tokens(profile: dict, campaign: dict) -> tuple[int, int]:
    token_amount = int(campaign.get("token_amount") or 0)
    current_tokens = int(profile.get("tokens") or 0)
    new_tokens = current_tokens + token_amount

    if token_amount > 0:
        supabase.table("profiles").update({
            "tokens": new_tokens
        }).eq("id", profile["id"]).execute()

    return token_amount, new_tokens


def mark_code_used(code_rec: dict, user_id: str) -> None:
    supabase.table("promo_codes").update({
        "is_used": True,
        "used_by": user_id,
        "used_at": now_utc().isoformat(),
        "bound_user_id": user_id
    }).eq("id", code_rec["id"]).execute()


def log_redemption(
    code_rec: dict,
    campaign: dict,
    profile_before: dict,
    profile_after: dict,
    user_id: str,
    source: str,
    membership_months: int,
    granted_tokens: int,
    membership_ends_at: Optional[str],
    package_code: Optional[str]
) -> None:
    supabase.table("promo_redemptions").insert({
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
        "after_membership_end": membership_ends_at,
        "before_tokens": int(profile_before.get("tokens") or 0),
        "after_tokens": int(profile_after.get("tokens") or 0),
    }).execute()


def verify_bearer(auth_header: Optional[str]) -> None:
    # İstersen burada Supabase JWT doğrulaması yaparsın.
    # Şimdilik boş bırakıyorum çünkü mevcut yapında Authorization header zaten gidiyor.
    if not auth_header:
        raise HTTPException(status_code=401, detail="MISSING_AUTH")


# =========================
# ROUTE
# =========================

@router.post("/redeem", response_model=PromoRedeemResponse)
def redeem_promo(payload: PromoRedeemRequest, authorization: Optional[str] = Header(None)):
    verify_bearer(authorization)

    profile_before = get_profile(payload.user_id)
    code_rec = find_code_record(payload.source, payload.code, payload.uid)
    campaign = get_campaign(code_rec["campaign_id"])

    validate_campaign_and_code(code_rec, campaign, payload.user_id)

    membership_months = int(campaign.get("membership_months") or 0)
    package_code = campaign.get("package_code")

    membership_started_at = None
    membership_ends_at = None
    tokens_loaded = 0

    if campaign["grant_type"] in ("membership", "bundle"):
        package_code, membership_started_at, membership_ends_at = apply_membership(profile_before, campaign)

    # membership update sonrası profili tekrar çek
    profile_mid = get_profile(payload.user_id)

    if campaign["grant_type"] in ("tokens", "bundle"):
        tokens_loaded, _ = apply_tokens(profile_mid, campaign)

    profile_after = get_profile(payload.user_id)

    mark_code_used(code_rec, payload.user_id)

    log_redemption(
        code_rec=code_rec,
        campaign=campaign,
        profile_before=profile_before,
        profile_after=profile_after,
        user_id=payload.user_id,
        source=payload.source,
        membership_months=membership_months,
        granted_tokens=tokens_loaded,
        membership_ends_at=membership_ends_at,
        package_code=package_code
    )

    message_parts = []
    if campaign["grant_type"] in ("membership", "bundle") and membership_months > 0:
        message_parts.append(f"{membership_months} aylık üyelik tanımlandı")
    if campaign["grant_type"] in ("tokens", "bundle") and tokens_loaded > 0:
        message_parts.append(f"{tokens_loaded} jeton yüklendi")

    return PromoRedeemResponse(
        ok=True,
        grant_type=campaign["grant_type"],
        membership_months=membership_months,
        package_code=package_code,
        membership_started_at=membership_started_at,
        membership_ends_at=membership_ends_at,
        tokens_loaded=tokens_loaded,
        tokens_after=int(profile_after.get("tokens") or 0),
        message=" ve ".join(message_parts) if message_parts else "Promosyon başarıyla uygulandı."
    )
