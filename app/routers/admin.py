from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/admin", tags=["Admin"])


# =========================================================
# ENV / SUPABASE
# =========================================================
def _need_env(name: str, val: str):
    if not val:
        raise HTTPException(status_code=500, detail=f"{name} not set")


def _get_env():
    return {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN", "").strip(),
        "GITHUB_OWNER": os.getenv("GITHUB_OWNER", "").strip(),
        "GITHUB_REPO": os.getenv("GITHUB_REPO", "").strip(),
        "RENDER_API_KEY": os.getenv("RENDER_API_KEY", "").strip(),
        "RENDER_SERVICE_ID": os.getenv("RENDER_SERVICE_ID", "").strip(),
        "VERCEL_DEPLOY_HOOK_URL": os.getenv("VERCEL_DEPLOY_HOOK_URL", "").strip(),
    }


def _get_supabase():
    env = _get_env()
    _need_env("SUPABASE_URL", env["SUPABASE_URL"])
    _need_env("SUPABASE_SERVICE_ROLE_KEY", env["SUPABASE_SERVICE_ROLE_KEY"])

    try:
        from supabase import create_client  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"supabase lib missing: {e}")

    try:
        return create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"supabase init failed: {e}")


# =========================================================
# AUTH
# =========================================================
async def _require_admin(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    sb = _get_supabase()

    try:
        u = sb.auth.get_user(token)
        user = getattr(u, "user", None) or (u.get("user") if isinstance(u, dict) else None)
        user_id = (
            (getattr(user, "id", None) if user else None)
            or (user.get("id") if isinstance(user, dict) and user else None)
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid session: {e}")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid session")

    try:
        prof = sb.table("profiles").select("id,role,email,full_name").eq("id", str(user_id)).single().execute()
        data = getattr(prof, "data", None) or (prof.get("data") if isinstance(prof, dict) else None) or {}
        role = str(data.get("role") or "user").lower().strip()
    except Exception as e:
        raise HTTPException(status_code=403, detail=f"Role check failed: {e}")

    if role not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="NOT_ADMIN")

    return {
        "user_id": str(user_id),
        "role": role,
        "email": data.get("email"),
        "full_name": data.get("full_name"),
    }


def _require_superadmin(ctx: Dict[str, Any]):
    if ctx.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="ONLY_SUPERADMIN")


# =========================================================
# HELPERS
# =========================================================
def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_grant_type(val: str) -> str:
    x = str(val or "").strip().lower()
    if x not in {"membership", "tokens", "bundle"}:
        raise HTTPException(status_code=400, detail="INVALID_GRANT_TYPE")
    return x


def _normalize_delivery_type(val: str) -> str:
    x = str(val or "").strip().lower()
    if x not in {"manual", "qr", "nfc"}:
        raise HTTPException(status_code=400, detail="INVALID_DELIVERY_TYPE")
    return x


def _normalize_stack_mode(val: str) -> str:
    x = str(val or "").strip().lower()
    if x not in {"extend", "replace", "ignore_if_active"}:
        raise HTTPException(status_code=400, detail="INVALID_STACK_MODE")
    return x


def _random_part(length: int = 6) -> str:
    import random
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(chars) for _ in range(length))


def _generate_campaign_code() -> str:
    return f"PROMO_{int(_utcnow().timestamp())}_{_random_part(4)}"


def _generate_promo_code() -> str:
    return f"ITK-{_random_part(4)}-{_random_part(4)}"


# =========================================================
# MODELS
# =========================================================
class RoleUpdateIn(BaseModel):
    user_id: str
    role: str


class GithubCommitIn(BaseModel):
    path: str
    content: str
    message: str
    branch: str = "main"


class PromoCampaignCreateIn(BaseModel):
    code: Optional[str] = None
    name: str
    description: Optional[str] = None
    is_active: bool = True
    grant_type: str = "membership"
    membership_months: int = Field(default=0, ge=0)
    token_amount: int = Field(default=0, ge=0)
    package_code: Optional[str] = "member"
    stack_mode: str = "extend"
    per_user_limit: int = Field(default=1, ge=1)
    max_total_redemptions: Optional[int] = Field(default=None, ge=1)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


class PromoCampaignUpdateIn(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    grant_type: Optional[str] = None
    membership_months: Optional[int] = Field(default=None, ge=0)
    token_amount: Optional[int] = Field(default=None, ge=0)
    package_code: Optional[str] = None
    stack_mode: Optional[str] = None
    per_user_limit: Optional[int] = Field(default=None, ge=1)
    max_total_redemptions: Optional[int] = Field(default=None, ge=1)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


class PromoCodeCreateIn(BaseModel):
    campaign_id: str
    code_value: Optional[str] = None
    delivery_type: str = "manual"
    nfc_uid: Optional[str] = None
    is_active: bool = True


class PromoCodeStatusIn(BaseModel):
    code_value: str
    is_active: bool


class ManualJetonLoadIn(BaseModel):
    user_id: str
    amount: int = Field(..., ge=1)
    note: Optional[str] = None


# =========================================================
# BASIC ADMIN
# =========================================================
@router.get("/me")
async def admin_me(ctx: Dict[str, Any] = Depends(_require_admin)):
    return {"ok": True, "me": ctx}


@router.get("/users")
async def list_users(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = (
            sb.table("profiles")
            .select(
                "id,email,full_name,role,tokens,created_at,last_login_at,"
                "selected_package_code,package_started_at,package_ends_at,"
                "promo_used_at,promo_code_used,has_ever_paid"
            )
            .order("created_at", desc=True)
            .limit(300)
            .execute()
        )
        return {"items": _safe_data(res) or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_users_failed: {e}")


@router.post("/users/role")
async def set_user_role(payload: RoleUpdateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)

    role = str(payload.role or "").lower().strip()
    if role not in {"user", "admin", "superadmin"}:
        raise HTTPException(status_code=400, detail="INVALID_ROLE")

    sb = _get_supabase()
    try:
        res = sb.table("profiles").update({"role": role}).eq("id", payload.user_id).execute()
        return {"ok": True, "result": _safe_data(res)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"role_update_failed: {e}")


# =========================================================
# PROMO CAMPAIGNS
# =========================================================
@router.get("/promo/campaigns")
async def list_promo_campaigns(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = (
            sb.table("promo_campaigns")
            .select("*")
            .order("created_at", desc=True)
            .limit(300)
            .execute()
        )
        return {"items": _safe_data(res) or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_promo_campaigns_failed: {e}")


@router.post("/promo/campaigns")
async def create_promo_campaign(payload: PromoCampaignCreateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()

    try:
        code = (payload.code or "").strip().upper() or _generate_campaign_code()
        grant_type = _normalize_grant_type(payload.grant_type)
        stack_mode = _normalize_stack_mode(payload.stack_mode)

        exists = sb.table("promo_campaigns").select("id,code").eq("code", code).maybe_single().execute()
        if _safe_data(exists):
            raise HTTPException(status_code=409, detail="CAMPAIGN_CODE_ALREADY_EXISTS")

        body = {
            "code": code,
            "name": payload.name.strip(),
            "description": payload.description,
            "is_active": payload.is_active,
            "grant_type": grant_type,
            "membership_months": payload.membership_months,
            "token_amount": payload.token_amount,
            "package_code": payload.package_code,
            "stack_mode": stack_mode,
            "per_user_limit": payload.per_user_limit,
            "max_total_redemptions": payload.max_total_redemptions,
            "starts_at": payload.starts_at,
            "ends_at": payload.ends_at,
        }

        res = sb.table("promo_campaigns").insert(body).execute()
        return {"ok": True, "item": _safe_data(res)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create_promo_campaign_failed: {e}")


@router.post("/promo/campaigns/update")
async def update_promo_campaign(payload: PromoCampaignUpdateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()

    try:
        patch: Dict[str, Any] = {}

        if payload.name is not None:
            patch["name"] = payload.name.strip()
        if payload.description is not None:
            patch["description"] = payload.description
        if payload.is_active is not None:
            patch["is_active"] = payload.is_active
        if payload.grant_type is not None:
            patch["grant_type"] = _normalize_grant_type(payload.grant_type)
        if payload.membership_months is not None:
            patch["membership_months"] = payload.membership_months
        if payload.token_amount is not None:
            patch["token_amount"] = payload.token_amount
        if payload.package_code is not None:
            patch["package_code"] = payload.package_code
        if payload.stack_mode is not None:
            patch["stack_mode"] = _normalize_stack_mode(payload.stack_mode)
        if payload.per_user_limit is not None:
            patch["per_user_limit"] = payload.per_user_limit
        if payload.max_total_redemptions is not None:
            patch["max_total_redemptions"] = payload.max_total_redemptions
        if payload.starts_at is not None:
            patch["starts_at"] = payload.starts_at
        if payload.ends_at is not None:
            patch["ends_at"] = payload.ends_at

        if not patch:
            raise HTTPException(status_code=400, detail="NO_FIELDS_TO_UPDATE")

        res = sb.table("promo_campaigns").update(patch).eq("id", payload.id).execute()
        return {"ok": True, "result": _safe_data(res)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_promo_campaign_failed: {e}")


# =========================================================
# PROMO CODES
# =========================================================
@router.get("/promo/codes")
async def list_promo_codes(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = (
            sb.table("promo_codes")
            .select("*, promo_campaigns(*)")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        return {"items": _safe_data(res) or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_promo_codes_failed: {e}")


@router.post("/promo/codes")
async def create_promo_code(payload: PromoCodeCreateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()

    try:
        delivery_type = _normalize_delivery_type(payload.delivery_type)
        code_value = (payload.code_value or "").strip().upper() or _generate_promo_code()

        campaign = sb.table("promo_campaigns").select("id,name").eq("id", payload.campaign_id).maybe_single().execute()
        campaign_row = _safe_data(campaign)
        if not campaign_row:
            raise HTTPException(status_code=404, detail="CAMPAIGN_NOT_FOUND")

        exists = sb.table("promo_codes").select("id,code_value").eq("code_value", code_value).maybe_single().execute()
        if _safe_data(exists):
            raise HTTPException(status_code=409, detail="PROMO_CODE_ALREADY_EXISTS")

        body = {
            "campaign_id": payload.campaign_id,
            "code_value": code_value,
            "delivery_type": delivery_type,
            "nfc_uid": payload.nfc_uid if delivery_type == "nfc" else None,
            "is_active": payload.is_active,
            "is_used": False,
        }

        res = sb.table("promo_codes").insert(body).execute()
        return {"ok": True, "item": _safe_data(res)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create_promo_code_failed: {e}")


@router.post("/promo/codes/status")
async def update_promo_code_status(payload: PromoCodeStatusIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()

    try:
        res = (
            sb.table("promo_codes")
            .update({"is_active": payload.is_active})
            .eq("code_value", payload.code_value.strip().upper())
            .execute()
        )
        return {"ok": True, "result": _safe_data(res)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_promo_code_status_failed: {e}")


# =========================================================
# PROMO REDEMPTIONS
# =========================================================
@router.get("/promo/redemptions")
async def list_promo_redemptions(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = (
            sb.table("promo_redemptions")
            .select("*")
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        return {"items": _safe_data(res) or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_promo_redemptions_failed: {e}")


# =========================================================
# MANUAL TOKEN LOAD
# =========================================================
@router.post("/wallet/manual-load")
async def manual_wallet_load(payload: ManualJetonLoadIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)
    sb = _get_supabase()

    try:
        prof = sb.table("profiles").select("tokens").eq("id", payload.user_id).maybe_single().execute()
        prof_row = _safe_data(prof)
        if not prof_row:
            raise HTTPException(status_code=404, detail="USER_NOT_FOUND")

        current_tokens = int(prof_row.get("tokens") or 0)
        next_tokens = current_tokens + payload.amount

        sb.table("profiles").update({"tokens": next_tokens}).eq("id", payload.user_id).execute()

        tx = {
            "user_id": payload.user_id,
            "type": "credit",
            "amount": payload.amount,
            "delta": payload.amount,
            "reason": "manual_admin_load",
            "note": payload.note or "Manual admin token load",
            "created_at": _iso(_utcnow())
        }
        sb.table("wallet_tx").insert(tx).execute()

        return {"ok": True, "tokens_after": next_tokens}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"manual_wallet_load_failed: {e}")


# =========================================================
# GITHUB / DEPLOY
# =========================================================
@router.post("/github/commit")
async def github_commit(payload: GithubCommitIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)

    env = _get_env()
    _need_env("GITHUB_TOKEN", env["GITHUB_TOKEN"])
    _need_env("GITHUB_OWNER", env["GITHUB_OWNER"])
    _need_env("GITHUB_REPO", env["GITHUB_REPO"])

    api = f"https://api.github.com/repos/{env['GITHUB_OWNER']}/{env['GITHUB_REPO']}/contents/{payload.path.lstrip('/')}"
    headers = {
        "Authorization": f"token {env['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "italky-admin-panel",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        sha = None
        r0 = await client.get(api, headers=headers, params={"ref": payload.branch})
        if r0.status_code == 200:
            sha = r0.json().get("sha")

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
    _require_superadmin(ctx)

    env = _get_env()
    _need_env("VERCEL_DEPLOY_HOOK_URL", env["VERCEL_DEPLOY_HOOK_URL"])

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(env["VERCEL_DEPLOY_HOOK_URL"])

    if r.status_code not in (200, 201, 202):
        raise HTTPException(status_code=502, detail=f"vercel_hook_failed {r.status_code}: {r.text[:300]}")
    return {"ok": True}


@router.post("/deploy/render")
async def deploy_render(ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)

    env = _get_env()
    _need_env("RENDER_API_KEY", env["RENDER_API_KEY"])
    _need_env("RENDER_SERVICE_ID", env["RENDER_SERVICE_ID"])

    url = f"https://api.render.com/v1/services/{env['RENDER_SERVICE_ID']}/deploys"
    headers = {"Authorization": f"Bearer {env['RENDER_API_KEY']}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json={})

    if r.status_code not in (200, 201, 202):
        raise HTTPException(status_code=502, detail=f"render_deploy_failed {r.status_code}: {r.text[:300]}")

    return {"ok": True, "render": r.json()}
