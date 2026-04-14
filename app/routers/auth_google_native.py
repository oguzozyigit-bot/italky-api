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

TRIAL_DAYS = 15

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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token",
        )

    sub = _safe_str(info.get("sub"))
    email = _safe_str(info.get("email"))
    full_name = _safe_str(info.get("name"))
    picture = _safe_str(info.get("picture"))

    if not sub or not email:
        raise HTTPException(status_code=400, detail="Google token missing sub/email")

    try:
        q = sb.table("profiles").select("*").eq("user_key", sub).limit(1).execute()
        existing = q.data[0] if q.data else None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase read failed: {str(e)}")

    try:
        now_dt = _now_utc()
        now_iso = now_dt.isoformat()
        trial_end_iso = (now_dt + timedelta(days=TRIAL_DAYS)).isoformat()

        if not existing:
            ins = {
                "user_key": sub,
                "email": email,
                "full_name": full_name,
                "avatar_url": picture,
                "tokens": 0,
                "last_login_at": now_iso,

                "trial_started_at": now_iso,
                "trial_ends_at": trial_end_iso,
                "trial_used": True,
                "membership_status": "trial",
            }

            created = sb.table("profiles").insert(ins).select("*").single().execute()
            profile = created.data

        else:
            upd = {
                "email": email,
                "full_name": full_name or existing.get("full_name"),
                "avatar_url": picture or existing.get("avatar_url"),
                "last_login_at": now_iso,
            }

            # Eski kullanıcıda trial kolonları boşsa bir kez doldur
            if not existing.get("trial_used"):
                upd["trial_started_at"] = now_iso
                upd["trial_ends_at"] = trial_end_iso
                upd["trial_used"] = True
                upd["membership_status"] = "trial"

            updated = (
                sb.table("profiles")
                .update(upd)
                .eq("user_key", sub)
                .select("*")
                .single()
                .execute()
            )
            profile = updated.data

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase write failed: {str(e)}")

    app_token = _create_jwt({"sub": sub, "email": email})

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
            "trial_started_at": profile.get("trial_started_at"),
            "trial_ends_at": profile.get("trial_ends_at"),
            "trial_used": bool(profile.get("trial_used") or False),
            "membership_status": profile.get("membership_status"),
            "membership_ends_at": profile.get("membership_ends_at"),
        },
    }
