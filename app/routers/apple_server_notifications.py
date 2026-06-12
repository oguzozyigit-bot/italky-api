from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query

from app.routers.session import supabase
from app.services.store_purchases import insert_purchase_audit_log, revoke_purchase_entitlement

router = APIRouter(prefix="/api/webhooks/apple", tags=["apple-server-notifications"])
logger = logging.getLogger(__name__)

REVOKE_NOTIFICATION_TYPES = {"REFUND", "REVOKE"}
LOG_ONLY_NOTIFICATION_TYPES = {"CONSUMPTION_REQUEST", "REFUND_DECLINED", "REFUND_REVERSED", "TEST"}


def _safe_data(result: Any) -> list[dict[str, Any]]:
    data = getattr(result, "data", None)
    if data is None and isinstance(result, dict):
        data = result.get("data")
    return data or []


def _b64url_decode(value: str) -> bytes:
    padded = value + ("=" * (-len(value) % 4))
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _decode_jws_payload(signed_payload: str) -> dict[str, Any]:
    parts = str(signed_payload or "").split(".")
    if len(parts) != 3:
        raise ValueError("invalid_jws_format")
    payload = _b64url_decode(parts[1])
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("invalid_jws_payload")
    return decoded


def _expected_bundle_id() -> str:
    return os.getenv("APPLE_BUNDLE_ID", "").strip()


def _expected_app_apple_id() -> str:
    return os.getenv("APPLE_APP_APPLE_ID", "").strip()


def _shared_secret() -> str:
    return os.getenv("APPLE_NOTIFICATION_SHARED_SECRET", "").strip()


def _mask_id(value: str) -> str:
    clean = str(value or "").strip()
    if len(clean) <= 8:
        return clean
    return f"{clean[:4]}...{clean[-4:]}"


def _parse_apple_ms(value: Any) -> datetime | None:
    if value in ("", None):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    except Exception:
        return None


def _validate_bundle_and_app(notification: dict[str, Any], transaction: dict[str, Any]) -> None:
    expected_bundle_id = _expected_bundle_id()
    if not expected_bundle_id:
        logger.error("apple server notification rejected: APPLE_BUNDLE_ID is not configured")
        raise HTTPException(status_code=503, detail="apple_bundle_id_not_configured")

    data = notification.get("data") if isinstance(notification.get("data"), dict) else {}
    bundle_id = str(data.get("bundleId") or transaction.get("bundleId") or "").strip()
    if bundle_id != expected_bundle_id:
        logger.warning(
            "apple server notification bundle mismatch expected=%s got=%s",
            expected_bundle_id,
            bundle_id or "(missing)",
        )
        raise HTTPException(status_code=403, detail="apple_bundle_id_mismatch")

    expected_app_id = _expected_app_apple_id()
    if expected_app_id:
        app_apple_id = str(data.get("appAppleId") or "").strip()
        if app_apple_id and app_apple_id != expected_app_id:
            logger.warning(
                "apple server notification app id mismatch expected=%s got=%s",
                expected_app_id,
                app_apple_id,
            )
            raise HTTPException(status_code=403, detail="apple_app_apple_id_mismatch")


def _validate_optional_shared_secret(x_apple_notification_secret: str | None) -> None:
    expected = _shared_secret()
    if expected and str(x_apple_notification_secret or "").strip() != expected:
        raise HTTPException(status_code=403, detail="apple_notification_secret_mismatch")


def _find_ios_purchase_by_transaction(transaction_id: str) -> dict[str, Any] | None:
    clean = str(transaction_id or "").strip()
    if not clean:
        return None
    result = (
        supabase.table("store_purchases")
        .select("*")
        .eq("platform", "ios")
        .eq("transaction_id", clean)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result)
    return rows[0] if rows else None


def _find_ios_purchase_by_original_transaction(original_transaction_id: str) -> dict[str, Any] | None:
    clean = str(original_transaction_id or "").strip()
    if not clean:
        return None
    result = (
        supabase.table("store_purchases")
        .select("*")
        .eq("platform", "ios")
        .eq("original_transaction_id", clean)
        .limit(1)
        .execute()
    )
    rows = _safe_data(result)
    return rows[0] if rows else None


def _find_ios_purchase_by_raw_payload(transaction_id: str) -> dict[str, Any] | None:
    clean = str(transaction_id or "").strip()
    if not clean:
        return None
    try:
        result = (
            supabase.table("store_purchases")
            .select("*")
            .eq("platform", "ios")
            .contains("raw_payload", {"transactionId": clean})
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.info("apple server notification raw payload lookup skipped: %s", exc)
        return None
    rows = _safe_data(result)
    return rows[0] if rows else None


def _find_ios_purchase(transaction_id: str, original_transaction_id: str) -> dict[str, Any] | None:
    return (
        _find_ios_purchase_by_transaction(transaction_id)
        or _find_ios_purchase_by_original_transaction(original_transaction_id)
        or _find_ios_purchase_by_raw_payload(transaction_id)
    )


def _audit_log_only(
    purchase: dict[str, Any] | None,
    *,
    action: str,
    reason: str,
    raw_payload: dict[str, Any],
) -> None:
    if not purchase:
        logger.info("apple server notification log_only unmatched reason=%s", reason)
        return
    insert_purchase_audit_log(
        supabase,
        purchase_id=purchase.get("id"),
        user_id=str(purchase.get("user_id") or ""),
        platform="ios",
        action=action,
        reason=reason,
        old_status=str(purchase.get("status") or ""),
        new_status=str(purchase.get("status") or ""),
        old_entitlement_end=purchase.get("entitlement_end"),
        new_entitlement_end=purchase.get("entitlement_end"),
        raw_payload=raw_payload,
    )


def _apply_revocation_time(purchase_id: str, new_status: str, revocation_date: datetime | None) -> None:
    if not revocation_date:
        return
    field = "refund_time" if new_status == "refunded" else "voided_time"
    try:
        supabase.table("store_purchases").update({field: revocation_date.isoformat()}).eq("id", purchase_id).execute()
    except Exception as exc:
        logger.warning("apple server notification revocation time update failed: %s", exc)


@router.post("/server-notifications")
def apple_server_notifications(
    body: dict[str, Any] | None = Body(default=None),
    debug: bool = Query(default=False),
    x_apple_notification_secret: str | None = Header(default=None, alias="X-Apple-Notification-Secret"),
):
    _validate_optional_shared_secret(x_apple_notification_secret)

    payload_body = body or {}
    signed_payload = str(payload_body.get("signedPayload") or "").strip()
    if not signed_payload:
        raise HTTPException(status_code=400, detail="signedPayload_required")

    try:
        notification = _decode_jws_payload(signed_payload)
    except Exception as exc:
        logger.warning("apple server notification decode failed type=%s message=%s", type(exc).__name__, exc)
        raise HTTPException(status_code=400, detail="invalid_signedPayload") from exc

    data = notification.get("data") if isinstance(notification.get("data"), dict) else {}
    signed_transaction_info = str(data.get("signedTransactionInfo") or "").strip()
    transaction: dict[str, Any] = {}
    if signed_transaction_info:
        try:
            transaction = _decode_jws_payload(signed_transaction_info)
        except Exception as exc:
            logger.warning("apple transaction info decode failed type=%s message=%s", type(exc).__name__, exc)
            raise HTTPException(status_code=400, detail="invalid_signedTransactionInfo") from exc

    _validate_bundle_and_app(notification, transaction)

    notification_type = str(notification.get("notificationType") or "").strip()
    subtype = str(notification.get("subtype") or "").strip()
    notification_uuid = str(notification.get("notificationUUID") or "").strip()
    transaction_id = str(transaction.get("transactionId") or "").strip()
    original_transaction_id = str(transaction.get("originalTransactionId") or "").strip()
    purchase = _find_ios_purchase(transaction_id, original_transaction_id)

    raw_payload = {
        "apple_notification_uuid": notification_uuid,
        "notification": notification,
        "transaction": transaction,
    }
    logger.info(
        "apple server notification received type=%s subtype=%s uuid=%s transaction=%s original=%s matched=%s",
        notification_type,
        subtype,
        notification_uuid or "(missing)",
        _mask_id(transaction_id),
        _mask_id(original_transaction_id),
        bool(purchase),
    )

    revoked = False
    skipped = False
    matched = bool(purchase)

    if notification_type in REVOKE_NOTIFICATION_TYPES:
        if purchase:
            new_status = "refunded" if notification_type == "REFUND" else "revoked"
            reason = "apple_refund" if notification_type == "REFUND" else "apple_revoke"
            result = revoke_purchase_entitlement(
                supabase,
                purchase_id=purchase["id"],
                reason=reason,
                new_status=new_status,
                raw_payload=raw_payload,
            )
            revoked = bool(result.get("ok")) and not bool(result.get("skipped"))
            skipped = bool(result.get("skipped")) or not bool(result.get("ok"))
            _apply_revocation_time(purchase["id"], new_status, _parse_apple_ms(transaction.get("revocationDate")))
        else:
            logger.info(
                "apple server notification unmatched revoke type=%s transaction=%s original=%s",
                notification_type,
                _mask_id(transaction_id),
                _mask_id(original_transaction_id),
            )
    elif notification_type in LOG_ONLY_NOTIFICATION_TYPES:
        _audit_log_only(
            purchase,
            action="apple_notification_log_only",
            reason=f"apple_{notification_type.lower()}",
            raw_payload=raw_payload,
        )
    else:
        logger.info("apple server notification ignored type=%s subtype=%s", notification_type, subtype)

    if debug:
        return {
            "ok": True,
            "notificationType": notification_type,
            "subtype": subtype,
            "transactionId": _mask_id(transaction_id),
            "originalTransactionId": _mask_id(original_transaction_id),
            "matched": matched,
            "revoked": revoked,
            "skipped": skipped,
        }
    return {"ok": True}
