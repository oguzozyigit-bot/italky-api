# FILE: app/routers/interpreter.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3

logger = logging.getLogger("italky-interpreter")
router = APIRouter(tags=["interpreter"])


# ===============================
# GOOGLE TRANSLATE
# ===============================

GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()

_translate_client = None
_translate_project = None


def get_translate():
    global _translate_client, _translate_project

    if _translate_client:
        return _translate_client, _translate_project

    info = json.loads(GOOGLE_CREDS_JSON)

    creds = service_account.Credentials.from_service_account_info(info)
    _translate_client = translate_v3.TranslationServiceClient(credentials=creds)
    _translate_project = info["project_id"]

    return _translate_client, _translate_project


def translate_text(text, src, dst):

    if not text:
        return ""

    client, project = get_translate()

    parent = f"projects/{project}/locations/global"

    resp = client.translate_text(
        request={
            "parent": parent,
            "contents": [text],
            "target_language_code": dst,
            "source_language_code": src,
        }
    )

    return resp.translations[0].translated_text


# ===============================
# ROOM STRUCTURE
# ===============================

@dataclass
class Room:
    room_id: str
    host_code: str
    host_lang: str
    guest_lang: Optional[str] = None
    sockets: Set[WebSocket] = field(default_factory=set)


ROOMS: Dict[str, Room] = {}
HOST_ROOM: Dict[str, str] = {}

LOCK = asyncio.Lock()


def new_room():
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]


# ===============================
# MODELS
# ===============================

class CreateRoomReq(BaseModel):
    host_code: str
    my_lang: str


class JoinRoomReq(BaseModel):
    room_id: str
    my_lang: str


class ResolveRoomReq(BaseModel):
    host_code: str
    my_lang: str


# ===============================
# CREATE ROOM (HOST)
# ===============================

@router.post("/interpreter/create-room")
async def create_room(req: CreateRoomReq):

    host_code = req.host_code.strip().upper()

    async with LOCK:

        if host_code in HOST_ROOM:
            room_id = HOST_ROOM[host_code]
            return {"room_id": room_id}

        room_id = new_room()

        room = Room(
            room_id=room_id,
            host_code=host_code,
            host_lang=req.my_lang.lower()
        )

        ROOMS[room_id] = room
        HOST_ROOM[host_code] = room_id

    return {"room_id": room_id}


# ===============================
# RESOLVE ROOM (GUEST)
# ===============================

@router.post("/interpreter/resolve-room")
async def resolve_room(req: ResolveRoomReq):

    host_code = req.host_code.strip().upper()

    async with LOCK:

        room_id = HOST_ROOM.get(host_code)

        if not room_id:
            raise HTTPException(404, "Room not found")

    return {"room_id": room_id}


# ===============================
# JOIN ROOM
# ===============================

@router.post("/interpreter/join-room")
async def join_room(req: JoinRoomReq):

    room = ROOMS.get(req.room_id)

    if not room:
        raise HTTPException(404, "Room not found")

    room.guest_lang = req.my_lang.lower()

    return {"ok": True}


# ===============================
# WEBSOCKET
# ===============================

@router.websocket("/ws/interpreter/{room_id}")
async def ws_interpreter(ws: WebSocket, room_id: str):

    await ws.accept()

    role = ws.query_params.get("role", "guest")
    lang = ws.query_params.get("lang", "en")

    room = ROOMS.get(room_id)

    if not room:
        await ws.close()
        return

    room.sockets.add(ws)

    try:

        await ws.send_text(json.dumps({
            "type": "presence",
            "room_id": room_id,
            "host_lang": room.host_lang,
            "guest_lang": room.guest_lang
        }))

        while True:

            raw = await ws.receive_text()
            data = json.loads(raw)

            if data["type"] == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
                continue

            if data["type"] == "text_message":

                text = data["text"]
                src = data["from_lang"]
                dst = data["to_lang"]

                translated = translate_text(text, src, dst)

                payload = {
                    "type": "translated_message",
                    "sender": role,
                    "original_text": text,
                    "translated_text": translated
                }

                for s in list(room.sockets):
                    try:
                        await s.send_text(json.dumps(payload))
                    except:
                        pass

    except WebSocketDisconnect:
        room.sockets.discard(ws)
