# FILE: italky-api/app/routers/f2f_ws.py
from __future__ import annotations

import json
import time
from typing import Dict, Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["f2f-ws"])

ROOM_TTL_SEC = 60 * 30  # 30 dk

# In-memory rooms
# ROOMS[room_id] = {
#   "created_at": float,
#   "updated_at": float,
#   "clients": set(WebSocket),
#   "meta": { WebSocket: {"from","from_name","from_pic","me_lang","role"} },
#   "settings": {"billing_mode":"per_user"|"host_pays"}
# }
ROOMS: Dict[str, Dict[str, Any]] = {}


def now() -> float:
    return time.time()


def room_expired(room: Dict[str, Any]) -> bool:
    return (now() - float(room.get("created_at", 0))) > ROOM_TTL_SEC


def get_room(room_id: str) -> Optional[Dict[str, Any]]:
    room_id = (room_id or "").strip().upper()
    r = ROOMS.get(room_id)
    if not r:
        return None
    if room_expired(r):
        try:
            del ROOMS[room_id]
        except Exception:
            pass
        return None
    return r


def create_room(room_id: str) -> Dict[str, Any]:
    r = {
        "created_at": now(),
        "updated_at": now(),
        "clients": set(),  # type: ignore
        "meta": {},        # type: ignore
        "settings": {"billing_mode": "per_user"},  # default: herkes öder
    }
    ROOMS[room_id] = r
    return r


async def ws_send(ws: Optional[WebSocket], msg: Dict[str, Any]) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(msg))
    except Exception:
        return


async def broadcast(room: Dict[str, Any], msg: Dict[str, Any], exclude: Optional[WebSocket] = None) -> None:
    dead = []
    for c in list(room["clients"]):
        if exclude is not None and c is exclude:
            continue
        try:
            await c.send_text(json.dumps(msg))
        except Exception:
            dead.append(c)

    for d in dead:
        try:
            room["clients"].discard(d)
            room["meta"].pop(d, None)
        except Exception:
            pass


def build_roster(room: Dict[str, Any]) -> list:
    roster = []
    meta = room.get("meta") or {}
    for _ws, m in meta.items():
        try:
            roster.append({
                "from": m.get("from") or "",
                "from_name": m.get("from_name") or "User",
                "from_pic": m.get("from_pic") or "",
                "me_lang": (m.get("me_lang") or "tr"),
                "role": m.get("role") or "guest",
            })
        except Exception:
            continue
    return roster


async def send_presence(room: Dict[str, Any]) -> None:
    await broadcast(room, {
        "type": "presence",
        "count": len(room["clients"]),
        "roster": build_roster(room),
        "settings": room.get("settings") or {"billing_mode": "per_user"},
        "ttl_sec": ROOM_TTL_SEC,
    })


@router.websocket("/f2f/ws/{room_id}")
async def f2f_ws(ws: WebSocket, room_id: str):
    room_id = (room_id or "").strip().upper()
    await ws.accept()

    joined_room: Optional[Dict[str, Any]] = None
    my_meta: Dict[str, Any] = {}

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw or "{}")
            mtype = str(msg.get("type") or "").strip()

            # ===== JOIN CHECK =====
            if mtype == "join_check":
                r = get_room(room_id)
                await ws_send(ws, {"type": "room_ok" if r else "room_not_found"})
                continue

            # ===== CREATE (HOST) =====
            if mtype == "create":
                if joined_room is not None:
                    await ws_send(ws, {"type": "error", "message": "ALREADY_JOINED"})
                    continue

                r = get_room(room_id)
                if r is None:
                    r = create_room(room_id)

                # billing mode (room setting)
                billing_mode = str(msg.get("billing_mode") or "per_user").strip().lower()
                if billing_mode not in ("per_user", "host_pays"):
                    billing_mode = "per_user"
                r["settings"]["billing_mode"] = billing_mode

                joined_room = r
                my_meta = {
                    "from": msg.get("from") or "",
                    "from_name": msg.get("from_name") or "Host",
                    "from_pic": msg.get("from_pic") or "",
                    "me_lang": (msg.get("me_lang") or "tr").strip().lower(),
                    "role": "host",
                }

                r["clients"].add(ws)
                r["meta"][ws] = my_meta
                r["updated_at"] = now()

                await ws_send(ws, {"type": "room_created", "room": room_id, "ttl_sec": ROOM_TTL_SEC})
                await send_presence(r)
                continue

            # ===== JOIN (GUEST) =====
            if mtype == "join":
                if joined_room is not None:
                    await ws_send(ws, {"type": "error", "message": "ALREADY_JOINED"})
                    continue

                r = get_room(room_id)
                if r is None:
                    await ws_send(ws, {
                        "type": "room_not_found",
                        "message": "Kod hatalı olabilir veya sohbet odası kapanmış olabilir."
                    })
                    await ws.close()
                    return

                joined_room = r
                my_meta = {
                    "from": msg.get("from") or "",
                    "from_name": msg.get("from_name") or "Guest",
                    "from_pic": msg.get("from_pic") or "",
                    "me_lang": (msg.get("me_lang") or "tr").strip().lower(),
                    "role": "guest",
                }

                r["clients"].add(ws)
                r["meta"][ws] = my_meta
                r["updated_at"] = now()

                await ws_send(ws, {"type": "room_joined", "room": room_id, "ttl_sec": ROOM_TTL_SEC})
                await send_presence(r)
                continue

            # ===== MESSAGE (RELAY ONLY) =====
            if mtype == "message":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                text = str(msg.get("text") or "").strip()
                if not text:
                    continue

                # client lang (or fallback to my_meta)
                lang = str(msg.get("lang") or my_meta.get("me_lang") or "tr").strip().lower()

                payload = {
                    "type": "message",
                    "from": my_meta.get("from") or "",
                    "from_name": my_meta.get("from_name") or "User",
                    "from_pic": my_meta.get("from_pic") or "",
                    "lang": lang,
                    "text": text,
                    "ts": int(now() * 1000),
                }

                # send to everyone except sender
                await broadcast(joined_room, payload, exclude=ws)
                joined_room["updated_at"] = now()
                continue

            await ws_send(ws, {"type": "error", "message": "UNKNOWN_TYPE"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if joined_room is not None:
            try:
                joined_room["clients"].discard(ws)
                joined_room["meta"].pop(ws, None)
                joined_room["updated_at"] = now()

                try:
                    await send_presence(joined_room)
                except Exception:
                    pass

                # delete empty room
                if len(joined_room["clients"]) == 0:
                    try:
                        del ROOMS[room_id]
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            await ws.close()
        except Exception:
            pass
