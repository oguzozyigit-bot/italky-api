from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.routers.session import get_access_state, get_current_user_id, supabase

router = APIRouter(prefix="/api/ios-iap", tags=["ios-iap"])

ALLOWED_IOS_PRODUCT_IDS = {
    "com.ozyigits.italkyai.premium.weekly",
    "com.ozyigits.italkyai.premium.monthly",
    "com.ozyigits.italkyai.premium.quarterly",
    "com.ozyigits.italkyai.premium.halfyear",
    "com.ozyigits.italkyai.premium.yearly",
}

IOS_DAY_PRODUCT_DURATIONS = {
    "italky_ios_7gun": 7,
    "italky_ios_30gun": 30,
    "italky_ios_90gun": 90,
    "italky_ios_180gun": 180,
    "italky_ios_365gun": 365,
}

ALLOWED_SOURCES = {"ios_iap", "ios_iap_days"}


class IOSIAPConfirmPayload(BaseModel):
    productId: str
    days: int | None = None
    transactionId: str | None = None
    receipt: str | None = None
    appAccountToken: str | None = None
    expirationDate: str | None = None
    platform: str | None = None
    source: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_expiration(value: str | None) -> str | None:
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_expirationDate") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.isoformat()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def _max_active_base(*values) -> datetime:
    now = datetime.now(timezone.utc)
    future_values = [dt for dt in (_parse_dt(value) for value in values) if dt and dt > now]
    if future_values:
        return max(future_values)
    return now


def _update_optional_profile_column(user_id: str, column: str, value) -> None:
    try:
        supabase.table("profiles").update({column: value}).eq("id", user_id).execute()
    except Exception as exc:  # pragma: no cover - depends on deployed schema
        print(f"[ios-iap] optional profile column skipped {column}: {exc}")


@router.post("/confirm")
def confirm_ios_iap_purchase(
    payload: IOSIAPConfirmPayload,
    authorization: str | None = Header(default=None),
):
    user_id = get_current_user_id(authorization)
    product_id = (payload.productId or "").strip()
    source = (payload.source or "").strip()

    if source not in ALLOWED_SOURCES:
        raise HTTPException(status_code=400, detail="invalid_source")

    if source == "ios_iap_days":
        duration_days = IOS_DAY_PRODUCT_DURATIONS.get(product_id)
        if not duration_days:
            raise HTTPException(status_code=400, detail="invalid_product_id")
    elif product_id not in ALLOWED_IOS_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="invalid_product_id")
    else:
        duration_days = 0

    expires_at = _normalize_expiration(payload.expirationDate)
    if source == "ios_iap_days":
        try:
            profile_res = (
                supabase.table("profiles")
                .select("package_ends_at,membership_ends_at")
                .eq("id", user_id)
                .maybe_single()
                .execute()
            )
            profile = profile_res.data or {}
        except Exception as exc:
            print(f"[ios-iap] profile read failed user_id={user_id}: {exc}")
            raise HTTPException(status_code=500, detail="membership_read_failed") from exc

        base_dt = _max_active_base(profile.get("package_ends_at"), profile.get("membership_ends_at"))
        expires_at = (base_dt + timedelta(days=duration_days)).isoformat()

    checked_at = _utc_now_iso()

    required_update = {
        "membership_status": "active",
        "membership_source": "ios_iap",
        "membership_product_id": product_id,
        "membership_last_checked_at": checked_at,
    }
    if expires_at:
        required_update["membership_ends_at"] = expires_at

    try:
        supabase.table("profiles").update(required_update).eq("id", user_id).execute()
    except Exception as exc:
        print(f"[ios-iap] profile update failed user_id={user_id}: {exc}")
        raise HTTPException(status_code=500, detail="membership_update_failed") from exc

    optional_updates = {
        "subscription_active": True,
        "has_active_membership": True,
        "is_member": True,
        "ads_disabled": True,
        "no_ads": True,
        "package_active": True,
        "selected_package_code": product_id,
    }
    if expires_at:
        optional_updates["package_ends_at"] = expires_at
    if payload.transactionId:
        optional_updates["ios_iap_transaction_id"] = payload.transactionId

    for column, value in optional_updates.items():
        _update_optional_profile_column(user_id, column, value)

    access = get_access_state(authorization)
    return {"ok": True, "access": access}
