from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.routers.billing_google import (
    _auth_user_id,
    _clean,
    _clean_lower,
    _get_purchase_owner,
    _google_access_token,
    _insert_purchase_log,
    _iso,
    _now,
    _safe_data,
    _update_purchase_log,
    supabase,
)
from app.services.store_purchases import (
    find_store_purchase_by_android_token,
    insert_purchase_audit_log,
    insert_store_purchase,
    normalize_purchase_token,
)

router = APIRouter(tags=["billing-google-inapp"])
logger = logging.getLogger(__name__)

PACKAGE_NAME_ENV = (
    os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "").strip()
    or os.getenv("ANDROID_PACKAGE_NAME", "").strip()
)
PACKAGE_NAME = PACKAGE_NAME_ENV or "com.ozyigits.italkyai"

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


def _stage(stage_ctx: dict[str, str], stage: str) -> None:
    stage_ctx["stage"] = stage


def _error(status_code: int, detail: Any = None, stage: str = "unknown", reason: str | None = None) -> JSONResponse:
    if isinstance(detail, dict):
        derived_reason = _clean(detail.get("error")) or _clean(detail.get("reason"))
    else:
        derived_reason = _clean(detail)

    clean_reason = reason or derived_reason or "google_play_confirm_failed"
    safe_status = status_code or 500
    if safe_status == 502:
        safe_status = 500

    return JSONResponse(
        status_code=safe_status,
        content={
            "ok": False,
            "error": "google_play_confirm_failed",
            "reason": clean_reason,
            "detail": detail or clean_reason,
            "stage": stage,
        },
    )


def _safe_str(value: Any, fallback: str = "") -> str:
    try:
        return _clean(value)
    except Exception:
        return fallback


def _request_context(req: GoogleInAppConfirmReq, user_id: str | None = None) -> dict[str, Any]:
    purchase_token = _safe_str(_field(req, "purchase_token", "purchaseToken"))
    return {
        "product_id": _safe_str(_field(req, "product_id", "productId")),
        "package_name": _safe_str(_field(req, "package_name", "packageName")),
        "user_id": user_id or _safe_str(req.user_id),
        "purchase_token_exists": bool(purchase_token),
    }


def _log_confirm_exception(
    exc: Exception,
    req: GoogleInAppConfirmReq,
    stage_ctx: dict[str, str],
    user_id: str | None = None,
) -> None:
    ctx = _request_context(req, user_id=user_id)
    logger.error(
        "google_play_confirm_failed stage=%s exception_type=%s exception_message=%s "
        "product_id=%s package_name=%s user_id=%s purchase_token_exists=%s traceback=%s",
        stage_ctx.get("stage", "unknown"),
        type(exc).__name__,
        str(exc),
        ctx["product_id"],
        ctx["package_name"],
        ctx["user_id"],
        ctx["purchase_token_exists"],
        traceback.format_exc(),
    )


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


def _assert_mutation_ok(result: object, detail: str) -> None:
    if getattr(result, "error", None):
        raise HTTPException(status_code=500, detail=detail)
    data = getattr(result, "data", None)
    if data is None:
        raise HTTPException(status_code=500, detail=detail)


def _validate_google_credentials_env() -> None:
    if not PACKAGE_NAME_ENV:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "google_play_package_name_missing",
                "message": "GOOGLE_PLAY_PACKAGE_NAME or ANDROID_PACKAGE_NAME is required",
            },
        )

    raw_json = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw_json and not credentials_file:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "google_play_credentials_missing",
                "message": "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS is required",
            },
        )

    if raw_json and raw_json.lstrip().startswith("{"):
        try:
            json.loads(raw_json)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "google_play_credentials_invalid_json",
                    "message": str(exc),
                },
            ) from exc

    if credentials_file and not raw_json and not os.path.exists(credentials_file):
        raise HTTPException(
            status_code=500,
            detail={
                "error": "google_play_credentials_file_not_found",
                "message": "GOOGLE_APPLICATION_CREDENTIALS file was not found",
            },
        )


def _google_get_inapp(url: str) -> dict[str, Any]:
    try:
        access_token = _google_access_token()
    except HTTPException as exc:
        detail = exc.detail
        raise HTTPException(
            status_code=500,
            detail={
                "error": _clean(detail) or "google_play_auth_failed",
                "message": detail,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "google_play_auth_failed", "message": str(exc)},
        ) from exc

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, timeout=12)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "google_play_request_failed", "message": str(exc)},
        ) from exc

    if response.status_code in {401, 403}:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "google_play_permission_denied",
                "google_status": response.status_code,
                "google_body": response.text[:600],
            },
        )

    if response.status_code in {404, 410}:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "google_play_purchase_not_found",
                "google_status": response.status_code,
                "google_body": response.text[:600],
            },
        )

    if not response.ok:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "google_play_verification_failed",
                "google_status": response.status_code,
                "google_body": response.text[:600],
            },
        )

    try:
        data = response.json()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "google_play_invalid_json", "message": str(exc)},
        ) from exc

    return data or {}


def _verify_inapp_product(product_id: str, purchase_token: str) -> dict[str, Any]:
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/"
        f"applications/{PACKAGE_NAME}/purchases/products/"
        f"{product_id}/tokens/{purchase_token}"
    )
    data = _google_get_inapp(url)
    purchase_state = int(data.get("purchaseState", 1))
    if purchase_state != 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "google_play_inapp_not_purchased", "purchaseState": purchase_state},
        )
    verified_product_id = _clean_lower(data.get("productId"))
    if verified_product_id and verified_product_id != product_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "google_play_product_id_mismatch",
                "expected_product_id": product_id,
                "actual_product_id": verified_product_id,
            },
        )
    return data


def _order_id(req: GoogleInAppConfirmReq, verification: dict[str, Any] | None = None) -> str:
    return _clean((verification or {}).get("orderId")) or _clean(_field(req, "order_id", "orderId"))


def _get_inapp_purchase_owner(purchase_token: str) -> dict[str, Any] | None:
    res = (
        supabase.table("google_play_inapp_purchases")
        .select("*")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )
    rows = _safe_data(res) or []
    return rows[0] if rows else None


def _write_inapp_purchase(
    *,
    user_id: str,
    email: str,
    product_id: str,
    days: int,
    purchase_token: str,
    order_id: str,
    verification: dict[str, Any],
) -> None:
    payload = {
        "user_id": user_id,
        "email": email,
        "product_id": product_id,
        "purchase_token": purchase_token,
        "order_id": order_id,
        "days_added": days,
        "purchase_state": verification.get("purchaseState"),
        "consumption_state": verification.get("consumptionState"),
        "acknowledgement_state": verification.get("acknowledgementState"),
        "raw_payload": verification,
        "created_at": _iso(_now()),
    }
    try:
        ins = supabase.table("google_play_inapp_purchases").insert(payload).execute()
    except Exception as exc:
        minimal_payload = {
            "user_id": user_id,
            "email": email,
            "product_id": product_id,
            "purchase_token": purchase_token,
            "order_id": order_id,
            "days_added": days,
            "created_at": _iso(_now()),
        }
        try:
            ins = supabase.table("google_play_inapp_purchases").insert(minimal_payload).execute()
        except Exception as fallback_exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "google_play_inapp_purchase_insert_failed",
                    "message": str(fallback_exc),
                    "first_attempt_message": str(exc),
                },
            ) from fallback_exc
    _assert_mutation_ok(ins, "google_play_inapp_purchase_insert_failed")


def _write_access_duration_event(
    *,
    user_id: str,
    email: str,
    product_id: str,
    days: int,
    purchase_token: str,
    order_id: str,
    previous_ends_at: datetime,
    new_ends_at: datetime,
) -> None:
    payload = {
        "user_id": user_id,
        "email": email,
        "source": "google_play_inapp",
        "source_ref": purchase_token,
        "product_id": product_id,
        "days_added": days,
        "previous_ends_at": _iso(previous_ends_at),
        "new_ends_at": _iso(new_ends_at),
        "metadata": {
            "provider": "google_play",
            "purchase_token": purchase_token,
            "order_id": order_id,
            "product_id": product_id,
            "days_added": days,
        },
    }
    try:
        ins = supabase.table("access_duration_events").insert(payload).execute()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "access_duration_event_insert_failed", "message": str(exc)},
        ) from exc
    _assert_mutation_ok(ins, "access_duration_event_insert_failed")


def _existing_response(user_id: str, product_id: str, purchase_token: str) -> dict[str, Any]:
    fresh = _profile_or_404(user_id)
    try:
        _update_purchase_log(purchase_token, user_id, _clean_lower(fresh.get("email")), product_id, "google_play_inapp")
    except Exception:
        pass
    return {
        "ok": True,
        "duplicate": True,
        "already_processed": True,
        "stage": "success",
        "product_id": product_id,
        "days_added": 0,
        "membership_ends_at": fresh.get("membership_ends_at"),
        "package_ends_at": fresh.get("package_ends_at"),
    }


def _confirm(req: GoogleInAppConfirmReq, authorization: str | None, stage_ctx: dict[str, str]) -> dict[str, Any]:
    _stage(stage_ctx, "auth_user_loaded")
    user_id = _resolve_request_user_id(req.user_id, authorization)

    _stage(stage_ctx, "request_parsed")
    product_id = _clean_lower(_field(req, "product_id", "productId"))
    purchase_token = normalize_purchase_token(_field(req, "purchase_token", "purchaseToken"))
    package_name = _clean(_field(req, "package_name", "packageName"))
    product_type = _clean_lower(_field(req, "product_type", "productType"))

    if package_name and package_name != PACKAGE_NAME:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "google_play_package_mismatch",
                "expected_package_name": PACKAGE_NAME,
                "actual_package_name": package_name,
            },
        )
    if product_type and product_type != "inapp":
        raise HTTPException(status_code=400, detail="google_play_inapp_required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token_required")

    _stage(stage_ctx, "product_loaded")
    if product_id not in DAY_PRODUCTS:
        raise HTTPException(status_code=400, detail="invalid_day_product_id")
    days = DAY_PRODUCTS[product_id]

    _stage(stage_ctx, "google_credentials_loaded")
    _validate_google_credentials_env()

    profile = _profile_or_404(user_id)
    user_email = _clean_lower(profile.get("email"))

    existing_inapp = _get_inapp_purchase_owner(purchase_token)
    if existing_inapp:
        existing_user_id = _clean(existing_inapp.get("user_id"))
        if existing_user_id and existing_user_id != user_id:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")
        _stage(stage_ctx, "success")
        return _existing_response(user_id, product_id, purchase_token)

    existing_owner = _get_purchase_owner(purchase_token)
    if existing_owner:
        existing_user_id = _clean(existing_owner.get("user_id"))
        if existing_user_id and existing_user_id != user_id:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")
        _stage(stage_ctx, "success")
        return _existing_response(user_id, product_id, purchase_token)

    _stage(stage_ctx, "store_purchase_duplicate_check")
    try:
        existing_store_purchase = find_store_purchase_by_android_token(supabase, purchase_token)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="store_purchase_lookup_failed") from exc
    if existing_store_purchase:
        existing_user_id = _clean(existing_store_purchase.get("user_id"))
        if existing_user_id and existing_user_id != user_id:
            raise HTTPException(status_code=409, detail="purchase_token_already_bound_to_other_user")
        _stage(stage_ctx, "success")
        return _existing_response(user_id, product_id, purchase_token)

    _stage(stage_ctx, "google_purchase_verify_started")
    verification = _verify_inapp_product(product_id, purchase_token)
    _stage(stage_ctx, "google_purchase_verify_done")

    now = _now()
    base_date = _active_base_date(profile)
    new_end = base_date + timedelta(days=days)
    order_id = _order_id(req, verification)

    _stage(stage_ctx, "profile_update_started")
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
    upd = supabase.table("profiles").update(payload).eq("id", user_id).execute()
    _assert_mutation_ok(upd, "profile_update_failed")

    _stage(stage_ctx, "db_purchase_insert_started")
    _write_inapp_purchase(
        user_id=user_id,
        email=user_email,
        product_id=product_id,
        days=days,
        purchase_token=purchase_token,
        order_id=order_id,
        verification=verification,
    )

    _stage(stage_ctx, "access_event_insert_started")
    _write_access_duration_event(
        user_id=user_id,
        email=user_email,
        product_id=product_id,
        days=days,
        purchase_token=purchase_token,
        order_id=order_id,
        previous_ends_at=base_date,
        new_ends_at=new_end,
    )
    _insert_purchase_log(user_id, user_email, product_id, days, purchase_token, "google_play_inapp")

    _stage(stage_ctx, "store_purchase_insert_started")
    try:
        store_purchase = insert_store_purchase(
            supabase,
            user_id=user_id,
            platform="android",
            product_id=product_id,
            purchase_token=purchase_token,
            order_id=order_id,
            purchase_time=_parse_dt(verification.get("purchaseTimeMillis")),
            granted_days=days,
            entitlement_start=base_date,
            entitlement_end=new_end,
            raw_payload=verification,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="store_purchase_insert_failed") from exc

    insert_purchase_audit_log(
        supabase,
        purchase_id=(store_purchase or {}).get("id"),
        user_id=user_id,
        platform="android",
        action="grant_days",
        reason="google_play_inapp_confirm",
        old_status=None,
        new_status="active",
        old_entitlement_end=base_date,
        new_entitlement_end=new_end,
        raw_payload={
            "product_id": product_id,
            "purchase_token": purchase_token,
            "order_id": order_id,
            "granted_days": days,
        },
    )

    _stage(stage_ctx, "success")
    fresh = _profile_or_404(user_id)
    return {
        "ok": True,
        "duplicate": False,
        "already_processed": False,
        "stage": "success",
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
        "google_order_id": order_id,
    }


@router.post("/api/billing/google/inapp/confirm")
def billing_google_inapp_confirm(req: GoogleInAppConfirmReq, authorization: str | None = Header(default=None)):
    stage_ctx = {"stage": "auth_user_loaded"}
    user_id_for_log = _safe_str(req.user_id)
    try:
        result = _confirm(req, authorization, stage_ctx)
        return result
    except HTTPException as exc:
        _log_confirm_exception(exc, req, stage_ctx, user_id=user_id_for_log)
        detail = exc.detail
        if isinstance(detail, dict):
            reason = _clean(detail.get("error")) or _clean(detail.get("reason"))
        else:
            reason = _clean(detail)
        return _error(exc.status_code or 500, detail, stage_ctx.get("stage", "unknown"), reason)
    except Exception as exc:
        _log_confirm_exception(exc, req, stage_ctx, user_id=user_id_for_log)
        return _error(
            500,
            {"error": "unhandled_exception", "message": str(exc), "exception_type": type(exc).__name__},
            stage_ctx.get("stage", "unknown"),
            "unhandled_exception",
        )
