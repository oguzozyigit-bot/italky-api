# FILE: italky-api/app/routers/offline.py
from __future__ import annotations

import os
import time
from typing import Optional, Any, Dict

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from supabase import create_client, Client
import jwt

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
# SCHEMAS
# =============================
class OfflineLinkReq(BaseModel):
    base_lang: str
    target_lang: str


class OfflineLinkResp(BaseModel):
    ok: bool
    url: str


class FirstInstallReq(BaseModel):
    # İlk kurulumda kullanıcı ana dili seçebilir; şu an sadece TR için trial veriyoruz
    base_lang: str = Field(default="tr", max_length=8)
    target_lang: str = Field(default="en", max_length=8)


class PurchasePairReq(BaseModel):
    base_lang: str
    target_lang: str
    days: int = 365
    fee: int = 50  # jeton


class GenericResp(BaseModel):
    ok: bool
    detail: str = ""
    expires_at: Optional[str] = None  # ISO


# =============================
# HELPERS
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
    if expires_at is None:
        return 0
    if isinstance(expires_at, (int, float)):
        return int(expires_at)
    if isinstance(expires_at, datetime):
        dt = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if isinstance(expires_at, str):
        s = expires_at.strip()
        if not s:
            return 0
        if s.isdigit():
            return int(s)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return 0
    return 0


def _norm_lang(x: str) -> str:
    return (x or "").strip().lower().replace("_", "-")

def _pair(a: str, b: str) -> str:
    return f"{_norm_lang(a)}-{_norm_lang(b)}"


def _has_valid_pack(user_id: str, pair1: str, pair2: str) -> bool:
    now_ts = int(time.time())
    packs = (
        supabase
        .table("offline_packs")
        .select("lang_code, expires_at")
        .eq("user_id", user_id)
        .in_("lang_code", [pair1, pair2])
        .execute()
    )
    if not packs.data:
        return False

    for p in packs.data:
        exp = _to_epoch_seconds(p.get("expires_at"))
        if exp > now_ts:
            return True
    return False


def _signed_url(storage_path: str, ttl_sec: int = 60) -> str:
    signed = supabase.storage.from_(STORAGE_BUCKET).create_signed_url(storage_path, expires_in=ttl_sec)
    if not isinstance(signed, dict):
        return ""
    return (signed.get("signedURL") or signed.get("signedUrl") or "").strip()


# =============================
# ROUTES
# =============================

@router.post("/offline/get_link", response_model=OfflineLinkResp)
async def get_offline_link(req: OfflineLinkReq, authorization: Optional[str] = Header(None)):
    user_id = get_user_from_token(authorization)

    base = _norm_lang(req.base_lang)
    target = _norm_lang(req.target_lang)
    if not base or not target or base == target:
        raise HTTPException(status_code=400, detail="Dil parametresi hatalı")

    pair_code_1 = _pair(base, target)
    pair_code_2 = _pair(target, base)

    if not _has_valid_pack(user_id, pair_code_1, pair_code_2):
        raise HTTPException(status_code=403, detail="Offline paket yok / süresi dolmuş")

    # ✅ istenen yön indirilecek
    storage_path = f"langpacks/{pair_code_1}/model.zip"

    url = _signed_url(storage_path, ttl_sec=60)
    if not url:
        raise HTTPException(status_code=404, detail="Dosya bulunamadı (storage_path yanlış olabilir)")

    return OfflineLinkResp(ok=True, url=url)


@router.post("/offline/first_install", response_model=GenericResp)
async def first_install(req: FirstInstallReq, authorization: Optional[str] = Header(None)):
    """
    İlk kurulum denemesi:
    - Sadece 1 kez verilir (profiles.offline_trial_used = true)
    - Şu an: TR ↔ EN 30 gün
    """
    user_id = get_user_from_token(authorization)

    base = _norm_lang(req.base_lang or "tr")
    target = _norm_lang(req.target_lang or "en")

    if base != "tr" or target != "en":
        # şu an MVP: sadece TR-EN trial
        base = "tr"
        target = "en"

    # 1) trial kullanılmış mı?
    prof = supabase.table("profiles").select("offline_trial_used").eq("id", user_id).maybe_single().execute()
    used = bool((prof.data or {}).get("offline_trial_used"))
    if used:
        return GenericResp(ok=True, detail="trial_already_used")

    # 2) paketi yaz (TR-EN ve EN-TR)
    days = 30
    expires = datetime.now(timezone.utc).timestamp() + (days * 86400)
    expires_iso = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()

    p1 = _pair(base, target)  # tr-en
    p2 = _pair(target, base)  # en-tr

    # upsert
    for code in (p1, p2):
        supabase.table("offline_packs").upsert({
            "user_id": user_id,
            "lang_code": code,
            "expires_at": expires_iso,
            "source": "trial"
        }, on_conflict="user_id,lang_code").execute()

    # 3) flag set
    supabase.table("profiles").update({"offline_trial_used": True}).eq("id", user_id).execute()

    return GenericResp(ok=True, detail="trial_granted", expires_at=expires_iso)


@router.post("/offline/purchase_pair", response_model=GenericResp)
async def purchase_pair(req: PurchasePairReq, authorization: Optional[str] = Header(None)):
    """
    Ücretli satın alma:
    - fee jeton düşer
    - çift yön kaydedilir
    """
    user_id = get_user_from_token(authorization)

    base = _norm_lang(req.base_lang)
    target = _norm_lang(req.target_lang)
    if not base or not target or base == target:
        raise HTTPException(status_code=400, detail="Dil parametresi hatalı")

    fee = int(req.fee or 50)
    days = int(req.days or 365)
    if fee < 0 or days <= 0:
        raise HTTPException(status_code=400, detail="fee/days hatalı")

    # 1) tokens lock + read
    prof = supabase.table("profiles").select("tokens").eq("id", user_id).maybe_single().execute()
    tokens = int((prof.data or {}).get("tokens") or 0)
    if tokens < fee:
        raise HTTPException(status_code=402, detail="Not enough tokens")

    # 2) düş
    supabase.table("profiles").update({"tokens": tokens - fee}).eq("id", user_id).execute()

    # 3) packs upsert (çift yön)
    expires = datetime.now(timezone.utc).timestamp() + (days * 86400)
    expires_iso = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()

    p1 = _pair(base, target)
    p2 = _pair(target, base)

    for code in (p1, p2):
        supabase.table("offline_packs").upsert({
            "user_id": user_id,
            "lang_code": code,
            "expires_at": expires_iso,
            "source": "paid"
        }, on_conflict="user_id,lang_code").execute()

    return GenericResp(ok=True, detail="purchase_ok", expires_at=expires_iso)
