# FILE: italky-api/app/routers/word_duel_ws.py
from __future__ import annotations

import asyncio
import json
import os
import time
import unicodedata
from typing import Any, Dict, Optional, Set, List

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["word-duel-ws"])

ROOM_TTL_SEC = 60 * 30
GAME_SECONDS = 120

# ENV: LANGPOOL_BASE (frontend config.js ile aynı olmalı)
# örn: https://<supabase-project>.supabase.co/storage/v1/object/public/langpool
LANGPOOL_BASE = (os.getenv("LANGPOOL_BASE", "") or "").strip()

# ROOMS[room_id] = {
#   "created_at": float,
#   "updated_at": float,
#   "lang": "en",
#   "clients": set(WebSocket),
#   "meta": { ws: {"id","name","pic"} },
#   "scores": { id: int },
#   "used": set(str),
#   "running": bool,
#   "ends_at": float|None,
#   "task": asyncio.Task|None,
#   "combo": { id: {"last": float, "streak": int} }
# }
ROOMS: Dict[str, Dict[str, Any]] = {}

# WORD_CACHE[lang] = set(normalized_word)
WORD_CACHE: Dict[str, Set[str]] = {}
WORD_CACHE_AT: Dict[str, float] = {}
WORD_CACHE_TTL = 60 * 15  # 15 dk

ALLOWED_LANGS = {"en", "de", "fr", "es", "it"}

def now() -> float:
    return time.time()

def norm_room_id(room_id: str) -> str:
    s = (room_id or "").strip().upper()
    s = "".join(ch for ch in s if ch.isalnum())
    return s[:8]

def _strip_diacritics(s: str) -> str:
    # NFD remove accents
    return "".join(
        ch for ch in unicodedata.normalize("NFD", s)
        if unicodedata.category(ch) != "Mn"
    )

def norm_word(s: str) -> str:
    s = (s or "").strip().lower()
    s = _strip_diacritics(s)
    # keep letters/numbers/spaces only
    out = []
    for ch in s:
        if ch.isalnum() or ch.isspace():
            out.append(ch)
    s = "".join(out)
    s = " ".join(s.split())
    return s

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
    roster = []
    meta = room.get("meta") or {}
    for _ws, m in meta.items():
        pid = str(m.get("id") or "")
        roster.append({
            "id": pid,
            "name": str(m.get("name") or "User"),
            "pic": str(m.get("pic") or ""),
            "score": int(room["scores"].get(pid, 0)),
        })
    return roster

async def send_state(room: Dict[str, Any]) -> None:
    await broadcast(room, {
        "type": "state",
        "lang": room["lang"],
        "running": bool(room.get("running")),
        "ends_at": room.get("ends_at"),
        "count": len(room["clients"]),
        "roster": build_roster(room),
    })

async def load_word_pool(lang: str) -> Set[str]:
    lang = (lang or "en").strip().lower()
    if lang not in ALLOWED_LANGS:
        lang = "en"

    # cache ttl
    if lang in WORD_CACHE and (now() - WORD_CACHE_AT.get(lang, 0)) < WORD_CACHE_TTL:
        return WORD_CACHE[lang]

    if not LANGPOOL_BASE:
        # no base -> empty (game won't validate)
        WORD_CACHE[lang] = set()
        WORD_CACHE_AT[lang] = now()
        return WORD_CACHE[lang]

    url = f"{LANGPOOL_BASE}/{lang}.json"
    words: Set[str] = set()

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            WORD_CACHE[lang] = set()
            WORD_CACHE_AT[lang] = now()
            return WORD_CACHE[lang]

        data = r.json()
        items = data.get("items") or []
        for it in items:
            w = norm_word(str(it.get("w") or ""))
            if w:
                words.add(w)

        WORD_CACHE[lang] = words
        WORD_CACHE_AT[lang] = now()
        return words
    except Exception:
        WORD_CACHE[lang] = set()
        WORD_CACHE_AT[lang] = now()
        return WORD_CACHE[lang]

def get_room(room_id: str) -> Optional[Dict[str, Any]]:
    rid = norm_room_id(room_id)
    r = ROOMS.get(rid)
    if not r:
        return None
    if (now() - float(r.get("created_at", 0))) > ROOM_TTL_SEC:
        # expire
        try:
            t = r.get("task")
            if t and not t.done():
                t.cancel()
        except Exception:
            pass
        ROOMS.pop(rid, None)
        return None
    return r

def create_room(room_id: str, lang: str) -> Dict[str, Any]:
    rid = norm_room_id(room_id)
    lang = (lang or "en").strip().lower()
    if lang not in ALLOWED_LANGS:
        lang = "en"
    r = {
        "created_at": now(),
        "updated_at": now(),
        "lang": lang,
        "clients": set(),
        "meta": {},
        "scores": {},
        "used": set(),
        "running": False,
        "ends_at": None,
        "task": None,
        "combo": {},
    }
    ROOMS[rid] = r
    return r

async def game_timer(room_id: str) -> None:
    # called once per game start
    rid = norm_room_id(room_id)
    room = ROOMS.get(rid)
    if not room:
        return

    room["running"] = True
    room["ends_at"] = now() + GAME_SECONDS
    room["updated_at"] = now()
    await send_state(room)

    # tick
    try:
        while True:
            await asyncio.sleep(1.0)
            room = ROOMS.get(rid)
            if not room:
                return
            if not room.get("running"):
                return
            if room.get("ends_at") and now() >= float(room["ends_at"]):
                break

        room = ROOMS.get(rid)
        if not room:
            return

        room["running"] = False
        room["ends_at"] = None
        room["updated_at"] = now()

        # winner
        scores: Dict[str, int] = room.get("scores") or {}
        winner_id = None
        winner_score = -1
        for pid, sc in scores.items():
            if int(sc) > winner_score:
                winner_id = pid
                winner_score = int(sc)

        await broadcast(room, {
            "type": "ended",
            "winner_id": winner_id,
            "scores": scores,
            "roster": build_roster(room),
        })
        await send_state(room)

    except asyncio.CancelledError:
        return
    except Exception:
        return

def combo_points(room: Dict[str, Any], pid: str) -> int:
    """
    Base +10
    If user sends another valid word within 3s: +5 * streak (cap 3)
    """
    base = 10
    c = room["combo"].get(pid) or {"last": 0.0, "streak": 0}
    last = float(c.get("last") or 0.0)
    streak = int(c.get("streak") or 0)

    if now() - last <= 3.0:
        streak += 1
    else:
        streak = 0

    streak_cap = min(streak, 3)
    bonus = 5 * streak_cap

    room["combo"][pid] = {"last": now(), "streak": streak}
    return base + bonus

@router.websocket("/duel/ws/{room_id}")
async def duel_ws(ws: WebSocket, room_id: str):
    rid = norm_room_id(room_id)
    await ws.accept()

    joined_room: Optional[Dict[str, Any]] = None
    my_id = ""
    my_name = "User"
    my_pic = ""

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw or "{}")
            mtype = str(msg.get("type") or "").strip()

            # join_check
            if mtype == "join_check":
                r = get_room(rid)
                await ws_send(ws, {"type": "room_ok" if r else "room_not_found"})
                continue

            # create
            if mtype == "create":
                if joined_room is not None:
                    await ws_send(ws, {"type":"error","message":"ALREADY_JOINED"})
                    continue

                lang = str(msg.get("lang") or "en").strip().lower()
                r = get_room(rid)
                if r is None:
                    r = create_room(rid, lang)

                joined_room = r

                my_id = str(msg.get("id") or msg.get("from") or "").strip() or f"p_{int(now()*1000)}"
                my_name = str(msg.get("name") or msg.get("from_name") or "Host").strip()[:40]
                my_pic = str(msg.get("pic") or msg.get("from_pic") or "").strip()

                r["clients"].add(ws)
                r["meta"][ws] = {"id": my_id, "name": my_name, "pic": my_pic}
                r["scores"].setdefault(my_id, 0)
                r["updated_at"] = now()

                # pre-warm word cache
                asyncio.create_task(load_word_pool(r["lang"]))

                await ws_send(ws, {"type":"room_created","room": rid, "lang": r["lang"], "ttl_sec": ROOM_TTL_SEC})
                await send_state(r)

                # auto-start when >=2
                if (not r["running"]) and len(r["clients"]) >= 2 and r.get("task") is None:
                    r["task"] = asyncio.create_task(game_timer(rid))
                continue

            # join
            if mtype == "join":
                if joined_room is not None:
                    await ws_send(ws, {"type":"error","message":"ALREADY_JOINED"})
                    continue

                r = get_room(rid)
                if r is None:
                    await ws_send(ws, {"type":"room_not_found","message":"Kod hatalı olabilir veya oda kapanmış olabilir."})
                    await ws.close()
                    return

                joined_room = r

                my_id = str(msg.get("id") or msg.get("from") or "").strip() or f"p_{int(now()*1000)}"
                my_name = str(msg.get("name") or msg.get("from_name") or "Guest").strip()[:40]
                my_pic = str(msg.get("pic") or msg.get("from_pic") or "").strip()

                r["clients"].add(ws)
                r["meta"][ws] = {"id": my_id, "name": my_name, "pic": my_pic}
                r["scores"].setdefault(my_id, 0)
                r["updated_at"] = now()

                asyncio.create_task(load_word_pool(r["lang"]))

                await ws_send(ws, {"type":"room_joined","room": rid, "lang": r["lang"], "ttl_sec": ROOM_TTL_SEC})
                await send_state(r)

                # auto-start when >=2
                if (not r["running"]) and len(r["clients"]) >= 2 and r.get("task") is None:
                    r["task"] = asyncio.create_task(game_timer(rid))
                continue

            # submit word
            if mtype == "word":
                if joined_room is None:
                    await ws_send(ws, {"type":"error","message":"NOT_IN_ROOM"})
                    continue

                r = joined_room
                if not r.get("running"):
                    await ws_send(ws, {"type":"not_running"})
                    continue

                text = str(msg.get("word") or "").strip()
                w = norm_word(text)
                if not w or len(w) < 2:
                    await ws_send(ws, {"type":"invalid","word": text})
                    continue

                # validate
                pool = await load_word_pool(r["lang"])
                if not pool:
                    await ws_send(ws, {"type":"invalid","word": text})
                    continue
                if w not in pool:
                    await ws_send(ws, {"type":"invalid","word": text})
                    continue
                if w in r["used"]:
                    await ws_send(ws, {"type":"duplicate","word": text})
                    continue

                r["used"].add(w)
                pts = combo_points(r, my_id)
                r["scores"][my_id] = int(r["scores"].get(my_id, 0)) + int(pts)
                r["updated_at"] = now()

                await broadcast(r, {
                    "type": "scored",
                    "by": my_id,
                    "name": my_name,
                    "word": w,
                    "points": pts,
                    "scores": r["scores"],
                })
                await send_state(r)
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
                # if empty -> cleanup (but keep TTL is ok; we clean hard when empty)
                if len(joined_room["clients"]) == 0:
                    try:
                        t = joined_room.get("task")
                        if t and not t.done():
                            t.cancel()
                    except Exception:
                        pass
                    ROOMS.pop(rid, None)
                else:
                    try:
                        await send_state(joined_room)
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
