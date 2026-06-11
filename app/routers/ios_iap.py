from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.routers.session import get_access_state, get_current_user_id, supabase
from app.services.store_purchases import (
    compute_entitlement_window,
    days_for_ios_product,
    find_store_purchase_by_ios_transaction,
    insert_purchase_audit_log,
    insert_store_purchase,
    normalize_transaction_id,
)

router = APIRouter(prefix="/api/ios-iap", tags=["ios-iap"])

ALLOWED_IOS_PRODUCT_IDS = {
    "com.ozyigits.italkyai.premium.weekly",
    "com.ozyigits.italkyai.premium.monthly",
    "com.ozyigits.italkyai.premium.quarterly",
    "com.ozyigits.italkyai.premium.halfyear",
    "com.ozyigits.italkyai.premium.yearly",
    "italky_ios_7gun",
    "italky_ios_30gun",
    "italky_ios_90gun",
    "italky_ios_180gun",
    "italky_ios_365gun",
}

ALLOWED_SOURCES = {"ios_iap", "ios_iap_days"}


class IOSIAPConfirmPayload(BaseModel):
    productId: str
    transactionId: str | None = None
    transaction_id: str | None = None
    transactionID: str | None = None
    receipt: str | None = None
    appAccountToken: str | None = None
    expirationDate: str | None = None
    platform: str | None = None
    source: str | None = None
    days: int | None = None


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


def _payload_dict(payload: IOSIAPConfirmPayload) -> dict:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return payload.dict()


def _transaction_id(payload: IOSIAPConfirmPayload) -> str:
    return normalize_transaction_id(
        payload.transactionId or payload.transaction_id or payload.transactionID
    )


def _safe_data(result):
    return getattr(result, "data", None) or (result.get("data") if isinstance(result, dict) else None)


def _profile_or_404(user_id: str) -> dict:
    result = (
        supabase.table("profiles")
        .select("id,package_started_at,package_ends_at,membership_started_at,membership_ends_at")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return rows[0] or {}


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

    if product_id not in ALLOWED_IOS_PRODUCT_IDS:
        raise HTTPException(status_code=400, detail="invalid_product_id")

    granted_days = days_for_ios_product(product_id)
    is_day_purchase = granted_days is not None
    transaction_id = _transaction_id(payload)
    raw_payload = _payload_dict(payload)

    if source == "ios_iap_days" and not is_day_purchase:
        raise HTTPException(status_code=400, detail="invalid_ios_day_product_id")

    if (source == "ios_iap_days" or is_day_purchase) and not transaction_id:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "missing_transaction_id"},
        )

    if transaction_id:
        try:
            existing_purchase = find_store_purchase_by_ios_transaction(supabase, transaction_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="store_purchase_lookup_failed") from exc
        if existing_purchase:
            access = get_access_state(authorization)
            return {
                "ok": True,
                "duplicate": True,
                "already_processed": True,
                "product_id": product_id,
                "transaction_id": transaction_id,
                "days_added": 0,
                "access": access,
            }

    if is_day_purchase:
        profile = _profile_or_404(user_id)
        entitlement_start, entitlement_end = compute_entitlement_window(
            profile.get("package_ends_at"),
            profile.get("membership_ends_at"),
            granted_days,
        )
        checked_at = _utc_now_iso()
        update_payload = {
            "plan": "member",
            "app_access_mode": "member",
            "package_active": True,
            "selected_package_code": product_id,
            "package_started_at": profile.get("package_started_at") or checked_at,
            "package_ends_at": entitlement_end.isoformat(),
            "membership_status": "active",
            "membership_source": source,
            "membership_product_id": product_id,
            "membership_started_at": profile.get("membership_started_at") or checked_at,
            "membership_ends_at": entitlement_end.isoformat(),
            "membership_last_checked_at": checked_at,
        }
        try:
            supabase.table("profiles").update(update_payload).eq("id", user_id).execute()
        except Exception as exc:
            print(f"[ios-iap] profile update failed user_id={user_id}: {exc}")
            raise HTTPException(status_code=500, detail="membership_update_failed") from exc

        _update_optional_profile_column(user_id, "ios_iap_transaction_id", transaction_id)

        try:
            store_purchase = insert_store_purchase(
                supabase,
                user_id=user_id,
                platform="ios",
                product_id=product_id,
                transaction_id=transaction_id,
                granted_days=granted_days,
                entitlement_start=entitlement_start,
                entitlement_end=entitlement_end,
                raw_payload=raw_payload,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="store_purchase_insert_failed") from exc

        insert_purchase_audit_log(
            supabase,
            purchase_id=(store_purchase or {}).get("id"),
            user_id=user_id,
            platform="ios",
            action="grant_days",
            reason="ios_iap_days_confirm",
            old_status=None,
            new_status="active",
            old_entitlement_end=entitlement_start,
            new_entitlement_end=entitlement_end,
            raw_payload={
                "product_id": product_id,
                "transaction_id": transaction_id,
                "granted_days": granted_days,
            },
        )

        access = get_access_state(authorization)
        return {
            "ok": True,
            "duplicate": False,
            "already_processed": False,
            "product_id": product_id,
            "transaction_id": transaction_id,
            "days_added": granted_days,
            "membership_ends_at": entitlement_end.isoformat(),
            "package_ends_at": entitlement_end.isoformat(),
            "access": access,
        }

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
    if transaction_id:
        optional_updates["ios_iap_transaction_id"] = transaction_id

    for column, value in optional_updates.items():
        _update_optional_profile_column(user_id, column, value)

    if transaction_id:
        try:
            store_purchase = insert_store_purchase(
                supabase,
                user_id=user_id,
                platform="ios",
                product_id=product_id,
                transaction_id=transaction_id,
                granted_days=0,
                entitlement_start=None,
                entitlement_end=expires_at,
                raw_payload=raw_payload,
            )
            insert_purchase_audit_log(
                supabase,
                purchase_id=(store_purchase or {}).get("id"),
                user_id=user_id,
                platform="ios",
                action="confirm_purchase",
                reason="ios_iap_confirm",
                old_status=None,
                new_status="active",
                old_entitlement_end=None,
                new_entitlement_end=expires_at,
                raw_payload={
                    "product_id": product_id,
                    "transaction_id": transaction_id,
                },
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="store_purchase_insert_failed") from exc

    access = get_access_state(authorization)
    return {"ok": True, "access": access}
