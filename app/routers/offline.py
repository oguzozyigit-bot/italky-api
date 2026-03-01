from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from supabase import create_client, Client

router = APIRouter(tags=["offline"])

# =============================
# ENV
# =============================
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

# NOTE:
# - Token doğrulaması için manuel jwt decode yerine Supabase Auth get_user kullanıyoruz.
# - Service role ile auth admin endpoint'e gidebilir.
STORAGE_BUCKET = "offline"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("Supabase ENV eksik: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

# =============================
# SCHEMA
# =============================
class OfflineLinkReq(BaseModel):
    base_lang: str = Field(..., min_length=2, max_length=16)
    target_lang: str = Field(..., min_length=2, max_length=16)

class OfflineLinkResp(BaseModel):
    ok: bool
    pair_1: str
    pair_2: str
    url_1: str
    url_2: str

# =============================
# HELPERS
# =============================
def _norm(code: str) -> str:
    return (code or "").strip().lower().replace("_", "-")

def _parse_expires_at(v: Any) -> int:
    """
    Supabase'den gelen expires_at:
    - datetime olabilir
    - ISO string olabilir
    - None olabilir
    Dönen: epoch seconds (0 if invalid)
    """
    if v is None:
        return 0
    try:
        # datetime
        if hasattr(v, "timestamp"):
            return int(v.timestamp())
    except Exception:
        pass
    try:
        s = str(v).strip()
        if not s:
            return 0
        # ör: 2026-03-02T12:34:56+00:00 / 2026-03-02 12:34:56+00
        s = s.replace(" ", "T")
        # Python 3.11+ fromisoformat supports offsets
        import datetime as dt
        return int(dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0

def _require_bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization missing")
    return authorization.replace("Bearer ", "").strip()

async def _get_user_id_from_access_token(access_token: str) -> str:
    """
    access_token Supabase JWT'dir. Supabase Auth endpoint üzerinden doğrulayıp user alır.
    """
    try:
        user = sb.auth.get_user(access_token)
        uid = getattr(user, "user", None)
        # supabase-py bazen dict benzeri döner
        if uid and getattr(uid, "id", None):
            return str(uid.id)
        if isinstance(user, dict):
            # eski/alternatif shape
            return str(((user.get("user") or {}).get("id")) or "")
        # fallback: object attribute search
        try:
            return str(user.user.id)
        except Exception:
            return ""
    except Exception:
        return ""

def _storage_path(pair_code: str) -> str:
    # offline/langpacks/en-tr/model.zip  (bucket=offline)
    return f"langpacks/{pair_code}/model.zip"

def _signed_url(path: str) -> str:
    # 60 sn geçerli signed url
    signed = sb.storage.from_(STORAGE_BUCKET).create_signed_url(path, 60)
    if not signed:
        return ""
    # supabase-py dönüşlerinde anahtar değişebiliyor:
    # {"signedURL": "..."} veya {"signedUrl": "..."} vb.
    return (
        signed.get("signedURL")
        or signed.get("signedUrl")
        or signed.get("signed_url")
        or signed.get("url")
        or ""
    )

# =============================
# ROUTE
# =============================
@router.post("/offline/get_links", response_model=OfflineLinkResp)
async def get_offline_links(req: OfflineLinkReq, authorization: Optional[str] = Header(None)):
    token = _require_bearer(authorization)
    user_id = await _get_user_id_from_access_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    base = _norm(req.base_lang)
    target = _norm(req.target_lang)

    if not base or not target or base == target:
        raise HTTPException(status_code=400, detail="Dil parametresi hatalı")

    pair_1 = f"{base}-{target}"
    pair_2 = f"{target}-{base}"

    now_ts = int(time.time())

    # 1) Kullanıcı paketi var mı?
    packs = (
        sb.table("offline_packs")
        .select("lang_code, expires_at")
        .eq("user_id", user_id)
        .in_("lang_code", [pair_1, pair_2])
        .execute()
    )

    if not packs.data or len(packs.data) < 2:
        raise HTTPException(status_code=403, detail="Offline paket yok (2 yön gerekli)")

    # 2) expiry kontrol (ikisi de aktif olmalı)
    exp_map: Dict[str, int] = {}
    for p in packs.data:
        code = str(p.get("lang_code") or "").strip().lower()
        exp_map[code] = _parse_expires_at(p.get("expires_at"))

    if exp_map.get(pair_1, 0) <= now_ts or exp_map.get(pair_2, 0) <= now_ts:
        raise HTTPException(status_code=403, detail="Offline paketin süresi dolmuş")

    # 3) Signed URL (iki zip)
    p1 = _storage_path(pair_1)
    p2 = _storage_path(pair_2)

    url1 = _signed_url(p1)
    url2 = _signed_url(p2)

    if not url1 or not url2:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı (storage path yanlış olabilir)")

    return OfflineLinkResp(ok=True, pair_1=pair_1, pair_2=pair_2, url_1=url1, url_2=url2)
