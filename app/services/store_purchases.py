from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

IOS_DAY_PRODUCTS = {
    "italky_ios_7gun": 7,
    "italky_ios_30gun": 30,
    "italky_ios_90gun": 90,
    "italky_ios_180gun": 180,
    "italky_ios_365gun": 365,
}

ANDROID_DAY_PRODUCTS = {
    "italky_7gun": 7,
    "italky_30gun": 30,
    "italky_90gun": 90,
    "italky_180gun": 180,
    "italky_365gun": 365,
}


def normalize_transaction_id(value: Any) -> str:
    return str(value or "").strip()


def normalize_purchase_token(value: Any) -> str:
    return str(value or "").strip()


def days_for_ios_product(product_id: str) -> int | None:
    return IOS_DAY_PRODUCTS.get(str(product_id or "").strip())


def days_for_android_product(product_id: str) -> int | None:
    return ANDROID_DAY_PRODUCTS.get(str(product_id or "").strip().lower())


def _safe_data(result: Any) -> list[dict[str, Any]]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return data or []


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


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def find_store_purchase_by_ios_transaction(supabase: Any, transaction_id: str) -> dict[str, Any] | None:
    clean_transaction_id = normalize_transaction_id(transaction_id)
    if not clean_transaction_id:
        return None
    try:
        result = (
            supabase.table("store_purchases")
            .select("*")
            .eq("platform", "ios")
            .eq("transaction_id", clean_transaction_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.warning("store_purchases ios lookup failed: %s", exc)
        raise
    rows = _safe_data(result)
    return rows[0] if rows else None


def find_store_purchase_by_android_token(supabase: Any, purchase_token: str) -> dict[str, Any] | None:
    clean_purchase_token = normalize_purchase_token(purchase_token)
    if not clean_purchase_token:
        return None
    try:
        result = (
            supabase.table("store_purchases")
            .select("*")
            .eq("platform", "android")
            .eq("purchase_token", clean_purchase_token)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.warning("store_purchases android lookup failed: %s", exc)
        raise
    rows = _safe_data(result)
    return rows[0] if rows else None


def insert_store_purchase(
    supabase: Any,
    *,
    user_id: str,
    platform: str,
    product_id: str,
    granted_days: int,
    entitlement_start: datetime | str | None = None,
    entitlement_end: datetime | str | None = None,
    transaction_id: str | None = None,
    original_transaction_id: str | None = None,
    purchase_token: str | None = None,
    order_id: str | None = None,
    purchase_time: datetime | str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    def clean_dt(value: datetime | str | None) -> str | None:
        if isinstance(value, datetime):
            return _iso(value)
        return str(value).strip() if value else None

    payload = {
        "user_id": user_id,
        "platform": platform,
        "product_id": product_id,
        "transaction_id": normalize_transaction_id(transaction_id),
        "original_transaction_id": normalize_transaction_id(original_transaction_id),
        "purchase_token": normalize_purchase_token(purchase_token),
        "order_id": str(order_id or "").strip(),
        "purchase_time": clean_dt(purchase_time),
        "granted_days": int(granted_days or 0),
        "entitlement_start": clean_dt(entitlement_start),
        "entitlement_end": clean_dt(entitlement_end),
        "status": "active",
        "raw_payload": raw_payload or {},
    }
    payload = {key: value for key, value in payload.items() if value not in ("", None)}
    try:
        result = supabase.table("store_purchases").insert(payload).execute()
    except Exception as exc:
        logger.error("store_purchases insert failed: %s", exc)
        raise
    rows = _safe_data(result)
    return rows[0] if rows else None


def insert_purchase_audit_log(
    supabase: Any,
    *,
    purchase_id: str | None,
    user_id: str,
    platform: str,
    action: str,
    reason: str | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
    old_entitlement_end: datetime | str | None = None,
    new_entitlement_end: datetime | str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    def clean_dt(value: datetime | str | None) -> str | None:
        if isinstance(value, datetime):
            return _iso(value)
        return str(value).strip() if value else None

    payload = {
        "purchase_id": purchase_id,
        "user_id": user_id,
        "platform": platform,
        "action": action,
        "reason": reason,
        "old_status": old_status,
        "new_status": new_status,
        "old_entitlement_end": clean_dt(old_entitlement_end),
        "new_entitlement_end": clean_dt(new_entitlement_end),
        "raw_payload": raw_payload or {},
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    try:
        supabase.table("purchase_audit_logs").insert(payload).execute()
    except Exception as exc:
        logger.warning("purchase_audit_logs insert failed: %s", exc)


def compute_entitlement_window(
    current_package_ends_at: Any,
    current_membership_ends_at: Any,
    granted_days: int,
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    candidates = [
        _parse_dt(current_package_ends_at),
        _parse_dt(current_membership_ends_at),
        now,
    ]
    start = max(dt for dt in candidates if dt is not None)
    end = start + timedelta(days=int(granted_days or 0))
    return start, end


def _find_store_purchase_by_id(supabase: Any, purchase_id: str) -> dict[str, Any] | None:
    clean_purchase_id = str(purchase_id or "").strip()
    if not clean_purchase_id:
        return None
    result = (
        supabase.table("store_purchases")
        .select("*")
        .eq("id", clean_purchase_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result)
    return rows[0] if rows else None


def _profile_for_revoke(supabase: Any, user_id: str) -> dict[str, Any] | None:
    result = (
        supabase.table("profiles")
        .select("id,package_active,package_ends_at,membership_status,membership_ends_at")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result)
    return rows[0] if rows else None


def _subtract_days_or_clamp(value: Any, days: int, now: datetime) -> datetime:
    current = _parse_dt(value) or now
    if days <= 0:
        return current
    updated = current - timedelta(days=days)
    return max(updated, now)


def revoke_purchase_entitlement(
    supabase: Any,
    purchase_id: str,
    reason: str,
    new_status: str,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_status = str(new_status or "").strip().lower()
    if clean_status not in {"refunded", "voided", "revoked", "cancelled"}:
        return {"ok": False, "error": "invalid_revoke_status"}

    purchase = _find_store_purchase_by_id(supabase, purchase_id)
    if not purchase:
        return {"ok": False, "error": "purchase_not_found"}

    user_id = str(purchase.get("user_id") or "").strip()
    platform = str(purchase.get("platform") or "").strip()
    old_status = str(purchase.get("status") or "").strip().lower()
    granted_days = int(purchase.get("granted_days") or 0)

    if old_status != "active":
        insert_purchase_audit_log(
            supabase,
            purchase_id=purchase.get("id"),
            user_id=user_id,
            platform=platform,
            action="revoke_skipped_already_inactive",
            reason=reason,
            old_status=old_status,
            new_status=old_status,
            old_entitlement_end=purchase.get("entitlement_end"),
            new_entitlement_end=purchase.get("entitlement_end"),
            raw_payload=raw_payload or {},
        )
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_inactive",
            "purchase_id": purchase.get("id"),
            "user_id": user_id,
            "status": old_status,
        }

    profile = _profile_for_revoke(supabase, user_id)
    if not profile:
        return {"ok": False, "error": "profile_not_found", "purchase_id": purchase.get("id")}

    now = datetime.now(timezone.utc)
    old_membership_end = _parse_dt(profile.get("membership_ends_at"))
    old_package_end = _parse_dt(profile.get("package_ends_at"))
    old_entitlement_end = max(
        [dt for dt in [old_membership_end, old_package_end, _parse_dt(purchase.get("entitlement_end"))] if dt],
        default=None,
    )

    new_membership_end = _subtract_days_or_clamp(profile.get("membership_ends_at"), granted_days, now)
    new_package_end = _subtract_days_or_clamp(profile.get("package_ends_at"), granted_days, now)
    new_entitlement_end = max(new_membership_end, new_package_end)
    should_deactivate = new_entitlement_end <= now

    profile_update = {
        "membership_ends_at": _iso(new_membership_end),
        "package_ends_at": _iso(new_package_end),
        "membership_last_checked_at": _iso(now),
    }
    if should_deactivate:
        profile_update.update(
            {
                "package_active": False,
                "membership_status": "inactive",
            }
        )
    supabase.table("profiles").update(profile_update).eq("id", user_id).execute()

    purchase_update = {
        "status": clean_status,
        "revoke_reason": reason,
        "raw_payload": raw_payload or purchase.get("raw_payload") or {},
        "updated_at": _iso(now),
    }
    if clean_status == "refunded":
        purchase_update["refund_time"] = _iso(now)
    else:
        purchase_update["voided_time"] = _iso(now)
    supabase.table("store_purchases").update(purchase_update).eq("id", purchase.get("id")).execute()

    insert_purchase_audit_log(
        supabase,
        purchase_id=purchase.get("id"),
        user_id=user_id,
        platform=platform,
        action="revoke_days",
        reason=reason,
        old_status=old_status,
        new_status=clean_status,
        old_entitlement_end=old_entitlement_end,
        new_entitlement_end=new_entitlement_end,
        raw_payload={
            "purchase_id": purchase.get("id"),
            "granted_days": granted_days,
            "raw_payload": raw_payload or {},
        },
    )

    return {
        "ok": True,
        "purchase_id": purchase.get("id"),
        "user_id": user_id,
        "old_entitlement_end": _iso(old_entitlement_end) if old_entitlement_end else None,
        "new_entitlement_end": _iso(new_entitlement_end),
        "revoked_days": max(0, granted_days),
        "status": clean_status,
    }
