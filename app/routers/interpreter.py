# FILE: app/routers/interpreter.py

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("italky-interpreter")
router = APIRouter(tags=["interpreter"])

SAFE_TRANSLATION_ERROR = "Çeviri şu anda kullanılamıyor."

# =========================
# STATE
# =========================

@dataclass
class ClientState:
    ws: WebSocket
    role: str
    lang: str


@dataclass
class RoomState:
    host: Optional[ClientState] = None
    guest: Optional[ClientState] = None


ROOMS: Dict[str, RoomState] = {}
LOCK = asyncio.Lock()

# =========================
# HELPERS
# =========================

async def send(ws: Optional[WebSocket], payload: dict):
    if not ws:
        return
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def fake_translate(text: str):
    # Şimdilik direkt geçiriyoruz
    return text


# =========================
# WS
# =========================

@router.websocket("/ws/interpreter/{room_id}")
async def interpreter_ws(websocket: WebSocket, room_id: str):
    await websocket.accept()

    role = (websocket.query_params.get("role") or "guest").strip().lower()
    lang = (websocket.query_params.get("lang") or "tr").strip().lower()

    async with LOCK:
        room = ROOMS.get(room_id)
        if not room:
            room = RoomState()
            ROOMS[room_id] = room

        client = ClientState(ws=websocket, role=role, lang=lang)

        if role == "host":
            room.host = client
        else:
            room.guest = client

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = str(data.get("type") or "").strip()

            # =========================
            # MESSAGE
            # =========================
            if mtype == "text_message":
                text = str(data.get("text") or "").strip()
                sender_id = str(data.get("sender_id") or "").strip()

                if not text:
                    continue

                # hedef taraf
                target = room.guest if role == "host" else room.host

                if not target:
                    await send(websocket, {
                        "type": "error",
                        "message": "Karşı taraf bağlı değil"
                    })
                    continue

                try:
                    translated = await fake_translate(text)
                except Exception as e:
                    logger.exception("translate fail %s", e)
                    await send(websocket, {
                        "type": "error",
                        "message": SAFE_TRANSLATION_ERROR
                    })
                    continue

                await send(target.ws, {
                    "type": "translated_message",
                    "text": translated,
                    "sender_id": sender_id
                })

    except WebSocketDisconnect:
        pass

    finally:
        async with LOCK:
            room = ROOMS.get(room_id)
            if room:
                if role == "host":
                    room.host = None
                else:
                    room.guest = None

                if not room.host and not room.guest:
                    ROOMS.pop(room_id, None)
