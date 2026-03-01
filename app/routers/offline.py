# FILE: italky-api/app/routers/offline.py
from __future__ import annotations

import os
import time
from typing import Optional, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from supabase import create_client, Client
import jwt
from datetime import datetime, timezone

router = APIRouter(tags=["offline"])

# =============================
# ENV
# =============================
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
SUPABASE_JWT_SECRET = (os.getenv("SUPABASE_JWT_SECRET") or "").strip()

STORAGE_BUCKET = "offline"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("Supabase ENV eksik: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_JWT_SECRET:
    raise RuntimeError("Supabase ENV eksik: SUPABASE_JWT_SECRET")

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
    if not token:
        raise HTTPException(status_code=401, detail="Authorization missing")

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Invalid token (no sub)")
        return str(sub)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def _to_epoch_seconds(expires_at: Any) -> int:
    """
    Supabase'den expires_at bazen:
    - int/float (epoch)
    - datetime
    - ISO string (timestamptz)
    gelebilir. Hepsini epoch saniyeye çevirir.
    """
    if expires_at is None:
        return 0

    # epoch already
    if isinstance(expires_at, (int, float)):
        return int(expires_at)

    # datetime
    if isinstance(expires_at, datetime):
        dt = expires_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    # string
    if isinstance(expires_at, str):
        s = expires_at.strip()
        if not s:
            return 0

        # numeric string
        if s.isdigit():
            return int(s)

        # ISO format: "2026-03-02T10:20:30+00:00" or "...Z"
        try:
            # Python isoformat doesn't like Z directly
            s2 = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return 0

    # unknown type
    return 0


# =============================
# ROUTE
# =============================
@router.post("/offline/get_link", response_model=OfflineLinkResp)
async def get_offline_link(
    req: OfflineLinkReq,
    authorization: Optional[str] = Header(None)
):
    user_id = get_user_from_token(authorization)

    base = (req.base_lang or "").lower().strip()
    target = (req.target_lang or "").lower().strip()

    if not base or not target:
        raise HTTPException(status_code=400, detail="Dil parametresi eksik")
    if base == target:
        raise HTTPException(status_code=400, detail="base_lang ve target_lang aynı olamaz")

    # 1) Kullanıcı paketi var mı kontrol (iki yön de kabul)
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
        exp = _to_epoch_seconds(p.get("expires_at"))
        if exp > now_ts:
            valid = True
            break

    if not valid:
        raise HTTPException(status_code=403, detail="Offline paketin süresi dolmuş")

    # 2) Storage path (istenen yön!)
    # Beklenen klasör:
    # offline/langpacks/en-tr/model.zip
    storage_path = f"langpacks/{pair_code_1}/model.zip"

    # 3) Signed URL üret
    try:
        signed = supabase.storage.from_(STORAGE_BUCKET).create_signed_url(
            storage_path,
            expires_in=60  # 60 sn
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage error: {str(e)}")

    # supabase-py sürümüne göre key değişebiliyor
    url = ""
    if isinstance(signed, dict):
        url = (signed.get("signedURL") or signed.get("signedUrl") or "").strip()

    if not url:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı (signed url boş)")

    return OfflineLinkResp(ok=True, url=url)
