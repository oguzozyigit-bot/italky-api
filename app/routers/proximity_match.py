from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.routers.interpreter import (
    HOST_ACTIVE_ROOM,
    ROOMS,
    ROOM_LOCK,
    PeerState,
    RoomState,
)

logger = logging.getLogger("italky-proximity")
router = APIRouter(tags=["italky-proximity"])

# =========================================================
# AYARLAR
# =========================================================

MATCH_RADIUS_METERS = float(os.getenv("SHAKE_MATCH_RADIUS_METERS", "20"))
MATCH_WINDOW_SECONDS = int(os.getenv("SHAKE_MATCH_WINDOW_SECONDS", "5"))
SEARCH_POLL_MS = int(os.getenv("SHAKE_SEARCH_POLL_MS", "1000"))

FRONTEND_BASE_URL = (os.getenv("FRONTEND_BASE_URL") or "https://italky.ai").rstrip("/")
PUBLIC_WS_BASE = (os.getenv("PUBLIC_WS_BASE") or "wss://italky-api.onrender.com").rstrip("/")

SEARCH_TTL_SECONDS = MATCH_WINDOW_SECONDS
MATCH_TTL_SECONDS = 60
DUPLICATE_SUBMIT_COOLDOWN_MS = 1200

# =========================================================
# MODELLER
# =========================================================


class ShakeMatchRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    lat: float
    lon: float
    my_lang: str = "tr"
    accuracy_m: Optional[float] = None
    client_ts: Optional[int] = None


class GuestLinkResponse(BaseModel):
    ok: bool
    room_id: str
    join_url: str
    instructions: str


@dataclass
class SearchState:
    search_id: str
    user_id: str
    lat: float
    lon: float
    my_lang: str = "tr"
    accuracy_m: Optional[float] = None
    client_ts: Optional[int] = None
    status: str = "searching"  # searching | matched
    room_id: Optional[str] = None
    peer_id: Optional[str] = None
    match_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SEARCH_TTL_SECONDS)
    matched_at: Optional[float] = None


# =========================================================
# HAFIZA
# =========================================================

SEARCHES_BY_ID: Dict[str, SearchState] = {}
ACTIVE_SEARCH_ID_BY_USER: Dict[str, str] = {}
LAST_SUBMIT_MS_BY_USER: Dict[str, int] = {}
MATCH_LOCK = asyncio.Lock()


# =========================================================
# YARDIMCILAR
# =========================================================

def now_ts() -> int:
    return int(time.time())


def now_ms() -> int:
    return int(time.time() * 1000)


def new_search_id() -> str:
    return "srch_" + secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]


def new_match_id() -> str:
    return "mtch_" + secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]


def new_host_code() -> str:
    return "SHAKE-" + secrets.token_hex(3).upper()


def new_guest_token() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]


def normalize_lang(value: Optional[str], fallback: str = "tr") -> str:
    v = str(value or fallback).strip().lower()
    return v or fallback


def get_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    İki koordinat arasındaki mesafeyi metre cinsinden hesaplar.
    """
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def search_to_searching_payload(search: SearchState) -> dict:
    remaining_ms = max(0, int((search.expires_at - time.time()) * 1000))
    return {
        "ok": True,
        "status": "searching",
        "search_id": search.search_id,
        "expires_in_ms": remaining_ms,
        "poll_after_ms": SEARCH_POLL_MS,
        "message": "Yakınlarda sallanan cihaz aranıyor...",
    }


def build_join_url(room_id: str) -> str:
    return f"{FRONTEND_BASE_URL}/open/interpreter?room={room_id}&v=1"


def build_ws_url(room_id: str) -> str:
    return f"{PUBLIC_WS_BASE}/api/ws/interpreter/{room_id}"


def search_to_matched_payload(search: SearchState) -> dict:
    if not search.room_id or not search.peer_id or not search.match_id:
        raise RuntimeError("Matched search missing critical fields")

    return {
        "ok": True,
        "status": "matched",
        "search_id": search.search_id,
        "match_id": search.match_id,
        "room_id": search.room_id,
        "peer_id": search.peer_id,
        "join_url": build_join_url(search.room_id),
        "ws_url": build_ws_url(search.room_id),
        "matched_at": int(search.matched_at or time.time()),
    }


def prune_expired_states() -> None:
    now = time.time()
    to_delete = []

    for search_id, search in SEARCHES_BY_ID.items():
        if search.expires_at <= now:
            to_delete.append(search_id)

    for search_id in to_delete:
        search = SEARCHES_BY_ID.pop(search_id, None)
        if search and ACTIVE_SEARCH_ID_BY_USER.get(search.user_id) == search_id:
            ACTIVE_SEARCH_ID_BY_USER.pop(search.user_id, None)


async def ensure_interpreter_room_for_match(
    host_user_id: str,
    host_lang: str,
    guest_user_id: str,
    guest_lang: str,
) -> str:
    """
    interpreter.py içindeki oda sistemini kullanır.
    Manuel kod girmeden aynı room_id'yi iki kullanıcıya verir.
    """
    room_id = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]
    host_code = new_host_code()

    async with ROOM_LOCK:
        room = RoomState(
            room_id=room_id,
            host_code=host_code,
            mode="interpreter",
            host_lang=host_lang,
            guest_lang=guest_lang,
            status="active",
        )
        room.peers["host"] = PeerState(role="host", lang=host_lang)
        room.peers["guest"] = PeerState(role="guest", lang=guest_lang)

        ROOMS[room_id] = room
        HOST_ACTIVE_ROOM[host_code] = room_id

    logger.info(
        "SHAKE_ROOM_CREATED room_id=%s host=%s guest=%s",
        room_id,
        host_user_id,
        guest_user_id,
    )
    return room_id


def get_existing_active_search_for_user(user_id: str) -> Optional[SearchState]:
    search_id = ACTIVE_SEARCH_ID_BY_USER.get(user_id)
    if not search_id:
        return None
    return SEARCHES_BY_ID.get(search_id)


def create_search(req: ShakeMatchRequest) -> SearchState:
    now = time.time()
    search = SearchState(
        search_id=new_search_id(),
        user_id=req.user_id.strip(),
        lat=req.lat,
        lon=req.lon,
        my_lang=normalize_lang(req.my_lang, "tr"),
        accuracy_m=req.accuracy_m,
        client_ts=req.client_ts,
        created_at=now,
        updated_at=now,
        expires_at=now + SEARCH_TTL_SECONDS,
    )
    SEARCHES_BY_ID[search.search_id] = search
    ACTIVE_SEARCH_ID_BY_USER[search.user_id] = search.search_id
    return search


def refresh_search(search: SearchState, req: ShakeMatchRequest) -> SearchState:
    now = time.time()
    search.lat = req.lat
    search.lon = req.lon
    search.my_lang = normalize_lang(req.my_lang, "tr")
    search.accuracy_m = req.accuracy_m
    search.client_ts = req.client_ts
    search.updated_at = now
    search.expires_at = now + SEARCH_TTL_SECONDS
    return search


def find_best_peer(req: ShakeMatchRequest) -> Optional[tuple[SearchState, float]]:
    closest_search: Optional[SearchState] = None
    closest_distance: Optional[float] = None
    now = time.time()

    for search in SEARCHES_BY_ID.values():
        if search.user_id == req.user_id:
            continue
        if search.status != "searching":
            continue
        if search.expires_at <= now:
            continue

        distance = get_distance(req.lat, req.lon, search.lat, search.lon)
        if distance > MATCH_RADIUS_METERS:
            continue

        if closest_search is None or closest_distance is None or distance < closest_distance:
            closest_search = search
            closest_distance = distance

    if closest_search is None or closest_distance is None:
        return None

    return closest_search, closest_distance


# =========================================================
# ENDPOINTLER
# =========================================================

@router.post("/italky/shake-match")
async def shake_match(req: ShakeMatchRequest):
    """
    Akış:
    1) eski kayıtları temizle
    2) aynı user çok hızlı üst üste vuruyorsa mevcut aramayı döndür
    3) uygun peer varsa aynı room_id ile iki tarafı matched yap
    4) yoksa searching dön
    """
    if not req.user_id.strip():
        raise HTTPException(status_code=422, detail="user_id is required")

    async with MATCH_LOCK:
        prune_expired_states()

        user_id = req.user_id.strip()
        now_submit_ms = now_ms()
        last_submit_ms = LAST_SUBMIT_MS_BY_USER.get(user_id, 0)
        LAST_SUBMIT_MS_BY_USER[user_id] = now_submit_ms

        existing = get_existing_active_search_for_user(user_id)

        if (
            existing
            and existing.status == "searching"
            and (now_submit_ms - last_submit_ms) < DUPLICATE_SUBMIT_COOLDOWN_MS
        ):
            logger.info("SHAKE_DUPLICATE_RETURN user_id=%s search_id=%s", user_id, existing.search_id)
            return search_to_searching_payload(existing)

        if existing and existing.status == "matched":
            return search_to_matched_payload(existing)

        peer_result = find_best_peer(req)
        if peer_result:
            peer_search, distance = peer_result

            current_search = existing if existing and existing.status == "searching" else create_search(req)
            if existing and existing.status == "searching":
                refresh_search(current_search, req)

            room_id = await ensure_interpreter_room_for_match(
                host_user_id=peer_search.user_id,
                host_lang=normalize_lang(peer_search.my_lang, "tr"),
                guest_user_id=current_search.user_id,
                guest_lang=normalize_lang(current_search.my_lang, "en"),
            )
            match_id = new_match_id()
            matched_at = time.time()

            current_search.status = "matched"
            current_search.room_id = room_id
            current_search.peer_id = peer_search.user_id
            current_search.match_id = match_id
            current_search.matched_at = matched_at
            current_search.expires_at = matched_at + MATCH_TTL_SECONDS
            current_search.updated_at = matched_at

            peer_search.status = "matched"
            peer_search.room_id = room_id
            peer_search.peer_id = current_search.user_id
            peer_search.match_id = match_id
            peer_search.matched_at = matched_at
            peer_search.expires_at = matched_at + MATCH_TTL_SECONDS
            peer_search.updated_at = matched_at

            ACTIVE_SEARCH_ID_BY_USER.pop(current_search.user_id, None)
            ACTIVE_SEARCH_ID_BY_USER.pop(peer_search.user_id, None)

            logger.info(
                "MATCHED user_a=%s user_b=%s room_id=%s distance=%.2f",
                current_search.user_id,
                peer_search.user_id,
                room_id,
                distance,
            )
            return {
                **search_to_matched_payload(current_search),
                "distance": round(distance, 2),
            }

        search = existing if existing and existing.status == "searching" else create_search(req)
        if existing and existing.status == "searching":
            refresh_search(search, req)

        logger.info("SEARCHING user_id=%s search_id=%s", search.user_id, search.search_id)
        return search_to_searching_payload(search)


@router.get("/italky/shake-status/{search_id}")
async def shake_status(search_id: str, user_id: Optional[str] = Query(default=None)):
    """
    İlk sallayan kullanıcı sonradan match oldu mu diye buradan poll eder.
    """
    async with MATCH_LOCK:
        prune_expired_states()

        search = SEARCHES_BY_ID.get(search_id)
        if not search:
            return {
                "ok": False,
                "status": "not_found",
                "message": "search_id bulunamadı veya süresi doldu",
            }

        if user_id and search.user_id != user_id:
            return {
                "ok": False,
                "status": "not_found",
                "message": "search_id bu kullanıcıya ait değil",
            }

        if search.status == "matched":
            return search_to_matched_payload(search)

        return search_to_searching_payload(search)


@router.get("/italky/create-guest-link", response_model=GuestLinkResponse)
async def create_guest_link(
    user_id: str = Query(..., min_length=1),
    room_id: Optional[str] = Query(default=None),
    my_lang: str = Query(default="tr"),
):
    """
    Uygulaması olmayan arkadaş için interpreter room linki üretir.
    room_id verilirse mevcut odayı kullanır.
    room_id verilmezse yeni interpreter room açar.
    """
    user_id = user_id.strip()
    my_lang = normalize_lang(my_lang, "tr")

    if not room_id:
        room_id = await ensure_interpreter_room_for_match(
            host_user_id=user_id,
            host_lang=my_lang,
            guest_user_id="guest",
            guest_lang="en",
        )

    token = new_guest_token()
    join_url = f"{FRONTEND_BASE_URL}/open/interpreter?room={room_id}&guest=1&t={token}"

    return GuestLinkResponse(
        ok=True,
        room_id=room_id,
        join_url=join_url,
        instructions="Bu linki arkadaşına gönder, tarayıcıdan anında bağlansın.",
    )


@router.get("/italky/proximity-health")
async def proximity_health():
    prune_expired_states()
    return {
        "ok": True,
        "active_search_count": len(SEARCHES_BY_ID),
        "active_waiting_count": len([s for s in SEARCHES_BY_ID.values() if s.status == "searching"]),
        "matched_count": len([s for s in SEARCHES_BY_ID.values() if s.status == "matched"]),
        "radius_meters": MATCH_RADIUS_METERS,
        "window_seconds": MATCH_WINDOW_SECONDS,
    }
