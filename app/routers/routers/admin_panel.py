from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client

router = APIRouter(prefix="/api/admin", tags=["admin"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# =========================================================
# MODELS
# =========================================================
class AdminLoginBody(BaseModel):
    email: EmailStr
    password: str


class AdminCreateUserBody(BaseModel):
    admin_user_id: str
    email: EmailStr
    password: str
    full_name: str = ""
    is_admin: bool = False


class AdminChangePasswordBody(BaseModel):
    admin_user_id: str
    target_user_id: str
    new_password: str


class AdminListUsersBody(BaseModel):
    admin_user_id: str
    limit: int = 50


class AdminToggleStatusBody(BaseModel):
    admin_user_id: str
    target_user_id: str
    blocked: bool


# =========================================================
# HELPERS
# =========================================================
def get_profile(user_id: str) -> Optional[dict]:
    res = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    return res.data or None


def require_admin(admin_user_id: str) -> dict:
    admin_user_id = str(admin_user_id or "").strip()
    if not admin_user_id:
        raise HTTPException(status_code=400, detail="admin_user_id gerekli")

    profile = get_profile(admin_user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Admin profili bulunamadı")

    if not bool(profile.get("is_admin")):
        raise HTTPException(status_code=403, detail="Bu işlem için admin yetkisi gerekli")

    return profile


def validate_password(pw: str) -> None:
    pw = str(pw or "")
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="Şifre en az 8 karakter olmalı")
    if len(pw) > 128:
        raise HTTPException(status_code=400, detail="Şifre çok uzun")


# =========================================================
# LOGIN CHECK
# Not: gerçek login frontend'de Supabase Auth ile olur.
# Bu endpoint sadece giriş yapan kullanıcının admin olup olmadığını doğrular.
# =========================================================
@router.post("/login-check")
def login_check(body: AdminLoginBody):
    # Burada email+password ile gerçek auth yapılmaz;
    # frontend Supabase login sonrası user_id ile admin-check çağırmalı.
    # Bu endpointi sade bırakıyoruz ki yanlış güven hissi vermeyelim.
    return {
        "ok": True,
        "message": "Gerçek giriş frontend Supabase Auth ile yapılmalı."
    }


@router.get("/me/{user_id}")
def admin_me(user_id: str):
    profile = require_admin(user_id)
    return {
        "ok": True,
        "user": {
            "id": profile.get("id"),
            "email": profile.get("email"),
            "full_name": profile.get("full_name") or "",
            "is_admin": bool(profile.get("is_admin")),
        }
    }


# =========================================================
# CREATE USER
# =========================================================
@router.post("/create-user")
def create_user(body: AdminCreateUserBody):
    require_admin(body.admin_user_id)
    validate_password(body.password)

    try:
        auth_res = supabase.auth.admin.create_user({
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
            "user_metadata": {
                "full_name": body.full_name or ""
            }
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Kullanıcı oluşturulamadı: {e}")

    new_user = getattr(auth_res, "user", None)
    if not new_user:
        raise HTTPException(status_code=500, detail="Auth user oluşturuldu ama user objesi dönmedi")

    user_id = str(getattr(new_user, "id", "") or "").strip()
    if not user_id:
        raise HTTPException(status_code=500, detail="Yeni kullanıcı id alınamadı")

    existing = get_profile(user_id)

    profile_payload = {
        "id": user_id,
        "email": str(body.email),
        "full_name": body.full_name or "",
        "is_admin": bool(body.is_admin),
        "app_access_mode": "basic",
    }

    if existing:
        supabase.table("profiles").update(profile_payload).eq("id", user_id).execute()
    else:
        supabase.table("profiles").insert(profile_payload).execute()

    return {
        "ok": True,
        "user": {
            "id": user_id,
            "email": str(body.email),
            "full_name": body.full_name or "",
            "is_admin": bool(body.is_admin),
        }
    }


# =========================================================
# CHANGE PASSWORD
# =========================================================
@router.post("/change-password")
def change_password(body: AdminChangePasswordBody):
    require_admin(body.admin_user_id)
    validate_password(body.new_password)

    target_user_id = str(body.target_user_id or "").strip()
    if not target_user_id:
        raise HTTPException(status_code=400, detail="target_user_id gerekli")

    try:
        supabase.auth.admin.update_user_by_id(
            target_user_id,
            {"password": body.new_password}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Şifre değiştirilemedi: {e}")

    return {
        "ok": True,
        "message": "Şifre başarıyla değiştirildi"
    }


# =========================================================
# LIST USERS
# =========================================================
@router.post("/list-users")
def list_users(body: AdminListUsersBody):
    require_admin(body.admin_user_id)

    limit = max(1, min(int(body.limit or 50), 200))

    res = (
        supabase.table("profiles")
        .select("id,email,full_name,is_admin,app_access_mode,jeton_balance,created_at,blocked")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    return {
        "ok": True,
        "users": res.data or []
    }


# =========================================================
# BLOCK / UNBLOCK
# =========================================================
@router.post("/toggle-block")
def toggle_block(body: AdminToggleStatusBody):
    require_admin(body.admin_user_id)

    target_user_id = str(body.target_user_id or "").strip()
    if not target_user_id:
        raise HTTPException(status_code=400, detail="target_user_id gerekli")

    supabase.table("profiles").update({
        "blocked": bool(body.blocked)
    }).eq("id", target_user_id).execute()

    return {
        "ok": True,
        "blocked": bool(body.blocked)
    }
