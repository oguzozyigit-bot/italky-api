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
from app.services.store_purchases import revoke_purchase_entitlement

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
        revoked = 0
        skipped = 0
        unmatched = 0

        for item in voided_purchases:
            purchase_token = str(item.get("purchaseToken") or "").strip()
            order_id = str(item.get("orderId") or "").strip()
            purchase = _find_android_purchase_by_token(purchase_token)
            if not purchase:
                purchase = _find_android_purchase_by_order_id(order_id)
            if not purchase:
                unmatched += 1
                continue

            matched += 1
            purchase_status = str(purchase.get("status") or "").strip().lower()
            if purchase_status != "active":
                skipped += 1
                continue

            result = revoke_purchase_entitlement(
                supabase,
                purchase_id=purchase["id"],
                reason="google_voided_purchase",
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

    return {
        "ok": True,
        "checked": checked,
        "matched": matched,
        "revoked": revoked,
        "skipped": skipped,
        "unmatched": unmatched,
        "window_hours": window_hours,
    }
