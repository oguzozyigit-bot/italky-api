# FILE: italky-api/app/routers/admin.py
from __future__ import annotations

import base64
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/admin", tags=["Admin"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()

RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
VERCEL_DEPLOY_HOOK_URL = os.getenv("VERCEL_DEPLOY_HOOK_URL", "").strip()


def _need_env(name: str, val: str):
    if not val:
        raise HTTPException(status_code=500, detail=f"{name} not set")


def _get_supabase():
    """
    Lazy import + lazy init: ENV eksikse deploy patlatmasın, sadece endpoint çağrılınca hata versin.
    """
    _need_env("SUPABASE_URL", SUPABASE_URL)
    _need_env("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY)
    try:
        from supabase import create_client  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"supabase lib missing: {e}")
    try:
        return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"supabase init failed: {e}")


async def _require_admin(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """
    Authorization: Bearer <supabase_access_token>
    - Token doğrula: supabase.auth.get_user(token)
    - DB role kontrol: profiles.role in ('admin','superadmin')
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    sb = _get_supabase()

    # 1) user doğrula
    try:
        u = sb.auth.get_user(token)
        user = getattr(u, "user", None) or (u.get("user") if isinstance(u, dict) else None)
        user_id = (getattr(user, "id", None) if user else None) or (user.get("id") if isinstance(user, dict) and user else None)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid session: {e}")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid session")

    # 2) role kontrol
    try:
        prof = sb.table("profiles").select("id,role,email,full_name").eq("id", user_id).single().execute()
        data = getattr(prof, "data", None) or (prof.get("data") if isinstance(prof, dict) else None) or {}
        role = str(data.get("role") or "user").lower().strip()
    except Exception as e:
        raise HTTPException(status_code=403, detail=f"Role check failed: {e}")

    if role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="NOT_ADMIN")

    return {"user_id": user_id, "role": role}


class RoleUpdateIn(BaseModel):
    user_id: str
    role: str  # user/admin/superadmin


@router.get("/me")
async def admin_me(ctx: Dict[str, Any] = Depends(_require_admin)):
    return {"ok": True, "me": ctx}


@router.get("/users")
async def list_users(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = sb.table("profiles").select("id,email,full_name,role,tokens,created_at,last_login_at").order("created_at", desc=True).limit(200).execute()
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None) or []
        return {"items": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_users_failed: {e}")


@router.post("/users/role")
async def set_user_role(payload: RoleUpdateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    # superadmin değilse superadmin atamasını engelle
    if payload.role.lower().strip() == "superadmin" and ctx["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="ONLY_SUPERADMIN_CAN_SET_SUPERADMIN")

    sb = _get_supabase()
    try:
        res = sb.table("profiles").update({"role": payload.role.lower().strip()}).eq("id", payload.user_id).execute()
        return {"ok": True, "result": getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"role_update_failed: {e}")


class GithubCommitIn(BaseModel):
    path: str          # örn: pages/hangman.html
    content: str       # dosya içeriği (plain text)
    message: str       # commit mesajı
    branch: str = "main"


@router.post("/github/commit")
async def github_commit(payload: GithubCommitIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _need_env("GITHUB_TOKEN", GITHUB_TOKEN)
    _need_env("GITHUB_OWNER", GITHUB_OWNER)
    _need_env("GITHUB_REPO", GITHUB_REPO)

    # GitHub Contents API: önce mevcut sha al, sonra PUT ile commit
    api = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{payload.path.lstrip('/')}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "italky-admin-panel",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        sha = None
        r0 = await client.get(api, headers=headers, params={"ref": payload.branch})
        if r0.status_code == 200:
            j0 = r0.json()
            sha = j0.get("sha")

        b64 = base64.b64encode(payload.content.encode("utf-8")).decode("utf-8")
        body = {"message": payload.message, "content": b64, "branch": payload.branch}
        if sha:
            body["sha"] = sha

        r = await client.put(api, headers=headers, json=body)
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"github_commit_failed {r.status_code}: {r.text[:400]}")

        return {"ok": True, "path": payload.path, "branch": payload.branch}


@router.post("/deploy/vercel")
async def deploy_vercel(ctx: Dict[str, Any] = Depends(_require_admin)):
    _need_env("VERCEL_DEPLOY_HOOK_URL", VERCEL_DEPLOY_HOOK_URL)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(VERCEL_DEPLOY_HOOK_URL)
    if r.status_code not in (200, 201, 202):
        raise HTTPException(status_code=502, detail=f"vercel_hook_failed {r.status_code}: {r.text[:300]}")
    return {"ok": True}


@router.post("/deploy/render")
async def deploy_render(ctx: Dict[str, Any] = Depends(_require_admin)):
    _need_env("RENDER_API_KEY", RENDER_API_KEY)
    _need_env("RENDER_SERVICE_ID", RENDER_SERVICE_ID)
    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json={})
    if r.status_code not in (200, 201, 202):
        raise HTTPException(status_code=502, detail=f"render_deploy_failed {r.status_code}: {r.text[:300]}")
    return {"ok": True, "render": r.json()}
