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
    name: str
    email: EmailStr
    phone: str | None = None
    uid: str | None = None
    login_type: str | None = "google"   # google | nfc

@router.post("/register")
def register(body: RegisterBody):
    name = (body.name or "").strip()
    email = (body.email or "").strip().lower()
    phone = (body.phone or "").strip() if body.phone else None
    uid = (body.uid or "").strip() if body.uid else None
    login_type = (body.login_type or "google").strip().lower()

    if not name or not email:
        return {"ok": False, "error": "missing_fields"}

    try:
        # Önce aynı email var mı bak
        existing = supabase.table("users") \
            .select("*") \
            .eq("email", email) \
            .limit(1) \
            .execute()

        if existing.data and len(existing.data) > 0:
            user = existing.data[0]

            # Eksik alan varsa güncelle
            update_data = {}

            if not user.get("name") and name:
                update_data["name"] = name

            if phone and not user.get("phone"):
                update_data["phone"] = phone

            if uid and not user.get("uid"):
                update_data["uid"] = uid

            if login_type:
                update_data["login_type"] = login_type

            if update_data:
                supabase.table("users") \
                    .update(update_data) \
                    .eq("id", user["id"]) \
                    .execute()

            return {
                "ok": True,
                "user_id": user["id"],
                "new_user": False
            }

        # Kullanıcı yoksa yeni oluştur
        insert_data = {
            "name": name,
            "email": email,
            "phone": phone,
            "login_type": login_type
        }

        if uid:
            insert_data["uid"] = uid

        res = supabase.table("users").insert(insert_data).execute()

        if not res.data or len(res.data) == 0:
            return {"ok": False, "error": "insert_failed"}

        user = res.data[0]

        return {
            "ok": True,
            "user_id": user["id"],
            "new_user": True
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}
