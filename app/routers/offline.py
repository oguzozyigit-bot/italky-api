# FILE: italky-api/app/routers/offline.py
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

# =========================
# SCHEMAS
# =========================
class OfflineLinkReq(BaseModel):
    base_lang: str = Field(..., min_length=2, max_length=16)
    target_lang: str = Field(..., min_length=2, max_length=16)

class OfflineLinkResp(BaseModel):
    ok: bool
    pair_1: str
    pair_2: str
    url_1: str
    url_2: str

# Backward-compatible response for older frontend
class OfflineSingleLinkResp(BaseModel):
    ok: bool
    url: str

# =========================
# HELPERS
# =========================
def _norm(code: str) -> str:
    return (code or "").strip().lower().replace("_", "-")

def _require_bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization missing")
    return authorization.replace("Bearer ", "").strip()

def _storage_path(pair_code: str) -> str:
    # Bucket içinde: langpacks/en-tr/model.zip
    return f"langpacks/{pair_code}/model.zip"

def _public_url(path: str) -> str:
    """
    Bucket public olduğu için: signed link gerekmeden public URL üretir.
    """
    if sb is None:
        return ""
    out = sb.storage.from_(STORAGE_BUCKET).get_public_url(path)
    # supabase-py bazen string, bazen dict döndürür
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        data = out.get("data") if isinstance(out.get("data"), dict) else {}
        return (
            data.get("publicUrl")
            or data.get("public_url")
            or out.get("publicUrl")
            or out.get("public_url")
            or out.get("url")
            or ""
        )
    return ""

def _signed_url(path: str, seconds: int = 60) -> str:
    """
    İleride bucket private yapılırsa diye: signed URL opsiyonu.
    Şu an public kullandığımız için zorunlu değil.
    """
    if sb is None:
        return ""
    signed = sb.storage.from_(STORAGE_BUCKET).create_signed_url(path, seconds)
    if not signed:
        return ""
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
    (Opsiyonel) Signed link + yetkilendirme gerektiğinde kullanılır.
    Şu an offline FREE + public ise bu fonksiyon kullanılmayabilir.
    """
    if sb is None:
        return ""
    try:
        try:
            user = sb.auth.get_user(jwt=access_token)
        except TypeError:
            user = sb.auth.get_user(access_token)

        if isinstance(user, dict):
            u = user.get("user") or {}
            return str(u.get("id") or "")

        uobj = getattr(user, "user", None)
        if uobj and getattr(uobj, "id", None):
            return str(uobj.id)

        try:
            return str(user.user.id)
        except Exception:
            return ""
    except Exception:
        return ""

# =========================
# ✅ FREE + PUBLIC LINKS (NO LOGIN)
# =========================
@router.post("/offline/public_links", response_model=OfflineLinkResp)
async def get_offline_public_links(req: OfflineLinkReq):
    """
    ✅ OFFLINE FREE model:
    - Login yok
    - Paket/jeton yok
    - Bucket public -> public URL veriyoruz
    """
    if sb is None:
        raise HTTPException(status_code=500, detail="Offline service misconfigured (Supabase ENV missing)")

    base = _norm(req.base_lang)
    target = _norm(req.target_lang)
    if not base or not target or base == target:
        raise HTTPException(status_code=400, detail="Dil parametresi hatalı")

    pair_1 = f"{base}-{target}"
    pair_2 = f"{target}-{base}"

    p1 = _storage_path(pair_1)
    p2 = _storage_path(pair_2)

    url1 = _public_url(p1)
    url2 = _public_url(p2)
    if not url1 or not url2:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı (storage path yanlış olabilir)")

    return OfflineLinkResp(ok=True, pair_1=pair_1, pair_2=pair_2, url_1=url1, url_2=url2)

# =========================
# ✅ BACKWARD COMPAT: /offline/get_link  (tek dosya isteyen eski frontend)
# =========================
@router.post("/offline/get_link", response_model=OfflineSingleLinkResp)
async def get_offline_single_public_link(req: Dict[str, Any]):
    """
    Eski sayfalar tek link istiyordu.
    Body örn: { base_lang:"tr", target_lang:"en" }
    """
    base = _norm(str(req.get("base_lang") or ""))
    target = _norm(str(req.get("target_lang") or ""))
    if not base or not target or base == target:
        raise HTTPException(status_code=400, detail="Dil parametresi hatalı")

    path = _storage_path(f"{base}-{target}")
    url = _public_url(path)
    if not url:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    return OfflineSingleLinkResp(ok=True, url=url)

# =========================
# (OPTIONAL) SIGNED + AUTH LINKS (ileride tekrar ücretli/limitli istersen)
# =========================
@router.post("/offline/get_links", response_model=OfflineLinkResp)
async def get_offline_signed_links(req: OfflineLinkReq, authorization: Optional[str] = Header(None)):
    """
    Bu endpointi şimdilik KULLANMA.
    İleride bucket private yaparsan veya erişimi kısıtlamak istersen devreye alırsın.
    """
    if sb is None:
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

    p1 = _storage_path(pair_1)
    p2 = _storage_path(pair_2)

    url1 = _signed_url(p1, 60)
    url2 = _signed_url(p2, 60)
    if not url1 or not url2:
        raise HTTPException(status_code=404, detail="Signed link üretilemedi")

    return OfflineLinkResp(ok=True, pair_1=pair_1, pair_2=pair_2, url_1=url1, url_2=url2)
