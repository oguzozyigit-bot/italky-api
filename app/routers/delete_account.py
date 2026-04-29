# FILE: account.py

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from supabase import create_client

router = APIRouter(prefix="/api/account", tags=["account"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL veya SUPABASE_SERVICE_ROLE_KEY eksik")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_user_from_token(auth_header: Optional[str]):
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Geçersiz token")

    try:
        user_res = supabase.auth.get_user(token)
        user = getattr(user_res, "user", None)

        if not user or not getattr(user, "id", None):
            raise HTTPException(status_code=401, detail="Kullanıcı alınamadı")

        return user

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Oturum doğrulanamadı: {e}")


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def safe_execute(label: str, fn):
    try:
        return fn()
    except Exception as e:
        # Silme akışını tek tablo yüzünden patlatmıyoruz.
        # Log gerekiyorsa Render loglarında bu print görünür.
        print(f"[account-delete] skip {label}: {e}")
        return None


def safe_delete_eq(table_name: str, column_name: str, value: Any):
    if value is None or str(value).strip() == "":
        return

    return safe_execute(
        f"delete {table_name}.{column_name}",
        lambda: supabase.table(table_name).delete().eq(column_name, value).execute()
    )


def safe_delete_or_user_email(table_name: str, user_id: str, email: str):
    """
    Bazı tablolarda sadece user_id var, bazılarında email de olabilir.
    Her ihtimale karşı ikisini de dener.
    """
    safe_delete_eq(table_name, "user_id", user_id)

    if email:
        safe_delete_eq(table_name, "email", email)


def safe_update_bound_nfc_cards(user_id: str):
    safe_execute(
        "reset nfc_cards",
        lambda: supabase.table("nfc_cards").update({
            "bound_user_id": None,
            "is_bound": False,
            "status": "new",
            "first_bound_at": None,
            "last_seen_at": None,
        }).eq("bound_user_id", user_id).execute()
    )


def delete_profile_records(user_id: str, email: str):
    """
    Kritik kısım:
    Sadece id ile değil, email ile de siliyoruz.
    Böylece auth user silindikten sonra kalan orphan profile,
    aynı Google hesabıyla yeniden girişte çakışmaz.
    """
    safe_delete_eq("profiles", "id", user_id)

    if email:
        safe_delete_eq("profiles", "email", email)
        safe_delete_eq("profiles", "user_key", email)


def delete_public_user_records(user_id: str, email: str):
    safe_delete_eq("users", "id", user_id)

    if email:
        safe_delete_eq("users", "email", email)


@router.post("/delete")
def delete_my_account(authorization: Optional[str] = Header(default=None)):
    user = get_user_from_token(authorization)

    user_id = str(getattr(user, "id", "") or "").strip()
    user_email = normalize_email(getattr(user, "email", ""))

    if not user_id:
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    try:
        # -------------------------------------------------
        # Kullanıcıya bağlı alt kayıtları temizle
        # -------------------------------------------------
        user_tables = [
            "billing_purchases",
            "course_sessions",
            "debug_log",
            "devices",
            "exam_attempts",
            "facetoface_sessions",
            "interpreter_rooms",
            "italky_cultural_memory",
            "level_tests",
            "nfc_entitlements",
            "nfc_logs",
            "offline_downloads",
            "offline_files",
            "offline_packs",
            "offline_packages",
            "practice_ai_memory",
            "user_devices",
            "user_weak_topics",
            "usage_logs",
            "wallet_tx",
            "wallets",
            "promo_redemptions",
            "trial_audit",
        ]

        for table_name in user_tables:
            safe_delete_or_user_email(table_name, user_id, user_email)

        # -------------------------------------------------
        # Oda / meeting gibi tablolarda farklı kolonlar olabilir
        # -------------------------------------------------
        safe_delete_eq("interpreter_rooms", "created_by", user_id)
        safe_delete_eq("facetoface_sessions", "created_by", user_id)
        safe_delete_eq("course_sessions", "created_by", user_id)

        # -------------------------------------------------
        # Eski NFC bağları varsa boşa çıkar
        # -------------------------------------------------
        safe_update_bound_nfc_cards(user_id)

        # -------------------------------------------------
        # Profile ve public users kayıtlarını en sonda temizle
        # Hem id hem email üzerinden temizlik yapıyoruz.
        # -------------------------------------------------
        delete_profile_records(user_id, user_email)
        delete_public_user_records(user_id, user_email)

        # -------------------------------------------------
        # En son auth kullanıcısını sil
        # -------------------------------------------------
        try:
            supabase.auth.admin.delete_user(user_id)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Auth kullanıcı silinemedi: {e}"
            )

        return {
            "ok": True,
            "message": "Hesap kalıcı olarak silindi",
            "deleted_user_id": user_id,
            "deleted_email": user_email,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hesap silinemedi: {e}")
