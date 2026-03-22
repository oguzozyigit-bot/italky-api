from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client
import os
import json
import requests

from google.oauth2 import service_account
from google.auth.transport.requests import Request

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PROJECT_ID = "italkyai-new"


class TokenBody(BaseModel):
    token: str


# ===============================
# TOKEN SAVE
# ===============================

@router.post("/save-token")
def save_token(body: TokenBody):
    try:
        token = body.token

        if not token:
            return {"ok": False, "error": "empty token"}

        supabase.table("profiles").update({
            "fcm_token": token
        }).neq("id", "").execute()

        print("TOKEN SAVED:", token)

        return {"ok": True}

    except Exception as e:
        print("SAVE TOKEN ERROR:", e)
        return {"ok": False, "error": str(e)}


# ===============================
# ACCESS TOKEN
# ===============================

def get_access_token():
    try:
        creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

        if not creds_json:
            raise Exception("GOOGLE_APPLICATION_CREDENTIALS_JSON empty")

        creds_dict = json.loads(creds_json)

        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )

        creds.refresh(Request())

        print("ACCESS TOKEN OK")

        return creds.token

    except Exception as e:
        print("ACCESS TOKEN ERROR:", e)
        return None


# ===============================
# PUSH SEND
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

    body = {
        "message": {
            "token": token,
            "data": data,
            "notification": {
                "title": "italkyAI",
                "body": "Bağlantı isteği geldi"
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

        print("PUSH STATUS:", response.status_code)
        print("PUSH RESPONSE:", response.text)

    except Exception as e:
        print("PUSH ERROR:", e)
