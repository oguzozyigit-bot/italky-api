from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.routers.billing_google import (
    ACTIVE_GOOGLE_SUBSCRIPTION_STATES,
    PLAY_SUBSCRIPTION_PRODUCT_ID,
    _clean,
    _clean_lower,
    _get_purchase_owner,
    _google_get,
    _insert_purchase_log,
    _iso,
    _now,
    _parse_google_time,
    _resolve_request_user_id,
    _safe_data,
    _update_purchase_log,
    supabase,
)

router = APIRouter()

PACKAGE_NAME = (
    os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "").strip()
    or os.getenv("ANDROID_PACKAGE_NAME", "").strip()
    or "com.ozyigits.italkyai"
)

PURCHASE_NOT_VERIFIED_MESSAGE = (
    "Satın alma doğrulanamadı. Lütfen Google Play hesabınızı ve internet bağlantınızı kontrol edin."
)

BASE_PLAN_PACKAGE_CODES = {
    "haftalık-abonelik": "google_play_haftalik",
    "haftalik-abonelik": "google_play_haftalik",
    "reklamsız-haftalık": "google_play_haftalik",
    "reklamsiz-haftalik": "google_play_haftalik",
    "1-ay-abonelik": "google_play_1ay",
    "3-ay-abonelik": "google_play_3ay",
    "6-ay-abonelik": "google_play_6ay",
    "9-ay-abonelik": "google_play_9ay",
    "12-ay-abonelik": "google_play_12ay",
    "24-ay-abonelik": "google_play_24ay",
}

BASE_PLAN_MONTHS = {
    "1-ay-abonelik": 1,
    "3-ay-abonelik": 3,
    "6-ay-abonelik": 6,
    "9-ay-abonelik": 9,
    "12-ay-abonelik": 12,
    "24-ay-abonelik": 24,
}

BASE_PLAN_DAYS = {
    "haftalık-abonelik": 7,
    "haftalik-abonelik": 7,
    "reklamsız-haftalık": 7,
    "reklamsiz-haftalik": 7,
}

ALLOWED_PRODUCT_IDS = {PLAY_SUBSCRIPTION_PRODUCT_ID, "italky_pro"}
ALLOWED_BASE_PLAN_IDS = set(BASE_PLAN_PACKAGE_CODES.keys())


class GooglePlayPurchaseConfirmReq(BaseModel):
    user_id: str | None = None
    product_id: str | None = None
    productId: str | None = None
    purchase_token: str | None = None
    purchaseToken: str | None = None
    order_id: str | None = None
    orderId: str | None = None
    package_name: str | None = None
    packageName: str | None = None
    base_plan_id: str | None = None
    basePlanId: str | None = None
    offer_id: str | None = None
    offerId: str | None = None
    purchase_time: str | None = None
    purchaseTime: str | None = None
    subscription_starts_at: str | None = None
    subscription_ends_at: str | None = None
    is_active: bool | None = None
    is_subscription: bool | None = None
    isSubscription: bool | None = None
    source: str | None = None


def _field(req: GooglePlayPurchaseConfirmReq, snake: str, camel: str | None = None) -> Any:
    value = getattr(req, snake, None)
    if value is not None:
        return value
    if camel:
        return getattr(req, camel, None)
    return None


def _json_error(status_code: int, error: str, message: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": error,
            "reason": error,
            "message": message or PURCHASE_NOT_VERIFIED_MESSAGE,
        },
    )


def _add_months_safe(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    current = datetime(year, month, 1, tzinfo=timezone.utc)
    return (next_month - current).days


def _fallback_expiry(start_dt: datetime, base_plan_id: str | None) -> datetime | None:
    plan = _clean_lower(base_plan_id)
    if not plan:
        return None
    if plan in BASE_PLAN_MONTHS:
        return _add_months_safe(start_dt, BASE_PLAN_MONTHS[plan])
    if plan in BASE_PLAN_DAYS:
        return start_dt + timedelta(days=BASE_PLAN_DAYS[plan])
    return None


def _canonical_product_id(product_id: str | None, base_plan_id: str | None = None) -> str:
    product = _clean_lower(product_id)
    base_plan = _clean_lower(base_plan_id)
    if product in ALLOWED_PRODUCT_IDS:
        return product
    if product in ALLOWED_BASE_PLAN_IDS or base_plan in ALLOWED_BASE_PLAN_IDS:
        return PLAY_SUBSCRIPTION_PRODUCT_ID
    raise HTTPException(status_code=400, detail="unsupported_google_play_product")


def _package_code(product_id: str, base_plan_id: str | None) -> str:
    plan = _clean_lower(base_plan_id)
    if plan in BASE_PLAN_PACKAGE_CODES:
        return BASE_PLAN_PACKAGE_CODES[plan]
    if product_id == PLAY_SUBSCRIPTION_PRODUCT_ID:
        return "google_play"
    return product_id


def _profile_or_404(user_id: str) -> dict[str, Any]:
    data = _safe_data(supabase.table("profiles").select("*").eq("id", user_id).limit(1).execute())
    if not data:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return data[0]


def _verify_subscription_v2(
    product_id: str,
    purchase_token: str,
    requested_base_plan_id: str | None = None,
) -> dict[str, Any]:
    canonical_product_id = _canonical_product_id(product_id, requested_base_plan_id)
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        f"{PACKAGE_NAME}/purchases/subscriptionsv2/tokens/{purchase_token}"
    )
    data = _google_get(url)
    line_items = data.get("lineItems") or []
    if not line_items:
        raise HTTPException(status_code=400, detail="google_play_subscription_line_item_missing")

    selected_item = None
    for item in line_items:
        if _clean_lower(item.get("productId")) == canonical_product_id:
            selected_item = item
            break
    if selected_item is None:
        selected_item = line_items[0]

    google_product_id = _clean_lower(selected_item.get("productId")) or canonical_product_id
    if google_product_id != canonical_product_id:
        raise HTTPException(status_code=400, detail="google_play_product_mismatch")

    offer_details = selected_item.get("offerDetails") or {}
    base_plan_id = _clean_lower(offer_details.get("basePlanId")) or _clean_lower(requested_base_plan_id)
    start_dt = _parse_google_time(data.get("startTime")) or _now()
    expiry_dt = _parse_google_time(selected_item.get("expiryTime")) or _fallback_expiry(start_dt, base_plan_id)
    if not expiry_dt:
        raise HTTPException(status_code=400, detail="google_play_expiry_missing")

    subscription_state = _clean(data.get("subscriptionState")) or "SUBSCRIPTION_STATE_UNSPECIFIED"
    acknowledgement_state = _clean(data.get("acknowledgementState"))
    active = subscription_state in ACTIVE_GOOGLE_SUBSCRIPTION_STATES and expiry_dt > _now()

    return {
        "active": active,
        "canonical_product_id": canonical_product_id,
        "google_product_id": google_product_id,
        "base_plan_id": base_plan_id,
        "subscription_state": subscription_state,
        "acknowledgement_state": acknowledgement_state,
        "start_dt": start_dt,
        "expiry_dt": expiry_dt,
        "order_id": _clean(data.get("latestOrderId")),
    }


def _same_owner(owner: dict[str, Any] | None, user_id: str, profile: dict[str, Any]) -> bool:
    if not owner:
        return False
    owner_user_id = _clean(owner.get("user_id"))
    owner_email = _clean_lower(owner.get("user_email"))
    profile_email = _clean_lower(profile.get("email"))
    return owner_user_id == user_id or bool(owner_email and profile_email and owner_email == profile_email)


def _bind_purchase_token(
    purchase_token: str,
    user_id: str,
    product_id: str,
    profile: dict[str, Any],
) -> bool:
    owner = _get_purchase_owner(purchase_token)
    if owner and not _same_owner(owner, user_id, profile):
        raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")
    if owner:
        _update_purchase_log(purchase_token, user_id, "google_play", product_id, profile.get("email"))
        return True
    _insert_purchase_log(purchase_token, user_id, "google_play", product_id, profile.get("email"))
    return False


def _update_profile_entitlement(
    user_id: str,
    profile: dict[str, Any],
    verification: dict[str, Any],
    purchase_token: str,
) -> dict[str, Any]:
    now_iso = _iso(_now())
    product_id = verification["canonical_product_id"]
    package_code = _package_code(product_id, verification.get("base_plan_id"))
    active = bool(verification.get("active"))
    start_iso = _iso(verification["start_dt"])
    end_iso = _iso(verification["expiry_dt"])

    payload: dict[str, Any] = {
        "membership_status": "active" if active else "expired",
        "membership_source": "google_play",
        "membership_product_id": product_id,
        "membership_started_at": profile.get("membership_started_at") or start_iso,
        "membership_ends_at": end_iso,
        "membership_last_checked_at": now_iso,
        "package_active": active,
        "selected_package_code": package_code,
        "package_started_at": profile.get("package_started_at") or start_iso,
        "package_ends_at": end_iso,
    }
    if active:
        payload.update(
            {
                "plan": "member",
                "app_access_mode": "member",
            }
        )

    supabase.table("profiles").update(payload).eq("id", user_id).execute()
    fresh = _profile_or_404(user_id)
    _update_purchase_log(purchase_token, user_id, "google_play", product_id, fresh.get("email"))
    return fresh


def _confirm_google_play_subscription(
    req: GooglePlayPurchaseConfirmReq,
    authorization: str | None,
) -> dict[str, Any]:
    if _field(req, "is_subscription", "isSubscription") is False:
        raise HTTPException(status_code=400, detail="google_play_subscription_required")

    package_name = _clean(_field(req, "package_name", "packageName"))
    if package_name and package_name != PACKAGE_NAME:
        raise HTTPException(status_code=400, detail="google_play_package_mismatch")

    user_id = _resolve_request_user_id(req.user_id, authorization)
    product_id = _clean_lower(_field(req, "product_id", "productId"))
    purchase_token = _clean(_field(req, "purchase_token", "purchaseToken"))
    base_plan_id = _clean_lower(_field(req, "base_plan_id", "basePlanId"))

    if not product_id:
        raise HTTPException(status_code=400, detail="product_id_required")
    if not purchase_token:
        raise HTTPException(status_code=400, detail="purchase_token_required")

    profile = _profile_or_404(user_id)
    verification = _verify_subscription_v2(product_id, purchase_token, base_plan_id)
    already_bound_to_user = _bind_purchase_token(
        purchase_token,
        user_id,
        verification["canonical_product_id"],
        profile,
    )

    fresh = _update_profile_entitlement(user_id, profile, verification, purchase_token)
    active = bool(verification["active"])
    if not active:
        raise HTTPException(status_code=400, detail="google_play_subscription_not_active")

    return {
        "ok": True,
        "source": "google_play",
        "already_processed": already_bound_to_user,
        "membership_status": fresh.get("membership_status"),
        "membership_source": fresh.get("membership_source"),
        "membership_product_id": fresh.get("membership_product_id"),
        "membership_started_at": fresh.get("membership_started_at"),
        "membership_ends_at": fresh.get("membership_ends_at"),
        "membership_last_checked_at": fresh.get("membership_last_checked_at"),
        "package_active": fresh.get("package_active"),
        "selected_package_code": fresh.get("selected_package_code"),
        "package_started_at": fresh.get("package_started_at"),
        "package_ends_at": fresh.get("package_ends_at"),
        "plan": fresh.get("plan"),
        "app_access_mode": fresh.get("app_access_mode"),
        "google_product_id": verification.get("google_product_id"),
        "google_base_plan_id": verification.get("base_plan_id"),
        "google_subscription_state": verification.get("subscription_state"),
        "google_acknowledgement_state": verification.get("acknowledgement_state"),
        "google_order_id": verification.get("order_id") or _clean(_field(req, "order_id", "orderId")),
    }


def _handle_confirm_error(exc: HTTPException) -> JSONResponse:
    detail = str(exc.detail or "purchase_not_verified")
    if detail == "purchase_token_already_bound_to_other_user":
        return _json_error(409, detail, "Bu satın alma başka bir kullanıcıya bağlı görünüyor.")
    if detail in {"profile_not_found", "google_play_subscription_required"}:
        return _json_error(exc.status_code or 400, detail, "Üyelik etkinleştirilemedi.")
    return _json_error(exc.status_code or 400, "purchase_not_verified")


@router.post("/api/billing/google/subscription/confirm")
def google_billing_subscription_confirm_override(
    req: GooglePlayPurchaseConfirmReq,
    authorization: str | None = Header(default=None),
):
    try:
        return _confirm_google_play_subscription(req, authorization)
    except HTTPException as exc:
        return _handle_confirm_error(exc)


@router.post("/api/google-play/verify-purchase")
def google_play_verify_purchase(
    req: GooglePlayPurchaseConfirmReq,
    authorization: str | None = Header(default=None),
):
    try:
        return _confirm_google_play_subscription(req, authorization)
    except HTTPException as exc:
        return _handle_confirm_error(exc)
