# FILE: italky-api/app/routers/f2f_ws.py
from __future__ import annotations

import json
import time
from typing import Dict, Any, Optional, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["f2f-ws"])

ROOM_TTL_SEC = 60 * 30   # 30 dk
FLOOR_TTL_SEC = 15       # konuşma hakkı max 15 sn

# ROOMS[room_id] = {
#   "created_at": float,
#   "updated_at": float,
#   "clients": set(WebSocket),
#   "meta": {
#       WebSocket: {
#           "from","from_name","from_pic","me_lang","role","user_id"
#       }
#   },
#   "floor": {
#       "holder_ws": Optional[WebSocket],
#       "holder_id": str,
#       "holder_name": str,
#       "until": float
#   }
# }
ROOMS: Dict[str, Dict[str, Any]] = {}


def now() -> float:
    return time.time()


def norm_room_id(room_id: str) -> str:
    s = (room_id or "").strip().upper()
    s = "".join([ch for ch in s if ch.isalnum()])
    return s[:8]


def clean_name(value: str, fallback: str = "User") -> str:
    v = str(value or "").strip()
    return v[:60] if v else fallback


def clean_pic(value: str) -> str:
    return str(value or "").strip()[:500]


def clean_lang(value: str, fallback: str = "tr") -> str:
    v = str(value or fallback).strip().lower()
    return v or fallback


def clean_user_id(value: str) -> str:
    return str(value or "").strip()[:120]


def room_expired(room: Dict[str, Any]) -> bool:
    return (now() - float(room.get("created_at", 0))) > ROOM_TTL_SEC


def get_room(room_id: str) -> Optional[Dict[str, Any]]:
    rid = norm_room_id(room_id)
    r = ROOMS.get(rid)
    if not r:
        return None
    if room_expired(r):
        try:
            del ROOMS[rid]
        except Exception:
            pass
        return None
    return r


def create_room(room_id: str) -> Dict[str, Any]:
    rid = norm_room_id(room_id)
    r = {
        "created_at": now(),
        "updated_at": now(),
        "clients": set(),   # type: ignore
        "meta": {},         # type: ignore
        "floor": {
            "holder_ws": None,
            "holder_id": "",
            "holder_name": "",
            "until": 0.0,
        }
    }
    ROOMS[rid] = r
    return r


async def ws_send(ws: Optional[WebSocket], msg: Dict[str, Any]) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(msg, ensure_ascii=False))
    except Exception:
        return


async def broadcast(room: Dict[str, Any], msg: Dict[str, Any], exclude: Optional[WebSocket] = None) -> None:
    dead: List[WebSocket] = []
    for c in list(room["clients"]):
        if exclude is not None and c is exclude:
            continue
        try:
            await c.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            dead.append(c)

    for d in dead:
        try:
            room["clients"].discard(d)
            room["meta"].pop(d, None)
        except Exception:
            pass


def build_roster(room: Dict[str, Any]) -> List[Dict[str, Any]]:
    roster: List[Dict[str, Any]] = []
    meta = room.get("meta") or {}

    for _ws, m in meta.items():
        try:
            roster.append({
                "from": str(m.get("from") or ""),
                "from_name": str(m.get("from_name") or "User"),
                "from_pic": str(m.get("from_pic") or ""),
                "me_lang": str(m.get("me_lang") or "tr"),
                "role": str(m.get("role") or "guest"),
                "user_id": str(m.get("user_id") or ""),
            })
        except Exception:
            continue

    roster.sort(key=lambda x: (0 if x.get("role") == "host" else 1, x.get("from_name") or ""))
    return roster


def get_floor_state(room: Dict[str, Any]) -> Dict[str, Any]:
    f = room.get("floor") or {}
    holder_id = str(f.get("holder_id") or "")
    holder_name = str(f.get("holder_name") or "")
    until = float(f.get("until") or 0.0)
    active = bool(holder_id) and (until > now())

    if not active:
      f["holder_ws"] = None
      f["holder_id"] = ""
      f["holder_name"] = ""
      f["until"] = 0.0
      holder_id = ""
      holder_name = ""
      until = 0.0

    return {
        "active": bool(holder_id),
        "holder_id": holder_id,
        "holder_name": holder_name,
        "until_ms": int(until * 1000) if until else 0,
        "ttl_sec": FLOOR_TTL_SEC
    }


async def send_presence(room: Dict[str, Any]) -> None:
    await broadcast(room, {
        "type": "presence",
        "count": len(room["clients"]),
        "roster": build_roster(room),
        "ttl_sec": ROOM_TTL_SEC,
    })


async def broadcast_floor(room: Dict[str, Any]) -> None:
    await broadcast(room, {
        "type": "floor_state",
        **get_floor_state(room),
    })


def try_acquire_floor(room: Dict[str, Any], ws: WebSocket, holder_id: str, holder_name: str) -> bool:
    f = room.get("floor") or {}

    _ = get_floor_state(room)

    if f.get("holder_ws") is None and not f.get("holder_id"):
        f["holder_ws"] = ws
        f["holder_id"] = holder_id
        f["holder_name"] = holder_name
        f["until"] = now() + FLOOR_TTL_SEC
        room["floor"] = f
        return True

    if f.get("holder_ws") is ws or str(f.get("holder_id") or "") == holder_id:
        f["holder_ws"] = ws
        f["holder_id"] = holder_id
        f["holder_name"] = holder_name
        f["until"] = now() + FLOOR_TTL_SEC
        room["floor"] = f
        return True

    return False


def release_floor_if_holder(room: Dict[str, Any], ws: WebSocket, holder_id: str) -> bool:
    f = room.get("floor") or {}
    if f.get("holder_ws") is ws or str(f.get("holder_id") or "") == holder_id:
        f["holder_ws"] = None
        f["holder_id"] = ""
        f["holder_name"] = ""
        f["until"] = 0.0
        room["floor"] = f
        return True
    return False


@router.websocket("/f2f/ws/{room_id}")
async def f2f_ws(ws: WebSocket, room_id: str):
    rid = norm_room_id(room_id)
    await ws.accept()

    joined_room: Optional[Dict[str, Any]] = None
    my_meta: Dict[str, Any] = {}

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw or "{}")
            mtype = str(msg.get("type") or "").strip()

            if mtype == "join_check":
                r = get_room(rid)
                await ws_send(ws, {"type": "room_ok" if r else "room_not_found"})
                continue

            if mtype == "create":
                if joined_room is not None:
                    await ws_send(ws, {"type": "error", "message": "ALREADY_JOINED"})
                    continue

                r = get_room(rid)
                if r is None:
                    r = create_room(rid)

                joined_room = r
                my_meta = {
                    "from": str(msg.get("from") or ""),
                    "from_name": clean_name(msg.get("from_name"), "Host"),
                    "from_pic": clean_pic(msg.get("from_pic")),
                    "me_lang": clean_lang(msg.get("me_lang"), "tr"),
                    "role": "host",
                    "user_id": clean_user_id(msg.get("user_id")),
                }

                r["clients"].add(ws)
                r["meta"][ws] = my_meta
                r["updated_at"] = now()

                await ws_send(ws, {
                    "type": "room_created",
                    "room": rid,
                    "ttl_sec": ROOM_TTL_SEC,
                    "self": my_meta,
                })
                await send_presence(r)
                await broadcast_floor(r)
                continue

            if mtype == "join":
                if joined_room is not None:
                    await ws_send(ws, {"type": "error", "message": "ALREADY_JOINED"})
                    continue

                r = get_room(rid)
                if r is None:
                    await ws_send(ws, {
                        "type": "room_not_found",
                        "message": "Kod hatalı olabilir veya sohbet odası kapanmış olabilir."
                    })
                    await ws.close()
                    return

                joined_room = r
                my_meta = {
                    "from": str(msg.get("from") or ""),
                    "from_name": clean_name(msg.get("from_name"), "Guest"),
                    "from_pic": clean_pic(msg.get("from_pic")),
                    "me_lang": clean_lang(msg.get("me_lang"), "tr"),
                    "role": "guest",
                    "user_id": clean_user_id(msg.get("user_id")),
                }

                r["clients"].add(ws)
                r["meta"][ws] = my_meta
                r["updated_at"] = now()

                await ws_send(ws, {
                    "type": "room_joined",
                    "room": rid,
                    "ttl_sec": ROOM_TTL_SEC,
                    "self": my_meta,
                })

                await broadcast(r, {
                    "type": "peer_joined",
                    "peer": my_meta,
                    "count": len(r["clients"]),
                    "roster": build_roster(r),
                })
                await send_presence(r)
                await broadcast_floor(r)
                continue

            if mtype == "profile_sync":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                old = joined_room["meta"].get(ws, {}) or {}

                old["from_name"] = clean_name(msg.get("from_name"), old.get("from_name") or "User")
                pic_candidate = clean_pic(msg.get("from_pic"))
                if pic_candidate:
                    old["from_pic"] = pic_candidate
                old["me_lang"] = clean_lang(msg.get("me_lang"), old.get("me_lang") or "tr")
                uid_candidate = clean_user_id(msg.get("user_id"))
                if uid_candidate:
                    old["user_id"] = uid_candidate

                joined_room["meta"][ws] = old
                my_meta = old
                joined_room["updated_at"] = now()

                await broadcast(joined_room, {
                    "type": "profile_updated",
                    "peer": my_meta,
                    "roster": build_roster(joined_room),
                })
                await send_presence(joined_room)
                continue

            if mtype == "floor_request":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                holder_id = str(my_meta.get("from") or msg.get("from") or "")
                holder_name = str(my_meta.get("from_name") or msg.get("from_name") or "User")

                ok = try_acquire_floor(joined_room, ws, holder_id, holder_name)
                if ok:
                    await ws_send(ws, {"type": "floor_granted"})
                    await broadcast_floor(joined_room)
                else:
                    state = get_floor_state(joined_room)
                    await ws_send(ws, {"type": "floor_busy", **state})

                joined_room["updated_at"] = now()
                continue

            if mtype == "floor_release":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                holder_id = str(my_meta.get("from") or msg.get("from") or "")
                changed = release_floor_if_holder(joined_room, ws, holder_id)
                if changed:
                    await broadcast_floor(joined_room)

                joined_room["updated_at"] = now()
                continue

            # NOT:
            # Bu websocket dosyası SADECE mesaj relay eder.
            # Translate ve TTS burada yok.
            if mtype == "message":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                _ = get_floor_state(joined_room)

                text = str(msg.get("text") or "").strip()
                if not text:
                    continue

                lang = clean_lang(msg.get("lang"), my_meta.get("me_lang") or "tr")

                payload = {
                    "type": "message",
                    "from": my_meta.get("from") or "",
                    "from_name": my_meta.get("from_name") or "User",
                    "from_pic": my_meta.get("from_pic") or "",
                    "user_id": my_meta.get("user_id") or "",
                    "role": my_meta.get("role") or "guest",
                    "lang": lang,
                    "text": text,
                    "ts": int(now() * 1000),
                }

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
                leaving_meta = joined_room["meta"].get(ws, {}) or {}

                try:
                    holder_ws = (joined_room.get("floor") or {}).get("holder_ws")
                    if holder_ws is ws:
                        release_floor_if_holder(joined_room, ws, str(leaving_meta.get("from") or ""))
                        await broadcast_floor(joined_room)
                except Exception:
                    pass

                joined_room["clients"].discard(ws)
                joined_room["meta"].pop(ws, None)
                joined_room["updated_at"] = now()

                try:
                    await broadcast(joined_room, {
                        "type": "peer_left",
                        "peer": {
                            "from": leaving_meta.get("from") or "",
                            "from_name": leaving_meta.get("from_name") or "User",
                            "from_pic": leaving_meta.get("from_pic") or "",
                            "role": leaving_meta.get("role") or "guest",
                            "user_id": leaving_meta.get("user_id") or "",
                        },
                        "count": len(joined_room["clients"]),
                        "roster": build_roster(joined_room),
                    })
                except Exception:
                    pass

                try:
                    await send_presence(joined_room)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            await ws.close()
        except Exception:
            pass
