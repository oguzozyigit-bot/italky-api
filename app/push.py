from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client
import os

router = APIRouter()

# ===============================
# SUPABASE
# ===============================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ===============================
# MODEL
# ===============================

class TokenBody(BaseModel):
    token: str


# ===============================
# TOKEN SAVE ENDPOINT
# ===============================

@router.post("/save-token")
def save_token(body: TokenBody):
    try:
        token = body.token

        if not token:
            return {"ok": False, "error": "empty token"}

        # 🔥 GEÇİCİ: tüm kullanıcıya yaz (test için)
        supabase.table("profiles").update({
            "fcm_token": token
        }).neq("id", "").execute()

        return {"ok": True}

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }
