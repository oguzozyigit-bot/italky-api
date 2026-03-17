from __future__ import annotations

import json
import logging
import os
import secrets
import time
from typing import Dict, Any, Optional, List

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google.oauth2 import service_account
from google.cloud import translate as translate_v3

router = APIRouter(tags=["alltoall-ws"])
logger = logging.getLogger("italky-alltoall")

ROOM_TTL_SEC = 60 * 60 * 4
MAX_PARTICIPANTS = 50

GOOGLE_CREDS_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
GOOGLE_CREDS_JSON = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON") or "").strip()

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

_translate_client: Optional[translate_v3.TranslationServiceClient] = None
_translate_project_id: Optional[str] = None

ROOMS: Dict[str, Dict[str, Any]] = {}


def now() -> float:
    return time.time()


def norm_room_id(room_id: str) -> str:
    s = (room_id or "").strip().upper()
    s = "".join(ch for ch in s if ch.isalnum())
    return s[:8]


def new_room_code() -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(chars) for _ in range(6))


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
    return (now() - float(room.get("updated_at", 0))) > ROOM_TTL_SEC


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


def create_room(room_id: Optional[str] = None) -> Dict[str, Any]:
    rid = norm_room_id(room_id or new_room_code())
    while rid in ROOMS:
        rid = new_room_code()

    room = {
        "room_id": rid,
        "created_at": now(),
        "updated_at": now(),
        "clients": set(),
        "meta": {},
    }
    ROOMS[rid] = room
    return room


async def ws_send(ws: Optional[WebSocket], msg: Dict[str, Any]) -> None:
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(msg, ensure_ascii=False))
    except Exception:
        return


async def broadcast(room: Dict[str, Any], msg: Dict[str, Any], exclude: Optional[WebSocket] = None) -> None:
    dead: List[WebSocket] = []
    payload = json.dumps(msg, ensure_ascii=False)

    for c in list(room["clients"]):
        if exclude is not None and c is exclude:
            continue
        try:
            await c.send_text(payload)
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


async def send_presence(room: Dict[str, Any]) -> None:
    await broadcast(room, {
        "type": "presence",
        "room": room.get("room_id") or "",
        "count": len(room["clients"]),
        "roster": build_roster(room),
        "max_participants": MAX_PARTICIPANTS,
        "ttl_sec": ROOM_TTL_SEC,
    })


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

    resp = client.translate_text(request=payload, timeout=4.0)

    out = ""
    if resp.translations:
        out = (resp.translations[0].translated_text or "").strip()

    if not out:
        raise RuntimeError("Google Translate returned empty response")

    return out


def post_clean_translation(text: str) -> str:
    value = str(text or "").strip()
    value = " ".join(value.split())
    if value and value[-1] not in ".!?":
        value += "."
    return value


def build_meaning_prompt(text: str, from_lang: str, to_lang: str) -> str:
    return f"""
You are an expert live interpreter for a multilingual meeting.

Task:
- Understand the intended meaning, even if there are spelling mistakes, slang, short fragments, or informal speech.
- Translate by meaning, not word-for-word.
- Keep the output natural, short, and complete.
- Never explain your answer.
- Return exactly one final translated sentence only.

Rules:
- Do not add extra friendliness.
- Do not over-dramatize.
- Prefer the most natural everyday equivalent in the target language.
- If the source already matches the target language, return it naturally.
- Make the output a complete sentence when possible.

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

    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini API key missing")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

    body = {
        "system_instruction": {
            "parts": [
                {
                    "text": "You are a professional live interpreter. Always return one complete natural translated sentence only."
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
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=body)
    except Exception as e:
        raise RuntimeError("Gemini request failed") from e

    if resp.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError("Gemini invalid JSON") from e

    try:
        out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        raise RuntimeError("Gemini empty response") from e

    if not out:
        raise RuntimeError("Gemini blank translation")

    return post_clean_translation(out)


async def translate_with_fallback(text: str, from_lang: str, to_lang: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""

    src = clean_lang(from_lang, "auto")
    dst = clean_lang(to_lang, "tr")

    if src == dst:
        return value

    try:
        return await translate_with_gemini(value, src, dst)
    except Exception as gemini_error:
        logger.warning("ALLTOALL_GEMINI_FAILED_FALLBACK_GOOGLE: %s", gemini_error)

    return post_clean_translation(translate_with_google(value, src, dst))


async def fanout_translated(room: Dict[str, Any], sender_ws: WebSocket, original_text: str, from_lang: str):
    meta = room.get("meta") or {}
    sender_meta = meta.get(sender_ws, {}) or {}

    sender_name = str(sender_meta.get("from_name") or "User")
    sender_pic = str(sender_meta.get("from_pic") or "")
    sender_id = str(sender_meta.get("from") or "")
    sender_user_id = str(sender_meta.get("user_id") or "")
    sender_role = str(sender_meta.get("role") or "guest")

    for target_ws, target_meta in list(meta.items()):
        if target_ws is sender_ws:
            continue

        to_lang = clean_lang(target_meta.get("me_lang"), "tr")

        try:
            translated = await translate_with_fallback(original_text, from_lang, to_lang)
        except Exception as e:
            logger.exception("ALLTOALL_TRANSLATE_FAIL %s", e)
            translated = original_text

        await ws_send(target_ws, {
            "type": "translated_message",
            "from": sender_id,
            "from_name": sender_name,
            "from_pic": sender_pic,
            "from_user_id": sender_user_id,
            "role": sender_role,
            "original_text": original_text,
            "translated_text": translated,
            "from_lang": from_lang,
            "to_lang": to_lang,
            "ts": int(now() * 1000),
        })


@router.websocket("/alltoall/ws/{room_id}")
async def alltoall_ws(ws: WebSocket, room_id: str):
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
                if r is None:
                    await ws_send(ws, {
                        "type": "room_not_found",
                        "message": "Kanal henüz oluşturulmamış."
                    })
                    continue

                roster = build_roster(r)
                has_host = any((x.get("role") == "host") for x in roster)

                if not has_host:
                    await ws_send(ws, {
                        "type": "host_not_ready",
                        "message": "Host henüz kanala giriş yapmadı."
                    })
                    continue

                await ws_send(ws, {
                    "type": "room_ok",
                    "message": "Kanal hazır."
                })
                continue

            if mtype == "create":
                if joined_room is not None:
                    await ws_send(ws, {"type": "error", "message": "ALREADY_JOINED"})
                    continue

                r = get_room(rid)
                if r is None:
                    r = create_room(rid)

                if len(r["clients"]) >= MAX_PARTICIPANTS:
                    await ws_send(ws, {"type": "error", "message": "ROOM_FULL"})
                    await ws.close()
                    return

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
                    "max_participants": MAX_PARTICIPANTS,
                    "ttl_sec": ROOM_TTL_SEC,
                    "self": my_meta,
                })
                await send_presence(r)
                continue

            if mtype == "join":
                if joined_room is not None:
                    await ws_send(ws, {"type": "error", "message": "ALREADY_JOINED"})
                    continue

                r = get_room(rid)
                if r is None:
                    await ws_send(ws, {
                        "type": "room_not_found",
                        "message": "Kanal henüz oluşturulmamış veya kapanmış."
                    })
                    await ws.close()
                    return

                roster = build_roster(r)
                has_host = any((x.get("role") == "host") for x in roster)

                if not has_host:
                    await ws_send(ws, {
                        "type": "error",
                        "message": "HOST_NOT_READY"
                    })
                    await ws.close()
                    return

                if len(r["clients"]) >= MAX_PARTICIPANTS:
                    await ws_send(ws, {"type": "error", "message": "ROOM_FULL"})
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
                    "max_participants": MAX_PARTICIPANTS,
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

            if mtype == "typing":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                await broadcast(joined_room, {
                    "type": "typing",
                    "from": my_meta.get("from") or "",
                    "from_name": my_meta.get("from_name") or "User",
                    "from_pic": my_meta.get("from_pic") or "",
                    "role": my_meta.get("role") or "guest",
                    "user_id": my_meta.get("user_id") or "",
                    "ts": int(now() * 1000),
                }, exclude=ws)

                joined_room["updated_at"] = now()
                continue

            if mtype == "message":
                if joined_room is None:
                    await ws_send(ws, {"type": "error", "message": "NOT_IN_ROOM"})
                    continue

                text = str(msg.get("text") or "").strip()
                if not text:
                    continue

                from_lang = clean_lang(msg.get("lang"), my_meta.get("me_lang") or "tr")
                joined_room["updated_at"] = now()

                await ws_send(ws, {
                    "type": "message_sent",
                    "text": text,
                    "from_lang": from_lang,
                    "ts": int(now() * 1000),
                })

                await fanout_translated(joined_room, ws, text, from_lang)
                continue

            logger.warning("ALLTOALL_UNKNOWN_TYPE: %s", mtype)
            await ws_send(ws, {"type": "error", "message": f"UNKNOWN_TYPE:{mtype}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("ALLTOALL_WS_ERROR %s", e)
    finally:
        if joined_room is not None:
            try:
                leaving_meta = joined_room["meta"].get(ws, {}) or {}

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
