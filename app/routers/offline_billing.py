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

# 🔥 YENİ MODEL
OFFLINE_PRICE = 5          # 5 jeton
OFFLINE_DURATION = 365     # 12 ay


class OfflineFileReq(BaseModel):
    user_id: str
    file_name: str


def parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except:
        return None


# =========================
# AKTİVASYON
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
    now = datetime.now(timezone.utc)

    # mevcut kayıt
    existing = (
        supabase.table("offline_files")
        .select("*")
        .eq("user_id", user_id)
        .eq("file_name", file_name)
        .limit(1)
        .execute()
    )

    # 🔥 AKTİFSE ÜCRET ALMA
    if existing.data:
        row = existing.data[0]
        exp = parse_dt(row.get("expires_at"))

        if exp and exp > now:
            return {
                "ok": True,
                "already_active": True,
                "tokens": tokens,
                "price": OFFLINE_PRICE,
                "expires_at": exp.isoformat(),
                "duration_days": OFFLINE_DURATION
            }

    # 🔥 TOKEN KONTROL (5 jeton)
    if tokens < OFFLINE_PRICE:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    expires = now + timedelta(days=OFFLINE_DURATION)

    if existing.data:
        supabase.table("offline_files").update({
            "expires_at": expires.isoformat()
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        supabase.table("offline_files").insert({
            "user_id": user_id,
            "file_name": file_name,
            "expires_at": expires.isoformat()
        }).execute()

    # 🔥 5 jeton düş
    next_tokens = tokens - OFFLINE_PRICE

    supabase.table("profiles").update({
        "tokens": next_tokens
    }).eq("id", user_id).execute()

    return {
        "ok": True,
        "already_active": False,
        "tokens": next_tokens,
        "price": OFFLINE_PRICE,
        "expires_at": expires.isoformat(),
        "duration_days": OFFLINE_DURATION
    }


# =========================
# LİSTE
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
        exp = parse_dt(r.get("expires_at"))
        active = bool(exp and exp > now)

        items.append({
            **r,
            "active": active
        })

    return {
        "ok": True,
        "items": items,
        "price": OFFLINE_PRICE,
        "duration_days": OFFLINE_DURATION
    }
