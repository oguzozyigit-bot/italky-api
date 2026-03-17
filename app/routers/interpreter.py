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

from google.oauth2 import service_account
from google.cloud import translate as translate_v3

logger = logging.getLogger("italky-interpreter")
router = APIRouter(tags=["interpreter"])

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()

_translate_client: Optional[translate_v3.TranslationServiceClient] = None
_translate_project_id: Optional[str] = None

SAFE_TRANSLATION_ERROR = "Çeviri şu anda kullanılamıyor."


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


IDIOM_OVERRIDES: Dict[tuple[str, str], Dict[str, str]] = {
    ("tr", "en"): {
        "ensemde boza pişiriyorsun": "You're really getting on my nerves.",
        "ensemde boza pisiriyorsun": "You're really getting on my nerves.",
        "bu site anamı ağlattı": "This site is driving me crazy.",
        "bu site anami aglatti": "This site is driving me crazy.",
        "anamı ağlattı": "It drove me crazy.",
        "anami aglatti": "It drove me crazy.",
        "kafa ütülüyor": "It's really getting on my nerves.",
        "kafa utuluyor": "It's really getting on my nerves.",
        "kafayı yedim": "I'm losing my mind.",
        "kafayi yedim": "I'm losing my mind.",
        "içim dışıma çıktı": "I'm exhausted.",
        "icim disima cikti": "I'm exhausted.",
        "kan beynime sıçradı": "I got really mad.",
        "kan beynime sicradi": "I got really mad.",
    }
}


def normalize_text_for_idiom_match(text: str) -> str:
    value = str(text or "").strip().lower()
    replacements = {
        "â": "a",
        "î": "i",
        "û": "u",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)

    value = " ".join(value.split())
    return value


def maybe_translate_with_idiom_override(
    text: str, from_lang: str, to_lang: str
) -> Optional[str]:
    src = (from_lang or "").strip().lower()
    dst = (to_lang or "").strip().lower()
    table = IDIOM_OVERRIDES.get((src, dst)) or {}

    norm = normalize_text_for_idiom_match(text)

    if norm in table:
        return table[norm]

    for phrase, translated in table.items():
        if phrase in norm:
            return translated

    return None


def build_meaning_prompt(text: str, from_lang: str, to_lang: str) -> str:
    return f"""
You are an expert live interpreter for real-world conversation.

Task:
- Understand the intended meaning, even if there are spelling mistakes, slang, missing words, or idioms.
- Translate by meaning, not word-for-word.
- Keep the translation natural, short, and complete.
- Never return a partial sentence.
- Never explain your answer.
- Return exactly one final translated sentence only.

Rules:
- Do not add extra friendliness such as buddy, bro, dear, my friend.
- Do not over-dramatize.
- Prefer the most natural everyday equivalent in the target language.
- If the source is an idiom, use the closest natural idiom in the target language.
- Make sure the output is a complete sentence.
- Always finish the sentence properly.

Examples:
Turkish: "Bu site anamı ağlattı"
English: "This site is driving me crazy."

Turkish: "Ensemde boza pişiriyorsun"
English: "You're really getting on my nerves."

Turkish: "Kanka yarın uğrayayım mı"
English: "Should I drop by tomorrow?"

Turkish: "Yarın gelcem sana da uygunsa"
English: "I'll come tomorrow if that works for you."

Source language: {from_lang}
Target language: {to_lang}

Text:
{text}
""".strip()


def post_clean_translation(text: str) -> str:
    value = str(text or "").strip()
    value = " ".join(value.split())
    if value and value[-1] not in ".!?":
        value += "."
    return value


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
        raise RuntimeError("Translation provider key missing")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    body = {
        "system_instruction": {
            "parts": [
                {
                    "text": "You are a professional interpreter. Always return one complete natural translated sentence only."
                }
            ]
        },
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
            "temperature": 0.0,
            "topP": 0.8,
            "maxOutputTokens": 160,
        },
    }

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception as e:
        raise RuntimeError("Translation provider request failed") from e

    if resp.status_code >= 400:
        raise RuntimeError(f"Translation provider HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError("Translation provider returned invalid JSON") from e

    try:
        out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        raise RuntimeError("Translation provider returned empty response") from e

    if not out:
        raise RuntimeError("Translation provider returned blank translation")

    return post_clean_translation(out)


async def translate_with_fallback(text: str, from_lang: str, to_lang: str) -> str:
    forced = maybe_translate_with_idiom_override(text, from_lang, to_lang)
    if forced:
        return forced

    try:
        return await translate_with_gemini(text, from_lang, to_lang)
    except Exception as gemini_error:
        logger.warning("PRIMARY_TRANSLATION_FAILED_FALLING_BACK: %s", gemini_error)

    try:
        return post_clean_translation(
            translate_with_google(text, from_lang, to_lang)
        )
    except Exception as google_error:
        logger.exception("FALLBACK_TRANSLATION_FAILED: %s", google_error)
        raise RuntimeError("all_translation_providers_failed") from google_error


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


class CreateRoomReq(BaseModel):
    my_lang: str = "tr"
    host_code: str = "HOME-HOST"
    mode: str = "interpreter"


class CreateRoomResp(BaseModel):
    ok: bool
    room_id: str
    join_url: str
    ws_url: str
    status: str
    host_code: str


class ResolveRoomReq(BaseModel):
    host_code: str
    my_lang: str = "tr"
    mode: str = "interpreter"


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


@router.post("/interpreter/create-room", response_model=CreateRoomResp)
async def create_room(req: CreateRoomReq):
    room_id = new_room_id()
    host_lang = (req.my_lang or "tr").strip().lower()
    host_code = (req.host_code or "HOME-HOST").strip().upper()
    mode = (req.mode or "interpreter").strip().lower()

    async with ROOM_LOCK:
        room = RoomState(
            room_id=room_id,
            host_code=host_code,
            mode=mode,
            host_lang=host_lang,
        )
        room.peers["host"] = PeerState(role="host", lang=host_lang)
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
        host_code=host_code,
    )


@router.post("/interpreter/resolve-room", response_model=ResolveRoomResp)
async def resolve_room(req: ResolveRoomReq):
    host_code = (req.host_code or "").strip().upper()
    my_lang = (req.my_lang or "tr").strip().lower()
    mode = (req.mode or "interpreter").strip().lower()

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
            room.peers["host"] = PeerState(role="host", lang=my_lang)
            ROOMS[room_id] = room
            HOST_ACTIVE_ROOM[host_code] = room_id

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

    await broadcast(
        room,
        {
            "type": "peer_joined",
            "room_id": room_id,
            "status": room.status,
            "guest_lang": guest_lang,
            "ts": now_ts(),
        },
    )

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

    role = (websocket.query_params.get("role") or "guest").strip().lower()
    my_lang = (websocket.query_params.get("lang") or "en").strip().lower()

    room = ROOMS.get(room_id)
    if not room:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "error",
                    "message": "Room not found",
                    "ts": now_ts(),
                },
                ensure_ascii=False,
            )
        )
        await websocket.close()
        return

    room.sockets.add(websocket)
    room.updated_at = time.time()

    if role not in room.peers:
        room.peers[role] = PeerState(role=role, lang=my_lang)

    await broadcast(
        room,
        {
            "type": "presence",
            "room_id": room_id,
            "host_code": room.host_code,
            "mode": room.mode,
            "status": room.status,
            "host_lang": room.host_lang,
            "guest_lang": room.guest_lang,
            "peer_count": len(room.peers),
            "ts": now_ts(),
        },
    )

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
                await broadcast(
                    room,
                    {
                        "type": "typing",
                        "sender": role,
                        "ts": now_ts(),
                    },
                )
                continue

            if mtype == "set_lang":
                new_lang = str(data.get("lang") or my_lang).strip().lower()
                my_lang = new_lang

                async with ROOM_LOCK:
                    if role == "host":
                        room.host_lang = new_lang
                    else:
                        room.guest_lang = new_lang

                    room.peers[role] = PeerState(role=role, lang=new_lang)
                    room.updated_at = time.time()

                await broadcast(
                    room,
                    {
                        "type": "presence",
                        "room_id": room_id,
                        "host_code": room.host_code,
                        "mode": room.mode,
                        "status": room.status,
                        "host_lang": room.host_lang,
                        "guest_lang": room.guest_lang,
                        "peer_count": len(room.peers),
                        "ts": now_ts(),
                    },
                )
                continue

            if mtype == "text_message":
                original_text = str(data.get("text") or "").strip()
                from_lang = str(data.get("from_lang") or my_lang).strip().lower()
                to_lang = str(data.get("to_lang") or "").strip().lower()
                sender_user_id = str(data.get("user_id") or "").strip()

                if not original_text:
                    continue

                if not to_lang:
                    if role == "host":
                        to_lang = room.guest_lang or "en"
                    else:
                        to_lang = room.host_lang or "tr"

                try:
                    translated = await translate_with_fallback(
                        original_text, from_lang, to_lang
                    )
                except Exception as e:
                    logger.exception("INTERPRETER_TRANSLATE_FAIL %s", e)
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": SAFE_TRANSLATION_ERROR,
                                "ts": now_ts(),
                            },
                            ensure_ascii=False,
                        )
                    )
                    continue

                await broadcast(
                    room,
                    {
                        "type": "translated_message",
                        "sender": role,
                        "user_id": sender_user_id,
                        "original_text": original_text,
                        "translated_text": translated,
                        "from_lang": from_lang,
                        "to_lang": to_lang,
                        "ts": now_ts(),
                    },
                )

    except WebSocketDisconnect:
        room.sockets.discard(websocket)
        room.updated_at = time.time()

        async with ROOM_LOCK:
            room.peers.pop(role, None)

            if role == "guest":
                room.guest_lang = None
                room.status = "waiting"

            if not room.sockets:
                if HOST_ACTIVE_ROOM.get(room.host_code) == room.room_id:
                    HOST_ACTIVE_ROOM.pop(room.host_code, None)

        await broadcast(
            room,
            {
                "type": "peer_left",
                "sender": role,
                "message": "Karşı taraf odadan ayrıldı.",
                "ts": now_ts(),
            },
        )

    except Exception as e:
        logger.exception("INTERPRETER_WS_ERROR %s", e)
        room.sockets.discard(websocket)
        try:
            await websocket.close()
        except Exception:
            pass
