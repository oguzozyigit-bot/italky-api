from __future__ import annotations

import json
import time
from typing import Dict, Any, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["f2f-ws"])

ROOM_TTL_SEC = 60 * 30  # 30 dk
ROOMS: Dict[str, Dict[str, Any]] = {}
# ROOMS[room_id] = {
#   "created_at": float,
#   "updated_at": float,
#   "clients": set(WebSocket),
#   "meta": { WebSocket: {"from","from_name","from_pic","me_lang"} }
# }

def now() -> float:
    return time.time()

def room_exists(room_id: str) -> bool:
    r = ROOMS.get(room_id)
    if not r:
        return False
    if (now() - float(r.get("created_at", 0))) > ROOM_TTL_SEC:
        # expire
        try:
            del ROOMS[room_id]
        except Exception:
            pass
        return False
    return True

def get_room(room_id: str) -> Optional[Dict[str, Any]]:
    if not room_exists(room_id):
        return None
    return ROOMS.get(room_id)

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

async def send_presence(room_id: str, room: Dict[str, Any]) -> None:
    await broadcast(room, {"type": "presence", "count": len(room["clients"])})
    # (İstersen burada liste de gönderebiliriz)

def create_room(room_id: str) -> Dict[str, Any]:
    r = {
        "created_at": now(),
        "updated_at": now(),
        "clients": set(),     # type: ignore
        "meta": {},           # type: ignore
    }
    ROOMS[room_id] = r
    return r

# ✅ ödeme hook’u (şimdilik boş)
async def charge_sender_if_needed(sender_meta: Dict[str, Any], cost: int = 1) -> None:
    """
    Burada ileride Supabase RPC ile kullanıcı jeton düşeceksin.
    Şimdilik NO-OP.
    """
    return

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

            # -------------------
            # HOST creates room
            # -------------------
            if mtype == "create":
                # only once
                if joined_room is not None:
                    await ws_send(ws, {"type":"error","message":"ALREADY_JOINED"})
                    continue

                # if already exists -> reuse (host can reopen same code)
                room = get_room(room_id)
                if room is None:
                    room = create_room(room_id)

                joined_room = room

                my_meta = {
                    "from": msg.get("from"),
                    "from_name": msg.get("from_name"),
                    "from_pic": msg.get("from_pic"),
                    "me_lang": (msg.get("me_lang") or "").strip().lower() or "tr",
                    "role": "host",
                }

                room["clients"].add(ws)
                room["meta"][ws] = my_meta
                room["updated_at"] = now()

                await ws_send(ws, {"type":"room_created","room": room_id, "ttl_sec": ROOM_TTL_SEC})
                await send_presence(room_id, room)
                continue

            # -------------------
            # GUEST joins room (must exist)
            # -------------------
            if mtype == "join":
                if joined_room is not None:
                    await ws_send(ws, {"type":"error","message":"ALREADY_JOINED"})
                    continue

                room = get_room(room_id)
                if room is None:
                    await ws_send(ws, {"type":"room_not_found", "message":"Kod hatalı olabilir veya sohbet odası kapanmış olabilir."})
                    # join başarısız → kapat
                    await ws.close()
                    return

                joined_room = room

                my_meta = {
                    "from": msg.get("from"),
                    "from_name": msg.get("from_name"),
                    "from_pic": msg.get("from_pic"),
                    "me_lang": (msg.get("me_lang") or "").strip().lower() or "tr",
                    "role": "guest",
                }

                room["clients"].add(ws)
                room["meta"][ws] = my_meta
                room["updated_at"] = now()

                await ws_send(ws, {"type":"room_joined","room": room_id, "ttl_sec": ROOM_TTL_SEC})
                await send_presence(room_id, room)
                continue

            # -------------------
            # Join check (no auto create)
            # -------------------
            if mtype == "join_check":
                room = get_room(room_id)
                if room is None:
                    await ws_send(ws, {"type":"room_not_found"})
                else:
                    await ws_send(ws, {"type":"room_ok"})
                continue

            # -------------------
            # MESSAGE relay ONLY (NO AI, NO translate)
            # -------------------
            if mtype == "message":
                if joined_room is None:
                    await ws_send(ws, {"type":"error","message":"NOT_IN_ROOM"})
                    continue

                text = str(msg.get("text") or "").strip()
                if not text:
                    continue

                # herkes kendi ödesin (ileride)
                await charge_sender_if_needed(my_meta, cost=1)

                payload = {
                    "type": "message",
                    "from": my_meta.get("from"),
                    "from_name": my_meta.get("from_name"),
                    "from_pic": my_meta.get("from_pic"),
                    "lang": my_meta.get("me_lang"),
                    "text": text,          # ✅ ham metin
                    "ts": int(now()*1000),
                }

                # ✅ gönderen hariç herkese
                await broadcast(joined_room, payload, exclude=ws)
                continue

            await ws_send(ws, {"type":"error","message":"UNKNOWN_TYPE"})

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

                # presence
                try:
                    await send_presence(room_id, joined_room)
                except Exception:
                    pass

                # oda boşsa sil
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
