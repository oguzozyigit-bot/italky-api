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
# CONFIG
# ===============================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PROJECT_ID = "italkyai-new"


# ===============================
# MODEL
# ===============================
class TokenBody(BaseModel):
    user_id: str
    token: str


# ===============================
# TOKEN SAVE
# ===============================
@router.post("/save-token")
def save_token(body: TokenBody):
    try:
        user_id = body.user_id
        token = body.token

        if not token or not user_id:
            return {"ok": False, "error": "UID or Token empty"}

        supabase.table("profiles").update({
            "fcm_token": token
        }).eq("id", user_id).execute()

        print(f"✅ TOKEN SAVED: {user_id}")

        return {"ok": True}

    except Exception as e:
        print("❌ SAVE TOKEN ERROR:", e)
        return {"ok": False, "error": str(e)}


# ===============================
# TEST ENDPOINT (DEBUG)
# ===============================
@router.get("/test-save")
def test_save():
    try:
        supabase.table("profiles").update({
            "fcm_token": "TEST_TOKEN_123"
        }).neq("id", "").execute()

        print("✅ TEST TOKEN WRITTEN")

        return {"ok": True}

    except Exception as e:
        print("❌ TEST SAVE ERROR:", e)
        return {"ok": False, "error": str(e)}


# ===============================
# GOOGLE ACCESS TOKEN
# ===============================
def get_access_token():
    try:
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

        if not creds_json:
            print("❌ GOOGLE_APPLICATION_CREDENTIALS_JSON EMPTY")
            return None

        creds_dict = json.loads(creds_json)

        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )

        creds.refresh(Request())

        return creds.token

    except Exception as e:
        print("❌ ACCESS TOKEN ERROR:", e)
        return None


# ===============================
# PUSH SEND
# ===============================
def send_push_v1(token: str, data: dict):

    if not token:
        print("⚠️ PUSH SKIPPED: token empty")
        return

    access_token = get_access_token()

    if not access_token:
        print("❌ PUSH ERROR: no access token")
        return

    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"

    body = {
        "message": {
            "token": token,
            "notification": {
                "title": "italkyAI",
                "body": "Bağlantı isteği geldi 👋"
            },
            "data": data,
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "default",
                    "sound": "default"
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

        print("📨 PUSH RESULT:", response.status_code, response.text)

    except Exception as e:
        print("❌ PUSH HTTP ERROR:", e)
