from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client
from app.push import send_push_v1

logger = logging.getLogger("italky-proximity")

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

MATCH_RADIUS_METERS = float(os.getenv("SHAKE_MATCH_RADIUS_METERS", "200"))  # genişlettik
USER_TTL = 10  # saniye


# ===============================
# MODELLER
# ===============================

class ShakeMatchRequest(BaseModel):
    user_id: str
    lat: float
    lon: float
    my_lang: str = "tr"


# ===============================
# MEMORY
# ===============================

ACTIVE_USERS = {}
LOCK = asyncio.Lock()


# ===============================
# HELPER
# ===============================

def new_id():
    return secrets.token_hex(6)


def get_distance(lat1, lon1, lat2, lon2):
    r = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def get_token(user_id: str) -> str:
    try:
        r = supabase.table("profiles") \
            .select("fcm_token") \
            .eq("id", user_id) \
            .execute()

        if r.data and len(r.data) > 0:
            return r.data[0].get("fcm_token") or ""

        return ""
    except Exception as e:
        logger.warning(f"TOKEN ERROR: {e}")
        return ""


def clean_old_users():
    now = time.time()
    to_delete = []

    for uid, data in ACTIVE_USERS.items():
        if now - data["ts"] > USER_TTL:
            to_delete.append(uid)

    for uid in to_delete:
        del ACTIVE_USERS[uid]


# ===============================
# MAIN MATCH
# ===============================

@router.post("/shake-match")
async def shake_match(req: ShakeMatchRequest):

    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    async with LOCK:

        clean_old_users()

        # 🔥 kendini kaydet
        ACTIVE_USERS[req.user_id] = {
            "lat": req.lat,
            "lon": req.lon,
            "ts": time.time(),
            "lang": req.my_lang
        }

        # 🔍 en yakın kullanıcıyı bul
        closest_user = None
        closest_distance = 999999

        for uid, data in ACTIVE_USERS.items():

            if uid == req.user_id:
                continue

            distance = get_distance(req.lat, req.lon, data["lat"], data["lon"])

            if distance < closest_distance:
                closest_distance = distance
                closest_user = uid

        # 🔥 EŞLEŞME
        if closest_user and closest_distance <= MATCH_RADIUS_METERS:

            room_id = new_id()

            logger.info(f"MATCH → {req.user_id} <-> {closest_user} | {room_id}")

            # 🔥 PUSH (karşı tarafa)
            target_token = get_token(closest_user)

            try:
                send_push_v1(target_token, {
                    "room_id": room_id,
                    "role": "guest",
                    "my_lang": ACTIVE_USERS[closest_user]["lang"],
                    "peer_lang": req.my_lang,
                    "auto": "1"
                })
            except Exception as e:
                logger.warning(f"PUSH ERROR: {e}")

            return {
                "status": "matched",
                "room_id": room_id,
                "client_role": "host"
            }

        # 🔄 BEKLEME
        return {
            "status": "searching"
        }


# ===============================
# GUEST LINK
# ===============================

@router.get("/create-guest-link")
async def create_guest_link(user_id: str):

    room_id = new_id()

    return {
        "ok": True,
        "room_id": room_id,
        "join_url": f"https://italky.ai/open/interpreter?room={room_id}&guest=1"
    }
