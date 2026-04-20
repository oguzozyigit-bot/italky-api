from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

router = APIRouter(prefix="/api/admin/push", tags=["admin-push"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

FCM_SCOPE = ["https://www.googleapis.com/auth/firebase.messaging"]


class PushSendReq(BaseModel):
    target_mode: str = Field(..., description="single | all")
    user_id: Optional[str] = None
    title: str
    body: str
    push_type: str = "general"
    target_url: str = "/pages/home.html"


def _get_bearer(auth_header: str | None) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="missing_authorization")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="invalid_authorization")

    return parts[1].strip()


def _get_current_user(token: str) -> Dict[str, Any]:
    url = f"{SUPABASE_URL}/auth/v1/user"
    resp = requests.get(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {token}",
        },
        timeout=20,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid_session")

    data = resp.json() or {}
    if not data.get("id"):
        raise HTTPException(status_code=401, detail="user_not_found")

    return data


def _require_admin(user_id: str) -> Dict[str, Any]:
    res = (
        supabase.table("profiles")
        .select("id,email,is_admin,role")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    rows = getattr(res, "data", None) or []
    if not rows:
        raise HTTPException(status_code=403, detail="admin_profile_not_found")

    row = rows[0] or {}
    is_admin = bool(row.get("is_admin"))
    role = str(row.get("role") or "").strip().lower()

    if not is_admin and role not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="admin_required")

    return row


def _firebase_access_token() -> str:
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(status_code=500, detail="firebase_service_account_missing")
    if not FIREBASE_PROJECT_ID:
        raise HTTPException(status_code=500, detail="firebase_project_id_missing")

    try:
        info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=FCM_SCOPE)
        creds.refresh(GoogleAuthRequest())
        token = creds.token
        if not token:
            raise HTTPException(status_code=500, detail="firebase_access_token_empty")
        return token
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"firebase_auth_failed: {e}")


def _select_tokens_for_single(user_id: str) -> List[Dict[str, Any]]:
    res = (
        supabase.table("profiles")
        .select("id,email,fcm_token")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    return [r for r in rows if str(r.get("fcm_token") or "").strip()]


def _select_tokens_for_all() -> List[Dict[str, Any]]:
    res = (
        supabase.table("profiles")
        .select("id,email,fcm_token")
        .neq("fcm_token", "")
        .execute()
    )
    rows = getattr(res, "data", None) or []
    out = []
    seen = set()

    for row in rows:
        token = str(row.get("fcm_token") or "").strip()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(row)

    return out


def _send_fcm_message(
    access_token: str,
    device_token: str,
    title: str,
    body: str,
    push_type: str,
    target_url: str,
) -> Dict[str, Any]:
    url = f"https://fcm.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/messages:send"

    payload = {
        "message": {
            "token": device_token,
            "notification": {
                "title": title,
                "body": body,
            },
            "data": {
                "type": push_type,
                "title": title,
                "body": body,
                "target_url": target_url,
            },
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": (
                        "italky_priority_v1"
                        if push_type in {"membership", "payment", "bonus", "subscription_restored", "subscription_verified"}
                        else "italky_general_v1"
                    )
                },
            },
        }
    }

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=25,
    )

    ok = 200 <= resp.status_code < 300
    data = {}
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    return {
        "ok": ok,
        "status_code": resp.status_code,
        "response": data,
    }


@router.post("/send")
def admin_push_send(
    req: PushSendReq,
    authorization: str | None = Header(default=None),
):
    token = _get_bearer(authorization)
    current_user = _get_current_user(token)
    _require_admin(current_user["id"])

    target_mode = str(req.target_mode or "").strip().lower()
    title = str(req.title or "").strip()
    body = str(req.body or "").strip()
    push_type = str(req.push_type or "general").strip()
    target_url = str(req.target_url or "/pages/home.html").strip() or "/pages/home.html"

    if target_mode not in {"single", "all"}:
        raise HTTPException(status_code=422, detail="target_mode_must_be_single_or_all")

    if not title:
        raise HTTPException(status_code=422, detail="title_required")
    if not body:
        raise HTTPException(status_code=422, detail="body_required")

    if target_mode == "single":
        user_id = str(req.user_id or "").strip()
        if not user_id:
            raise HTTPException(status_code=422, detail="user_id_required_for_single")
        targets = _select_tokens_for_single(user_id)
    else:
        targets = _select_tokens_for_all()

    if not targets:
        return {
            "ok": False,
            "detail": "no_target_tokens_found",
            "sent": 0,
            "failed": 0,
            "results": [],
        }

    access_token = _firebase_access_token()

    results = []
    sent = 0
    failed = 0

    for row in targets:
        fcm_token = str(row.get("fcm_token") or "").strip()
        result = _send_fcm_message(
            access_token=access_token,
            device_token=fcm_token,
            title=title,
            body=body,
            push_type=push_type,
            target_url=target_url,
        )
        results.append(
            {
                "user_id": row.get("id"),
                "email": row.get("email"),
                "ok": result["ok"],
                "status_code": result["status_code"],
                "response": result["response"],
            }
        )
        if result["ok"]:
            sent += 1
        else:
            failed += 1

    return {
        "ok": sent > 0,
        "target_mode": target_mode,
        "sent": sent,
        "failed": failed,
        "results": results,
    }
