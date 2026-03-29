from __future__ import annotations

from datetime import datetime, timezone
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

REPEAT_DOWNLOAD_PRICE = 20


class OfflineFileReq(BaseModel):
    user_id: str
    file_name: str


def norm_file_name(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =========================
# KONTROL
# ilk indirme ücretsiz mi?
# tekrar indirme ücretli mi?
# =========================
@router.post("/api/offline/files/check")
async def check_file(req: OfflineFileReq):
    user_id = (req.user_id or "").strip()
    file_name = norm_file_name(req.file_name)

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if not file_name:
        raise HTTPException(status_code=422, detail="file_name required")

    existing = (
        supabase.table("offline_files")
        .select("id,user_id,file_name,download_count,last_downloaded_at")
        .eq("user_id", user_id)
        .eq("file_name", file_name)
        .limit(1)
        .execute()
    )

    if existing.data:
        row = existing.data[0]
        return {
            "ok": True,
            "already_downloaded": True,
            "download_count": int(row.get("download_count") or 1),
            "repeat_price": REPEAT_DOWNLOAD_PRICE,
            "file_name": file_name
        }

    return {
        "ok": True,
        "already_downloaded": False,
        "download_count": 0,
        "repeat_price": REPEAT_DOWNLOAD_PRICE,
        "file_name": file_name
    }


# =========================
# AKTİVASYON / İNDİRME KAYDI
# ilk indirme ücretsiz
# tekrar indirme 20 jeton
# =========================
@router.post("/api/offline/files/activate")
async def activate_file(req: OfflineFileReq):
    user_id = (req.user_id or "").strip()
    file_name = norm_file_name(req.file_name)

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

    existing = (
        supabase.table("offline_files")
        .select("id,download_count")
        .eq("user_id", user_id)
        .eq("file_name", file_name)
        .limit(1)
        .execute()
    )

    already_downloaded = bool(existing.data)
    charged = 0

    if already_downloaded:
        if tokens < REPEAT_DOWNLOAD_PRICE:
            raise HTTPException(status_code=402, detail="insufficient_tokens")

        charged = REPEAT_DOWNLOAD_PRICE
        next_tokens = tokens - charged

        supabase.table("profiles").update({
            "tokens": next_tokens
        }).eq("id", user_id).execute()

        row = existing.data[0]
        next_count = int(row.get("download_count") or 1) + 1

        supabase.table("offline_files").update({
            "download_count": next_count,
            "last_downloaded_at": now_iso()
        }).eq("id", row["id"]).execute()

        return {
            "ok": True,
            "already_downloaded": True,
            "charged": charged,
            "tokens": next_tokens,
            "download_count": next_count,
            "file_name": file_name
        }

    supabase.table("offline_files").insert({
        "user_id": user_id,
        "file_name": file_name,
        "download_count": 1,
        "first_downloaded_at": now_iso(),
        "last_downloaded_at": now_iso()
    }).execute()

    return {
        "ok": True,
        "already_downloaded": False,
        "charged": 0,
        "tokens": tokens,
        "download_count": 1,
        "file_name": file_name
    }


# =========================
# LİSTE
# kullanıcının indirdiği offline dosyalar
# =========================
@router.get("/api/offline/files/list")
async def list_files(user_id: str):
    user_id = (user_id or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    rows = (
        supabase.table("offline_files")
        .select("*")
        .eq("user_id", user_id)
        .order("last_downloaded_at", desc=True)
        .execute()
    )

    return {
        "ok": True,
        "items": rows.data or [],
        "repeat_price": REPEAT_DOWNLOAD_PRICE
        }
