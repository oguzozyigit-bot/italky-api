from __future__ import annotations

import json
import os
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

from google.oauth2 import service_account
from google.auth.transport.requests import Request

router = APIRouter(prefix="/api/push", tags=["push"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Render Secret File yolu
GOOGLE_APPLICATION_CREDENTIALS = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/secrets/gcp-sa.json"
).strip()


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


def load_service_account_info() -> dict:
    if not os.path.exists(GOOGLE_APPLICATION_CREDENTIALS):
        raise HTTPException(
            status_code=500,
            detail=f"Service account file not found: {GOOGLE_APPLICATION_CREDENTIALS}"
        )

    try:
        with open(GOOGLE_APPLICATION_CREDENTIALS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Service account file read failed: {str(e)}")


def get_google_access_token() -> tuple[str, str]:
    info = load_service_account_info()
    project_id = str(info.get("project_id") or "").strip()

    if not project_id:
        raise HTTPException(status_code=500, detail="project_id missing in service account json")

    try:
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        credentials.refresh(Request())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google access token failed: {str(e)}")

    token = str(credentials.token or "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="Google access token empty")

    return token, project_id


def send_fcm_v1_to_token(token: str, data: dict) -> dict:
    access_token, project_id = get_google_access_token()

    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": data.get("title") or "italkyAI",
                "body": data.get("body") or "Yeni bildirimin var",
            },
            "data": {k: str(v) for k, v in data.items() if v is not None},
            "android": {
                "priority": "HIGH",
                "notification": {
                    "channel_id": "italky_arkadasla_channel",
                    "sound": "default",
                    "default_sound": True
                }
            }
        }
    }

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=payload,
        timeout=20,
    )

    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"FCM v1 send failed: {resp.text}")

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

    result = send_fcm_v1_to_token(token, data)
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
