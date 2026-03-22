from __future__ import annotations

import asyncio
import logging
import os
import secrets
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
# MODELLER
# ===============================

class ShakeMatchRequest(BaseModel):
    user_id: str
    lat: float
    lon: float
    my_lang: str = "tr"


LOCK = asyncio.Lock()


# ===============================
# HELPER
# ===============================

def new_id():
    return secrets.token_hex(6)


def get_all_tokens():
    try:
        r = supabase.table("profiles").select("fcm_token").execute()
        return [x["fcm_token"] for x in r.data if x.get("fcm_token")]
    except Exception as e:
        logger.warning(f"TOKEN FETCH ERROR: {e}")
        return []


# ===============================
# SHAKE MATCH (FORCED VERSION)
# ===============================

@router.post("/shake-match")
async def shake_match(req: ShakeMatchRequest):

    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    async with LOCK:

        # 🔥 HER ZAMAN ROOM OLUŞTUR
        room_id = new_id()

        logger.info(f"FORCED MATCH → {room_id}")

        # 🔥 TÜM TOKENLARI AL
        tokens = get_all_tokens()

        # 🔥 HERKESE PUSH GÖNDER (TEST MOD)
        for token in tokens:
            try:
                send_push_v1(token, {
                    "room_id": room_id,
                    "role": "guest",
                    "my_lang": req.my_lang,
                    "peer_lang": "en",
                    "auto": "1"
                })
            except Exception as e:
                logger.warning(f"PUSH ERROR: {e}")

        return {
            "status": "matched",
            "room_id": room_id,
            "client_role": "host"
        }


# ===============================
# GUEST LINK (QR FALLBACK)
# ===============================

@router.get("/create-guest-link")
async def create_guest_link(user_id: str):

    room_id = new_id()

    return {
        "ok": True,
        "room_id": room_id,
        "join_url": f"https://italky.ai/open/interpreter?room={room_id}&guest=1"
    }
