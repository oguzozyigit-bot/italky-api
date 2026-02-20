from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    # Render env'de bunlar mutlaka olmalı
    pass


class RoleIn(BaseModel):
    role: str


class TokensIn(BaseModel):
    delta: int


def _hdrs(token: Optional[str] = None) -> Dict[str, str]:
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    # user tokenı ile /auth/v1/user çekmek için ayrı header kullanacağız
    if token:
        h2 = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {token}",
        }
        return h2
    return h


async def _get_user_from_token(access_token: str) -> Dict[str, Any]:
    if not access_token:
        raise HTTPException(status_code=401, detail="NOT_AUTHENTICATED")

    url = f"{SUPABASE_URL}/auth/v1/user"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=_hdrs(access_token))
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="NOT_AUTHENTICATED")
    return r.json() or {}


async def _get_profile(user_id: str) -> Dict[str, Any]:
    url = f"{SUPABASE_URL}/rest/v1/profiles"
    params = {"select": "id,email,full_name,avatar_url,tokens,role,last_login_at", "id": f"eq.{user_id}", "limit": "1"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=_hdrs(), params=params)
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail="PROFILE_READ_FAILED")
    arr = r.json() or []
    return arr[0] if arr else {}


async def _require_admin(request: Request) -> Dict[str, Any]:
    auth = request.headers.get("authorization") or ""
    token = auth.replace("Bearer", "").strip()
    u = await _get_user_from_token(token)
    user_id = (u.get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="NOT_AUTHENTICATED")

    prof = await _get_profile(user_id)
    role = (prof.get("role") or "user").lower().strip()

    if role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="NOT_AUTHORIZED")

    return {
        "id": user_id,
        "email": u.get("email"),
        "role": role,
    }


@router.get("/admin/me")
async def admin_me(request: Request):
    me = await _require_admin(request)
    return me


@router.get("/admin/users")
async def admin_users(request: Request, only_admins: int = 0):
    _ = await _require_admin(request)

    url = f"{SUPABASE_URL}/rest/v1/profiles"
    select = "id,email,full_name,avatar_url,tokens,role,last_login_at"
    params = {"select": select, "order": "last_login_at.desc.nullslast", "limit": "200"}

    if only_admins:
        # role in (admin, superadmin)
        params["or"] = "(role.eq.admin,role.eq.superadmin)"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, headers=_hdrs(), params=params)

    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"USERS_READ_FAILED {r.status_code}")

    rows = r.json() or []
    out = []
    for p in rows:
        out.append({
            "id": p.get("id"),
            "email": p.get("email") or "",
            "name": p.get("full_name") or "Kullanıcı",
            "picture": p.get("avatar_url") or "",
            "tokens": int(p.get("tokens") or 0),
            "role": (p.get("role") or "user"),
            "last_login_at": p.get("last_login_at"),
        })
    return out


@router.post("/admin/user/{user_id}/role")
async def admin_set_role(request: Request, user_id: str, payload: RoleIn):
    me = await _require_admin(request)

    role = (payload.role or "").lower().strip()
    if role not in ("user", "moderator", "admin", "superadmin"):
        raise HTTPException(status_code=422, detail="INVALID_ROLE")

    # admin kendini superadmin yapamasın istersen burada kural koyarız
    if me["role"] != "superadmin" and role in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="ONLY_SUPERADMIN_CAN_ASSIGN_ADMIN")

    url = f"{SUPABASE_URL}/rest/v1/profiles"
    params = {"id": f"eq.{user_id}"}
    body = {"role": role}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.patch(url, headers=_hdrs(), params=params, json=body)

    if r.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"ROLE_UPDATE_FAILED {r.status_code}")

    return {"ok": True, "id": user_id, "role": role}


@router.post("/admin/user/{user_id}/tokens")
async def admin_tokens(request: Request, user_id: str, payload: TokensIn):
    me = await _require_admin(request)

    delta = int(payload.delta or 0)
    if delta == 0:
        raise HTTPException(status_code=422, detail="DELTA_REQUIRED")

    # mevcut tokenı çek
    prof = await _get_profile(user_id)
    cur = int(prof.get("tokens") or 0)
    newv = max(0, cur + delta)

    url = f"{SUPABASE_URL}/rest/v1/profiles"
    params = {"id": f"eq.{user_id}"}
    body = {"tokens": newv}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.patch(url, headers=_hdrs(), params=params, json=body)

    if r.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"TOKENS_UPDATE_FAILED {r.status_code}")

    return {"ok": True, "id": user_id, "tokens": newv}
