# FILE: app/routers/interpreter.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Set, List

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from google.oauth2 import service_account
from google.cloud import translate as translate_v3

logger = logging.getLogger("italky-interpreter")
router = APIRouter(tags=["interpreter"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()

_translate_client: Optional[translate_v3.TranslationServiceClient] = None
_translate_project_id: Optional[str] = None


def _load_credentials_info() -> dict:
    if GOOGLE_CREDS_JSON:
        try:
            return json.loads(GOOGLE_CREDS_JSON)
        except Exception as e:
            raise RuntimeError(f"Invalid GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")

    if GOOGLE_CREDS_PATH:
        if not os.path.exists(GOOGLE_CREDS_PATH):
            raise RuntimeError(f"Credentials file not found: {GOOGLE_CREDS_PATH}")
        try:
            with open(GOOGLE_CREDS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            raise RuntimeError(f"Could not read credentials file: {e}")

    raise RuntimeError("Missing Google credentials.")


def _get_translate_client_and_project():
    global _translate_client, _translate_project_id

    info = _load_credentials_info()

    if _translate_client is None:
        creds = service_account.Credentials.from_service_account_info(info)
        _translate_client = translate_v3.TranslationServiceClient(credentials=creds)

    if not _translate_project_id:
        _translate_project_id = str(info.get("project_id") or "").strip()
        if not _translate_project_id:
            raise RuntimeError("project_id missing in Google credentials JSON")

    return _translate_client, _translate_project_id


def translate_with_google(text: str, from_lang: str, to_lang: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""

    src = (from_lang or "auto").strip().lower()
    dst = (to_lang or "tr").strip().lower()

    if src == dst:
        return value

    client, project_id = _get_translate_client_and_project()
    parent = f"projects/{project_id}/locations/global"

    payload = {
        "parent": parent,
        "contents": [value],
        "target_language_code": dst,
        "mime_type": "text/plain",
    }

    if src and src != "auto":
        payload["source_language_code"] = src

    resp = client.translate_text(request=payload, timeout=3.0)

    out = ""
    if resp.translations:
        out = (resp.translations[0].translated_text or "").strip()

    if not out:
        raise RuntimeError("Google Translate returned empty response")

    return out


def now_ts() -> int:
    return int(time.time())


def new_room_id() -> str:
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]


def clean_lang(value: str, fallback: str = "tr") -> str:
    v = str(value or fallback).strip().lower()
    return v or fallback


def clean_name(value: str, fallback: str) -> str:
    v = str(value or "").strip()
    return v[:60] if v else fallback


def clean_avatar(value: str) -> str:
    v = str(value or "").strip()
    return v[:500]


@dataclass
class PeerState:
    role: str
    lang: str
    name: str = ""
    avatar: str = ""
    user_id: str = ""
    joined_at: float = field(default_factory=lambda: time.time())


@dataclass
class RoomState:
    room_id: str
    host_code: str
    mode: str = "interpreter"
    host_lang: str = "tr"
    guest_lang: Optional[str] = None
    status: str = "waiting"
    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())
    peers: Dict[str, PeerState] = field(default_factory=dict)
    sockets: Set[WebSocket] = field(default_factory=set)


ROOMS: Dict[str, RoomState] = {}
HOST_ACTIVE_ROOM: Dict[str, str] = {}
ROOM_LOCK = asyncio.Lock()


def get_room_or_404(room_id: str) -> RoomState:
    room = ROOMS.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


def peer_to_payload(peer: PeerState) -> dict:
    return {
        "role": peer.role,
        "lang": peer.lang,
        "name": peer.name,
        "avatar": peer.avatar,
        "user_id": peer.user_id,
        "joined_at": int(peer.joined_at),
    }


def room_peers_payload(room: RoomState) -> List[dict]:
    return [peer_to_payload(peer) for peer in room.peers.values()]


def room_presence_payload(room: RoomState) -> dict:
    host_peer = room.peers.get("host")
    guest_peer = room.peers.get("guest")

    return {
        "type": "presence",
        "room_id": room.room_id,
        "host_code": room.host_code,
        "mode": room.mode,
        "status": room.status,
        "host_lang": room.host_lang,
        "guest_lang": room.guest_lang,
        "peer_count": len(room.peers),
        "peers": room_peers_payload(room),
        "host_name": host_peer.name if host_peer else "Host",
        "host_avatar": host_peer.avatar if host_peer else "",
        "host_user_id": host_peer.user_id if host_peer else "",
        "guest_name": guest_peer.name if guest_peer else "Guest",
        "guest_avatar": guest_peer.avatar if guest_peer else "",
        "guest_user_id": guest_peer.user_id if guest_peer else "",
        "ts": now_ts(),
    }


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


class CreateRoomReq(BaseModel):
    my_lang: str = "tr"
    host_code: str = "HOME-HOST"
    mode: str = "interpreter"
    name: Optional[str] = None
    avatar: Optional[str] = None
    user_id: Optional[str] = None


class CreateRoomResp(BaseModel):
    ok: bool
    room_id: str
    join_url: str
    ws_url: str
    status: str


class ResolveRoomReq(BaseModel):
    host_code: str
    my_lang: str = "tr"
    mode: str = "interpreter"
    name: Optional[str] = None
    avatar: Optional[str] = None
    user_id: Optional[str] = None


class ResolveRoomResp(BaseModel):
    ok: bool
    room_id: str
    host_code: str
    status: str
    mode: str
    join_url: str
    ws_url: str


class JoinRoomReq(BaseModel):
    room_id: str
    my_lang: str = "en"
    name: Optional[str] = None
    avatar: Optional[str] = None
    user_id: Optional[str] = None


class JoinRoomResp(BaseModel):
    ok: bool
    room_id: str
    status: str


class RoomResp(BaseModel):
    ok: bool
    room_id: str
    host_code: str
    mode: str
    status: str
    host_lang: str
    guest_lang: Optional[str] = None
    peer_count: int = 0
    peers: List[dict] = []


@router.post("/interpreter/create-room", response_model=CreateRoomResp)
async def create_room(req: CreateRoomReq):
    room_id = new_room_id()
    host_lang = clean_lang(req.my_lang, "tr")
    host_code = str(req.host_code or "HOME-HOST").strip().upper()
    mode = str(req.mode or "interpreter").strip().lower()

    host_name = clean_name(req.name, "Host")
    host_avatar = clean_avatar(req.avatar)
    host_user_id = str(req.user_id or "").strip()

    async with ROOM_LOCK:
        room = RoomState(
            room_id=room_id,
            host_code=host_code,
            mode=mode,
            host_lang=host_lang,
        )
        room.peers["host"] = PeerState(
            role="host",
            lang=host_lang,
            name=host_name,
            avatar=host_avatar,
            user_id=host_user_id,
        )
        ROOMS[room_id] = room
        HOST_ACTIVE_ROOM[host_code] = room_id

    join_url = f"https://italky.ai/open/interpreter?room={room_id}&v=1"
    ws_url = f"wss://italky-api.onrender.com/api/ws/interpreter/{room_id}"

    return CreateRoomResp(
        ok=True,
        room_id=room_id,
        join_url=join_url,
        ws_url=ws_url,
        status=room.status,
    )


@router.post("/interpreter/resolve-room", response_model=ResolveRoomResp)
async def resolve_room(req: ResolveRoomReq):
    host_code = str(req.host_code or "").strip().upper()
    my_lang = clean_lang(req.my_lang, "tr")
    mode = str(req.mode or "interpreter").strip().lower()

    host_name = clean_name(req.name, "Host")
    host_avatar = clean_avatar(req.avatar)
    host_user_id = str(req.user_id or "").strip()

    if not host_code:
        raise HTTPException(status_code=422, detail="host_code is required")

    async with ROOM_LOCK:
        room_id = HOST_ACTIVE_ROOM.get(host_code)
        room: Optional[RoomState] = None

        if room_id:
            room = ROOMS.get(room_id)
            if not room:
                HOST_ACTIVE_ROOM.pop(host_code, None)

        if not room:
            room_id = new_room_id()
            room = RoomState(
                room_id=room_id,
                host_code=host_code,
                mode=mode,
                host_lang=my_lang,
            )
            room.peers["host"] = PeerState(
                role="host",
                lang=my_lang,
                name=host_name,
                avatar=host_avatar,
                user_id=host_user_id,
            )
            ROOMS[room_id] = room
            HOST_ACTIVE_ROOM[host_code] = room_id
        else:
            host_peer = room.peers.get("host")
            if host_peer:
                if host_name:
                    host_peer.name = host_name
                if host_avatar:
                    host_peer.avatar = host_avatar
                if host_user_id:
                    host_peer.user_id = host_user_id
                host_peer.lang = my_lang
            else:
                room.peers["host"] = PeerState(
                    role="host",
                    lang=my_lang,
                    name=host_name,
                    avatar=host_avatar,
                    user_id=host_user_id,
                )

        room.updated_at = time.time()

    join_url = f"https://italky.ai/open/interpreter?room={room.room_id}&v=1"
    ws_url = f"wss://italky-api.onrender.com/api/ws/interpreter/{room.room_id}"

    return ResolveRoomResp(
        ok=True,
        room_id=room.room_id,
        host_code=host_code,
        status=room.status,
        mode=room.mode,
        join_url=join_url,
        ws_url=ws_url,
    )


@router.post("/interpreter/join-room", response_model=JoinRoomResp)
async def join_room(req: JoinRoomReq):
    room_id = str(req.room_id or "").strip()
    guest_lang = clean_lang(req.my_lang, "en")
    guest_name = clean_name(req.name, "Guest")
    guest_avatar = clean_avatar(req.avatar)
    guest_user_id = str(req.user_id or "").strip()

    if not room_id:
        raise HTTPException(status_code=422, detail="room_id is required")

    async with ROOM_LOCK:
        room = get_room_or_404(room_id)

        room.guest_lang = guest_lang
        room.peers["guest"] = PeerState(
            role="guest",
            lang=guest_lang,
            name=guest_name,
            avatar=guest_avatar,
            user_id=guest_user_id,
        )
        room.status = "active"
        room.updated_at = time.time()

    await broadcast(room, {
        "type": "peer_joined",
        "room_id": room_id,
        "status": room.status,
        "guest_lang": guest_lang,
        "guest_name": guest_name,
        "guest_avatar": guest_avatar,
        "guest_user_id": guest_user_id,
        "peers": room_peers_payload(room),
        "ts": now_ts(),
    })

    await broadcast(room, room_presence_payload(room))

    return JoinRoomResp(ok=True, room_id=room_id, status=room.status)


@router.get("/interpreter/room/{room_id}", response_model=RoomResp)
async def get_room(room_id: str):
    room = get_room_or_404(room_id)
    return RoomResp(
        ok=True,
        room_id=room.room_id,
        host_code=room.host_code,
        mode=room.mode,
        status=room.status,
        host_lang=room.host_lang,
        guest_lang=room.guest_lang,
        peer_count=len(room.peers),
        peers=room_peers_payload(room),
    )


@router.get("/interpreter/health")
async def interpreter_health():
    return {
        "ok": True,
        "rooms": len(ROOMS),
        "active_hosts": len(HOST_ACTIVE_ROOM),
    }


@router.websocket("/ws/interpreter/{room_id}")
async def interpreter_ws(websocket: WebSocket, room_id: str):
    await websocket.accept()

    role = str(websocket.query_params.get("role") or "guest").strip().lower()
    my_lang = clean_lang(websocket.query_params.get("lang") or "en", "en")
    my_name = clean_name(websocket.query_params.get("name"), "Host" if role == "host" else "Guest")
    my_avatar = clean_avatar(websocket.query_params.get("avatar"))
    my_user_id = str(websocket.query_params.get("user_id") or "").strip()

    room = ROOMS.get(room_id)
    if not room:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "Room not found",
            "ts": now_ts(),
        }, ensure_ascii=False))
        await websocket.close()
        return

    room.sockets.add(websocket)
    room.updated_at = time.time()

    async with ROOM_LOCK:
        if role == "host":
            room.host_lang = my_lang
        else:
            room.guest_lang = my_lang

        old_peer = room.peers.get(role)
        if old_peer:
            old_peer.lang = my_lang
            if my_name:
                old_peer.name = my_name
            if my_avatar:
                old_peer.avatar = my_avatar
            if my_user_id:
                old_peer.user_id = my_user_id
        else:
            room.peers[role] = PeerState(
                role=role,
                lang=my_lang,
                name=my_name,
                avatar=my_avatar,
                user_id=my_user_id,
            )

        if role == "guest":
            room.status = "active"

    await broadcast(room, room_presence_payload(room))

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = str(data.get("type") or "").strip()

            if mtype == "ping":
                await websocket.send_text(
                    json.dumps({"type": "pong", "ts": now_ts()}, ensure_ascii=False)
                )
                continue

            if mtype == "typing":
                await broadcast(room, {
                    "type": "typing",
                    "sender": role,
                    "sender_name": room.peers.get(role).name if room.peers.get(role) else my_name,
                    "sender_avatar": room.peers.get(role).avatar if room.peers.get(role) else my_avatar,
                    "ts": now_ts(),
                })
                continue

            if mtype == "set_lang":
                new_lang = clean_lang(str(data.get("lang") or my_lang).strip(), my_lang)
                my_lang = new_lang

                async with ROOM_LOCK:
                    if role == "host":
                        room.host_lang = new_lang
                    else:
                        room.guest_lang = new_lang

                    peer = room.peers.get(role)
                    if peer:
                        peer.lang = new_lang
                    else:
                        room.peers[role] = PeerState(
                            role=role,
                            lang=new_lang,
                            name=my_name,
                            avatar=my_avatar,
                            user_id=my_user_id,
                        )

                    room.updated_at = time.time()

                await broadcast(room, room_presence_payload(room))
                continue

            if mtype == "profile_sync":
                new_name = clean_name(data.get("name"), my_name)
                new_avatar = clean_avatar(data.get("avatar"))
                new_user_id = str(data.get("user_id") or my_user_id).strip()

                my_name = new_name or my_name
                my_avatar = new_avatar or my_avatar
                my_user_id = new_user_id or my_user_id

                async with ROOM_LOCK:
                    peer = room.peers.get(role)
                    if peer:
                        peer.name = my_name
                        peer.avatar = my_avatar
                        peer.user_id = my_user_id
                    else:
                        room.peers[role] = PeerState(
                            role=role,
                            lang=my_lang,
                            name=my_name,
                            avatar=my_avatar,
                            user_id=my_user_id,
                        )
                    room.updated_at = time.time()

                await broadcast(room, room_presence_payload(room))
                continue

            if mtype == "text_message":
                original_text = str(data.get("text") or "").strip()
                from_lang = clean_lang(str(data.get("from_lang") or my_lang).strip(), my_lang)
                to_lang = clean_lang(str(data.get("to_lang") or "").strip(), "")

                if not original_text:
                    continue

                if not to_lang:
                    if role == "host":
                        to_lang = room.guest_lang or "en"
                    else:
                        to_lang = room.host_lang or "tr"

                peer = room.peers.get(role)
                sender_name = peer.name if peer else my_name
                sender_avatar = peer.avatar if peer else my_avatar
                sender_user_id = peer.user_id if peer else my_user_id

                try:
                    translated = translate_with_google(original_text, from_lang, to_lang)
                except Exception as e:
                    logger.exception("INTERPRETER_TRANSLATE_FAIL %s", e)
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Translation failed: {e}",
                        "ts": now_ts(),
                    }, ensure_ascii=False))
                    continue

                await broadcast(room, {
                    "type": "translated_message",
                    "sender": role,
                    "sender_name": sender_name,
                    "sender_avatar": sender_avatar,
                    "sender_user_id": sender_user_id,
                    "original_text": original_text,
                    "translated_text": translated,
                    "from_lang": from_lang,
                    "to_lang": to_lang,
                    "peers": room_peers_payload(room),
                    "ts": now_ts(),
                })

    except WebSocketDisconnect:
        room.sockets.discard(websocket)
        room.updated_at = time.time()

        leaving_peer = room.peers.get(role)
        leaving_name = leaving_peer.name if leaving_peer else ("Host" if role == "host" else "Guest")
        leaving_avatar = leaving_peer.avatar if leaving_peer else ""

        async with ROOM_LOCK:
            room.peers.pop(role, None)

            if role == "guest":
                room.guest_lang = None
                room.status = "waiting"

            if not room.sockets:
                if HOST_ACTIVE_ROOM.get(room.host_code) == room.room_id:
                    HOST_ACTIVE_ROOM.pop(room.host_code, None)

        await broadcast(room, {
            "type": "peer_left",
            "sender": role,
            "sender_name": leaving_name,
            "sender_avatar": leaving_avatar,
            "message": "Karşı taraf odadan ayrıldı.",
            "peers": room_peers_payload(room),
            "ts": now_ts(),
        })

        if room.sockets:
            await broadcast(room, room_presence_payload(room))

    except Exception as e:
        logger.exception("INTERPRETER_WS_ERROR %s", e)
        room.sockets.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
