from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional
import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@dataclass
class ClientState:
    ws: WebSocket
    role: str = "listener"          # speaker | listener
    lang: str = "en"
    voice: str = "default_female"
    output: str = "voice"           # speaker side output mode
    mode: str = "audio"             # listener side mode
    joined: bool = False


@dataclass
class RoomState:
    speaker: Optional[ClientState] = None
    listeners: Dict[str, ClientState] = field(default_factory=dict)


ROOMS: Dict[str, RoomState] = {}
ROOM_LOCK = asyncio.Lock()


def _clean_room(room: str) -> str:
    return str(room or "").strip().upper()


def _safe_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


async def _send(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_text(_safe_json(payload))
    except Exception:
        pass


async def _broadcast_listeners(room: RoomState, payload: dict) -> None:
    dead = []
    for key, client in room.listeners.items():
        try:
            await client.ws.send_text(_safe_json(payload))
        except Exception:
            dead.append(key)

    for key in dead:
        room.listeners.pop(key, None)


async def _broadcast_listener_count(room_code: str) -> None:
    room = ROOMS.get(room_code)
    if not room:
        return

    payload = {
        "type": "listener_count",
        "room": room_code,
        "count": len(room.listeners),
    }

    if room.speaker:
        await _send(room.speaker.ws, payload)

    await _broadcast_listeners(room, payload)


async def _translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """
    Şimdilik güvenli geçiş modu:
    çeviri servisine bağlamadan önce sistemi ayağa kaldırmak için metni olduğu gibi döndürüyor.
    Sonra bunu gerçek translate pipeline'a bağlarız.
    """
    value = str(text or "").strip()
    if not value:
        return ""
    return value


@router.websocket("/onetoall/ws/{room_code}")
async def onetoall_ws(websocket: WebSocket, room_code: str):
    await websocket.accept()

    room_code = _clean_room(room_code)
    client_id = f"{id(websocket)}"
    client = ClientState(ws=websocket)

    async with ROOM_LOCK:
        room = ROOMS.get(room_code)
        if not room:
            room = RoomState()
            ROOMS[room_code] = room

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw or "{}")
            except Exception:
                await _send(websocket, {
                    "type": "error",
                    "message": "INVALID_JSON",
                })
                continue

            msg_type = str(data.get("type") or "").strip().lower()

            # =========================
            # JOIN
            # =========================
            if msg_type == "speaker_join":
                client.role = "speaker"
                client.lang = str(data.get("lang") or "tr").strip().lower()
                client.voice = str(data.get("voice") or "default_female").strip()
                client.output = str(data.get("output") or "voice").strip().lower()
                client.joined = True

                async with ROOM_LOCK:
                    room = ROOMS.setdefault(room_code, RoomState())
                    room.speaker = client

                await _send(websocket, {
                    "type": "joined",
                    "room": room_code,
                    "role": "speaker",
                    "lang": client.lang,
                })

                await _broadcast_listener_count(room_code)
                continue

            if msg_type == "listener_join":
                client.role = "listener"
                client.lang = str(data.get("lang") or "en").strip().lower()
                client.mode = str(data.get("mode") or "audio").strip().lower()
                client.joined = True

                async with ROOM_LOCK:
                    room = ROOMS.setdefault(room_code, RoomState())
                    room.listeners[client_id] = client

                await _send(websocket, {
                    "type": "joined",
                    "room": room_code,
                    "role": "listener",
                    "lang": client.lang,
                })

                if room.speaker:
                    await _send(websocket, {
                        "type": "presence",
                        "room": room_code,
                        "speaker_lang": room.speaker.lang,
                    })

                await _broadcast_listener_count(room_code)
                continue

            # =========================
            # SPEAKER LIVE TEXT
            # =========================
            if msg_type in {"speaker_text", "speaker_final"}:
                if client.role != "speaker":
                    await _send(websocket, {
                        "type": "error",
                        "message": "ONLY_SPEAKER_CAN_SEND",
                    })
                    continue

                text = str(data.get("text") or "").strip()
                if not text:
                    continue

                source_lang = str(data.get("lang") or client.lang or "tr").strip().lower()
                room = ROOMS.get(room_code)

                if not room:
                    await _send(websocket, {
                        "type": "error",
                        "message": "ROOM_NOT_FOUND",
                    })
                    continue

                # Önce ham akışı listener'lara ilet
                await _broadcast_listeners(room, {
                    "type": "speaker_chunk" if msg_type == "speaker_text" else "speaker_text",
                    "room": room_code,
                    "text": text,
                    "lang": source_lang,
                })

                # Son metinse, her listener için ayrı "çeviri" paketini gönder
                if msg_type == "speaker_final":
                    for _, listener in list(room.listeners.items()):
                        translated = await _translate_text(
                            text=text,
                            source_lang=source_lang,
                            target_lang=listener.lang,
                        )

                        await _send(listener.ws, {
                            "type": "broadcast_translation",
                            "room": room_code,
                            "source_text": text,
                            "translated_text": translated,
                            "source_lang": source_lang,
                            "target_lang": listener.lang,
                        })

                    if room.speaker and room.speaker.output == "voice":
                        await _send(room.speaker.ws, {
                            "type": "broadcast_translation",
                            "room": room_code,
                            "source_text": text,
                            "translated_text": text,
                            "source_lang": source_lang,
                            "target_lang": source_lang,
                        })

                continue

            # =========================
            # PING
            # =========================
            if msg_type == "ping":
                await _send(websocket, {"type": "pong"})
                continue

            await _send(websocket, {
                "type": "error",
                "message": f"UNKNOWN_TYPE:{msg_type}",
            })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await _send(websocket, {
            "type": "error",
            "message": f"SERVER_ERROR:{str(e)}",
        })
    finally:
        async with ROOM_LOCK:
            room = ROOMS.get(room_code)

            if room:
                if room.speaker and room.speaker.ws is websocket:
                    room.speaker = None

                dead_keys = []
                for key, c in room.listeners.items():
                    if c.ws is websocket:
                        dead_keys.append(key)

                for key in dead_keys:
                    room.listeners.pop(key, None)

                if room.speaker is None and not room.listeners:
                    ROOMS.pop(room_code, None)

        await _broadcast_listener_count(room_code)
