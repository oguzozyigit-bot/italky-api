from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client
import os
import json
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

router = APIRouter()

# ===============================
# CONFIG & SUPABASE
# ===============================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 🔥 KRİTİK: Firebase Console > Proje Ayarları'ndaki Project ID ile aynı olmalı
PROJECT_ID = "italkyai-new"

class TokenBody(BaseModel):
    user_id: str  # Android'den gelen Supabase UID (Artık TEXT formatında)
    token: str    # FCM Token

# ===============================
# TOKEN SAVE (UID UYUMLU)
# ===============================
@router.post("/save-token")
def save_token(body: TokenBody):
    try:
        user_id = body.user_id
        token = body.token

        if not token or not user_id:
            return {"ok": False, "error": "UID or Token is empty"}

        # Veritabanında ilgili kullanıcıyı bul ve token'ını güncelle
        supabase.table("profiles").update({
            "fcm_token": token
        }).eq("id", user_id).execute()

        print(f"TOKEN SAVED: User {user_id} -> {token[:10]}...")
        return {"ok": True}

    except Exception as e:
        print("SAVE TOKEN ERROR:", e)
        return {"ok": False, "error": str(e)}

# ===============================
# ACCESS TOKEN (GOOGLE v1)
# ===============================
def get_access_token():
    try:
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if not creds_json:
            raise Exception("GOOGLE_APPLICATION_CREDENTIALS_JSON is empty")

        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        
        # Token'ı yenile ve al
        creds.refresh(Request())
        return creds.token

    except Exception as e:
        print("ACCESS TOKEN ERROR:", e)
        return None

# ===============================
# PUSH SEND (YAMALI & GARANTİ SÜRÜM)
# ===============================
def send_push_v1(token: str, data: dict):
    if not token:
        print("PUSH SKIPPED: token empty")
        return

    access_token = get_access_token()
    if not access_token:
        print("PUSH ERROR: no access token")
        return

    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"

    # 🔥 YAMALANMIŞ BODY YAPISI
    body = {
        "message": {
            "token": token,
            "notification": {
                "title": "italkyAI",
                "body": "Yakınında bir italky oturumu başladı! 👋"
            },
            "data": data, # room_id, role vb. buraya basıyoruz
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "default", # 🔥 Android kanalı için kritik
                    "sound": "default",
                    "click_action": "OPEN_MAIN_ACTIVITY"
                }
            }
        }
    }

    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            json=body
        )
        # Loglarda tam sonucu görelim:
        print(f"FCM RESULT: {response.status_code} | {response.text}")
    except Exception as e:
        print("PUSH HTTP ERROR:", e)
