from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from supabase import create_client
import os
import secrets

router = APIRouter(prefix="/api/session", tags=["session"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ===============================
# MODEL
# ===============================
class LoginSessionBody(BaseModel):
    user_id: str


# ===============================
# LOGIN → SESSION OLUŞTUR
# ===============================
@router.post("/create")
def create_session(body: LoginSessionBody):
    user_id = body.user_id.strip()

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id gerekli")

    session_id = "sess_" + secrets.token_hex(12)

    supabase.table("profiles").update({
        "active_session_id": session_id
    }).eq("id", user_id).execute()

    return {
        "ok": True,
        "session_id": session_id
    }


# ===============================
# SESSION KONTROL
# ===============================
def check_session(user_id: str, session_id: str):
    res = supabase.table("profiles").select("active_session_id").eq("id", user_id).maybe_single().execute()
    profile = res.data or {}

    if not profile:
        raise HTTPException(status_code=404, detail="user_not_found")

    if profile.get("active_session_id") != session_id:
        raise HTTPException(status_code=403, detail="SESSION_EXPIRED")

    return True
