from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field
from supabase import create_client, Client

router = APIRouter(prefix="/api/wallet", tags=["wallet"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL veya SUPABASE_SERVICE_ROLE_KEY eksik")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    return parts[1].strip()


def _get_user_from_jwt(jwt_token: str) -> Dict[str, Any]:
    try:
        res = supabase.auth.get_user(jwt_token)
        user = getattr(res, "user", None)
        if not user or not getattr(user, "id", None):
            raise HTTPException(status_code=401, detail="User not found from token")

        return {
            "id": str(user.id),
            "email": getattr(user, "email", None),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"JWT doğrulama hatası: {e}")


class UsageChargeBody(BaseModel):
    usage_kind: str = Field(..., pattern="^(text|voice)$")
    chars_used: int = Field(..., gt=0)
    source: str = Field(default="usage")
    description: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


@router.post("/charge")
def charge_usage(
    body: UsageChargeBody,
    authorization: Optional[str] = Header(default=None),
):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)
    user_id = user["id"]

    try:
        rpc = supabase.rpc(
            "apply_usage_charge",
            {
                "p_user_id": user_id,
                "p_usage_kind": body.usage_kind,
                "p_chars_used": body.chars_used,
                "p_source": body.source,
                "p_description": body.description,
                "p_meta": body.meta,
            },
        ).execute()

        data = rpc.data
        if data is None:
            raise HTTPException(status_code=500, detail="RPC boş cevap döndü")

        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Charge error: {e}")


@router.get("/summary")
def wallet_summary(
    authorization: Optional[str] = Header(default=None),
):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)
    user_id = user["id"]

    try:
        rpc = supabase.rpc(
            "get_wallet_summary",
            {"p_user_id": user_id},
        ).execute()

        data = rpc.data
        if data is None:
            raise HTTPException(status_code=500, detail="Summary boş cevap döndü")

        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Summary error: {e}")


@router.get("/history")
def wallet_history(
    limit: int = Query(default=50, ge=1, le=200),
    authorization: Optional[str] = Header(default=None),
):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)
    user_id = user["id"]

    try:
        res = (
            supabase.table("wallet_tx")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        return {
            "ok": True,
            "items": res.data or [],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"History error: {e}")
