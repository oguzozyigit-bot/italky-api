from __future__ import annotations

import os
import time
from typing import Any

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.routers.billing_google import (
    ANDROID_PACKAGE_NAME,
    PRODUCT_TOKENS,
    _clean,
    _clean_lower,
    _get_purchase_owner,
    _google_get,
    _profile_or_404,
    _resolve_request_user_id,
    _safe_data,
    supabase,
)

router = APIRouter(tags=["billing-google-token-secure"])

ICANY_ORIGIN = os.getenv("ICANY_ORIGIN", "https://www.icany.ai").strip().rstrip("/")
BRIDGE_SECRET = os.getenv("ICANY_ITALKY_BRIDGE_SECRET", "").strip()
PROCESSING_PREFIX = "google_play_processing:"
FINAL_PROVIDER = "google_play_verified_icany"
PROCESSING_TTL_SECONDS = 45


class GoogleTokenConfirmReq(BaseModel):
    user_id: str | None = None
    product_id: str | None = None
    productId: str | None = None
    purchase_token: str | None = None
    purchaseToken: str | None = None
    order_id: str | None = None
    orderId: str | None = None
    package_name: str | None = None
    packageName: str | None = None
    product_type: str | None = None
    productType: str | None = None


def _field(req: GoogleTokenConfirmReq, snake: str, camel: str) -> Any:
    value = getattr(req, snake, None)
    if value is not None:
        return value
    return getattr(req, camel, None)


def _verify_google_token_purchase(product_id: str, purchase_token: str) -> dict[str, Any]:
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/"
        f"applications/{ANDROID_PACKAGE_NAME}/purchases/products/"
        f"{product_id}/tokens/{purchase_token}"
    )
    data = _google_get(url)

    try:
        purchase_state = int(data.get("purchaseState", 1))
    except Exception:
        purchase_state = 1
    if purchase_state != 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "google_play_token_not_purchased", "purchase_state": purchase_state},
        )

    verified_product_id = _clean_lower(data.get("productId"))
    if verified_product_id and verified_product_id != product_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "google_play_product_id_mismatch",
                "expected_product_id": product_id,
                "actual_product_id": verified_product_id,
            },
        )
    return data


def _processing_provider() -> str:
    return f"{PROCESSING_PREFIX}{int(time.time())}"


def _processing_age(provider: str) -> int | None:
    if not provider.startswith(PROCESSING_PREFIX):
        return None
    try:
        return max(0, int(time.time()) - int(provider.split(":", 1)[1]))
    except Exception:
        return None


def _assert_owner(existing: dict[str, Any] | None, user_id: str) -> None:
    if not existing:
        return
    existing_user_id = _clean(existing.get("user_id"))
    if existing_user_id and existing_user_id != user_id:
        raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")


def _claim_purchase(
    *,
    user_id: str,
    user_email: str,
    product_id: str,
    amount: int,
    purchase_token: str,
) -> dict[str, Any] | None:
    provider = _processing_provider()
    payload = {
        "user_id": user_id,
        "user_email": user_email,
        "product_id": product_id,
        "amount": amount,
        "purchase_token": purchase_token,
        "provider": provider,
    }

    try:
        supabase.table("billing_purchases").insert(payload).execute()
        return None
    except Exception:
        existing = _get_purchase_owner(purchase_token)
        if not existing:
            raise HTTPException(status_code=500, detail="purchase_claim_failed")
        _assert_owner(existing, user_id)

        existing_provider = _clean(existing.get("provider"))
        if existing_provider == FINAL_PROVIDER:
            return existing

        age = _processing_age(existing_provider)
        if age is not None and age < PROCESSING_TTL_SECONDS:
            raise HTTPException(status_code=409, detail="purchase_processing_retry")

        supabase.table("billing_purchases").update(
            {
                "user_id": user_id,
                "user_email": user_email,
                "product_id": product_id,
                "amount": amount,
                "provider": provider,
            }
        ).eq("purchase_token", purchase_token).execute()
        return existing


def _credit_icany_master(
    *,
    user_id: str,
    user_email: str,
    product_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    if not BRIDGE_SECRET:
        raise HTTPException(status_code=503, detail="icany_bridge_secret_missing")

    try:
        response = requests.post(
            f"{ICANY_ORIGIN}/api/bridge/iap-credit",
            headers={
                "Content-Type": "application/json",
                "X-Icany-Bridge-Key": BRIDGE_SECRET,
            },
            json={
                "memberId": user_id,
                "email": user_email,
                "productId": product_id,
                "purchaseToken": purchase_token,
                "source": "italky_api_google_play_verified",
            },
            timeout=20,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="icany_iap_credit_request_failed") from exc

    try:
        data = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="icany_iap_credit_invalid_json") from exc

    if not response.ok or not data.get("ok"):
        raise HTTPException(
            status_code=502,
            detail={
                "error": "icany_iap_credit_failed",
                "status": response.status_code,
                "reason": data.get("error") or data.get("reason") or "unknown",
            },
        )
    return data


def _wallet_tx_exists(user_id: str, purchase_token: str) -> bool:
    try:
        result = (
            supabase.table("wallet_tx")
            .select("id")
            .eq("user_id", user_id)
            .contains("meta", {"purchase_token": purchase_token})
            .limit(1)
            .execute()
        )
        return bool(_safe_data(result))
    except Exception:
        return False


def _finalize_italky_mirror(
    *,
    user_id: str,
    user_email: str,
    product_id: str,
    purchase_token: str,
    purchase_amount: int,
    token_balance: int,
    credited_balance_after: int,
) -> None:
    supabase.table("profiles").update({"tokens": token_balance}).eq("id", user_id).execute()

    supabase.table("billing_purchases").upsert(
        {
            "user_id": user_id,
            "user_email": user_email,
            "product_id": product_id,
            "amount": purchase_amount,
            "purchase_token": purchase_token,
            "provider": FINAL_PROVIDER,
        },
        on_conflict="purchase_token",
    ).execute()

    if purchase_amount > 0 and not _wallet_tx_exists(user_id, purchase_token):
        balance_after = max(0, int(credited_balance_after))
        balance_before = max(0, balance_after - purchase_amount)
        supabase.table("wallet_tx").insert(
            {
                "user_id": user_id,
                "type": "purchase",
                "amount": purchase_amount,
                "reason": f"Google Play jeton yükleme: {product_id}",
                "meta": {
                    "source": "google_play_token_load_verified",
                    "provider": "google_play",
                    "product_id": product_id,
                    "purchase_token": purchase_token,
                    "balance_before": balance_before,
                    "balance_after": balance_after,
                    "master": "icany_personal_token_balance",
                },
            }
        ).execute()


@router.post("/api/billing/google/confirm")
def billing_google_token_confirm_secure(
    req: GoogleTokenConfirmReq,
    authorization: str | None = Header(default=None),
):
    user_id = _resolve_request_user_id(req.user_id, authorization)
    product_id = _clean_lower(_field(req, "product_id", "productId"))
    purchase_token = _clean(_field(req, "purchase_token", "purchaseToken"))
    package_name = _clean(_field(req, "package_name", "packageName"))
    product_type = _clean_lower(_field(req, "product_type", "productType"))

    if package_name and package_name != ANDROID_PACKAGE_NAME:
        raise HTTPException(status_code=400, detail="google_play_package_mismatch")
    if product_type and product_type != "inapp":
        raise HTTPException(status_code=400, detail="google_play_inapp_required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    amount = PRODUCT_TOKENS.get(product_id)
    if not amount:
        raise HTTPException(status_code=400, detail="invalid_token_product_id")

    profile = _profile_or_404(user_id)
    user_email = _clean_lower(profile.get("email"))
    if not user_email:
        raise HTTPException(status_code=422, detail="profile_email_missing")

    existing = _get_purchase_owner(purchase_token)
    _assert_owner(existing, user_id)
    already_verified = _clean((existing or {}).get("provider")) == FINAL_PROVIDER

    if not already_verified:
        _verify_google_token_purchase(product_id, purchase_token)
        _claim_purchase(
            user_id=user_id,
            user_email=user_email,
            product_id=product_id,
            amount=amount,
            purchase_token=purchase_token,
        )

    credited = _credit_icany_master(
        user_id=user_id,
        user_email=user_email,
        product_id=product_id,
        purchase_token=purchase_token,
    )

    token_balance = max(0, int(credited.get("tokenBalance") or credited.get("tokens_after") or 0))
    purchase_amount = max(0, int(credited.get("purchaseAmount") or amount))
    credited_balance_after = max(
        0,
        int(credited.get("creditedBalanceAfter") or credited.get("tokenBalance") or token_balance),
    )
    loaded_tokens = max(0, int(credited.get("loadedTokens") or 0))

    _finalize_italky_mirror(
        user_id=user_id,
        user_email=user_email,
        product_id=product_id,
        purchase_token=purchase_token,
        purchase_amount=purchase_amount,
        token_balance=token_balance,
        credited_balance_after=credited_balance_after,
    )

    return {
        "ok": True,
        "already_processed": bool(credited.get("already_processed")) or already_verified,
        "google_verified": True,
        "product_id": product_id,
        "loaded_tokens": loaded_tokens,
        "purchase_amount": purchase_amount,
        "tokens": token_balance,
        "tokens_after": token_balance,
        "master_wallet": "icany.business_members.personal_token_balance",
    }
