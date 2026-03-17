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

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger("italky-interpreter")
router = APIRouter(tags=["interpreter"])


# =========================
# GEMINI TRANSLATE
# =========================

def build_meaning_prompt(text: str, from_lang: str, to_lang: str) -> str:
    return f"""
You are an expert live interpreter.

Your job:
- Understand the user's intended meaning, even with spelling mistakes, slang, or incomplete sentences.
- Fix the meaning silently if needed.
- Do NOT translate word-for-word.
- Translate naturally as a native speaker would say it.

Rules:
- Do NOT add extra words that are not in the original meaning.
- Do NOT add "buddy", "bro", names, or personal expressions unless clearly present.
- Keep tone natural but neutral unless emotion is clearly expressed.
- Keep the sentence clean and concise.
- Do not exaggerate or over-interpret.
- Do not explain anything.

Output:
- Return ONLY the final translated sentence.
- No quotes.
- No extra text.

Source language: {from_lang}
Target language: {to_lang}

Text:
{text}
""".strip()


async def translate_with_gemini(text: str, from_lang: str, to_lang: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""

    src = (from_lang or "auto").strip().lower()
    dst = (to_lang or "tr").strip().lower()

    if src == dst:
        return value

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": build_meaning_prompt(value, src, dst)
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": 200
        }
    }

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=body)

    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini API error: {resp.status_code} {resp.text}")

    data = resp.json()

    try:
        out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        raise RuntimeError("Gemini returned empty response")

    if not out:
        raise RuntimeError("Gemini returned blank translation")

    return out


# =========================
# ROOM STATE
# =========================

@dataclass
class PeerState:
    role: str
    lang: str
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
# API
# =========================

class CreateRoomReq(BaseModel):
    my_lang: str = "tr"
    host_code: str = "HOME-HOST"
    mode: str = "interpreter"


@router.post("/interpreter/create-room")
async def create_room(req: CreateRoomReq):
    room_id = new_room_id()

    async with ROOM_LOCK:
        room = RoomState(
            room_id=room_id,
            host_code=req.host_code,
            host_lang=req.my_lang,
        )
        room.peers["host"] = PeerState(role="host", lang=req.my_lang)
        ROOMS[room_id] = room
        HOST_ACTIVE_ROOM[req.host_code] = room_id

    return {
        "ok": True,
        "room_id": room_id
    }


# =========================
# WS
# =========================

@router.websocket("/ws/interpreter/{room_id}")
async def interpreter_ws(websocket: WebSocket, room_id: str):
    await websocket.accept()

    role = (websocket.query_params.get("role") or "guest").strip().lower()
    my_lang = (websocket.query_params.get("lang") or "en").strip().lower()

    room = ROOMS.get(room_id)
    if not room:
        await websocket.close()
        return

    room.sockets.add(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            if data.get("type") == "text_message":
                text = data.get("text")
                from_lang = data.get("from_lang", my_lang)

                to_lang = room.guest_lang if role == "host" else room.host_lang
                if not to_lang:
                    to_lang = "en"

                translated = await translate_with_gemini(text, from_lang, to_lang)

                await broadcast(room, {
                    "type": "translated_message",
                    "sender": role,
                    "translated_text": translated,
                })

    except WebSocketDisconnect:
        room.sockets.discard(websocket)
