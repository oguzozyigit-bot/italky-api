from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os

router = APIRouter(tags=["offline-files"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class OfflineFileReq(BaseModel):
    user_id: str
    file_name: str


# =========================
# DOSYA AKTİF ET / YENİLE
# =========================
@router.post("/api/offline/files/activate")
async def activate_file(req: OfflineFileReq):

    user_id = (req.user_id or "").strip()
    file_name = (req.file_name or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if not file_name:
        raise HTTPException(status_code=422, detail="file_name required")

    # kullanıcı token
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

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=30)

    # dosya var mı
    existing = (
        supabase.table("offline_files")
        .select("*")
        .eq("user_id", user_id)
        .eq("file_name", file_name)
        .limit(1)
        .execute()
    )

    if existing.data:

        # sadece süre uzat
        supabase.table("offline_files").update({
            "expires_at": expires.isoformat()
        }).eq("id", existing.data[0]["id"]).execute()

    else:

        # yeni dosya
        supabase.table("offline_files").insert({
            "user_id": user_id,
            "file_name": file_name,
            "expires_at": expires.isoformat()
        }).execute()

    # kontör düş
    next_tokens = tokens - 1

    supabase.table("profiles").update({
        "tokens": next_tokens
    }).eq("id", user_id).execute()

    return {
        "ok": True,
        "tokens": next_tokens,
        "expires_at": expires.isoformat()
    }


# =========================
# DOSYA LİSTESİ
# =========================
@router.get("/api/offline/files/list")
async def list_files(user_id: str):

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    rows = (
        supabase.table("offline_files")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )

    now = datetime.now(timezone.utc)

    items = []

    for r in rows.data or []:

        expires = r.get("expires_at")

        try:
            exp = datetime.fromisoformat(str(expires).replace("Z","+00:00"))
            active = exp > now
        except:
            active = False

        items.append({
            **r,
            "active": active
        })

    return {
        "ok": True,
        "items": items
    }
