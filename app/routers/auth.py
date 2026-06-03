from datetime import datetime, timedelta, timezone
import os

from fastapi import APIRouter
from pydantic import BaseModel
from supabase import create_client

router = APIRouter(prefix="/api/auth", tags=["auth"])

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY"),
)


class RegisterBody(BaseModel):
    id: str
    full_name: str
    email: str
    phone: str | None = None
    uid: str | None = None
    login_type: str | None = "google"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _one_day_trial_payload() -> dict:
    now = _now()
    trial_end = now + timedelta(days=1)
    return {
        "trial_started_at": _iso(now),
        "trial_ends_at": _iso(trial_end),
        "trial_used": True,
        "package_active": True,
        "package_started_at": _iso(now),
        "package_ends_at": _iso(trial_end),
        "membership_status": "active",
        "membership_source": "free_trial_1day",
        "membership_product_id": "free_trial_1day",
        "membership_started_at": _iso(now),
        "membership_ends_at": _iso(trial_end),
        "membership_last_checked_at": _iso(now),
        "plan": "trial",
        "app_access_mode": "trial",
    }


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
        existing = (
            supabase.table("profiles")
            .select("id, full_name, email, phone, uid, login_type, trial_used")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )

        if existing.data and len(existing.data) > 0:
            update_data = {
                "full_name": full_name,
                "email": email,
                "login_type": login_type,
            }

            if phone:
                update_data["phone"] = phone

            if uid:
                update_data["uid"] = uid

            supabase.table("profiles").update(update_data).eq("id", user_id).execute()

            return {
                "ok": True,
                "user_id": user_id,
                "new_user": False,
            }

        insert_data = {
            "id": user_id,
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "uid": uid,
            "login_type": login_type,
            **_one_day_trial_payload(),
        }

        supabase.table("profiles").insert(insert_data).execute()

        return {
            "ok": True,
            "user_id": user_id,
            "new_user": True,
            "trial_granted": True,
            "trial_days": 1,
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}
