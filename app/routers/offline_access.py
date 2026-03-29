from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/offline", tags=["offline-access"])

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL missing")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY missing")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

REPEAT_DOWNLOAD_TOKENS = 20


class OfflineDownloadBody(BaseModel):
    user_id: str
    lang: str


def norm_lang(v: Optional[str]) -> str:
    return str(v or "").strip().lower()


@router.post("/check")
def check_download(body: OfflineDownloadBody):
    user_id = str(body.user_id or "").strip()
    lang = norm_lang(body.lang)

    if not user_id:
      raise HTTPException(status_code=400, detail="user_id required")
    if not lang:
      raise HTTPException(status_code=400, detail="lang required")

    try:
        res = (
            sb.table("offline_downloads")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("lang", lang)
            .limit(1)
            .execute()
        )
        count = int(res.count or 0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"offline check failed: {str(e)}")

    return {
        "ok": True,
        "already_downloaded": count > 0,
        "repeat_download_tokens": REPEAT_DOWNLOAD_TOKENS
    }


@router.post("/save")
def save_download(body: OfflineDownloadBody):
    user_id = str(body.user_id or "").strip()
    lang = norm_lang(body.lang)

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    if not lang:
        raise HTTPException(status_code=400, detail="lang required")

    try:
        exists = (
            sb.table("offline_downloads")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("lang", lang)
            .limit(1)
            .execute()
        )
        if int(exists.count or 0) > 0:
            return {"ok": True, "saved": False, "already_exists": True}

        sb.table("offline_downloads").insert({
            "user_id": user_id,
            "lang": lang
        }).execute()

        return {"ok": True, "saved": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"offline save failed: {str(e)}")
