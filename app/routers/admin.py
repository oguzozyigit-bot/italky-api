from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone
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
def _normalize_source_type(source_type: str) -> str:
    val = str(source_type or "").strip().lower()
    if val not in {"playstore", "nfc_qr", "manual"}:
        raise HTTPException(status_code=400, detail="INVALID_SOURCE_TYPE")
    return val


def _normalize_status(status: str) -> str:
    val = str(status or "").strip().lower()
    if val not in {"active", "expired", "cancelled", "passive"}:
        raise HTTPException(status_code=400, detail="INVALID_STATUS")
    return val


def _normalize_card_status(status: str) -> str:
    val = str(status or "").strip().lower()
    if val not in {"new", "bound", "blocked", "passive"}:
        raise HTTPException(status_code=400, detail="INVALID_CARD_STATUS")
    return val


def _normalize_uid(uid: Optional[str]) -> Optional[str]:
    if uid is None:
        return None
    cleaned = str(uid).upper().replace(":", "").replace(" ", "").strip()
    return cleaned or None


def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _add_days(start_at: datetime, days: int) -> datetime:
    return start_at + timedelta(days=max(0, int(days)))


def _build_entitlement_from_package(
    *,
    user_id: str,
    package_row: Dict[str, Any],
    source_type: str,
    granted_by: str,
    card_uid: Optional[str] = None,
    purchase_token: Optional[str] = None,
    note: Optional[str] = None,
    started_at: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    now = _utcnow()
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")) if started_at else now

    if expires_at:
        expire_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    else:
        duration_days = int(package_row.get("duration_days") or 0)
        expire_dt = _add_days(start_dt, duration_days)

    return {
        "user_id": user_id,
        "card_uid": _normalize_uid(card_uid),
        "package_code": package_row["code"],
        "started_at": _iso(start_dt),
        "expires_at": _iso(expire_dt),
        "remaining_languages": int(package_row.get("language_limit") or 0),
        "remaining_jeton": int(package_row.get("jeton_amount") or 0),
        "can_use_text_to_text": bool(package_row.get("can_use_text_to_text", True)),
        "can_use_face_to_face": bool(package_row.get("can_use_face_to_face", False)),
        "can_use_side_to_side": bool(package_row.get("can_use_side_to_side", False)),
        "can_use_offline": bool(package_row.get("can_use_offline", False)),
        "can_use_clone_voice": bool(package_row.get("can_use_clone_voice", False)),
        "status": "active",
        "source_type": source_type,
        "purchase_token": purchase_token,
        "granted_by": granted_by,
        "note": note,
    }


def _apply_profile_access_fields(sb: Any, user_id: str, ent: Dict[str, Any]) -> None:
    package_code = ent["package_code"]
    started_at = ent["started_at"]
    expires_at = ent["expires_at"]
    remaining_jeton = int(ent.get("remaining_jeton") or 0)

    sb.table("profiles").update(
        {
            "selected_package_code": package_code,
            "package_active": True,
            "package_started_at": started_at,
            "package_ends_at": expires_at,
            "nfc_package_code": package_code if ent.get("source_type") == "nfc_qr" else None,
            "nfc_expires_at": expires_at if ent.get("source_type") == "nfc_qr" else None,
            "tokens": remaining_jeton,
        }
    ).eq("id", user_id).execute()


def _expire_profile_access_if_needed(sb: Any, user_id: str) -> None:
    now_iso = _iso(_utcnow())
    res = (
        sb.table("nfc_entitlements")
        .select("id,expires_at,status")
        .eq("user_id", user_id)
        .eq("status", "active")
        .gte("expires_at", now_iso)
        .order("expires_at", desc=True)
        .limit(1)
        .execute()
    )
    active_rows = _safe_data(res) or []
    if active_rows:
        return

    sb.table("profiles").update({"package_active": False}).eq("id", user_id).execute()


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


class PackageCreateIn(BaseModel):
    code: str
    name: str
    duration_days: int = Field(default=30, ge=1)
    language_limit: int = Field(default=0, ge=0)
    jeton_amount: int = Field(default=0, ge=0)
    can_use_text_to_text: bool = True
    can_use_face_to_face: bool = False
    can_use_side_to_side: bool = False
    can_use_offline: bool = False
    can_use_clone_voice: bool = False
    is_active: bool = True
    source_type: str = "nfc_qr"
    note: Optional[str] = None


class PackageUpdateIn(BaseModel):
    code: str
    name: Optional[str] = None
    duration_days: Optional[int] = Field(default=None, ge=1)
    language_limit: Optional[int] = Field(default=None, ge=0)
    jeton_amount: Optional[int] = Field(default=None, ge=0)
    can_use_text_to_text: Optional[bool] = None
    can_use_face_to_face: Optional[bool] = None
    can_use_side_to_side: Optional[bool] = None
    can_use_offline: Optional[bool] = None
    can_use_clone_voice: Optional[bool] = None
    is_active: Optional[bool] = None
    source_type: Optional[str] = None
    note: Optional[str] = None


class EntitlementAssignIn(BaseModel):
    user_id: str
    package_code: str
    source_type: str = "manual"
    card_uid: Optional[str] = None
    purchase_token: Optional[str] = None
    note: Optional[str] = None
    started_at: Optional[str] = None
    expires_at: Optional[str] = None


class EntitlementStatusIn(BaseModel):
    entitlement_id: int
    status: str


class NfcCardUpsertIn(BaseModel):
    uid: str
    serial_no: Optional[str] = None
    package_code: str
    is_active: bool = True
    expires_at: Optional[str] = None
    max_devices: int = Field(default=1, ge=1)
    status: str = "new"
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
                "selected_package_code,package_active,package_started_at,package_ends_at,"
                "nfc_card_uid,nfc_package_code,nfc_expires_at"
            )
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        items = _safe_data(res) or []

        user_ids = [str(x.get("id")) for x in items if x.get("id")]
        ent_map: Dict[str, Dict[str, Any]] = {}

        if user_ids:
            ent_res = (
                sb.table("nfc_entitlements")
                .select("id,user_id,package_code,source_type,started_at,expires_at,status,card_uid,note")
                .in_("user_id", user_ids)
                .order("created_at", desc=True)
                .execute()
            )
            ent_rows = _safe_data(ent_res) or []
            for row in ent_rows:
                uid = str(row.get("user_id") or "")
                if uid and uid not in ent_map:
                    ent_map[uid] = row

        enriched = []
        for item in items:
            uid = str(item.get("id") or "")
            last_ent = ent_map.get(uid) or {}
            item["access_source_type"] = last_ent.get("source_type")
            item["access_status"] = last_ent.get("status")
            item["access_card_uid"] = last_ent.get("card_uid")
            item["access_note"] = last_ent.get("note")
            enriched.append(item)

        return {"items": enriched}
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
# PACKAGES
# =========================================================
@router.get("/packages")
async def list_packages(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = (
            sb.table("nfc_packages")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        return {"items": _safe_data(res) or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_packages_failed: {e}")


@router.post("/packages")
async def create_package(payload: PackageCreateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)

    sb = _get_supabase()
    source_type = _normalize_source_type(payload.source_type)

    try:
        exists = sb.table("nfc_packages").select("id,code").eq("code", payload.code).maybe_single().execute()
        row = _safe_data(exists)
        if row:
            raise HTTPException(status_code=409, detail="PACKAGE_CODE_ALREADY_EXISTS")

        body = {
            "code": payload.code.strip(),
            "name": payload.name.strip(),
            "duration_days": payload.duration_days,
            "language_limit": payload.language_limit,
            "jeton_amount": payload.jeton_amount,
            "can_use_text_to_text": payload.can_use_text_to_text,
            "can_use_face_to_face": payload.can_use_face_to_face,
            "can_use_side_to_side": payload.can_use_side_to_side,
            "can_use_offline": payload.can_use_offline,
            "can_use_clone_voice": payload.can_use_clone_voice,
            "is_active": payload.is_active,
            "source_type": source_type,
            "note": payload.note,
        }

        res = sb.table("nfc_packages").insert(body).execute()
        return {"ok": True, "item": _safe_data(res)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create_package_failed: {e}")


@router.post("/packages/update")
async def update_package(payload: PackageUpdateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)

    sb = _get_supabase()
    try:
        patch: Dict[str, Any] = {}

        if payload.name is not None:
            patch["name"] = payload.name.strip()
        if payload.duration_days is not None:
            patch["duration_days"] = payload.duration_days
        if payload.language_limit is not None:
            patch["language_limit"] = payload.language_limit
        if payload.jeton_amount is not None:
            patch["jeton_amount"] = payload.jeton_amount
        if payload.can_use_text_to_text is not None:
            patch["can_use_text_to_text"] = payload.can_use_text_to_text
        if payload.can_use_face_to_face is not None:
            patch["can_use_face_to_face"] = payload.can_use_face_to_face
        if payload.can_use_side_to_side is not None:
            patch["can_use_side_to_side"] = payload.can_use_side_to_side
        if payload.can_use_offline is not None:
            patch["can_use_offline"] = payload.can_use_offline
        if payload.can_use_clone_voice is not None:
            patch["can_use_clone_voice"] = payload.can_use_clone_voice
        if payload.is_active is not None:
            patch["is_active"] = payload.is_active
        if payload.source_type is not None:
            patch["source_type"] = _normalize_source_type(payload.source_type)
        if payload.note is not None:
            patch["note"] = payload.note

        if not patch:
            raise HTTPException(status_code=400, detail="NO_FIELDS_TO_UPDATE")

        res = sb.table("nfc_packages").update(patch).eq("code", payload.code).execute()
        return {"ok": True, "result": _safe_data(res)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_package_failed: {e}")


# =========================================================
# ENTITLEMENTS
# =========================================================
@router.get("/entitlements")
async def list_entitlements(
    user_id: Optional[str] = None,
    source_type: Optional[str] = None,
    ctx: Dict[str, Any] = Depends(_require_admin),
):
    _require_superadmin(ctx)

    sb = _get_supabase()
    try:
        q = (
            sb.table("nfc_entitlements")
            .select("*")
            .order("created_at", desc=True)
            .limit(300)
        )

        if user_id:
            q = q.eq("user_id", user_id)
        if source_type:
            q = q.eq("source_type", _normalize_source_type(source_type))

        res = q.execute()
        return {"items": _safe_data(res) or []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_entitlements_failed: {e}")


@router.post("/entitlements/assign")
async def assign_entitlement(payload: EntitlementAssignIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    # admin ve superadmin ikisi de NFC/QR bağlayabilir
    sb = _get_supabase()
    source_type = _normalize_source_type(payload.source_type)
    card_uid = _normalize_uid(payload.card_uid)

    try:
        pkg_res = sb.table("nfc_packages").select("*").eq("code", payload.package_code).single().execute()
        package_row = _safe_data(pkg_res) or {}
        if not package_row:
            raise HTTPException(status_code=404, detail="PACKAGE_NOT_FOUND")

        if not bool(package_row.get("is_active", True)):
            raise HTTPException(status_code=400, detail="PACKAGE_NOT_ACTIVE")

        # admin yalnızca nfc_qr bağlayabilir
        if ctx.get("role") == "admin" and source_type != "nfc_qr":
            raise HTTPException(status_code=403, detail="ADMIN_ONLY_NFC_QR_ASSIGN")

        if source_type == "nfc_qr" and not card_uid:
            raise HTTPException(status_code=400, detail="CARD_UID_REQUIRED_FOR_NFC_QR")

        ent = _build_entitlement_from_package(
            user_id=payload.user_id,
            package_row=package_row,
            source_type=source_type,
            granted_by=ctx["user_id"],
            card_uid=card_uid,
            purchase_token=payload.purchase_token,
            note=payload.note,
            started_at=payload.started_at,
            expires_at=payload.expires_at,
        )

        ins = sb.table("nfc_entitlements").insert(ent).execute()
        inserted = _safe_data(ins)

        _apply_profile_access_fields(sb, payload.user_id, ent)

        if source_type == "nfc_qr" and card_uid:
            sb.table("nfc_cards").update(
                {
                    "is_bound": True,
                    "bound_user_id": payload.user_id,
                    "first_bound_at": _iso(_utcnow()),
                    "last_seen_at": _iso(_utcnow()),
                    "package_code": payload.package_code,
                    "expires_at": ent["expires_at"],
                    "status": "bound",
                }
            ).eq("uid", card_uid).execute()

        return {"ok": True, "item": inserted, "applied": ent}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"assign_entitlement_failed: {e}")


@router.post("/entitlements/status")
async def update_entitlement_status(payload: EntitlementStatusIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    _require_superadmin(ctx)

    sb = _get_supabase()
    status = _normalize_status(payload.status)

    try:
        res = (
            sb.table("nfc_entitlements")
            .update({"status": status, "updated_at": _iso(_utcnow())})
            .eq("id", payload.entitlement_id)
            .execute()
        )
        rows = _safe_data(res) or []
        user_id = None
        if rows and isinstance(rows, list):
            user_id = rows[0].get("user_id")
        elif isinstance(rows, dict):
            user_id = rows.get("user_id")

        if user_id and status != "active":
            _expire_profile_access_if_needed(sb, str(user_id))

        return {"ok": True, "result": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_entitlement_status_failed: {e}")


# =========================================================
# NFC / QR CARDS
# =========================================================
@router.get("/nfc/cards")
async def list_nfc_cards(ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    try:
        res = (
            sb.table("nfc_cards")
            .select("*")
            .order("created_at", desc=True)
            .limit(300)
            .execute()
        )
        return {"items": _safe_data(res) or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_nfc_cards_failed: {e}")


@router.post("/nfc/cards/upsert")
async def upsert_nfc_card(payload: NfcCardUpsertIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    # admin ve superadmin kart oluşturabilir
    sb = _get_supabase()
    uid = _normalize_uid(payload.uid)
    if not uid:
        raise HTTPException(status_code=400, detail="INVALID_UID")

    try:
        pkg = sb.table("nfc_packages").select("code,is_active,source_type").eq("code", payload.package_code).single().execute()
        pkg_row = _safe_data(pkg) or {}
        if not pkg_row:
            raise HTTPException(status_code=404, detail="PACKAGE_NOT_FOUND")

        serial_no = (payload.serial_no or "").strip() or None
        status = _normalize_card_status(payload.status or "new")

        body = {
            "uid": uid,
            "serial_no": serial_no,
            "package_code": payload.package_code,
            "is_active": payload.is_active,
            "expires_at": payload.expires_at,
            "max_devices": payload.max_devices,
            "status": status,
            "note": payload.note,
            "updated_at": _iso(_utcnow()),
        }

        existing = sb.table("nfc_cards").select("id,uid").eq("uid", uid).maybe_single().execute()
        old = _safe_data(existing)

        if old and old.get("id") is not None:
            res = sb.table("nfc_cards").update(body).eq("id", old["id"]).execute()
        else:
            body["created_at"] = _iso(_utcnow())
            res = sb.table("nfc_cards").insert(body).execute()

        return {"ok": True, "item": _safe_data(res)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upsert_nfc_card_failed: {e}")


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
