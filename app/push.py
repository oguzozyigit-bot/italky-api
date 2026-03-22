from google.oauth2 import service_account
from google.auth.transport.requests import Request
import requests
import os
import json

PROJECT_ID = "italkyai-new"  # firebase project id

def get_access_token():
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)

    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/firebase.messaging"]
    )

    creds.refresh(Request())
    return creds.token


def send_push_v1(token, data):

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
