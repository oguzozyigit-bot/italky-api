# FILE: italky-api/app/routers/offline.py

from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client, Client
import jwt

router = APIRouter(tags=["offline"])

# =============================
# ENV
# =============================

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "").strip()

STORAGE_BUCKET = "offline"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("Supabase ENV eksik")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)


# =============================
# SCHEMA
# =============================

class OfflineLinkReq(BaseModel):
    base_lang: str
    target_lang: str


class OfflineLinkResp(BaseModel):
    ok: bool
    url: str


# =============================
# JWT VALIDATION
# =============================

def get_user_from_token(auth_header: Optional[str]) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization missing")

    token = auth_header.replace("Bearer ", "").strip()

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


# =============================
# ROUTE
# =============================

@router.post("/offline/get_link", response_model=OfflineLinkResp)
async def get_offline_link(
    req: OfflineLinkReq,
    authorization: Optional[str] = Header(None)
):
    user_id = get_user_from_token(authorization)

    base = req.base_lang.lower().strip()
    target = req.target_lang.lower().strip()

    if not base or not target:
        raise HTTPException(status_code=400, detail="Dil parametresi eksik")

    # 1️⃣ Kullanıcı paketi var mı kontrol
    pair_code_1 = f"{base}-{target}"
    pair_code_2 = f"{target}-{base}"

    now_ts = int(time.time())

    packs = (
        supabase
        .table("offline_packs")
        .select("lang_code, expires_at")
        .eq("user_id", user_id)
        .in_("lang_code", [pair_code_1, pair_code_2])
        .execute()
    )

    if not packs.data:
        raise HTTPException(status_code=403, detail="Offline paket yok")

    valid = False
    for p in packs.data:
        if p["expires_at"]:
            if int(p["expires_at"].timestamp()) > now_ts:
                valid = True
                break

    if not valid:
        raise HTTPException(status_code=403, detail="Offline paketin süresi dolmuş")

    # 2️⃣ Storage path
    # klasör yapın:
    # offline/langpacks/en-tr/model.zip

    storage_path = f"langpacks/{pair_code_1}/model.zip"

    # 3️⃣ Signed URL üret
    try:
        signed = supabase.storage.from_(STORAGE_BUCKET).create_signed_url(
            storage_path,
            expires_in=60  # 1 dakika geçerli
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage error: {str(e)}")

    if not signed or "signedURL" not in signed:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")

    return OfflineLinkResp(
        ok=True,
        url=signed["signedURL"]
    )
