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

PRODUCT_TOKENS = {
    "jeton_10": 10,
    "jeton_20": 20,
    "jeton_50": 50,
    "jeton_100": 100,
    "jeton_250": 250,
    "jeton_500": 500,
}


class GoogleBillingConfirmReq(BaseModel):
    user_id: str
    product_id: str
    purchase_token: str


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")

    amount = PRODUCT_TOKENS.get(product_id)
    if not amount:
        raise HTTPException(status_code=400, detail="invalid product_id")

    existing = (
        supabase.table("billing_purchases")
        .select("id")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )

    if existing.data:
        return {"ok": True, "already_processed": True}

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

    update_res = (
        supabase.table("profiles")
        .update({"tokens": next_tokens})
        .eq("id", user_id)
        .execute()
    )

    print("PROFILE UPDATE RESULT:", update_res)

    insert_res = (
        supabase.table("billing_purchases")
        .insert({
            "user_id": user_id,
            "product_id": product_id,
            "amount": amount,
            "purchase_token": purchase_token,
            "provider": "google_play"
        })
        .execute()
    )

    print("PURCHASE INSERT RESULT:", insert_res)

    return {"ok": True, "tokens": next_tokens}
