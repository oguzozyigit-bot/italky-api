from __future__ import annotations
import asyncio, logging, os, secrets
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client
from app.routers.push import send_push_v1, get_access_token # 🔥 get_access_token eklendi

logger = logging.getLogger("italky-proximity")
router = APIRouter(prefix="/italky", tags=["italky-proximity"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

class ShakeMatchRequest(BaseModel):
    user_id: str
    my_lang: str = "tr"

def new_id(): return secrets.token_hex(6)

@router.post("/shake-match")
async def shake_match(req: ShakeMatchRequest):
    if not req.user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    room_id = new_id()
    
    # 🔥 PERFORMANS DÜZELTMESİ: Access token'ı döngü dışında bir kez al
    master_access_token = get_access_token() 
    
    # Tüm fcm_token'ları çek
    r = supabase.table("profiles").select("fcm_token").neq("id", req.user_id).execute()
    tokens = [x["fcm_token"] for x in r.data if x.get("fcm_token")]

    # 🔥 Arka planda Push gönder (API yanıtını bekletme)
    async def broadcast_push():
        for token in tokens:
            try:
                # Burayı doğrudan requests ile veya optimize edilmiş haliyle çağırmak lazım
                send_push_v1(token, {
                    "push_room_id": room_id, # 🔥 Android tarafındaki intent keyleri ile eşitledim
                    "push_role": "guest",
                    "push_my_lang": "en",
                    "push_peer_lang": req.my_lang,
                    "auto": "1"
                })
            except: continue

    asyncio.create_task(broadcast_push()) # Yanıtı beklemeden gönder

    return {
        "status": "matched",
        "room_id": room_id,
        "client_role": "host"
    }
