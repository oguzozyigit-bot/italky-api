import json
import os
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

PROJECT_ID = "italkyai"  # 🔥 Firebase project id

SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

def get_access_token():
    info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))

    creds = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES
    )

    creds.refresh(Request())
    return creds.token


def send_push_v1(token, data):
    access_token = get_access_token()

    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": "italkyAI",
                "body": "Bağlantı isteği geldi"
            },
            "data": data
        }
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    requests.post(url, headers=headers, json=payload)
