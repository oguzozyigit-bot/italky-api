# FILE: italky-api/app/routers/word_duel_ws.py

from __future__ import annotations

import json
import time
import asyncio
import httpx
from typing import Dict, Any, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["word-duel-ws"])

ROOM_TTL_SEC = 60 * 30  # 30 dk

# room structure
# ROOMS[room_id] = {
#   "created_at": float,
#   "clients": set(WebSocket),
#   "meta": { ws: {...} },
#   "lang": "en",
#   "words_used": set(),
#   "scores": { user_id: int }
# }

ROOMS: Dict[str, Dict[str, Any]] = {}

# RAM cache: lang -> set(words)
WORD_CACHE: Dict[str, Set[str]] = {}

LANGPOOL_BASE = "https://YOUR_SUPABASE_PUBLIC_URL/langpool"  # <-- BUNU config ile eşitle

def now():
    return time.time()

def norm_word(s: str) -> str:
    return (
        str(s or "")
        .lower()
        .strip()
    )

async def load_word_pool(lang: str) -> Set[str]:
    if lang in WORD_CACHE:
        return WORD_CACHE[lang]

    url = f"{LANGPOOL_BASE}/{lang}.json"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return set()

        data = r.json()
        items = data.get("items", [])

        words = set()
        for it in items:
            w = norm_word(it.get("w"))
            if w:
                words.add(w)

        WORD_CACHE[lang] = words
        return words

    except Exception:
        return set()


async def broadcast(room, payload):
    dead = []
    for c in list(room["clients"]):
        try:
            await c.send_text(json.dumps(payload))
        except Exception:
            dead.append(c)
    for d in dead:
        room["clients"].discard(d)
        room["meta"].pop(d, None)


@router.websocket("/word-duel/ws/{room_id}")
async def word_duel_ws(ws: WebSocket, room_id: str):

    await ws.accept()

    room_id = room_id.strip().upper()
    joined_room = None
    user_id = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw or "{}")
            mtype = msg.get("type")

            # CREATE
            if mtype == "create":
                lang = msg.get("lang", "en").lower()

                if room_id not in ROOMS:
                    ROOMS[room_id] = {
                        "created_at": now(),
                        "clients": set(),
                        "meta": {},
                        "lang": lang,
                        "words_used": set(),
                        "scores": {}
                    }

                joined_room = ROOMS[room_id]
                joined_room["clients"].add(ws)

                user_id = msg.get("user_id")
                joined_room["meta"][ws] = {"user_id": user_id}
                joined_room["scores"].setdefault(user_id, 0)

                await broadcast(joined_room, {
                    "type": "state",
                    "scores": joined_room["scores"]
                })
                continue

            # JOIN
            if mtype == "join":
                if room_id not in ROOMS:
                    await ws.send_text(json.dumps({"type":"room_not_found"}))
                    await ws.close()
                    return

                joined_room = ROOMS[room_id]
                joined_room["clients"].add(ws)

                user_id = msg.get("user_id")
                joined_room["meta"][ws] = {"user_id": user_id}
                joined_room["scores"].setdefault(user_id, 0)

                await broadcast(joined_room, {
                    "type": "state",
                    "scores": joined_room["scores"]
                })
                continue

            # WORD SUBMIT
            if mtype == "word":
                if not joined_room:
                    continue

                word = norm_word(msg.get("word"))
                if not word:
                    continue

                lang = joined_room["lang"]
                word_pool = await load_word_pool(lang)

                # geçerli mi?
                if word not in word_pool:
                    await ws.send_text(json.dumps({
                        "type": "invalid",
                        "word": word
                    }))
                    continue

                # daha önce yazıldı mı?
                if word in joined_room["words_used"]:
                    await ws.send_text(json.dumps({
                        "type": "duplicate",
                        "word": word
                    }))
                    continue

                # geçerli!
                joined_room["words_used"].add(word)
                joined_room["scores"][user_id] += 10

                await broadcast(joined_room, {
                    "type": "score",
                    "word": word,
                    "by": user_id,
                    "scores": joined_room["scores"]
                })
                continue

    except WebSocketDisconnect:
        pass

    finally:
        if joined_room:
            joined_room["clients"].discard(ws)
            joined_room["meta"].pop(ws, None)
