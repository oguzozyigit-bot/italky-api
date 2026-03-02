from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from supabase import create_client, Client

router = APIRouter(tags=["offline"])

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
STORAGE_BUCKET = "offline"

sb: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE:
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

class OfflineLinkReq(BaseModel):
    base_lang: str = Field(..., min_length=2, max_length=16)
    target_lang: str = Field(..., min_length=2, max_length=16)

class OfflineLinkResp(BaseModel):
    ok: bool
    pair_1: str
    pair_2: str
    url_1: str
    url_2: str

def _norm(code: str) -> str:
    return (code or "").strip().lower().replace("_", "-")

def _parse_expires_at(v: Any) -> int:
    if v is None:
        return 0
    try:
        if hasattr(v, "timestamp"):
            return int(v.timestamp())
    except Exception:
        pass
    try:
        s = str(v).strip()
        if not s:
            return 0
        s = s.replace(" ", "T")
        import datetime as dt
        return int(dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0

def _require_bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization missing")
    return authorization.replace("Bearer ", "").strip()

def _storage_path(pair_code: str) -> str:
    return f"langpacks/{pair_code}/model.zip"

def _signed_url(path: str) -> str:
    assert sb is not None
    signed = sb.storage.from_(STORAGE_BUCKET).create_signed_url(path, 60)
    if not signed:
        return ""

    # bazen data içine gömülü
    data = signed.get("data") if isinstance(signed, dict) else None
    if isinstance(data, dict):
        signed = {**signed, **data}

    return (
        signed.get("signedURL")
        or signed.get("signedUrl")
        or signed.get("signed_url")
        or signed.get("url")
        or ""
    )

async def _get_user_id_from_access_token(access_token: str) -> str:
    """
    supabase-py sürüm farklarına dayanıklı şekilde user id çek.
    """
    if sb is None:
        return ""
    try:
        # bazı sürümlerde get_user(jwt=token)
        try:
            user = sb.auth.get_user(jwt=access_token)
        except TypeError:
            user = sb.auth.get_user(access_token)

        # olası şekiller
        if isinstance(user, dict):
            u = user.get("user") or {}
            return str(u.get("id") or "")

        uobj = getattr(user, "user", None)
        if uobj and getattr(uobj, "id", None):
            return str(uobj.id)

        # fallback
        try:
            return str(user.user.id)
        except Exception:
            return ""
    except Exception:
        return ""

@router.post("/offline/get_links", response_model=OfflineLinkResp)
async def get_offline_links(req: OfflineLinkReq, authorization: Optional[str] = Header(None)):
    if sb is None:
        # Import'ta değil, burada patlatıyoruz
        raise HTTPException(status_code=500, detail="Offline service misconfigured (Supabase ENV missing)")

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

    # 1) Paket sorgusu: önce in_ dene, patlarsa fallback
    try:
        packs = (
            sb.table("offline_packs")
            .select("lang_code, expires_at")
            .eq("user_id", user_id)
            .in_("lang_code", [pair_1, pair_2])
            .execute()
        )
        rows = packs.data or []
    except Exception:
        r1 = (
            sb.table("offline_packs")
            .select("lang_code, expires_at")
            .eq("user_id", user_id)
            .eq("lang_code", pair_1)
            .execute()
        )
        r2 = (
            sb.table("offline_packs")
            .select("lang_code, expires_at")
            .eq("user_id", user_id)
            .eq("lang_code", pair_2)
            .execute()
        )
        rows = (r1.data or []) + (r2.data or [])

    if len(rows) < 2:
        raise HTTPException(status_code=403, detail="Offline paket yok (2 yön gerekli)")

    exp_map: Dict[str, int] = {}
    for p in rows:
        code = str(p.get("lang_code") or "").strip().lower()
        exp_map[code] = _parse_expires_at(p.get("expires_at"))

    if exp_map.get(pair_1, 0) <= now_ts or exp_map.get(pair_2, 0) <= now_ts:
        raise HTTPException(status_code=403, detail="Offline paketin süresi dolmuş")

    p1 = _storage_path(pair_1)
    p2 = _storage_path(pair_2)

    url1 = _signed_url(p1)
    url2 = _signed_url(p2)

    if not url1 or not url2:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı (storage path yanlış olabilir)")

    return OfflineLinkResp(ok=True, pair_1=pair_1, pair_2=pair_2, url_1=url1, url_2=url2)
