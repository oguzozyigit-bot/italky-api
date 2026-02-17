from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from jose import jwt

from supabase import create_client, Client

router = APIRouter()

# ---------- ENV ----------
GOOGLE_WEB_CLIENT_ID = os.getenv("GOOGLE_WEB_CLIENT_ID", "").strip()
JWT_SECRET = os.getenv("ITALKY_JWT_SECRET", "").strip()
JWT_ALG = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("ITALKY_JWT_EXPIRE_DAYS", "7"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not GOOGLE_WEB_CLIENT_ID:
    raise RuntimeError("Missing env: GOOGLE_WEB_CLIENT_ID")
if not JWT_SECRET:
    raise RuntimeError("Missing env: ITALKY_JWT_SECRET")
if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing env: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------- Models ----------
class GoogleNativeAuthIn(BaseModel):
    id_token: str

class GoogleNativeAuthOut(BaseModel):
    token: str
    user: Dict[str, Any]

# ---------- Helpers ----------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _create_jwt(payload: dict) -> str:
    exp = _now_utc() + timedelta(days=JWT_EXPIRE_DAYS)
    to_encode = dict(payload)
    to_encode["exp"] = int(exp.timestamp())
    to_encode["iat"] = int(_now_utc().timestamp())
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALG)

def _safe_str(x: Optional[str]) -> str:
    return (x or "").strip()

@router.post("/auth/google-native", response_model=GoogleNativeAuthOut)
async def auth_google_native(body: GoogleNativeAuthIn):
    # 1) verify google id_token
    token = _safe_str(body.id_token)
    if not token:
        raise HTTPException(status_code=400, detail="Missing id_token")

    try:
        info = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_WEB_CLIENT_ID,
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token")

    sub = _safe_str(info.get("sub"))
    email = _safe_str(info.get("email"))
    full_name = _safe_str(info.get("name"))
    picture = _safe_str(info.get("picture"))

    if not sub or not email:
        raise HTTPException(status_code=400, detail="Google token missing sub/email")

    # 2) find or create profile by user_key
    # Senin profiles kolonları: id(uuid), user_key(text), full_name(text), email(text), avatar_url(text),
    # site_lang(text), created_at(timestamptz), tokens(bigint), member_no(text), offline_langs(jsonb), last_login_at(timestamptz)
    try:
        q = sb.table("profiles").select("*").eq("user_key", sub).limit(1).execute()
        existing = q.data[0] if q.data else None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase read failed: {str(e)}")

    try:
        if not existing:
            # yeni kullanıcı => 400 token hediye
            ins = {
                "user_key": sub,
                "email": email,
                "full_name": full_name,
                "avatar_url": picture,
                "tokens": 400,
                "last_login_at": _now_utc().isoformat(),
            }
            created = sb.table("profiles").insert(ins).select("*").single().execute()
            profile = created.data
        else:
            # mevcut kullanıcı => last_login ve profil bilgilerini güncelle (email/name/pic değişmiş olabilir)
            upd = {
                "email": email,
                "full_name": full_name or existing.get("full_name"),
                "avatar_url": picture or existing.get("avatar_url"),
                "last_login_at": _now_utc().isoformat(),
            }
            updated = sb.table("profiles").update(upd).eq("user_key", sub).select("*").single().execute()
            profile = updated.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase write failed: {str(e)}")

    # 3) issue our JWT (your app token)
    app_token = _create_jwt({"sub": sub, "email": email})

    # 4) return
    return {
        "token": app_token,
        "user": {
            "id": profile.get("id"),
            "user_key": profile.get("user_key"),
            "email": profile.get("email"),
            "full_name": profile.get("full_name"),
            "picture": profile.get("avatar_url"),
            "tokens": int(profile.get("tokens") or 0),
            "member_no": profile.get("member_no"),
            "created_at": profile.get("created_at"),
            "last_login_at": profile.get("last_login_at"),
        }
    }
