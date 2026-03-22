from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client
import os, json, requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PROJECT_ID = "italkyai-new"

class TokenBody(BaseModel):
    user_id: str  # 🔥 EKLENDİ: Kimin tokenı olduğunu bilmemiz lazım
    token: str

@router.post("/save-token")
def save_token(body: TokenBody):
    try:
        if not body.token or not body.user_id:
            return {"ok": False, "error": "missing field"}

        # 🔥 DÜZELTİLDİ: Sadece ilgili kullanıcıyı güncelle
        supabase.table("profiles").update({
            "fcm_token": body.token
        }).eq("id", body.user_id).execute()

        print(f"TOKEN SAVED for {body.user_id}")
        return {"ok": True}
    except Exception as e:
        print("SAVE TOKEN ERROR:", e)
        return {"ok": False, "error": str(e)}

def get_access_token():
    try:
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if not creds_json: return None
        
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        print("ACCESS TOKEN ERROR:", e)
        return None

def send_push_v1(token: str, data: dict):
    if not token: return
    access_token = get_access_token()
    if not access_token: return

    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"
    
    # 🔥 DATA PAKETİNE TİP EKLEDİK (Android'in tanıması için)
    body = {
        "message": {
            "token": token,
            "data": {
                **data,
                "type": "shake_event" # Android tarafında ayırt etmek için
            },
            "notification": {
                "title": "italkyAI",
                "body": "Yakınında biri italky başlattı!"
            },
            "android": {
                "priority": "high"
            }
        }
    }
    requests.post(url, headers={"Authorization": f"Bearer {access_token}"}, json=body)
