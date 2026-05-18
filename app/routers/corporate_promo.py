from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(prefix="/api/promo/corporate", tags=["Activation Codes"])

ACTIVATION_CODES_TABLE = "activation_codes"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class ActivationCodeActivateIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)
    device_id: Optional[str] = Field(default=None, max_length=160)
    user_agent: Optional[str] = Field(default=None, max_length=600)


class ActivationCodeCheckIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)
    active_session_key: str = Field(..., min_length=8, max_length=160)
    device_id: Optional[str] = Field(default=None, max_length=160)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def normalize_code(value: str) -> str:
    raw = str(value or "").strip().upper()
    return re.sub(r"[\s-]+", "", raw)


def public_error(detail: str, status_code: int = 400):
    raise HTTPException(status_code=status_code, detail=detail)


def fetch_code_row(code: str) -> Optional[dict]:
    res = supabase.table(ACTIVATION_CODES_TABLE).select("*").eq("code", code).limit(1).execute()
    rows = safe_data(res) or []
    return rows[0] if rows else None


def validate_code_row(row: Optional[dict]) -> Optional[str]:
    if not row:
        return "CODE_NOT_FOUND"
    if row.get("is_active") is not True:
        return "CODE_INACTIVE"

    now = now_utc()
    starts_at = parse_dt(row.get("starts_at"))
    expires_at = parse_dt(row.get("expires_at"))
    if starts_at and starts_at > now:
        return "CODE_NOT_STARTED"
    if expires_at and expires_at < now:
        return "CODE_EXPIRED"
    return None


def normalize_device_id(device_id: Optional[str]) -> str:
    cleaned = str(device_id or "").strip()
    return cleaned[:160] if cleaned else str(uuid.uuid4())


@router.post("/activate")
def activate_code(payload: ActivationCodeActivateIn, user_agent: Optional[str] = Header(default=None)):
    code = normalize_code(payload.code)
    if not code:
        public_error("CODE_INVALID")

    try:
        row = fetch_code_row(code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ACTIVATION_LOOKUP_FAILED: {e}")

    invalid_reason = validate_code_row(row)
    if invalid_reason:
        public_error(invalid_reason, status_code=404 if invalid_reason == "CODE_NOT_FOUND" else 400)

    assert row is not None
    session_key = str(uuid.uuid4())
    device_id = normalize_device_id(payload.device_id)
    request_user_agent = str(payload.user_agent or user_agent or "")[:600]
    stamp = iso(now_utc())

    patch = {
        "active_session_key": session_key,
        "last_device_id": device_id,
        "last_user_agent": request_user_agent,
        "activated_at": stamp,
        "last_seen_at": stamp,
        "updated_at": stamp,
    }

    try:
        updated = (
            supabase.table(ACTIVATION_CODES_TABLE)
            .update(patch)
            .eq("id", row["id"])
            .eq("code", code)
            .execute()
        )
        if not safe_data(updated):
            public_error("CODE_UPDATE_FAILED", status_code=409)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ACTIVATION_UPDATE_FAILED: {e}")

    return {
        "ok": True,
        "access": True,
        "code": code,
        "active_session_key": session_key,
        "expires_at": row.get("expires_at"),
    }


@router.get("/status")
def check_code_session_get(
    code: str = Query(...),
    active_session_key: str = Query(...),
    device_id: Optional[str] = Query(default=None),
):
    return check_code_session_core(code=code, active_session_key=active_session_key, device_id=device_id)


@router.post("/check")
def check_code_session_post(payload: ActivationCodeCheckIn):
    return check_code_session_core(
        code=payload.code,
        active_session_key=payload.active_session_key,
        device_id=payload.device_id,
    )


def check_code_session_core(code: str, active_session_key: str, device_id: Optional[str] = None):
    normalized_code = normalize_code(code)
    session_key = str(active_session_key or "").strip()
    if not normalized_code or not session_key:
        public_error("SESSION_INVALID")

    try:
        row = fetch_code_row(normalized_code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SESSION_LOOKUP_FAILED: {e}")

    invalid_reason = validate_code_row(row)
    if invalid_reason:
        return {"ok": True, "active": False, "reason": invalid_reason.lower()}

    assert row is not None
    current_key = str(row.get("active_session_key") or "").strip()
    if not current_key or current_key != session_key:
        return {"ok": True, "active": False, "reason": "session_replaced"}

    stamp = iso(now_utc())
    patch = {"last_seen_at": stamp, "updated_at": stamp}
    if device_id:
        patch["last_device_id"] = normalize_device_id(device_id)

    try:
        supabase.table(ACTIVATION_CODES_TABLE).update(patch).eq("id", row["id"]).execute()
    except Exception:
        pass

    return {"ok": True, "active": True}
