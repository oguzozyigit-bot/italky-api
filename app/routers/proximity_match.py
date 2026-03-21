from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.routers.interpreter import (
    HOST_ACTIVE_ROOM,
    ROOMS,
    ROOM_LOCK,
    PeerState,
    RoomState,
)

# 🔥 SUPABASE
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 🔥 FCM
FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")

logger = logging.getLogger("italky-proximity")
router = APIRouter(tags=["italky-proximity"])


# =========================================================
# AYARLAR
# =========================================================

MATCH_RADIUS_METERS = float(os.getenv("SHAKE_MATCH_RADIUS_METERS", "20"))
MATCH_WINDOW_SECONDS = int(os.getenv("SHAKE_MATCH_WINDOW_SECONDS", "5"))

FRONTEND_BASE_URL = "https://italky.ai"


# =========================================================
# MODELLER
# =========================================================

class ShakeMatchRequest(BaseModel):
    user_id: str
    lat: float
    lon: float
    my_lang: str = "tr"


@dataclass
class SearchState:
    search_id: str
    user_id: str
    lat: float
    lon: float
    my_lang: str
    status: str = "searching"
    room_id: Optional[str] = None


SEARCHES: Dict[str, SearchState] = {}
LOCK = asyncio.Lock()


# =========================================================
# HELPER
# =========================================================

def get_distance(lat1, lon1, lat2, lon2):
    r = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def new_id():
    return secrets.token_hex(6)


def get_token(user_id):
    try:
        r = supabase.table("profiles").select("fcm_token").eq("id", user_id).single().execute()
        return r.data.get("fcm_token") or ""
    except:
        return ""


def send_push(token, room_id, role, my_lang, peer_lang):
    if not token:
        return

    requests.post(
        "https://fcm.googleapis.com/fcm/send",
        headers={
            "Authorization": f"key={FCM_SERVER_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "to": token,
            "priority": "high",
            "data": {
                "room_id": room_id,
                "role": role,
                "my_lang": my_lang,
                "peer_lang": peer_lang
            },
            "notification": {
                "title": "italkyAI",
                "body": "Bağlantı isteği geldi"
            }
        }
    )


# =========================================================
# MAIN ENDPOINT
# =========================================================

@router.post("/italky/shake-match")
async def shake_match(req: ShakeMatchRequest):

    async with LOCK:

        # 🔍 uygun peer bul
        for s in SEARCHES.values():
            if s.user_id == req.user_id:
                continue

            if s.status != "searching":
                continue

            dist = get_distance(req.lat, req.lon, s.lat, s.lon)

            if dist <= MATCH_RADIUS_METERS:

                room_id = new_id()

                # 🔥 PUSH GÖNDER
                send_push(get_token(req.user_id), room_id, "guest", req.my_lang, s.my_lang)
                send_push(get_token(s.user_id), room_id, "host", s.my_lang, req.my_lang)

                return {
                    "status": "matched",
                    "room_id": room_id,
                    "client_role": "guest"
                }

        # ❌ eşleşme yok → search ekle
        sid = new_id()
        SEARCHES[sid] = SearchState(
            search_id=sid,
            user_id=req.user_id,
            lat=req.lat,
            lon=req.lon,
            my_lang=req.my_lang
        )

        return {
            "status": "searching",
            "search_id": sid
        }
