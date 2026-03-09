from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

router = APIRouter(tags=["billing-google"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class GoogleBillingConfirmReq(BaseModel):
    user_id: str
    product_id: str
    amount: int
    purchase_token: str


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()
    amount = int(req.amount or 0)

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")
    if amount <= 0:
        raise HTTPException(status_code=422, detail="amount must be > 0")

    # aynı token daha önce işlendi mi?
    existing = (
        supabase.table("billing_purchases")
        .select("id")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )

    if existing.data:
        return {"ok": True, "already_processed": True}

    # mevcut token sayısı
    prof = (
        supabase.table("profiles")
        .select("tokens")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(status_code=404, detail="profile not found")

    current_tokens = int((prof.data[0] or {}).get("tokens") or 0)
    next_tokens = current_tokens + amount

    # token güncelle
    supabase.table("profiles").update(
        {"tokens": next_tokens}
    ).eq("id", user_id).execute()

    # satın almayı kaydet
    supabase.table("billing_purchases").insert({
        "user_id": user_id,
        "product_id": product_id,
        "amount": amount,
        "purchase_token": purchase_token,
        "provider": "google_play"
    }).execute()

    return {"ok": True, "tokens": next_tokens}
