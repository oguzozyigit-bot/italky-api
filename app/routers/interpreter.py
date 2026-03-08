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

# =========================
# Google Translate
# =========================
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


# =========================
# In-memory room store
# =========================
@dataclass
class PeerState:
    role: str
    lang: str
    joined_at: float = field(default_factory=lambda: time.time())


@dataclass
class RoomState:
    room_id: str
    host_lang: str
    guest_lang: Optional[str] = None
    status: str = "waiting"   # waiting | active | closed
    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())
    peers: Dict[str, PeerState] = field(default_factory=dict)
    sockets: Set[WebSocket] = field(default_factory=set)


ROOMS: Dict[str, RoomState] = {}
ROOM_LOCK = asyncio.Lock()


def now_ts() -> int:
    return int(time.time())


def new_room_id() -> str:
    return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]


def get_room_or_404(room_id: str) -> RoomState:
    room = ROOMS.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


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


# =========================
# Schemas
# =========================
class CreateRoomReq(BaseModel):
    my_lang: str = "tr"


class CreateRoomResp(BaseModel):
    ok: bool
    room_id: str
    join_url: str
    ws_url: str
    status: str


class JoinRoomReq(BaseModel):
    room_id: str
    my_lang: str = "en"


class JoinRoomResp(BaseModel):
    ok: bool
    room_id: str
    status: str


class RoomResp(BaseModel):
    ok: bool
    room_id: str
    status: str
    host_lang: str
    guest_lang: Optional[str] = None
    peer_count: int = 0


# =========================
# REST
# =========================
@router.post("/interpreter/create-room", response_model=CreateRoomResp)
async def create_room(req: CreateRoomReq):
    room_id = new_room_id()
    host_lang = (req.my_lang or "tr").strip().lower()

    async with ROOM_LOCK:
        room = RoomState(room_id=room_id, host_lang=host_lang)
        room.peers["host"] = PeerState(role="host", lang=host_lang)
        ROOMS[room_id] = room

    join_url = f"https://italky.ai/pages/interpreter_qr_scan.html?room={room_id}"
    ws_url = f"wss://italky-api.onrender.com/ws/interpreter/{room_id}"

    return CreateRoomResp(
        ok=True,
        room_id=room_id,
        join_url=join_url,
        ws_url=ws_url,
        status=room.status,
    )


@router.post("/interpreter/join-room", response_model=JoinRoomResp)
async def join_room(req: JoinRoomReq):
    room_id = (req.room_id or "").strip()
    guest_lang = (req.my_lang or "en").strip().lower()

    if not room_id:
        raise HTTPException(status_code=422, detail="room_id is required")

    async with ROOM_LOCK:
        room = get_room_or_404(room_id)
        room.guest_lang = guest_lang
        room.peers["guest"] = PeerState(role="guest", lang=guest_lang)
        room.status = "active"
        room.updated_at = time.time()

    await broadcast(room, {
        "type": "peer_joined",
        "room_id": room_id,
        "status": room.status,
        "guest_lang": guest_lang,
        "ts": now_ts(),
    })

    return JoinRoomResp(ok=True, room_id=room_id, status=room.status)


@router.get("/interpreter/room/{room_id}", response_model=RoomResp)
async def get_room(room_id: str):
    room = get_room_or_404(room_id)
    return RoomResp(
        ok=True,
        room_id=room.room_id,
        status=room.status,
        host_lang=room.host_lang,
        guest_lang=room.guest_lang,
        peer_count=len(room.peers),
    )


@router.get("/interpreter/health")
async def interpreter_health():
    return {
        "ok": True,
        "rooms": len(ROOMS),
    }


# =========================
# WebSocket
# =========================
@router.websocket("/ws/interpreter/{room_id}")
async def interpreter_ws(websocket: WebSocket, room_id: str):
    await websocket.accept()

    role = (websocket.query_params.get("role") or "guest").strip().lower()
    my_lang = (websocket.query_params.get("lang") or "en").strip().lower()

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

    if role not in room.peers:
        room.peers[role] = PeerState(role=role, lang=my_lang)

    await broadcast(room, {
        "type": "presence",
        "room_id": room_id,
        "status": room.status,
        "host_lang": room.host_lang,
        "guest_lang": room.guest_lang,
        "peer_count": len(room.peers),
        "ts": now_ts(),
    })

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            mtype = str(data.get("type") or "").strip()

            if mtype == "ping":
                await websocket.send_text(json.dumps({
                    "type": "pong",
                    "ts": now_ts(),
                }, ensure_ascii=False))
                continue

            if mtype == "typing":
                await broadcast(room, {
                    "type": "typing",
                    "sender": role,
                    "ts": now_ts(),
                })
                continue

            if mtype == "text_message":
                original_text = str(data.get("text") or "").strip()
                from_lang = str(data.get("from_lang") or my_lang).strip().lower()
                to_lang = str(data.get("to_lang") or "").strip().lower()

                if not original_text:
                    continue

                if not to_lang:
                    if role == "host":
                        to_lang = room.guest_lang or "en"
                    else:
                        to_lang = room.host_lang or "tr"

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
                    "original_text": original_text,
                    "translated_text": translated,
                    "from_lang": from_lang,
                    "to_lang": to_lang,
                    "ts": now_ts(),
                })
                continue

    except WebSocketDisconnect:
        room.sockets.discard(websocket)
        room.updated_at = time.time()

        await broadcast(room, {
            "type": "peer_left",
            "sender": role,
            "ts": now_ts(),
        })
    except Exception as e:
        logger.exception("INTERPRETER_WS_ERROR %s", e)
        room.sockets.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
