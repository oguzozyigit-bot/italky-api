from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.routers.billing_google import (
    _auth_user_id,
    _clean,
    _clean_lower,
    _get_purchase_owner,
    _google_get,
    _insert_purchase_log,
    _iso,
    _now,
    _safe_data,
    _update_purchase_log,
    supabase,
)

router = APIRouter(tags=["billing-google-inapp"])

PACKAGE_NAME = (
    os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "").strip()
    or os.getenv("ANDROID_PACKAGE_NAME", "").strip()
    or "com.ozyigits.italkyai"
)

DAY_PRODUCTS = {
    "italky_7gun": 7,
    "italky_30gun": 30,
    "italky_90gun": 90,
    "italky_180gun": 180,
    "italky_365gun": 365,
}


class GoogleInAppConfirmReq(BaseModel):
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


def _field(req: GoogleInAppConfirmReq, snake: str, camel: str) -> Any:
    value = getattr(req, snake, None)
    if value is not None:
        return value
    return getattr(req, camel, None)


def _resolve_request_user_id(payload_user_id: str | None, authorization: str | None) -> str:
    auth_user_id = _auth_user_id(authorization)
    clean_payload_user_id = _clean(payload_user_id)
    if clean_payload_user_id and clean_payload_user_id != auth_user_id:
        raise HTTPException(status_code=403, detail="user_id_mismatch")
    return auth_user_id


def _profile_or_404(user_id: str) -> dict[str, Any]:
    data = _safe_data(supabase.table("profiles").select("*").eq("id", user_id).limit(1).execute())
    if not data:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return data[0] or {}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _active_base_date(profile: dict[str, Any]) -> datetime:
    now = _now()
    dates = [
        _parse_dt(profile.get("membership_ends_at")),
        _parse_dt(profile.get("package_ends_at")),
        _parse_dt(profile.get("trial_ends_at")),
        now,
    ]
    return max(dt for dt in dates if dt is not None)


def _verify_inapp_product(product_id: str, purchase_token: str) -> dict[str, Any]:
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/"
        f"applications/{PACKAGE_NAME}/purchases/products/"
        f"{product_id}/tokens/{purchase_token}"
    )
    data = _google_get(url)
    purchase_state = int(data.get("purchaseState", 1))
    if purchase_state != 0:
        raise HTTPException(status_code=400, detail="google_play_inapp_not_purchased")
    return data


@router.post("/api/billing/google/inapp/confirm")
def billing_google_inapp_confirm(req: GoogleInAppConfirmReq, authorization: str | None = Header(default=None)):
    user_id = _resolve_request_user_id(req.user_id, authorization)
    product_id = _clean_lower(_field(req, "product_id", "productId"))
    purchase_token = _clean(_field(req, "purchase_token", "purchaseToken"))
    package_name = _clean(_field(req, "package_name", "packageName"))
    product_type = _clean_lower(_field(req, "product_type", "productType"))

    if package_name and package_name != PACKAGE_NAME:
        raise HTTPException(status_code=400, detail="google_play_package_mismatch")
    if product_type and product_type != "inapp":
        raise HTTPException(status_code=400, detail="google_play_inapp_required")
    if product_id not in DAY_PRODUCTS:
        raise HTTPException(status_code=400, detail="invalid_day_product_id")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    profile = _profile_or_404(user_id)
    user_email = _clean_lower(profile.get("email"))
    days = DAY_PRODUCTS[product_id]
    existing_owner = _get_purchase_owner(purchase_token)

    if existing_owner:
        existing_user_id = _clean(existing_owner.get("user_id"))
        if existing_user_id and existing_user_id != user_id:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")
        _update_purchase_log(purchase_token, user_id, user_email, product_id, "google_play_inapp")
        fresh = _profile_or_404(user_id)
        return {
            "ok": True,
            "already_processed": True,
            "product_id": product_id,
            "days_added": 0,
            "membership_ends_at": fresh.get("membership_ends_at"),
            "package_ends_at": fresh.get("package_ends_at"),
        }

    verification = _verify_inapp_product(product_id, purchase_token)
    now = _now()
    base_date = _active_base_date(profile)
    new_end = base_date + timedelta(days=days)
    payload = {
        "plan": "member",
        "app_access_mode": "member",
        "package_active": True,
        "selected_package_code": product_id,
        "package_started_at": profile.get("package_started_at") or _iso(now),
        "package_ends_at": _iso(new_end),
        "membership_status": "active",
        "membership_source": "google_play_inapp",
        "membership_product_id": product_id,
        "membership_started_at": profile.get("membership_started_at") or _iso(now),
        "membership_ends_at": _iso(new_end),
        "membership_last_checked_at": _iso(now),
    }
    supabase.table("profiles").update(payload).eq("id", user_id).execute()
    _insert_purchase_log(user_id, user_email, product_id, days, purchase_token, "google_play_inapp")

    fresh = _profile_or_404(user_id)
    return {
        "ok": True,
        "already_processed": False,
        "product_id": product_id,
        "days_added": days,
        "base_date": _iso(base_date),
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
        "google_purchase_state": verification.get("purchaseState"),
        "google_consumption_state": verification.get("consumptionState"),
        "google_order_id": verification.get("orderId") or _clean(_field(req, "order_id", "orderId")),
    }
