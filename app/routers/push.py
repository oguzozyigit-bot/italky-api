from __future__ import annotations

import json
import os
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/push", tags=["push"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class SaveTokenBody(BaseModel):
    user_id: str
    token: str


class SendPushBody(BaseModel):
    user_id: str
    title: str
    body: str
    type: str = "generic"
    requester_name: Optional[str] = None
    peer_name: Optional[str] = None
    preview: Optional[str] = None
    room_id: Optional[str] = None
    role: Optional[str] = None
    my_lang: Optional[str] = None
    peer_lang: Optional[str] = None
    open_page: Optional[str] = None


def get_fcm_token(user_id: str) -> str:
    res = (
        supabase.table("profiles")
        .select("fcm_token")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    row = res.data or {}
    return str(row.get("fcm_token") or "").strip()


def send_fcm_to_token(token: str, data: dict) -> dict:
    if not FCM_SERVER_KEY:
        raise HTTPException(status_code=500, detail="FCM_SERVER_KEY missing")

    headers = {
        "Authorization": f"key={FCM_SERVER_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "to": token,
        "priority": "high",
        "data": data,
        "notification": {
            "title": data.get("title") or "italkyAI",
            "body": data.get("body") or "Yeni bildirimin var",
        },
    }

    resp = requests.post(
        "https://fcm.googleapis.com/fcm/send",
        headers=headers,
        data=json.dumps(payload),
        timeout=20,
    )

    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"FCM send failed: {resp.text}")

    return resp.json() or {"ok": True}


def push_to_user(
    user_id: str,
    *,
    title: str,
    body: str,
    type_: str = "generic",
    requester_name: Optional[str] = None,
    peer_name: Optional[str] = None,
    preview: Optional[str] = None,
    room_id: Optional[str] = None,
    role: Optional[str] = None,
    my_lang: Optional[str] = None,
    peer_lang: Optional[str] = None,
    open_page: Optional[str] = None,
) -> dict:
    token = get_fcm_token(user_id)
    if not token:
        return {"ok": False, "reason": "fcm_token_missing"}

    data = {
        "type": type_,
        "title": title,
        "body": body,
    }

    if requester_name:
        data["requester_name"] = requester_name
    if peer_name:
        data["peer_name"] = peer_name
    if preview:
        data["preview"] = preview
    if room_id:
        data["room_id"] = room_id
    if role:
        data["role"] = role
    if my_lang:
        data["my_lang"] = my_lang
    if peer_lang:
        data["peer_lang"] = peer_lang
    if open_page:
        data["open_page"] = open_page

    result = send_fcm_to_token(token, data)
    return {"ok": True, "result": result}


@router.post("/save-token")
def save_token(body: SaveTokenBody):
    user_id = str(body.user_id or "").strip()
    token = str(body.token or "").strip()

    if not user_id or not token:
        raise HTTPException(status_code=400, detail="user_id or token missing")

    (
        supabase.table("profiles")
        .update({"fcm_token": token})
        .eq("id", user_id)
        .execute()
    )

    return {"ok": True}


@router.post("/send")
def send_push(body: SendPushBody):
    return push_to_user(
        body.user_id,
        title=body.title,
        body=body.body,
        type_=body.type,
        requester_name=body.requester_name,
        peer_name=body.peer_name,
        preview=body.preview,
        room_id=body.room_id,
        role=body.role,
        my_lang=body.my_lang,
        peer_lang=body.peer_lang,
        open_page=body.open_page,
    )
