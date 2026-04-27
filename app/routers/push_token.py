# FILE: app/routers/push_token.py

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api", tags=["push-token"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE ENV missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class SaveTokenReq(BaseModel):
    user_id: str
    token: str


@router.post("/save-token")
def save_token(req: SaveTokenReq):
    user_id = str(req.user_id or "").strip()
    token = str(req.token or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id_required")

    if not token:
        raise HTTPException(status_code=422, detail="token_required")

    now_iso = datetime.now(timezone.utc).isoformat()

    supabase.table("profiles").update(
        {
            "fcm_token": token,
            "active_session_updated_at": now_iso,
        }
    ).eq("id", user_id).execute()

    return {
        "ok": True,
        "saved": True,
        "user_id": user_id,
    }
