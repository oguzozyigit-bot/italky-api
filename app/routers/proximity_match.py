from __future__ import annotations
import logging
import os
import secrets
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client


logger = logging.getLogger("italky-proximity")

router = APIRouter(prefix="/italky", tags=["italky-proximity"])


# ===============================
# SUPABASE
# ===============================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ===============================
# MODEL
# ===============================
class ShakeMatchRequest(BaseModel):
    user_id: str
    my_lang: str = "tr"
    lat: float = 0.0
    lon: float = 0.0


# ===============================
# HELPER
# ===============================
def new_id():
    return secrets.token_hex(3).upper()


# ===============================
# SHAKE MATCH
# ===============================
@router.post("/shake-match")
async def shake_match(req: ShakeMatchRequest):

    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    room_id = f"SHAKE_{new_id()}"
    logger.info(f"SHAKE START → {req.user_id} → {room_id}")

    # 🔥 TOKENLARI ÇEK
    try:
        r = supabase.table("profiles") \
            .select("fcm_token") \
            .neq("id", req.user_id) \
            .execute()

        tokens = [x["fcm_token"] for x in r.data if x.get("fcm_token")]

    except Exception as e:
        logger.error(f"DB ERROR: {e}")
        tokens = []

    if not tokens:
        logger.warning("NO USERS TO PUSH")
        return {
            "status": "no_peers",
            "room_id": room_id,
            "client_role": "host"
        }

    logger.info(f"PUSH TO {len(tokens)} USERS")

    # 🔥 PUSH GÖNDER (ARTIK DIRECT)
    for token in tokens:
        try:
            send_push_v1(token, {
                "push_room_id": room_id,
                "push_role": "guest",
                "push_my_lang": "en",
                "push_peer_lang": req.my_lang,
                "auto": "1"
            })
        except Exception as e:
            logger.warning(f"PUSH FAIL: {e}")

    return {
        "status": "matched",
        "room_id": room_id,
        "client_role": "host"
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
