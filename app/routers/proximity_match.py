from __future__ import annotations
import math
import uuid
import logging
from datetime import datetime, timedelta
from typing import List, Dict
from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("italky-proximity")
router = APIRouter(tags=["italky-proximity"])

# Hafızada bekleyen aktif "sallayanlar" listesi
active_shakers: List[Dict] = []

class ShakeMatchRequest(BaseModel):
    user_id: str
    lat: float
    lon: float

def get_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki koordinat arasındaki mesafeyi metre cinsinden hesaplar (Haversine Formülü)"""
    R = 6371000  # Dünya yarıçapı (metre)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

@router.post("/italky/shake-match")
async def shake_match(req: ShakeMatchRequest):
    now = datetime.now()
    global active_shakers
    
    # 1. Temizlik: 5 saniyeden eski kayıtları listeden çıkar
    active_shakers = [u for u in active_shakers if u['time'] > now - timedelta(seconds=5)]
    
    # 2. Yakınlık ve Eşleşme Kontrolü
    for user in active_shakers:
        # Kendisi değilse ve mesafe 20 metreden azsa eşleştir
        distance = get_distance(req.lat, req.lon, user['lat'], user['lon'])
        
        if distance < 20 and user['user_id'] != req.user_id:
            matched_peer = user['user_id']
            active_shakers.remove(user) # Eşleşen kişiyi havuzdan çıkar
            logger.info(f"MATCHED: {req.user_id} with {matched_peer} (Dist: {distance}m)")
            return {
                "ok": True,
                "status": "matched",
                "peer_id": matched_peer,
                "distance": round(distance, 2)
            }
            
    # 3. Eğer eşleşme yoksa kendini listeye ekle ve bekleme moduna geç
    active_shakers.append({
        "user_id": req.user_id,
        "lat": req.lat,
        "lon": req.lon,
        "time": now
    })
    
    return {
        "ok": True,
        "status": "searching",
        "message": "Yakınlarda sallanan cihaz aranıyor..."
    }

@router.get("/italky/create-guest-link")
async def create_guest_link(user_id: str):
    """Uygulaması olmayan bir arkadaş için geçici oda linki üretir"""
    room_id = str(uuid.uuid4())[:8]
    # Bu URL senin Vercel üzerindeki frontend adresin olmalı
    join_url = f"https://italky.ai/join/{room_id}?host={user_id}"
    
    return {
        "ok": True,
        "room_id": room_id,
        "join_url": join_url,
        "instructions": "Bu linki arkadaşına gönder, tarayıcıdan anında bağlansın."
    }
