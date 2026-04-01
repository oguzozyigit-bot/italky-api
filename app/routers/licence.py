from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
from datetime import datetime, timezone, timedelta

router = APIRouter(prefix="/api/license", tags=["license"])

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL veya SUPABASE_SERVICE_ROLE_KEY eksik")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================================================
# MODELS
# =========================================================

class ActivateCodeBody(BaseModel):
    code: str


class StartTrialBody(BaseModel):
    days: int = 7
    nac_id: str | None = None


# =========================================================
# HELPERS
# =========================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_code(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper().strip() if ch.isalnum())[:8]


def get_user_from_token(auth_header: str | None):
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Yetkisiz erişim")

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Geçersiz token")

    try:
        user_res = supabase.auth.get_user(token)
        user = getattr(user_res, "user", None)
        if not user or not getattr(user, "id", None):
            raise HTTPException(status_code=401, detail="Kullanıcı alınamadı")
        return user
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Oturum doğrulanamadı: {e}")


def get_profile(user_id: str) -> dict:
    res = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    return res.data or {}


def upsert_profile(user_id: str, payload: dict):
    exists = (
        supabase.table("profiles")
        .select("id")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )

    if exists.data:
        return (
            supabase.table("profiles")
            .update(payload)
            .eq("id", user_id)
            .execute()
        )

    payload = {"id": user_id, **payload}
    return supabase.table("profiles").insert(payload).execute()


def get_license_row(code: str) -> dict | None:
    res = (
        supabase.table("nfc_cards")
        .select("*")
        .eq("uid", code)
        .maybe_single()
        .execute()
    )
    return res.data


def get_package(package_code: str) -> dict | None:
    if not package_code:
        return None

    res = (
        supabase.table("nfc_packages")
        .select("*")
        .eq("code", package_code)
        .maybe_single()
        .execute()
    )
    return res.data


def deactivate_old_entitlements(user_id: str):
    (
        supabase.table("nfc_entitlements")
        .update({"status": "passive"})
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    )


def create_entitlement_for_code(user_id: str, code_row: dict, package_row: dict) -> dict:
    duration_days = int(package_row.get("duration_days") or 0)
    started_at_dt = datetime.now(timezone.utc)
    expires_at_dt = started_at_dt + timedelta(days=duration_days if duration_days > 0 else 3650)

    payload = {
        "user_id": user_id,
        "package_code": package_row.get("code"),
        "source_type": "qr_code",
        "card_uid": code_row.get("uid"),
        "started_at": started_at_dt.isoformat(),
        "expires_at": expires_at_dt.isoformat(),
        "remaining_languages": int(package_row.get("language_limit") or 0),
        "remaining_jeton": int(package_row.get("jeton_amount") or 0),
        "can_use_text_to_text": bool(package_row.get("can_use_text_to_text") or False),
        "can_use_face_to_face": bool(package_row.get("can_use_face_to_face") or False),
        "can_use_side_to_side": bool(package_row.get("can_use_side_to_side") or False),
        "can_use_offline": bool(package_row.get("can_use_offline") or False),
        "can_use_clone_voice": bool(package_row.get("can_use_clone_voice") or False),
        "status": "active",
        "note": "Lisans kodu ile aktive edildi"
    }

    res = supabase.table("nfc_entitlements").insert(payload).execute()
    return (res.data or [{}])[0]


def activate_license_row(code_row: dict, user_id: str):
    prev_note = str(code_row.get("note") or "").strip()
    parts = [p.strip() for p in prev_note.split("|") if p.strip()]
    parts = [p for p in parts if not p.lower().startswith("activated_at:")]
    parts = [p for p in parts if not p.lower().startswith("activated_by:")]
    parts.append(f"activated_at:{now_iso()}")
    parts.append(f"activated_by:{user_id}")
    new_note = " | ".join(parts)

    return (
        supabase.table("nfc_cards")
        .update({
            "status": "bound",
            "bound_user_id": user_id,
            "note": new_note
        })
        .eq("uid", code_row.get("uid"))
        .execute()
    )


def has_used_trial_before(user_id: str, email: str, nac_id: str | None):
    try:
        r = (
            supabase.table("trial_audit")
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if r.data:
            return True
    except Exception:
        pass

    try:
        if email:
            r = (
                supabase.table("trial_audit")
                .select("id")
                .eq("email", email)
                .limit(1)
                .execute()
            )
            if r.data:
                return True
    except Exception:
        pass

    try:
        if nac_id:
            r = (
                supabase.table("trial_audit")
                .select("id")
                .eq("nac_id", nac_id)
                .limit(1)
                .execute()
            )
            if r.data:
                return True
    except Exception:
        pass

    return False


def write_trial_audit(user_id: str, email: str, nac_id: str | None, login_type: str | None):
    payload = {
        "user_id": user_id,
        "email": email or None,
        "nac_id": nac_id or None,
        "login_type": login_type or None,
        "trial_consumed": True,
        "first_trial_started_at": now_iso()
    }
    supabase.table("trial_audit").insert(payload).execute()


# =========================================================
# ROUTES
# =========================================================

@router.post("/activate-code")
def activate_code(
    body: ActivateCodeBody,
    authorization: str | None = Header(default=None)
):
    user = get_user_from_token(authorization)
    user_id = user.id
    code = clean_code(body.code)

    if len(code) != 8:
        raise HTTPException(status_code=400, detail="Kod 8 karakter olmalı")

    code_row = get_license_row(code)
    if not code_row:
        raise HTTPException(status_code=404, detail="Lisans kodu bulunamadı")

    if not bool(code_row.get("is_active", True)):
        raise HTTPException(status_code=400, detail="Bu lisans kodu pasif")

    current_status = str(code_row.get("status") or "").lower()
    bound_user_id = code_row.get("bound_user_id")

    if current_status == "bound" and bound_user_id and bound_user_id != user_id:
        raise HTTPException(status_code=409, detail="Bu lisans kodu başka kullanıcıya bağlı")

    package_code = code_row.get("package_code")
    package_row = get_package(package_code)
    if not package_row:
        raise HTTPException(status_code=404, detail="Koda bağlı paket bulunamadı")

    deactivate_old_entitlements(user_id)
    entitlement = create_entitlement_for_code(user_id, code_row, package_row)
    activate_license_row(code_row, user_id)

    profile_payload = {
        "selected_package_code": package_code,
        "package_active": True,
        "package_started_at": entitlement.get("started_at"),
        "package_ends_at": entitlement.get("expires_at"),
        "nfc_card_uid": code_row.get("uid"),
        "nfc_package_code": package_code,
        "nfc_expires_at": entitlement.get("expires_at")
    }
    upsert_profile(user_id, profile_payload)

    return {
        "ok": True,
        "message": "Lisans aktive edildi",
        "code": code,
        "package_code": package_code,
        "entitlement": entitlement
    }


@router.post("/start-trial")
def start_trial(
    body: StartTrialBody,
    authorization: str | None = Header(default=None)
):
    user = get_user_from_token(authorization)
    user_id = user.id

    profile = get_profile(user_id)
    email = str(profile.get("email") or getattr(user, "email", "") or "").strip().lower()
    login_type = str(profile.get("login_type") or "google").strip().lower()
    nac_id = str(body.nac_id or "").strip() or None

    if has_used_trial_before(user_id, email, nac_id):
        raise HTTPException(status_code=409, detail="Bu kullanıcı veya cihaz daha önce ücretsiz denemeyi kullanmış.")

    days = int(body.days or 7)
    if days < 1:
        days = 7

    trial_started_at = datetime.now(timezone.utc)
    trial_ends_at = trial_started_at + timedelta(days=days)

    upsert_profile(user_id, {
        "trial_started_at": trial_started_at.isoformat(),
        "trial_ends_at": trial_ends_at.isoformat(),
        "package_active": False,
        "selected_package_code": None
    })

    write_trial_audit(user_id, email, nac_id, login_type)

    return {
        "ok": True,
        "message": "Ücretsiz giriş başlatıldı",
        "trial_started_at": trial_started_at.isoformat(),
        "trial_ends_at": trial_ends_at.isoformat(),
        "days": days
    }
