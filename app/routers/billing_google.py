from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["billing-google"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

PLAY_SUBSCRIPTION_PRODUCT_ID = "italky_pro"
SUBSCRIPTION_WELCOME_BONUS = 5

PRODUCT_TOKENS = {
    "jeton_10": 10,
    "jeton_20": 25,
    "jeton_50": 50,
    "jeton_100": 100,
    "jeton_250": 250,
    "jeton_500": 500,
}


class GoogleBillingConfirmReq(BaseModel):
    user_id: str
    product_id: str
    purchase_token: str


class GoogleSubscriptionConfirmReq(BaseModel):
    user_id: str
    product_id: str
    purchase_token: str
    subscription_starts_at: str | None = None
    subscription_ends_at: str | None = None
    is_active: bool = True
    source: str = "google_play"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _profile_or_404(user_id: str) -> dict[str, Any]:
    prof = (
        supabase.table("profiles")
        .select(
            "id,email,tokens,welcome_bonus_claimed,welcome_bonus_claimed_at,"
            "membership_status,membership_source,membership_product_id,"
            "membership_started_at,membership_ends_at,membership_last_checked_at"
        )
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(prof) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return rows[0] or {}


def _get_purchase_owner(purchase_token: str) -> dict[str, Any] | None:
    existing = (
        supabase.table("billing_purchases")
        .select("id,user_id,user_email,product_id,provider,purchase_token")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )
    rows = _safe_data(existing) or []
    return rows[0] if rows else None


def _insert_purchase_log(
    user_id: str,
    user_email: str,
    product_id: str,
    amount: int,
    purchase_token: str,
    provider: str = "google_play",
):
    supabase.table("billing_purchases").insert(
        {
            "user_id": user_id,
            "user_email": user_email,
            "product_id": product_id,
            "amount": amount,
            "purchase_token": purchase_token,
            "provider": provider,
        }
    ).execute()


def _update_purchase_owner_email(purchase_token: str, user_id: str, user_email: str):
    supabase.table("billing_purchases").update(
        {
            "user_id": user_id,
            "user_email": user_email,
        }
    ).eq("purchase_token", purchase_token).execute()


def _insert_wallet_credit_tx(
    user_id: str,
    amount: int,
    balance_before: int,
    balance_after: int,
    source: str,
    description: str,
    meta: dict | None = None,
):
    supabase.table("wallet_tx").insert(
        {
            "user_id": user_id,
            "type": "purchase",
            "amount": amount,
            "reason": description,
            "meta": {
                "source": source,
                "balance_before": balance_before,
                "balance_after": balance_after,
                **(meta or {}),
            },
        }
    ).execute()


def _grant_subscription_welcome_bonus_if_needed(user_id: str, purchase_token: str) -> tuple[bool, int]:
    prof = _profile_or_404(user_id)

    already_claimed = bool(prof.get("welcome_bonus_claimed"))
    current_tokens = int(prof.get("tokens") or 0)

    if already_claimed:
        return False, current_tokens

    next_tokens = current_tokens + SUBSCRIPTION_WELCOME_BONUS
    now_iso = _iso(_now())

    supabase.table("profiles").update(
        {
            "tokens": next_tokens,
            "welcome_bonus_claimed": True,
            "welcome_bonus_claimed_at": now_iso,
        }
    ).eq("id", user_id).execute()

    _insert_wallet_credit_tx(
        user_id=user_id,
        amount=SUBSCRIPTION_WELCOME_BONUS,
        balance_before=current_tokens,
        balance_after=next_tokens,
        source="google_play_subscription_welcome_bonus",
        description="Google Play üyelik hoş geldin bonusu",
        meta={
            "provider": "google_play",
            "purchase_token": purchase_token,
            "bonus_tokens": SUBSCRIPTION_WELCOME_BONUS,
        },
    )

    return True, next_tokens


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id_required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    if product_id == PLAY_SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(
            status_code=400,
            detail="subscription_product_must_use_subscription_confirm"
        )

    amount = PRODUCT_TOKENS.get(product_id)
    if not amount:
        raise HTTPException(status_code=400, detail="invalid_token_product_id")

    prof = _profile_or_404(user_id)
    user_email = str(prof.get("email") or "").strip().lower()

    existing_owner = _get_purchase_owner(purchase_token)
    if existing_owner:
        existing_user_id = str(existing_owner.get("user_id") or "").strip()
        current_tokens = int(prof.get("tokens") or 0)

        if existing_user_id and existing_user_id != user_id:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")

        return {
            "ok": True,
            "already_processed": True,
            "product_id": product_id,
            "loaded_tokens": 0,
            "tokens": current_tokens,
            "tokens_after": current_tokens,
        }

    current_tokens = int(prof.get("tokens") or 0)
    next_tokens = current_tokens + amount

    supabase.table("profiles").update(
        {
            "tokens": next_tokens
        }
    ).eq("id", user_id).execute()

    _insert_purchase_log(
        user_id=user_id,
        user_email=user_email,
        product_id=product_id,
        amount=amount,
        purchase_token=purchase_token,
        provider="google_play",
    )

    _insert_wallet_credit_tx(
        user_id=user_id,
        amount=amount,
        balance_before=current_tokens,
        balance_after=next_tokens,
        source="google_play_token_load",
        description=f"Google Play jeton yükleme: {product_id}",
        meta={
            "provider": "google_play",
            "product_id": product_id,
            "purchase_token": purchase_token,
            "loaded_tokens": amount,
        },
    )

    return {
        "ok": True,
        "already_processed": False,
        "product_id": product_id,
        "loaded_tokens": amount,
        "tokens": next_tokens,
        "tokens_after": next_tokens,
    }


@router.post("/api/billing/google/subscription/confirm")
async def billing_google_subscription_confirm(req: GoogleSubscriptionConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()
    source = (req.source or "google_play").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id_required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")
    if product_id != PLAY_SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="invalid_subscription_product_id")

    prof = _profile_or_404(user_id)
    user_email = str(prof.get("email") or "").strip().lower()
    if not user_email:
        raise HTTPException(status_code=422, detail="profile_email_missing")

    now_dt = _now()
    now_iso = _iso(now_dt)

    start_dt = _parse_dt(req.subscription_starts_at) or now_dt
    end_dt = _parse_dt(req.subscription_ends_at)
    if not end_dt:
        raise HTTPException(status_code=422, detail="subscription_ends_at_required")

    membership_status = "active" if req.is_active and end_dt > now_dt else "expired"

    existing_owner = _get_purchase_owner(purchase_token)
    already_processed = bool(existing_owner)
    restored = False

    if existing_owner:
        existing_user_id = str(existing_owner.get("user_id") or "").strip()
        existing_user_email = str(existing_owner.get("user_email") or "").strip().lower()

        # Aynı kullanıcı -> normal restore
        if existing_user_id == user_id:
            restored = True

        # Hesap silinmiş / yeni user_id oluşmuş ama email aynı -> güvenli restore
        elif existing_user_email and existing_user_email == user_email:
            restored = True
            _update_purchase_owner_email(
                purchase_token=purchase_token,
                user_id=user_id,
                user_email=user_email,
            )

        # Başka kullanıcı -> taşıma yasak
        else:
            raise HTTPException(
                status_code=409,
                detail="purchase_token_already_bound_to_other_user"
            )

    update_payload = {
        "membership_status": membership_status,
        "membership_source": source,
        "membership_product_id": product_id,
        "membership_started_at": _iso(start_dt),
        "membership_ends_at": _iso(end_dt),
        "membership_last_checked_at": now_iso,
    }

    supabase.table("profiles").update(update_payload).eq("id", user_id).execute()

    if not already_processed:
        _insert_purchase_log(
            user_id=user_id,
            user_email=user_email,
            product_id=product_id,
            amount=0,
            purchase_token=purchase_token,
            provider="google_play_subscription",
        )

    bonus_given = False
    tokens_after_bonus = int((_profile_or_404(user_id)).get("tokens") or 0)

    # Sadece ilk işleme almada bonus
    if not already_processed and membership_status == "active":
        bonus_given, tokens_after_bonus = _grant_subscription_welcome_bonus_if_needed(
            user_id=user_id,
            purchase_token=purchase_token,
        )

    fresh = _profile_or_404(user_id)

    return {
        "ok": True,
        "already_processed": already_processed,
        "restored": restored,
        "bonus_given": bonus_given,
        "bonus_tokens": SUBSCRIPTION_WELCOME_BONUS if bonus_given else 0,
        "membership_status": fresh.get("membership_status"),
        "membership_source": fresh.get("membership_source"),
        "membership_product_id": fresh.get("membership_product_id"),
        "membership_started_at": fresh.get("membership_started_at"),
        "membership_ends_at": fresh.get("membership_ends_at"),
        "membership_last_checked_at": fresh.get("membership_last_checked_at"),
        "welcome_bonus_claimed": bool(fresh.get("welcome_bonus_claimed")),
        "welcome_bonus_claimed_at": fresh.get("welcome_bonus_claimed_at"),
        "tokens": int(fresh.get("tokens") or 0),
        "tokens_after_bonus": tokens_after_bonus,
    }


@router.post("/api/billing/google/subscription/cancel")
async def billing_google_subscription_cancel(req: GoogleSubscriptionConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id_required")
    if product_id != PLAY_SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="invalid_subscription_product_id")

    prof = _profile_or_404(user_id)
    end_dt = _parse_dt(prof.get("membership_ends_at"))

    status_value = "cancelled"
    if end_dt and end_dt > _now():
        status_value = "active"

    supabase.table("profiles").update(
        {
            "membership_status": status_value,
            "membership_last_checked_at": _iso(_now())
        }
    ).eq("id", user_id).execute()

    fresh = _profile_or_404(user_id)

    return {
        "ok": True,
        "membership_status": fresh.get("membership_status"),
        "membership_ends_at": fresh.get("membership_ends_at"),
    }
