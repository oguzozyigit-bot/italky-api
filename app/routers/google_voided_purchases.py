from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

from fastapi import APIRouter, Header, HTTPException, Query
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from app.routers.session import supabase
from app.services.store_purchases import revoke_purchase_entitlement

router = APIRouter(prefix="/api/cron/google", tags=["google-voided-purchases"])

GOOGLE_PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
MAX_WINDOW_HOURS = 24 * 30
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


def _load_google_credentials():
    raw_json = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        try:
            info = json.loads(raw_json)
            return service_account.Credentials.from_service_account_info(
                info,
                scopes=[GOOGLE_PLAY_SCOPE],
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error": "google_credentials_invalid", "message": str(exc)},
            ) from exc
    return None


def _google_access_token() -> str | None:
    credentials = _load_google_credentials()
    if not credentials:
        return None
    try:
        credentials.refresh(GoogleAuthRequest())
        return str(credentials.token or "").strip() or None
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "google_auth_failed", "message": str(exc)},
        ) from exc


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


def _fetch_voided_purchases(package_name: str, access_token: str, hours: int) -> list[dict[str, Any]]:
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
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "google_voided_api_error",
                    "google_status": response.status_code,
                    "google_body": response.text[:600],
                },
            )
        data = response.json() or {}
        purchases.extend(data.get("voidedPurchases") or [])
        next_token = str((data.get("tokenPagination") or {}).get("nextPageToken") or "").strip()
        if not next_token:
            break
    return purchases


@router.get("/voided-purchases")
def google_voided_purchases_cron(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
    hours: int = Query(default=DEFAULT_WINDOW_HOURS, ge=1, le=MAX_WINDOW_HOURS),
):
    _require_cron_secret(x_cron_secret)

    package_name = _package_name()
    if not package_name:
        return {"ok": False, "error": "google_package_name_not_configured"}

    access_token = _google_access_token()
    if not access_token:
        return {"ok": False, "error": "google_credentials_not_configured"}

    voided_purchases = _fetch_voided_purchases(package_name, access_token, hours)
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

    return {
        "ok": True,
        "checked": checked,
        "matched": matched,
        "revoked": revoked,
        "skipped": skipped,
        "unmatched": unmatched,
        "window_hours": hours,
    }
