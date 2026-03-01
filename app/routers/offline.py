# FILE: app/routers/offline.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from supabase import create_client
import os
from datetime import datetime, timezone

router = APIRouter(tags=["offline"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Supabase env missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

BUCKET = "offline"

class OfflineReq(BaseModel):
    base_lang: str
    target_lang: str

@router.post("/offline/get_link")
async def get_offline_link(req: OfflineReq, user_id: str = Depends(lambda: None)):
    """
    user_id burada auth middleware'den gelecek.
    Eğer auth sistemin hazırsa onu bağlayacağız.
    """

    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")

    pair = f"{req.base_lang}-{req.target_lang}"

    # 1️⃣ DB kontrol
    pack = supabase.table("offline_packs") \
        .select("expires_at") \
        .eq("user_id", user_id) \
        .eq("lang_code", pair) \
        .maybe_single() \
        .execute()

    if not pack.data:
        raise HTTPException(status_code=403, detail="Offline hakkı yok")

    expires_at = pack.data["expires_at"]
    if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail="Paket süresi dolmuş")

    # 2️⃣ Signed URL üret
    path = f"langpacks/{pair}/model.zip"

    signed = supabase.storage.from_(BUCKET).create_signed_url(
        path,
        60  # 60 saniye geçerli
    )

    if not signed:
        raise HTTPException(status_code=500, detail="Signed URL üretilemedi")

    return {
        "ok": True,
        "url": signed["signedURL"]
    }
