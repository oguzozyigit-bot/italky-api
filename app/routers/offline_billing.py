from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os

router = APIRouter(tags=["offline-billing"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class OfflineFileCreateReq(BaseModel):
    user_id: str
    file_name: str
    file_url: str | None = None


@router.post("/api/offline/files/create")
async def create_offline_file(req: OfflineFileCreateReq):
    user_id = (req.user_id or "").strip()
    file_name = (req.file_name or "").strip()
    file_url = (req.file_url or "").strip() or None

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not file_name:
        raise HTTPException(status_code=422, detail="file_name required")

    prof = (
        supabase.table("profiles")
        .select("tokens")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(status_code=404, detail="profile not found")

    tokens = int((prof.data[0] or {}).get("tokens") or 0)
    if tokens < 1:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    next_tokens = tokens - 1
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)

    supabase.table("profiles").update({"tokens": next_tokens}).eq("id", user_id).execute()

    ins = (
        supabase.table("offline_files")
        .insert({
            "user_id": user_id,
            "file_name": file_name,
            "file_url": file_url,
            "expires_at": expires_at.isoformat(),
        })
        .execute()
    )

    return {"ok": True, "tokens": next_tokens, "file": ins.data}
