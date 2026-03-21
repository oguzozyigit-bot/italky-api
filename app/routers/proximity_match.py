from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
from dataclasses import dataclass
from typing import Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from supabase import create_client
from app.push import send_push_v1

logger = logging.getLogger("italky-proximity")

# 🔥 PREFIX DOĞRU
router = APIRouter(prefix="/italky", tags=["italky-proximity"])


# ===============================
# SUPABASE
# ===============================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ===============================
# AYARLAR
# ===============================

MATCH_RADIUS_METERS = float(os.getenv("SHAKE_MATCH_RADIUS_METERS", "20"))


# ===============================
# MODELLER
# ===============================

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


SEARCHES: Dict[str, SearchState] = {}
LOCK = asyncio.Lock()


# ===============================
# HELPER
# ===============================

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


# 🔥 GEÇİCİ TOKEN FIX (UUID PROBLEMİNİ BYPASS)
def get_token(user_id: str) -> str:
    try:
        r = supabase.table("profiles") \
            .select("fcm_token") \
            .limit(1) \
            .execute()

        return r.data[0]["fcm_token"]
    except Exception as e:
        logger.warning(f"TOKEN ERROR: {e}")
        return ""


# ===============================
# SHAKE MATCH
# ===============================

@router.post("/shake-match")
async def shake_match(req: ShakeMatchRequest):

    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    async with LOCK:

        for s in SEARCHES.values():

            if s.user_id == req.user_id:
                continue

            if s.status != "searching":
                continue

            distance = get_distance(req.lat, req.lon, s.lat, s.lon)

            if distance <= MATCH_RADIUS_METERS:

                room_id = new_id()

                logger.info(f"MATCH → {room_id}")

                token_a = get_token(req.user_id)
                token_b = get_token(s.user_id)

                try:
                    send_push_v1(token_a, {
                        "room_id": room_id,
                        "role": "guest",
                        "my_lang": req.my_lang,
                        "peer_lang": s.my_lang
                    })

                    send_push_v1(token_b, {
                        "room_id": room_id,
                        "role": "host",
                        "my_lang": s.my_lang,
                        "peer_lang": req.my_lang
                    })

                except Exception as e:
                    logger.warning(f"PUSH ERROR: {e}")

                return {
                    "status": "matched",
                    "room_id": room_id,
                    "client_role": "guest"
                }

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


# ===============================
# GUEST LINK (QR FIX)
# ===============================

@router.get("/create-guest-link")
async def create_guest_link(user_id: str):

    room_id = new_id()

    return {
        "ok": True,
        "room_id": room_id,
        "join_url": f"https://italky.ai/open/interpreter?room={room_id}&guest=1"
    }
