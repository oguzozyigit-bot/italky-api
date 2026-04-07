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

PACK_PRICE = 10
FREE_BRIDGE_LANG = "en"
DEFAULT_NATIVE_LANG = "tr"


class OfflineFileReq(BaseModel):
    user_id: str
    file_name: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_file_name(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def norm_lang_code(value: str) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def pack_name_to_lang(file_name: str) -> str:
    """
    Beklenen format:
      tr-offline
      de-offline
      fr-offline
    """
    name = norm_file_name(file_name)
    if name.endswith("-offline"):
      return name[:-8].strip("-")
    return name


def get_profile(user_id: str) -> dict:
    prof = (
        supabase.table("profiles")
        .select("id,tokens,native_lang")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(status_code=404, detail="profile not found")

    row = prof.data[0] or {}
    return {
        "id": row.get("id"),
        "tokens": int(row.get("tokens") or 0),
        "native_lang": norm_lang_code(row.get("native_lang") or DEFAULT_NATIVE_LANG),
    }


def is_free_lang(lang_code: str, native_lang: str) -> bool:
    code = norm_lang_code(lang_code)
    return code in {norm_lang_code(native_lang), FREE_BRIDGE_LANG}


def get_existing_download(user_id: str, file_name: str):
    result = (
        supabase.table("offline_files")
        .select("id,user_id,file_name,download_count,first_downloaded_at,last_downloaded_at")
        .eq("user_id", user_id)
        .eq("file_name", file_name)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def update_profile_tokens(user_id: str, next_tokens: int) -> None:
    supabase.table("profiles").update({
        "tokens": int(next_tokens)
    }).eq("id", user_id).execute()


def log_wallet_tx_best_effort(
    user_id: str,
    amount: int,
    reason: str,
    meta: dict | None = None
) -> None:
    """
    Jeton hareket kaydını best-effort yazar.
    wallet_tx tablosu yoksa veya kolonlar farklıysa sistemi bozmaz.
    """
    payload = {
        "user_id": user_id,
        "amount": amount,
        "reason": reason,
        "meta": meta or {},
        "created_at": now_iso(),
    }

    try:
        supabase.table("wallet_tx").insert(payload).execute()
    except Exception:
        # tablo/kolon farklı olabilir, akışı bozma
        pass


def insert_new_download(user_id: str, file_name: str) -> None:
    ts = now_iso()
    supabase.table("offline_files").insert({
        "user_id": user_id,
        "file_name": file_name,
        "download_count": 1,
        "first_downloaded_at": ts,
        "last_downloaded_at": ts
    }).execute()


def touch_existing_download(row_id: str, next_count: int) -> None:
    supabase.table("offline_files").update({
        "download_count": int(next_count),
        "last_downloaded_at": now_iso()
    }).eq("id", row_id).execute()


# =========================
# KONTROL
# Bu paket ücretsiz mi?
# Daha önce açılmış mı?
# Şimdi açılsa kaç jeton düşer?
# =========================
@router.post("/api/offline/files/check")
async def check_file(req: OfflineFileReq):
    user_id = (req.user_id or "").strip()
    file_name = norm_file_name(req.file_name)

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if not file_name:
        raise HTTPException(status_code=422, detail="file_name required")

    profile = get_profile(user_id)
    native_lang = profile["native_lang"]
    lang_code = pack_name_to_lang(file_name)

    existing = get_existing_download(user_id, file_name)
    free_pack = is_free_lang(lang_code, native_lang)

    if existing:
        return {
            "ok": True,
            "file_name": file_name,
            "lang_code": lang_code,
            "native_lang": native_lang,
            "already_downloaded": True,
            "download_count": int(existing.get("download_count") or 1),
            "free_pack": free_pack,
            "price_now": 0,
            "tokens": profile["tokens"],
        }

    return {
        "ok": True,
        "file_name": file_name,
        "lang_code": lang_code,
        "native_lang": native_lang,
        "already_downloaded": False,
        "download_count": 0,
        "free_pack": free_pack,
        "price_now": 0 if free_pack else PACK_PRICE,
        "tokens": profile["tokens"],
    }


# =========================
# AKTİVASYON / İNDİRME KAYDI
# native_lang + en ücretsiz
# diğer yeni diller ilk açılışta 10 jeton
# aynı paket tekrar istenirse tekrar jeton düşmez
# =========================
@router.post("/api/offline/files/activate")
async def activate_file(req: OfflineFileReq):
    user_id = (req.user_id or "").strip()
    file_name = norm_file_name(req.file_name)

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if not file_name:
        raise HTTPException(status_code=422, detail="file_name required")

    profile = get_profile(user_id)
    tokens = profile["tokens"]
    native_lang = profile["native_lang"]
    lang_code = pack_name_to_lang(file_name)
    free_pack = is_free_lang(lang_code, native_lang)

    existing = get_existing_download(user_id, file_name)

    # Paket zaten kayıtlıysa tekrar ücret düşme
    if existing:
        next_count = int(existing.get("download_count") or 1) + 1
        touch_existing_download(existing["id"], next_count)

        return {
            "ok": True,
            "file_name": file_name,
            "lang_code": lang_code,
            "native_lang": native_lang,
            "already_downloaded": True,
            "charged": 0,
            "free_pack": free_pack,
            "tokens": tokens,
            "download_count": next_count,
            "used_token": False,
        }

    # Yeni ücretsiz paket
    if free_pack:
        insert_new_download(user_id, file_name)

        return {
            "ok": True,
            "file_name": file_name,
            "lang_code": lang_code,
            "native_lang": native_lang,
            "already_downloaded": False,
            "charged": 0,
            "free_pack": True,
            "tokens": tokens,
            "download_count": 1,
            "used_token": False,
        }

    # Yeni ücretli paket
    if tokens < PACK_PRICE:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    next_tokens = tokens - PACK_PRICE
    update_profile_tokens(user_id, next_tokens)
    insert_new_download(user_id, file_name)

    log_wallet_tx_best_effort(
        user_id=user_id,
        amount=-PACK_PRICE,
        reason="offline_lang_pack_opened",
        meta={
            "file_name": file_name,
            "lang_code": lang_code,
            "native_lang": native_lang,
            "price": PACK_PRICE,
        }
    )

    return {
        "ok": True,
        "file_name": file_name,
        "lang_code": lang_code,
        "native_lang": native_lang,
        "already_downloaded": False,
        "charged": PACK_PRICE,
        "free_pack": False,
        "tokens": next_tokens,
        "download_count": 1,
        "used_token": True,
    }


# =========================
# LİSTE
# kullanıcının açtığı offline dil paketleri
# =========================
@router.get("/api/offline/files/list")
async def list_files(user_id: str):
    user_id = (user_id or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    profile = get_profile(user_id)
    native_lang = profile["native_lang"]

    rows = (
        supabase.table("offline_files")
        .select("*")
        .eq("user_id", user_id)
        .order("last_downloaded_at", desc=True)
        .execute()
    )

    items = rows.data or []

    normalized_items = []
    for row in items:
        file_name = norm_file_name(row.get("file_name") or "")
        lang_code = pack_name_to_lang(file_name)

        normalized_items.append({
            **row,
            "file_name": file_name,
            "lang_code": lang_code,
            "free_pack": is_free_lang(lang_code, native_lang),
            "price_model": 0 if is_free_lang(lang_code, native_lang) else PACK_PRICE,
        })

    return {
        "ok": True,
        "native_lang": native_lang,
        "pack_price": PACK_PRICE,
        "free_langs": [native_lang, FREE_BRIDGE_LANG],
        "items": normalized_items,
    }
