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

from supabase import create_client

logger = logging.getLogger("italky-interpreter")
router = APIRouter(tags=["interpreter"])


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)


# =========================================================
# ROOM DATA
# =========================================================

@dataclass
class PeerState:
    role: str
    user_id: str
    full_name: str
    avatar_url: Optional[str]
    lang: str
    voice: Optional[str] = None
    joined_at: float = field(default_factory=lambda: time.time())


@dataclass
class RoomState:
    room_id: str
    host_code: str
    mode: str
    host_lang: str
    guest_lang: Optional[str] = None
    status: str = "waiting"
    peers: Dict[str, PeerState] = field(default_factory=dict)
    sockets: Set[WebSocket] = field(default_factory=set)


ROOMS: Dict[str, RoomState] = {}
HOST_ACTIVE_ROOM: Dict[str, str] = {}
ROOM_LOCK = asyncio.Lock()


# =========================================================
# HELPERS
# =========================================================

def new_room_id() -> str:
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]


def now_ts() -> int:
    return int(time.time())


async def broadcast(room: RoomState, payload: dict):

    dead = []
    text = json.dumps(payload, ensure_ascii=False)

    for ws in list(room.sockets):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)

    for ws in dead:
        room.sockets.discard(ws)


# =========================================================
# PROFILE READ
# =========================================================

def get_profile(user_id):

    try:
        r = supabase.table("profiles").select(
            "full_name,avatar_url,tts_voice"
        ).eq("id", user_id).single().execute()

        if not r.data:
            return None

        return r.data

    except Exception:
        return None


# =========================================================
# CREATE ROOM
# =========================================================

class CreateRoomReq(BaseModel):

    host_code: str
    my_lang: str = "tr"
    user_id: str
    mode: str = "interpreter"


@router.post("/interpreter/create-room")
async def create_room(req: CreateRoomReq):

    host_code = req.host_code.strip().upper()
    user_id = req.user_id
    host_lang = req.my_lang

    profile = get_profile(user_id)

    if not profile:
        raise HTTPException(400, "Profile bulunamadı")

    room_id = new_room_id()

    async with ROOM_LOCK:

        room = RoomState(
            room_id=room_id,
            host_code=host_code,
            mode=req.mode,
            host_lang=host_lang,
        )

        room.peers["host"] = PeerState(
            role="host",
            user_id=user_id,
            full_name=profile["full_name"],
            avatar_url=profile["avatar_url"],
            lang=host_lang,
            voice=profile.get("tts_voice"),
        )

        ROOMS[room_id] = room
        HOST_ACTIVE_ROOM[host_code] = room_id

    return {
        "ok": True,
        "room_id": room_id,
    }


# =========================================================
# RESOLVE ROOM
# =========================================================

class ResolveRoomReq(BaseModel):

    host_code: str
    my_lang: str
    user_id: str


@router.post("/interpreter/resolve-room")
async def resolve_room(req: ResolveRoomReq):

    host_code = req.host_code.strip().upper()
    user_id = req.user_id
    guest_lang = req.my_lang

    profile = get_profile(user_id)

    if not profile:
        raise HTTPException(400, "Profile bulunamadı")

    async with ROOM_LOCK:

        room_id = HOST_ACTIVE_ROOM.get(host_code)

        if not room_id:
            raise HTTPException(404, "Room bulunamadı")

        room = ROOMS.get(room_id)

        if not room:
            raise HTTPException(404, "Room bulunamadı")

        room.guest_lang = guest_lang

        room.peers["guest"] = PeerState(
            role="guest",
            user_id=user_id,
            full_name=profile["full_name"],
            avatar_url=profile["avatar_url"],
            lang=guest_lang,
            voice=profile.get("tts_voice"),
        )

        room.status = "active"

    return {
        "ok": True,
        "room_id": room.room_id
    }


# =========================================================
# WEBSOCKET
# =========================================================

@router.websocket("/ws/interpreter/{room_id}")
async def interpreter_ws(websocket: WebSocket, room_id: str):

    await websocket.accept()

    role = websocket.query_params.get("role") or "guest"
    lang = websocket.query_params.get("lang") or "en"

    room = ROOMS.get(room_id)

    if not room:
        await websocket.close()
        return

    room.sockets.add(websocket)

    peer = room.peers.get(role)

    await broadcast(room, {
        "type": "presence",
        "room_id": room_id,
        "peer_count": len(room.peers),
        "host_lang": room.host_lang,
        "guest_lang": room.guest_lang,
        "peer_name": peer.full_name,
        "peer_avatar": peer.avatar_url,
        "peer_voice": peer.voice,
        "ts": now_ts()
    })

    try:

        while True:

            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data["type"] == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            if data["type"] == "text_message":

                text = data["text"]

                await broadcast(room, {
                    "type": "translated_message",
                    "sender": role,
                    "original_text": text,
                    "translated_text": text,
                    "sender_name": peer.full_name,
                    "sender_avatar": peer.avatar_url,
                    "sender_voice": peer.voice,
                    "ts": now_ts()
                })

    except WebSocketDisconnect:

        room.sockets.discard(websocket)

        await broadcast(room, {
            "type": "peer_left",
            "sender": role,
            "sender_name": peer.full_name,
            "ts": now_ts()
        })
