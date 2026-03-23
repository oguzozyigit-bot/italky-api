from fastapi import APIRouter
from pydantic import BaseModel, EmailStr
from supabase import create_client
import os

router = APIRouter(prefix="/api/auth", tags=["auth"])

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)

class RegisterBody(BaseModel):
    id: str
    full_name: str
    email: EmailStr
    phone: str | None = None
    uid: str | None = None
    login_type: str | None = "google"

@router.post("/register")
def register(body: RegisterBody):
    user_id = (body.id or "").strip()
    full_name = (body.full_name or "").strip()
    email = str(body.email).strip().lower()
    phone = (body.phone or "").strip() if body.phone else None
    uid = (body.uid or "").strip() if body.uid else None
    login_type = (body.login_type or "google").strip().lower()

    if not user_id or not full_name or not email:
        return {"ok": False, "error": "missing_fields"}

    try:
        existing = supabase.table("profiles") \
            .select("id, full_name, email, phone, uid, login_type") \
            .eq("id", user_id) \
            .limit(1) \
            .execute()

        if existing.data and len(existing.data) > 0:
            update_data = {
                "full_name": full_name,
                "email": email,
                "login_type": login_type
            }

            if phone:
                update_data["phone"] = phone

            if uid:
                update_data["uid"] = uid

            supabase.table("profiles") \
                .update(update_data) \
                .eq("id", user_id) \
                .execute()

            return {
                "ok": True,
                "user_id": user_id,
                "new_user": False
            }

        insert_data = {
            "id": user_id,
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "uid": uid,
            "login_type": login_type
        }

        supabase.table("profiles").insert(insert_data).execute()

        return {
            "ok": True,
            "user_id": user_id,
            "new_user": True
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}
