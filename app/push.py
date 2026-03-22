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

PROJECT_ID = "italkyai-new"  # 🔥 BURAYA PROJE ID YAZ


class TokenBody(BaseModel):
    token: str


@router.post("/save-token")
def save_token(body: TokenBody):

    supabase.table("profiles").update({
        "fcm_token": body.token
    }).neq("id", "").execute()

    return {"ok": True}


# ===============================
# 🔥 V1 PUSH
# ===============================

def get_access_token():
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)

    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"]
    )

    creds.refresh(Request())
    return creds.token


def send_push_v1(token: str, data: dict):

    if not token:
        return

    access_token = get_access_token()

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

    requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json=body
    )
