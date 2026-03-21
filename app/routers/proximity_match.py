from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import time
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from supabase import create_client
from app.push import send_push_v1

logger = logging.getLogger("italky-proximity")
router = APIRouter(tags=["italky-proximity"])


# =========================================================
# SUPABASE
# =========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================================================
# AYARLAR
# =========================================================

MATCH_RADIUS_METERS = float(os.getenv("SHAKE_MATCH_RADIUS_METERS", "20"))


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


def get_token(user_id: str) -> str:
    try:
        r = supabase.table("profiles") \
            .select("fcm_token") \
            .eq("id", user_id) \
            .single() \
            .execute()

        return r.data.get("fcm_token") or ""
    except Exception as e:
        logger.warning(f"TOKEN FETCH ERROR: {e}")
        return ""


# =========================================================
# MAIN ENDPOINT
# =========================================================

@router.post("/italky/shake-match")
async def shake_match(req: ShakeMatchRequest):

    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    async with LOCK:

        # 🔍 eşleşme ara
        for s in SEARCHES.values():

            if s.user_id == req.user_id:
                continue

            if s.status != "searching":
                continue

            distance = get_distance(req.lat, req.lon, s.lat, s.lon)

            if distance <= MATCH_RADIUS_METERS:

                room_id = new_id()

                logger.info(f"MATCH FOUND → room={room_id}")

                # 🔥 TOKEN AL
                token_a = get_token(req.user_id)
                token_b = get_token(s.user_id)

                # 🔥 PUSH GÖNDER (V1)
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

        # ❌ eşleşme yok → listeye ekle
        sid = new_id()

        SEARCHES[sid] = SearchState(
            search_id=sid,
            user_id=req.user_id,
            lat=req.lat,
            lon=req.lon,
            my_lang=req.my_lang
        )

        logger.info(f"SEARCHING → user={req.user_id}")

        return {
            "status": "searching",
            "search_id": sid
        }
