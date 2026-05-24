from __future__ import annotations

from datetime import datetime, timezone

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


class IOSIAPConfirmPayload(BaseModel):
    productId: str
    transactionId: str | None = None
    expirationDate: str | None = None
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

    if source != "ios_iap":
        raise HTTPException(status_code=400, detail="invalid_source")

    if product_id not in ALLOWED_IOS_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="invalid_product_id")

    expires_at = _normalize_expiration(payload.expirationDate)
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
