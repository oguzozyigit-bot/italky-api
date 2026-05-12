# FILE: billing_google.py

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, Header, HTTPException
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["billing-google"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
ANDROID_PACKAGE_NAME = os.getenv("ANDROID_PACKAGE_NAME", "com.ozyigits.italkyai").strip()
GOOGLE_PLAY_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
GOOGLE_PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

if not ANDROID_PACKAGE_NAME:
    raise RuntimeError("ANDROID_PACKAGE_NAME missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

PLAY_SUBSCRIPTION_PRODUCT_ID = "reklamsiz"
LEGACY_SUBSCRIPTION_PRODUCT_IDS = {"italky_pro"}
ALLOWED_SUBSCRIPTION_PRODUCT_IDS = {
    PLAY_SUBSCRIPTION_PRODUCT_ID,
    *LEGACY_SUBSCRIPTION_PRODUCT_IDS,
}

ACTIVE_GOOGLE_SUBSCRIPTION_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
}

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
    user_id: str | None = None
    product_id: str
    purchase_token: str


class GoogleSubscriptionConfirmReq(BaseModel):
    user_id: str | None = None
    product_id: str
    purchase_token: str
    base_plan_id: str | None = None
    subscription_starts_at: str | None = None
    subscription_ends_at: str | None = None
    is_active: bool | None = None
    source: str = "google_play"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_lower(value: Any) -> str:
    return _clean(value).lower()


def _parse_google_time(raw: Any) -> datetime | None:
    if raw is None:
        return None

    value = str(raw).strip()
    if not value:
        return None

    if value.isdigit():
        try:
            return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        except Exception:
            return None

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _canonical_subscription_product_id(product_id: str) -> str:
    clean = _clean_lower(product_id)
    if clean not in ALLOWED_SUBSCRIPTION_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="invalid_subscription_product_id")
    return PLAY_SUBSCRIPTION_PRODUCT_ID


def _profile_or_404(user_id: str) -> dict[str, Any]:
    prof = (
        supabase.table("profiles")
        .select(
            "id,email,tokens,welcome_bonus_claimed,welcome_bonus_claimed_at,"
            "membership_status,membership_source,membership_product_id,"
            "membership_started_at,membership_ends_at,membership_last_checked_at,"
            "selected_package_code,package_active,package_started_at,package_ends_at"
        )
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(prof) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return rows[0] or {}


def _auth_user_id(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_auth")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing_auth")

    try:
        res = supabase.auth.get_user(token)
        user = getattr(res, "user", None)
        user_id = _clean(getattr(user, "id", ""))
        if not user_id:
            raise HTTPException(status_code=401, detail="invalid_auth")
        return user_id
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="invalid_auth")


def _resolve_request_user_id(payload_user_id: str | None, authorization: str | None) -> str:
    auth_user_id = _auth_user_id(authorization)
    clean_payload_user_id = _clean(payload_user_id)
    if clean_payload_user_id and clean_payload_user_id != auth_user_id:
        raise HTTPException(status_code=403, detail="user_id_mismatch")
    return auth_user_id


def _load_google_credentials():
    raw_json = GOOGLE_PLAY_SERVICE_ACCOUNT_JSON
    try:
        if raw_json:
            if raw_json.lstrip().startswith("{"):
                info = json.loads(raw_json)
                return service_account.Credentials.from_service_account_info(
                    info,
                    scopes=[GOOGLE_PLAY_SCOPE],
                )
            return service_account.Credentials.from_service_account_file(
                raw_json,
                scopes=[GOOGLE_PLAY_SCOPE],
            )

        if GOOGLE_APPLICATION_CREDENTIALS:
            return service_account.Credentials.from_service_account_file(
                GOOGLE_APPLICATION_CREDENTIALS,
                scopes=[GOOGLE_PLAY_SCOPE],
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="google_play_credentials_invalid") from exc

    raise HTTPException(status_code=500, detail="google_play_credentials_missing")


def _google_access_token() -> str:
    credentials = _load_google_credentials()
    try:
        credentials.refresh(GoogleAuthRequest())
        token = _clean(credentials.token)
        if not token:
            raise RuntimeError("empty_google_access_token")
        return token
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="google_play_auth_failed") from exc


def _google_get(url: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {_google_access_token()}"}
    try:
        response = requests.get(url, headers=headers, timeout=12)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="google_play_request_failed") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="google_play_purchase_not_found")

    if not response.ok:
        raise HTTPException(status_code=502, detail="google_play_verification_failed")

    try:
        data = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="google_play_invalid_json") from exc

    return data or {}


def _verify_subscription_v2(purchase_token: str) -> dict[str, Any]:
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/"
        f"applications/{ANDROID_PACKAGE_NAME}/purchases/subscriptionsv2/"
        f"tokens/{purchase_token}"
    )
    data = _google_get(url)
    line_items = data.get("lineItems") or []
    if not line_items:
        raise HTTPException(status_code=400, detail="google_play_subscription_line_items_empty")

    chosen = None
    for item in line_items:
        item_product_id = _clean_lower(item.get("productId"))
        if item_product_id in ALLOWED_SUBSCRIPTION_PRODUCT_IDS:
            chosen = item
            break
    chosen = chosen or line_items[0]

    product_id = _clean_lower(chosen.get("productId"))
    offer_details = chosen.get("offerDetails") or {}
    expiry_dt = _parse_google_time(chosen.get("expiryTime"))
    start_dt = _parse_google_time(data.get("startTime")) or _now()
    subscription_state = _clean(data.get("subscriptionState"))
    auto_renewing_plan = chosen.get("autoRenewingPlan") or {}
    auto_renewing = bool(auto_renewing_plan.get("autoRenewEnabled"))

    if not expiry_dt:
        raise HTTPException(status_code=400, detail="google_play_expiry_missing")

    is_active = subscription_state in ACTIVE_GOOGLE_SUBSCRIPTION_STATES and expiry_dt > _now()

    return {
        "source": "subscriptionsv2",
        "package_name": ANDROID_PACKAGE_NAME,
        "product_id": product_id,
        "base_plan_id": _clean(offer_details.get("basePlanId")),
        "offer_id": _clean(offer_details.get("offerId")),
        "start_dt": start_dt,
        "expiry_dt": expiry_dt,
        "subscription_state": subscription_state,
        "auto_renewing": auto_renewing,
        "order_id": _clean(data.get("latestOrderId")),
        "raw": data,
        "is_active": is_active,
    }


def _verify_subscription_legacy(product_id: str, purchase_token: str) -> dict[str, Any]:
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/"
        f"applications/{ANDROID_PACKAGE_NAME}/purchases/subscriptions/"
        f"{product_id}/tokens/{purchase_token}"
    )
    data = _google_get(url)
    expiry_dt = _parse_google_time(data.get("expiryTimeMillis"))
    start_dt = _parse_google_time(data.get("startTimeMillis")) or _now()
    payment_state = data.get("paymentState")
    auto_renewing = bool(data.get("autoRenewing"))

    if not expiry_dt:
        raise HTTPException(status_code=400, detail="google_play_expiry_missing")

    is_active = payment_state in (1, 2, 3) and expiry_dt > _now()

    return {
        "source": "subscriptions_legacy",
        "package_name": ANDROID_PACKAGE_NAME,
        "product_id": _clean_lower(product_id),
        "base_plan_id": "",
        "offer_id": "",
        "start_dt": start_dt,
        "expiry_dt": expiry_dt,
        "subscription_state": f"paymentState:{payment_state}",
        "auto_renewing": auto_renewing,
        "order_id": _clean(data.get("orderId")),
        "raw": data,
        "is_active": is_active,
    }


def _verify_google_subscription(product_id: str, purchase_token: str) -> dict[str, Any]:
    canonical_product_id = _canonical_subscription_product_id(product_id)
    try:
        result = _verify_subscription_v2(purchase_token)
    except HTTPException as exc:
        if exc.status_code not in (400, 404):
            raise
        result = _verify_subscription_legacy(canonical_product_id, purchase_token)

    verified_product_id = _clean_lower(result.get("product_id"))
    if verified_product_id and verified_product_id not in ALLOWED_SUBSCRIPTION_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="google_play_product_mismatch")

    result["canonical_product_id"] = canonical_product_id
    return result


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


def _update_purchase_log(purchase_token: str, user_id: str, user_email: str, product_id: str, provider: str):
    supabase.table("billing_purchases").update(
        {
            "user_id": user_id,
            "user_email": user_email,
            "product_id": product_id,
            "provider": provider,
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
            "subscription_product_id": PLAY_SUBSCRIPTION_PRODUCT_ID,
        },
    )

    return True, next_tokens


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq, authorization: str | None = Header(default=None)):
    user_id = _resolve_request_user_id(req.user_id, authorization)
    product_id = _clean_lower(req.product_id)
    purchase_token = _clean(req.purchase_token)

    if not product_id:
        raise HTTPException(status_code=422, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    if product_id in ALLOWED_SUBSCRIPTION_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="subscription_product_must_use_subscription_confirm")

    amount = PRODUCT_TOKENS.get(product_id)
    if not amount:
        raise HTTPException(status_code=400, detail="invalid_token_product_id")

    prof = _profile_or_404(user_id)
    user_email = _clean_lower(prof.get("email"))
    existing_owner = _get_purchase_owner(purchase_token)

    if existing_owner:
        existing_user_id = _clean(existing_owner.get("user_id"))
        current_tokens = int(prof.get("tokens") or 0)
        if existing_user_id and existing_user_id != user_id:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")
        _update_purchase_log(purchase_token, user_id, user_email, product_id, "google_play")
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

    supabase.table("profiles").update({"tokens": next_tokens}).eq("id", user_id).execute()
    _insert_purchase_log(user_id, user_email, product_id, amount, purchase_token, "google_play")
    _insert_wallet_credit_tx(
        user_id=user_id,
        amount=amount,
        balance_before=current_tokens,
        balance_after=next_tokens,
        source="google_play_token_load",
        description=f"Google Play jeton yükleme: {product_id}",
        meta={"provider": "google_play", "product_id": product_id, "purchase_token": purchase_token},
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
async def billing_google_subscription_confirm(
    req: GoogleSubscriptionConfirmReq,
    authorization: str | None = Header(default=None),
):
    user_id = _resolve_request_user_id(req.user_id, authorization)
    incoming_product_id = _clean_lower(req.product_id)
    purchase_token = _clean(req.purchase_token)

    if not incoming_product_id:
        raise HTTPException(status_code=422, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    verification = _verify_google_subscription(incoming_product_id, purchase_token)
    product_id = verification["canonical_product_id"]
    start_dt = verification["start_dt"]
    end_dt = verification["expiry_dt"]
    membership_status = "active" if verification["is_active"] else "expired"

    prof = _profile_or_404(user_id)
    user_email = _clean_lower(prof.get("email"))
    if not user_email:
        raise HTTPException(status_code=422, detail="profile_email_missing")

    existing_owner = _get_purchase_owner(purchase_token)
    already_processed = bool(existing_owner)
    restored = False

    if existing_owner:
        existing_user_id = _clean(existing_owner.get("user_id"))
        existing_user_email = _clean_lower(existing_owner.get("user_email"))

        if existing_user_id == user_id:
            restored = True
        elif existing_user_email and existing_user_email == user_email:
            restored = True
        else:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")

        _update_purchase_log(
            purchase_token=purchase_token,
            user_id=user_id,
            user_email=user_email,
            product_id=product_id,
            provider="google_play_subscription",
        )
    else:
        _insert_purchase_log(
            user_id=user_id,
            user_email=user_email,
            product_id=product_id,
            amount=0,
            purchase_token=purchase_token,
            provider="google_play_subscription",
        )

    now_iso = _iso(_now())
    update_payload = {
        "membership_status": membership_status,
        "membership_source": "google_play",
        "membership_product_id": product_id,
        "membership_started_at": _iso(start_dt),
        "membership_ends_at": _iso(end_dt),
        "membership_last_checked_at": now_iso,
        "package_active": membership_status == "active",
        "selected_package_code": product_id,
        "package_started_at": _iso(start_dt),
        "package_ends_at": _iso(end_dt),
    }

    supabase.table("profiles").update(update_payload).eq("id", user_id).execute()

    bonus_given = False
    tokens_after_bonus = int((_profile_or_404(user_id)).get("tokens") or 0)
    if not already_processed and membership_status == "active":
        bonus_given, tokens_after_bonus = _grant_subscription_welcome_bonus_if_needed(user_id, purchase_token)

    fresh = _profile_or_404(user_id)
    active = fresh.get("membership_status") == "active"

    return {
        "ok": True,
        "already_processed": already_processed,
        "restored": restored,
        "google_verification_source": verification.get("source"),
        "google_product_id": verification.get("product_id"),
        "google_base_plan_id": verification.get("base_plan_id"),
        "google_offer_id": verification.get("offer_id"),
        "google_subscription_state": verification.get("subscription_state"),
        "google_auto_renewing": verification.get("auto_renewing"),
        "google_order_id": verification.get("order_id"),
        "bonus_given": bonus_given,
        "bonus_tokens": SUBSCRIPTION_WELCOME_BONUS if bonus_given else 0,
        "membership_status": fresh.get("membership_status"),
        "membership_source": fresh.get("membership_source"),
        "membership_product_id": fresh.get("membership_product_id"),
        "membership_started_at": fresh.get("membership_started_at"),
        "membership_ends_at": fresh.get("membership_ends_at"),
        "membership_last_checked_at": fresh.get("membership_last_checked_at"),
        "package_active": fresh.get("package_active"),
        "package_started_at": fresh.get("package_started_at"),
        "package_ends_at": fresh.get("package_ends_at"),
        "subscription_active": active,
        "subscription_product_id": fresh.get("membership_product_id"),
        "subscription_started_at": fresh.get("membership_started_at"),
        "subscription_ends_at": fresh.get("membership_ends_at"),
        "is_member": active,
        "has_active_membership": active,
        "no_ads": active and fresh.get("membership_product_id") == PLAY_SUBSCRIPTION_PRODUCT_ID,
        "ads_disabled": active and fresh.get("membership_product_id") == PLAY_SUBSCRIPTION_PRODUCT_ID,
        "is_no_ads_member": active and fresh.get("membership_product_id") == PLAY_SUBSCRIPTION_PRODUCT_ID,
        "welcome_bonus_claimed": bool(fresh.get("welcome_bonus_claimed")),
        "welcome_bonus_claimed_at": fresh.get("welcome_bonus_claimed_at"),
        "tokens": int(fresh.get("tokens") or 0),
        "tokens_after_bonus": tokens_after_bonus,
    }


@router.post("/api/billing/google/subscription/cancel")
async def billing_google_subscription_cancel(
    req: GoogleSubscriptionConfirmReq,
    authorization: str | None = Header(default=None),
):
    user_id = _resolve_request_user_id(req.user_id, authorization)
    incoming_product_id = _clean_lower(req.product_id)
    purchase_token = _clean(req.purchase_token)

    if not incoming_product_id:
        raise HTTPException(status_code=422, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    verification = _verify_google_subscription(incoming_product_id, purchase_token)
    product_id = verification["canonical_product_id"]
    end_dt = verification["expiry_dt"]
    status_value = "active" if verification["is_active"] else "cancelled"

    supabase.table("profiles").update(
        {
            "membership_status": status_value,
            "membership_product_id": product_id,
            "membership_ends_at": _iso(end_dt),
            "package_active": status_value == "active",
            "package_ends_at": _iso(end_dt),
            "membership_last_checked_at": _iso(_now()),
        }
    ).eq("id", user_id).execute()

    fresh = _profile_or_404(user_id)
    active = fresh.get("membership_status") == "active"

    return {
        "ok": True,
        "membership_status": fresh.get("membership_status"),
        "membership_product_id": fresh.get("membership_product_id"),
        "membership_ends_at": fresh.get("membership_ends_at"),
        "subscription_active": active,
        "subscription_product_id": fresh.get("membership_product_id"),
        "subscription_ends_at": fresh.get("membership_ends_at"),
        "no_ads": active and fresh.get("membership_product_id") == PLAY_SUBSCRIPTION_PRODUCT_ID,
        "ads_disabled": active and fresh.get("membership_product_id") == PLAY_SUBSCRIPTION_PRODUCT_ID,
    }
