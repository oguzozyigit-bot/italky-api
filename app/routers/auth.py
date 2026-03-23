from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client
import os

router = APIRouter(prefix="/api/auth", tags=["auth"])

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

class RegisterBody(BaseModel):
    name: str
    email: str
    phone: str | None = None

@router.post("/register")
def register(body: RegisterBody):

    if not body.name or not body.email:
        return {"ok": False, "error": "missing_fields"}

    res = supabase.table("users").insert({
        "name": body.name,
        "email": body.email,
        "phone": body.phone
    }).execute()

    if not res.data:
        return {"ok": False}

    return {
        "ok": True,
        "user_id": res.data[0]["id"]
    }
