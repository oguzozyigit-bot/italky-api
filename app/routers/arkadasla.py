from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional, Literal

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/arkadasla", tags=["arkadasla"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY eksik.")

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
    "Content-Type": "application/json",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    return parts[1].strip()


def get_current_user_id(auth_header: Optional[str]) -> str:
    token = _get_bearer(auth_header)
    url = f"{SUPABASE_URL}/auth/v1/user"
    resp = requests.get(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE,
            "Authorization": f"Bearer {token}",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid user session")

    data = resp.json() or {}
    uid = data.get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="User not found")
    return uid


def sb_select(
    table: str,
    select: str = "*",
    filters: Optional[dict[str, str]] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params: dict[str, str] = {"select": select}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)

    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} select failed: {resp.text}")
    return resp.json() or []


def sb_insert(table: str, payload: dict | list[dict]) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} insert failed: {resp.text}")
    return resp.json() or []


def sb_patch(table: str, filters: dict[str, str], payload: dict) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    resp = requests.patch(url, headers=headers, params=filters, json=payload, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} update failed: {resp.text}")
    return resp.json() or []


class StartRequestBody(BaseModel):
    target_code: str = Field(..., min_length=6, max_length=6)
    requester_lang: str = Field(default="tr", min_length=2, max_length=8)


class RespondRequestBody(BaseModel):
    request_id: str
    action: Literal["accept", "reject"]


class SendMessageBody(BaseModel):
    conversation_id: str
    text: str = Field(..., min_length=1, max_length=4000)
    source_lang: str = Field(default="tr", min_length=2, max_length=8)
    translated_text: Optional[str] = None


@router.get("/me")
def arkadasla_me(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "profiles",
        select="id,full_name,email,chat_code",
        filters={"id": f"eq.{user_id}"},
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Profile not found")

    profile = rows[0]
    return {
        "ok": True,
        "user_id": user_id,
        "chat_code": profile.get("chat_code"),
        "full_name": profile.get("full_name"),
        "email": profile.get("email"),
    }


@router.post("/request")
def send_chat_request(
    body: StartRequestBody,
    authorization: Optional[str] = Header(default=None),
):
    requester_id = get_current_user_id(authorization)

    my_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"id": f"eq.{requester_id}"},
        limit=1,
    )
    if not my_rows:
        raise HTTPException(status_code=404, detail="Requester profile not found")

    me = my_rows[0]
    my_code = str(me.get("chat_code") or "").strip()
    if not my_code:
        raise HTTPException(status_code=400, detail="Requester chat_code missing")

    if body.target_code == my_code:
        raise HTTPException(status_code=400, detail="Kendi koduna istek gönderemezsin")

    target_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"chat_code": f"eq.{body.target_code}"},
        limit=1,
    )
    if not target_rows:
        raise HTTPException(status_code=404, detail="Target code not found")

    target = target_rows[0]
    target_user_id = target["id"]

    existing = sb_select(
        "arkadasla_requests",
        select="id,status,requester_user_id,target_user_id",
        filters={
            "requester_user_id": f"eq.{requester_id}",
            "target_user_id": f"eq.{target_user_id}",
            "status": "eq.pending",
        },
        order="created_at.desc",
        limit=1,
    )
    if existing:
        return {
            "ok": True,
            "request_id": existing[0]["id"],
            "status": "pending",
            "message": "Zaten bekleyen bir istek var."
        }

    inserted = sb_insert("arkadasla_requests", {
        "id": str(uuid.uuid4()),
        "requester_user_id": requester_id,
        "target_user_id": target_user_id,
        "requester_code": my_code,
        "target_code": body.target_code,
        "requester_lang": body.requester_lang,
        "status": "pending",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    })

    row = inserted[0]
    return {
        "ok": True,
        "request_id": row["id"],
        "status": row["status"],
        "target_user_id": target_user_id,
        "target_name": target.get("full_name") or "Karşı Taraf",
    }


@router.get("/incoming")
def list_incoming_requests(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_requests",
        select="id,requester_user_id,target_user_id,requester_code,target_code,requester_lang,status,created_at",
        filters={
            "target_user_id": f"eq.{user_id}",
            "status": "eq.pending",
        },
        order="created_at.desc",
        limit=20,
    )

    if not rows:
        return {"ok": True, "items": []}

    requester_ids = ",".join(r["requester_user_id"] for r in rows if r.get("requester_user_id"))
    profiles = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"id": f"in.({requester_ids})"} if requester_ids else {},
        limit=50,
    )
    profile_map = {p["id"]: p for p in profiles}

    items = []
    for r in rows:
        rp = profile_map.get(r["requester_user_id"], {})
        items.append({
            "request_id": r["id"],
            "requester_user_id": r["requester_user_id"],
            "requester_name": rp.get("full_name") or "Karşı Taraf",
            "requester_code": r.get("requester_code"),
            "requester_lang": r.get("requester_lang") or "tr",
            "status": r.get("status"),
            "created_at": r.get("created_at"),
        })

    return {"ok": True, "items": items}


@router.post("/respond")
def respond_chat_request(
    body: RespondRequestBody,
    authorization: Optional[str] = Header(default=None),
):
    target_user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_requests",
        select="*",
        filters={
            "id": f"eq.{body.request_id}",
            "target_user_id": f"eq.{target_user_id}",
        },
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Request not found")

    req = rows[0]
    if req.get("status") != "pending":
        return {
            "ok": True,
            "request_id": req["id"],
            "status": req.get("status"),
            "message": "İstek zaten işlenmiş."
        }

    if body.action == "reject":
        updated = sb_patch(
            "arkadasla_requests",
            {"id": f"eq.{body.request_id}"},
            {"status": "rejected", "updated_at": utc_now_iso()},
        )[0]
        return {
            "ok": True,
            "request_id": updated["id"],
            "status": updated["status"],
        }

    requester_user_id = req["requester_user_id"]
    conversation_id = str(uuid.uuid4())

    conv = sb_insert("arkadasla_conversations", {
        "id": conversation_id,
        "user_a": requester_user_id,
        "user_b": target_user_id,
        "request_id": req["id"],
        "status": "active",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    })[0]

    updated = sb_patch(
        "arkadasla_requests",
        {"id": f"eq.{body.request_id}"},
        {
            "status": "accepted",
            "conversation_id": conversation_id,
            "updated_at": utc_now_iso(),
        },
    )[0]

    return {
        "ok": True,
        "request_id": updated["id"],
        "status": updated["status"],
        "conversation_id": conv["id"],
    }


@router.get("/conversation/current")
def get_current_conversation(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_conversations",
        select="*",
        filters={
            "status": "eq.active",
            "or": f"(user_a.eq.{user_id},user_b.eq.{user_id})",
        },
        order="updated_at.desc",
        limit=1,
    )
    if not rows:
        return {"ok": True, "conversation": None}

    conv = rows[0]
    other_user_id = conv["user_b"] if conv["user_a"] == user_id else conv["user_a"]

    profile_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"id": f"eq.{other_user_id}"},
        limit=1,
    )
    other = profile_rows[0] if profile_rows else {}

    return {
        "ok": True,
        "conversation": {
            "id": conv["id"],
            "status": conv["status"],
            "other_user_id": other_user_id,
            "other_name": other.get("full_name") or "Karşı Taraf",
            "other_code": other.get("chat_code"),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at"),
        }
    }


@router.post("/message")
def send_message(
    body: SendMessageBody,
    authorization: Optional[str] = Header(default=None),
):
    sender_user_id = get_current_user_id(authorization)

    conv_rows = sb_select(
        "arkadasla_conversations",
        select="*",
        filters={
            "id": f"eq.{body.conversation_id}",
            "status": "eq.active",
            "or": f"(user_a.eq.{sender_user_id},user_b.eq.{sender_user_id})",
        },
        limit=1,
    )
    if not conv_rows:
        raise HTTPException(status_code=404, detail="Active conversation not found")

    inserted = sb_insert("arkadasla_messages", {
        "id": str(uuid.uuid4()),
        "conversation_id": body.conversation_id,
        "sender_user_id": sender_user_id,
        "text": body.text,
        "source_lang": body.source_lang,
        "translated_text": body.translated_text,
        "created_at": utc_now_iso(),
    })[0]

    sb_patch(
        "arkadasla_conversations",
        {"id": f"eq.{body.conversation_id}"},
        {"updated_at": utc_now_iso()},
    )

    return {
        "ok": True,
        "message": inserted,
    }


@router.get("/messages")
def list_messages(
    conversation_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user_id = get_current_user_id(authorization)

    conv_rows = sb_select(
        "arkadasla_conversations",
        select="id,user_a,user_b,status",
        filters={
            "id": f"eq.{conversation_id}",
            "or": f"(user_a.eq.{user_id},user_b.eq.{user_id})",
        },
        limit=1,
    )
    if not conv_rows:
        raise HTTPException(status_code=404, detail="Conversation not found")

    rows = sb_select(
        "arkadasla_messages",
        select="*",
        filters={"conversation_id": f"eq.{conversation_id}"},
        order="created_at.asc",
        limit=500,
    )

    return {
        "ok": True,
        "items": rows,
    }


@router.post("/end")
def end_conversation(
    conversation_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_conversations",
        select="*",
        filters={
            "id": f"eq.{conversation_id}",
            "status": "eq.active",
            "or": f"(user_a.eq.{user_id},user_b.eq.{user_id})",
        },
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Active conversation not found")

    updated = sb_patch(
        "arkadasla_conversations",
        {"id": f"eq.{conversation_id}"},
        {"status": "ended", "updated_at": utc_now_iso()},
    )[0]

    return {
        "ok": True,
        "conversation_id": updated["id"],
        "status": updated["status"],
    }
