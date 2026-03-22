from __future__ import annotations
import asyncio
import logging
import os
import secrets
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client

# Diğer dosyadan fonksiyonları içe aktarıyoruz
from app.routers.push import send_push_v1, get_access_token

logger = logging.getLogger("italky-proximity")

# Prefix ve Tag tanımlamaları
router = APIRouter(prefix="/italky", tags=["italky-proximity"])

# ===============================
# SUPABASE BAĞLANTISI
# ===============================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===============================
# MODELLER
# ===============================
class ShakeMatchRequest(BaseModel):
    user_id: str      # Sallayan kişinin UID'si
    my_lang: str = "tr"
    lat: float = 0.0  # Opsiyonel: Gelecekte mesafe filtresi için
    lon: float = 0.0

# ===============================
# YARDIMCI FONKSİYONLAR
# ===============================
def new_id():
    """6 haneli benzersiz oda ID'si üretir."""
    return secrets.token_hex(3).upper() 

# ===============================
# SHAKE MATCH (BROADCAST VERSION)
# ===============================
@router.post("/shake-match")
async def shake_match(req: ShakeMatchRequest):
    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    # 1. Yeni bir Oda oluştur
    room_id = f"SHAKE_{new_id()}"
    logger.info(f"SHAKE MATCH STARTED: User {req.user_id} -> Room {room_id}")

    # 2. Kendisi hariç, token'ı olan tüm kullanıcıları çek
    try:
        r = supabase.table("profiles") \
            .select("fcm_token") \
            .neq("id", req.user_id) \
            .execute()
        
        tokens = [x["fcm_token"] for x in r.data if x.get("fcm_token")]
    except Exception as e:
        logger.error(f"DATABASE FETCH ERROR: {e}")
        tokens = []

    if not tokens:
        return {"status": "no_peers", "room_id": room_id, "client_role": "host"}

    # 3. Arka Planda Push Gönderimi (API'yi bekletmeden yanıt döner)
    async def broadcast_logic():
        # Google Access Token'ı döngü başında bir kez al (Performans için)
        access_token = get_access_token()
        if not access_token:
            logger.error("BROADCAST FAILED: No access token")
            return

        for token in tokens:
            try:
                # Android tarafındaki intent keyleri ile tam uyumlu veri paketi
                push_data = {
                    "push_room_id": room_id,
                    "push_role": "guest",
                    "push_my_lang": "en",       # Karşı tarafın varsayılan dili
                    "push_peer_lang": req.my_lang, # Sallayanın dili
                    "auto": "1"
                }
                send_push_v1(token, push_data)
            except Exception as e:
                logger.warning(f"SINGLE PUSH FAILED: {e}")

    # Push işlemini ana akışı bozmadan başlat
    asyncio.create_task(broadcast_logic())

    return {
        "status": "matched",
        "room_id": room_id,
        "client_role": "host"
    }

# ===============================
# QR / LİNK OLUŞTURUCU (YEDEK)
# ===============================
@router.get("/create-guest-link")
async def create_guest_link(user_id: str):
    room_id = new_id()
    return {
        "ok": True,
        "room_id": room_id,
        "join_url": f"https://italky.ai/open/interpreter?room={room_id}&guest=1"
    }
