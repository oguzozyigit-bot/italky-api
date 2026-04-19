from __future__ import annotations

import os
from typing import Optional

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


def safe_delete(table_name: str, column_name: str, user_id: str):
    try:
        supabase.table(table_name).delete().eq(column_name, user_id).execute()
    except Exception:
        pass


def safe_update_bound_nfc_cards(user_id: str):
    try:
        supabase.table("nfc_cards").update({
            "bound_user_id": None,
            "is_bound": False,
            "status": "new",
            "first_bound_at": None,
            "last_seen_at": None,
        }).eq("bound_user_id", user_id).execute()
    except Exception:
        pass


@router.post("/delete")
def delete_my_account(authorization: Optional[str] = Header(default=None)):
    user = get_user_from_token(authorization)
    user_id = str(user.id).strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="Geçersiz kullanıcı")

    try:
        # -------------------------------------------------
        # 1) Kullanıcıya bağlı alt kayıtları sil
        # Not:
        # user_access_state muhtemelen view olduğu için burada yok
        # trial_audit / promo geçmişi de tutulmayacak
        # -------------------------------------------------
        table_ops = [
            ("billing_purchases", "user_id"),
            ("course_sessions", "user_id"),
            ("debug_log", "user_id"),
            ("devices", "user_id"),
            ("exam_attempts", "user_id"),
            ("facetoface_sessions", "user_id"),
            ("interpreter_rooms", "user_id"),
            ("italky_cultural_memory", "user_id"),
            ("level_tests", "user_id"),
            ("nfc_entitlements", "user_id"),
            ("nfc_logs", "user_id"),
            ("offline_downloads", "user_id"),
            ("offline_files", "user_id"),
            ("practice_ai_memory", "user_id"),
            ("user_devices", "user_id"),
            ("user_weak_topics", "user_id"),
            ("usage_logs", "user_id"),
            ("wallet_tx", "user_id"),
            ("wallets", "user_id"),
            ("promo_redemptions", "user_id"),
            ("trial_audit", "user_id"),
        ]

        for table_name, column_name in table_ops:
            safe_delete(table_name, column_name, user_id)

        # -------------------------------------------------
        # 2) Bu kullanıcıya bağlı NFC kart varsa boşa çıkar
        # NFC artık iptal olsa da eski kart kayıtları kalmış olabilir
        # -------------------------------------------------
        safe_update_bound_nfc_cards(user_id)

        # -------------------------------------------------
        # 3) Profile kaydını tamamen sil
        # Resetlemek yerine komple silmek daha doğru
        # Çünkü kullanıcı sıfırdan başlamalı
        # -------------------------------------------------
        try:
            supabase.table("profiles").delete().eq("id", user_id).execute()
        except Exception:
            pass

        # -------------------------------------------------
        # 4) Eğer public.users gibi özel bir tablo varsa onu da sil
        # auth.users ayrı yönetilir, bu satır public users tablosu içindir
        # -------------------------------------------------
        try:
            supabase.table("users").delete().eq("id", user_id).execute()
        except Exception:
            pass

        # -------------------------------------------------
        # 5) En son auth kullanıcısını sil
        # -------------------------------------------------
        try:
            supabase.auth.admin.delete_user(user_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Auth kullanıcı silinemedi: {e}")

        return {
            "ok": True,
            "message": "Hesap kalıcı olarak silindi"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hesap silinemedi: {e}")
