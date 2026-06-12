from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from fastapi import APIRouter, Header, HTTPException, Query
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from app.routers.session import supabase
from app.services.store_purchases import (
    days_for_android_product,
    insert_store_purchase,
    revoke_purchase_entitlement,
)

router = APIRouter(prefix="/api/cron/google", tags=["google-voided-purchases"])
logger = logging.getLogger(__name__)

GOOGLE_PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
MAX_WINDOW_HOURS = 719
DEFAULT_WINDOW_HOURS = 24
MAX_RESULTS = 1000


def _require_cron_secret(x_cron_secret: str | None) -> None:
    expected = (
        os.getenv("GOOGLE_VOIDED_PURCHASES_CRON_SECRET", "").strip()
        or os.getenv("CRON_SECRET", "").strip()
    )
    if not expected:
        raise HTTPException(status_code=503, detail="cron_secret_not_configured")
    if not x_cron_secret or x_cron_secret.strip() != expected:
        raise HTTPException(status_code=403, detail="forbidden")


def _package_name() -> str:
    return os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "").strip()


def _normalize_window_hours(hours: int | None) -> int:
    try:
        value = int(hours or 0)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_HOURS
    if value <= 0:
        return DEFAULT_WINDOW_HOURS
    return min(value, MAX_WINDOW_HOURS)


def _credential_source() -> tuple[str | None, str]:
    raw_json = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if raw_json:
        return raw_json, "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON"
    if credentials_file:
        return credentials_file, "GOOGLE_APPLICATION_CREDENTIALS"
    return None, "none"


def _load_google_credentials():
    credential_value, source = _credential_source()
    if not credential_value:
        return None, {"ok": False, "error": "google_credentials_not_configured"}

    if source == "GOOGLE_APPLICATION_CREDENTIALS":
        path = Path(credential_value)
        exists = path.exists()
        logger.info(
            "google_voided_purchases credential file check source=%s path=%s exists=%s",
            source,
            credential_value,
            exists,
        )
        if not exists:
            return None, {
                "ok": False,
                "error": "google_credentials_file_not_found",
                "path": credential_value,
            }
        try:
            return service_account.Credentials.from_service_account_file(
                credential_value,
                scopes=[GOOGLE_PLAY_SCOPE],
            ), None
        except Exception as exc:
            logger.exception(
                "google_voided_purchases credential file parse failed type=%s message=%s",
                type(exc).__name__,
                exc,
            )
            return None, {
                "ok": False,
                "error": "google_credentials_parse_error",
                "message": str(exc),
            }

    try:
        info = json.loads(credential_value)
        return service_account.Credentials.from_service_account_info(
            info,
            scopes=[GOOGLE_PLAY_SCOPE],
        ), None
    except Exception as exc:
        logger.exception(
            "google_voided_purchases credential json parse failed type=%s message=%s",
            type(exc).__name__,
            exc,
        )
        return None, {
            "ok": False,
            "error": "google_credentials_parse_error",
            "message": str(exc),
        }


def _google_access_token() -> tuple[str | None, dict[str, Any] | None]:
    credentials, error = _load_google_credentials()
    if error:
        return None, error
    try:
        credentials.refresh(GoogleAuthRequest())
        token = str(credentials.token or "").strip()
        if not token:
            return None, {"ok": False, "error": "google_auth_error", "message": "empty_access_token"}
        return token, None
    except Exception as exc:
        logger.exception(
            "google_voided_purchases auth failed type=%s message=%s",
            type(exc).__name__,
            exc,
        )
        return None, {"ok": False, "error": "google_auth_error", "message": str(exc)}


def _safe_data(result: Any) -> list[dict[str, Any]]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return data or []


def _parse_ms(value: Any) -> datetime | None:
    if value in ("", None):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    except Exception:
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mask_order_id(order_id: str) -> str:
    clean = str(order_id or "").strip()
    if not clean:
        return ""
    parts = clean.split("-")
    if clean.startswith("GPA.") and len(parts) >= 3:
        return f"{parts[0][:4]}XXXX-XXXX-{parts[-1]}"
    if len(clean) <= 8:
        return clean
    return f"{clean[:4]}...{clean[-4:]}"


def _debug_unmatched_item(item: dict[str, Any]) -> dict[str, Any]:
    purchase_token = str(item.get("purchaseToken") or "").strip()
    voided_time = _parse_ms(item.get("voidedTimeMillis"))
    return {
        "order_id": _mask_order_id(str(item.get("orderId") or "")),
        "purchase_token_tail": purchase_token[-8:] if purchase_token else "",
        "product_id": str(item.get("productId") or "").strip(),
        "voided_time": voided_time.isoformat() if voided_time else str(item.get("voidedTimeMillis") or ""),
        "voided_reason": str(item.get("voidedReason") or "").strip(),
    }


def _find_android_purchase_by_token(purchase_token: str) -> dict[str, Any] | None:
    clean = str(purchase_token or "").strip()
    if not clean:
        return None
    result = (
        supabase.table("store_purchases")
        .select("*")
        .eq("platform", "android")
        .eq("purchase_token", clean)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result)
    return rows[0] if rows else None


def _find_android_purchase_by_order_id(order_id: str) -> dict[str, Any] | None:
    clean = str(order_id or "").strip()
    if not clean:
        return None
    result = (
        supabase.table("store_purchases")
        .select("*")
        .eq("platform", "android")
        .eq("order_id", clean)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result)
    return rows[0] if rows else None


def _find_store_purchase(purchase_token: str, order_id: str) -> dict[str, Any] | None:
    return _find_android_purchase_by_token(purchase_token) or _find_android_purchase_by_order_id(order_id)


def _lookup_table_by_field(table: str, field: str, value: str) -> dict[str, Any] | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        result = supabase.table(table).select("*").eq(field, clean).limit(1).execute()
    except Exception as exc:
        logger.warning(
            "google_voided_purchases legacy lookup failed table=%s field=%s type=%s message=%s",
            table,
            field,
            type(exc).__name__,
            exc,
        )
        return None
    rows = _safe_data(result)
    return rows[0] if rows else None


def _find_access_duration_event(purchase_token: str, order_id: str) -> dict[str, Any] | None:
    event = _lookup_table_by_field("access_duration_events", "source_ref", purchase_token)
    if event:
        return event
    return _lookup_table_by_field("access_duration_events", "source_ref", order_id)


def _find_legacy_purchase(purchase_token: str, order_id: str) -> tuple[dict[str, Any] | None, str | None]:
    lookups = [
        ("google_play_inapp_purchases", "purchase_token", purchase_token),
        ("google_play_inapp_purchases", "order_id", order_id),
        ("billing_purchases", "purchase_token", purchase_token),
        ("billing_purchases", "order_id", order_id),
    ]
    for table, field, value in lookups:
        row = _lookup_table_by_field(table, field, value)
        if row:
            return row, table
    event = _find_access_duration_event(purchase_token, order_id)
    if event:
        return event, "access_duration_events"
    return None, None


def _legacy_product_id(row: dict[str, Any], item: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return str(
        row.get("product_id")
        or metadata.get("product_id")
        or item.get("productId")
        or "unknown_google_product"
    ).strip()


def _legacy_purchase_token(row: dict[str, Any], item: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return str(row.get("purchase_token") or metadata.get("purchase_token") or item.get("purchaseToken") or "").strip()


def _legacy_order_id(row: dict[str, Any], item: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return str(row.get("order_id") or metadata.get("order_id") or item.get("orderId") or "").strip()


def _legacy_granted_days(row: dict[str, Any], product_id: str) -> int:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    explicit_days = (
        row.get("granted_days")
        or row.get("days_added")
        or row.get("days")
        or metadata.get("granted_days")
        or metadata.get("days_added")
        or metadata.get("days")
    )
    days = _int_or_zero(explicit_days)
    if days > 0:
        return days
    return int(days_for_android_product(product_id) or 0)


def _legacy_purchase_time(row: dict[str, Any], item: dict[str, Any]) -> datetime | str | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return (
        row.get("purchase_time")
        or row.get("created_at")
        or metadata.get("purchase_time")
        or _parse_ms(item.get("purchaseTimeMillis"))
    )


def _legacy_entitlement_start(row: dict[str, Any]) -> datetime | str | None:
    return row.get("entitlement_start") or row.get("previous_ends_at") or row.get("created_at")


def _legacy_entitlement_end(row: dict[str, Any]) -> datetime | str | None:
    return row.get("entitlement_end") or row.get("new_ends_at") or row.get("created_at")


def _backfill_store_purchase_from_legacy(
    row: dict[str, Any],
    source_table: str,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    product_id = _legacy_product_id(row, item)
    purchase_token = _legacy_purchase_token(row, item)
    order_id = _legacy_order_id(row, item)
    existing = _find_store_purchase(purchase_token, order_id)
    if existing:
        return existing

    user_id = str(row.get("user_id") or "").strip()
    granted_days = _legacy_granted_days(row, product_id)
    if not user_id:
        logger.warning("google_voided_purchases legacy backfill skipped missing user_id table=%s", source_table)
        return None

    raw_payload = {
        "backfilled_from": source_table,
        "legacy_purchase_id": row.get("id"),
        "voided_purchase": item,
    }
    try:
        return insert_store_purchase(
            supabase,
            user_id=user_id,
            platform="android",
            product_id=product_id,
            purchase_token=purchase_token,
            order_id=order_id,
            purchase_time=_legacy_purchase_time(row, item),
            granted_days=granted_days,
            entitlement_start=_legacy_entitlement_start(row),
            entitlement_end=_legacy_entitlement_end(row),
            raw_payload=raw_payload,
        )
    except Exception as exc:
        logger.warning(
            "google_voided_purchases store_purchase backfill insert failed table=%s type=%s message=%s",
            source_table,
            type(exc).__name__,
            exc,
        )
        return _find_store_purchase(purchase_token, order_id)


def _fetch_voided_purchases(
    package_name: str,
    access_token: str,
    hours: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    now = datetime.now(timezone.utc)
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - timedelta(hours=hours)).timestamp() * 1000)
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        f"{quote(package_name, safe='')}/purchases/voidedpurchases"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict[str, Any] = {
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "type": 1,
        "includeQuantityBasedPartialRefund": "true",
        "maxResults": MAX_RESULTS,
    }
    purchases: list[dict[str, Any]] = []
    next_token = ""
    while True:
        if next_token:
            params["token"] = next_token
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
            if not response.ok:
                message = response.text[:600]
                logger.error(
                    "google_voided_purchases api error status=%s message=%s",
                    response.status_code,
                    message,
                )
                return [], {
                    "ok": False,
                    "error": "google_voided_api_error",
                    "status": response.status_code,
                    "message": message,
                }
            data = response.json() or {}
        except Exception as exc:
            logger.exception(
                "google_voided_purchases api request failed type=%s message=%s",
                type(exc).__name__,
                exc,
            )
            return [], {"ok": False, "error": "google_voided_api_error", "message": str(exc)}
        purchases.extend(data.get("voidedPurchases") or [])
        next_token = str((data.get("tokenPagination") or {}).get("nextPageToken") or "").strip()
        if not next_token:
            break
    return purchases, None


@router.get("/voided-purchases")
def google_voided_purchases_cron(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
    hours: int = Query(default=DEFAULT_WINDOW_HOURS),
    debug: bool = Query(default=False),
):
    _require_cron_secret(x_cron_secret)

    try:
        window_hours = _normalize_window_hours(hours)
        package_name = _package_name()
        credential_value, credential_source = _credential_source()
        credential_path_exists = None
        if credential_source == "GOOGLE_APPLICATION_CREDENTIALS" and credential_value:
            credential_path_exists = Path(credential_value).exists()
        logger.info(
            "google_voided_purchases start package_name=%s credential_source=%s credential_file_exists=%s hours=%s",
            package_name or "(missing)",
            credential_source,
            credential_path_exists,
            window_hours,
        )

        if not package_name:
            return {"ok": False, "error": "google_package_name_not_configured"}

        access_token, error = _google_access_token()
        if error:
            return error
        if not access_token:
            return {"ok": False, "error": "google_auth_error", "message": "empty_access_token"}

        voided_purchases, error = _fetch_voided_purchases(package_name, access_token, window_hours)
        if error:
            return error

        checked = len(voided_purchases)
        matched = 0
        backfilled = 0
        revoked = 0
        skipped = 0
        unmatched = 0
        unmatched_items: list[dict[str, Any]] = []

        for item in voided_purchases:
            purchase_token = str(item.get("purchaseToken") or "").strip()
            order_id = str(item.get("orderId") or "").strip()
            purchase = _find_store_purchase(purchase_token, order_id)
            existing_store_match = bool(purchase)
            revoke_reason = "google_voided_purchase"
            if not purchase:
                legacy_purchase, source_table = _find_legacy_purchase(purchase_token, order_id)
                if legacy_purchase and source_table:
                    purchase = _backfill_store_purchase_from_legacy(legacy_purchase, source_table, item)
                    if purchase:
                        backfilled += 1
                        revoke_reason = "google_voided_purchase_backfill"
                if not purchase:
                    unmatched += 1
                    if debug:
                        unmatched_items.append(_debug_unmatched_item(item))
                    continue

            if existing_store_match:
                matched += 1
            purchase_status = str(purchase.get("status") or "").strip().lower()
            if purchase_status != "active":
                skipped += 1
                continue

            result = revoke_purchase_entitlement(
                supabase,
                purchase_id=purchase["id"],
                reason=revoke_reason,
                new_status="voided",
                raw_payload=item,
            )
            if result.get("skipped"):
                skipped += 1
            elif result.get("ok"):
                revoked += 1
            else:
                skipped += 1
    except Exception as exc:
        logger.exception(
            "google_voided_purchases unexpected failure type=%s message=%s",
            type(exc).__name__,
            exc,
        )
        return {"ok": False, "error": "google_voided_api_error", "message": str(exc)}

    response = {
        "ok": True,
        "checked": checked,
        "matched": matched,
        "backfilled": backfilled,
        "revoked": revoked,
        "skipped": skipped,
        "unmatched": unmatched,
        "window_hours": window_hours,
    }
    if debug:
        response["unmatched_items"] = unmatched_items
    return response
