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

# Sadece tek üyelik ürünü
PLAY_SUBSCRIPTION_PRODUCT_ID = "italky_pro"

# Tek seferlik jeton ürünleri
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


def _purchase_exists(purchase_token: str) -> bool:
    existing = (
        supabase.table("billing_purchases")
        .select("id")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )
    return bool(_safe_data(existing))


def _insert_purchase_log(
    user_id: str,
    product_id: str,
    amount: int,
    purchase_token: str,
    provider: str = "google_play",
):
    supabase.table("billing_purchases").insert(
        {
            "user_id": user_id,
            "product_id": product_id,
            "amount": amount,
            "purchase_token": purchase_token,
            "provider": provider,
        }
    ).execute()


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
            "tx_type": "credit",
            "source": source,
            "usage_kind": None,
            "chars_used": 0,
            "jetons": amount,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "description": description,
            "meta": meta or {},
        }
    ).execute()


def _profile_or_404(user_id: str) -> dict[str, Any]:
    prof = (
        supabase.table("profiles")
        .select(
            "id,tokens,"
            "trial_started_at,trial_ends_at,trial_used,"
            "membership_status,membership_source,membership_product_id,"
            "membership_started_at,membership_ends_at,membership_last_checked_at"
        )
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(prof) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")
    return rows[0] or {}


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq):
    """
    Tek seferlik jeton ürünleri için.
    Üyelik ürünü burada işlenmez.
    """
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")

    if product_id == PLAY_SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(
            status_code=400,
            detail="subscription product must use /api/billing/google/subscription/confirm"
        )

    amount = PRODUCT_TOKENS.get(product_id)
    if not amount:
        raise HTTPException(status_code=400, detail="invalid token product_id")

    if _purchase_exists(purchase_token):
        prof = _profile_or_404(user_id)
        current_tokens = int(prof.get("tokens") or 0)
        return {
            "ok": True,
            "already_processed": True,
            "product_id": product_id,
            "loaded_tokens": 0,
            "tokens": current_tokens,
            "tokens_after": current_tokens,
        }

    prof = _profile_or_404(user_id)
    current_tokens = int(prof.get("tokens") or 0)
    next_tokens = current_tokens + amount

    supabase.table("profiles").update(
        {
            "tokens": next_tokens
        }
    ).eq("id", user_id).execute()

    _insert_purchase_log(user_id, product_id, amount, purchase_token, "google_play")

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
            "balance_after": next_tokens,
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
    """
    italky_pro üyeliği için.
    Play doğrulaması Android/backend tarafında yapıldıktan sonra
    doğrulanmış subscription sonucu buraya yazılır.
    """
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()
    source = (req.source or "google_play").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")

    if product_id != PLAY_SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="invalid subscription product_id")

    if _purchase_exists(purchase_token):
        prof = _profile_or_404(user_id)
        return {
            "ok": True,
            "already_processed": True,
            "membership_status": prof.get("membership_status"),
            "membership_product_id": prof.get("membership_product_id"),
            "membership_started_at": prof.get("membership_started_at"),
            "membership_ends_at": prof.get("membership_ends_at"),
            "tokens": int(prof.get("tokens") or 0),
        }

    now_dt = _now()
    now_iso = _iso(now_dt)

    start_dt = _parse_dt(req.subscription_starts_at) or now_dt
    end_dt = _parse_dt(req.subscription_ends_at)

    if not end_dt:
        raise HTTPException(status_code=422, detail="subscription_ends_at required")

    membership_status = "active" if req.is_active and end_dt > now_dt else "expired"

    update_payload = {
        "membership_status": membership_status,
        "membership_source": source,
        "membership_product_id": product_id,
        "membership_started_at": _iso(start_dt),
        "membership_ends_at": _iso(end_dt),
        "membership_last_checked_at": now_iso,
    }

    supabase.table("profiles").update(update_payload).eq("id", user_id).execute()

    _insert_purchase_log(
        user_id=user_id,
        product_id=product_id,
        amount=0,
        purchase_token=purchase_token,
        provider="google_play_subscription",
    )

    fresh = _profile_or_404(user_id)

    return {
        "ok": True,
        "already_processed": False,
        "membership_status": fresh.get("membership_status"),
        "membership_product_id": fresh.get("membership_product_id"),
        "membership_started_at": fresh.get("membership_started_at"),
        "membership_ends_at": fresh.get("membership_ends_at"),
        "membership_last_checked_at": fresh.get("membership_last_checked_at"),
        "tokens": int(fresh.get("tokens") or 0),
    }


@router.post("/api/billing/google/subscription/cancel")
async def billing_google_subscription_cancel(req: GoogleSubscriptionConfirmReq):
    """
    İptal geldiğinde hemen erişim kapatmayız.
    membership_ends_at tarihine kadar aktif kalır.
    """
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if product_id != PLAY_SUBSCRIPTION_PRODUCT_ID:
        raise HTTPException(status_code=400, detail="invalid subscription product_id")

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
