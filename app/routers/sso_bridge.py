from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Header, HTTPException, Query

from app.routers.session import get_current_user_id, supabase

router = APIRouter(prefix="/api/sso", tags=["sso"])

ICANY_ORIGIN = os.getenv("ICANY_ORIGIN", "https://www.icany.ai").strip().rstrip("/")
BRIDGE_SECRET = os.getenv("ICANY_ITALKY_BRIDGE_SECRET", "").strip()

_ALLOWED_NEXT = (
    "/hosgeldiniz",
    "/personal",
    "/deneme",
    "/discover",
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _safe_next(value: str | None) -> str:
    path = str(value or "/hosgeldiniz").strip().split("?", 1)[0]
    if not path.startswith("/") or path.startswith("//"):
        return "/hosgeldiniz"
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in _ALLOWED_NEXT):
        return path
    return "/hosgeldiniz"


def _create_handoff_token(*, member_id: str, email: str, name: str, ttl_seconds: int = 120) -> str:
    if not BRIDGE_SECRET:
        raise HTTPException(status_code=503, detail="SSO bridge secret tanımlı değil")

    payload = {
        "memberId": str(member_id or "").strip(),
        "email": str(email or "").strip().lower(),
        "name": str(name or "").strip()[:80],
        "exp": int(time.time()) + max(60, min(int(ttl_seconds), 300)),
    }
    if not payload["memberId"] or not payload["email"]:
        raise HTTPException(status_code=422, detail="Kullanıcı kimliği veya e-posta eksik")

    body = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = hmac.new(BRIDGE_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


@router.get("/icany-handoff")
def create_icany_handoff(
    next_path: str = Query(default="/hosgeldiniz", alias="next"),
    authorization: str | None = Header(default=None),
):
    user_id = get_current_user_id(authorization)

    result = (
        supabase.table("profiles")
        .select("id,email,full_name")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    profile = result.data or {}

    email = str(profile.get("email") or "").strip().lower()
    name = str(profile.get("full_name") or email.split("@")[0] or "italkyAI").strip()
    if not email:
        raise HTTPException(status_code=422, detail="Profil e-postası bulunamadı")

    safe_next = _safe_next(next_path)
    token = _create_handoff_token(member_id=user_id, email=email, name=name)
    query = urlencode({"token": token, "next": safe_next})

    return {
        "ok": True,
        "url": f"{ICANY_ORIGIN}/api/bridge/enter?{query}",
        "next": safe_next,
        "expires_in": 120,
    }
