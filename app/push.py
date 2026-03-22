import requests
import os

FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")


def send_push_v1(token: str, data: dict):

    if not token:
        return

    try:
        requests.post(
            "https://fcm.googleapis.com/fcm/send",
            headers={
                "Authorization": f"key={FCM_SERVER_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "to": token,
                "priority": "high",
                "data": data,
                "notification": {
                    "title": "italkyAI",
                    "body": "Bağlantı isteği geldi"
                }
            }
        )

    except Exception as e:
        print("PUSH ERROR:", e)
