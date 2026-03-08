from __future__ import annotations

import json
import os
import logging
from typing import Optional

import requests
from fastapi import APIRouter, Header, HTTPException

logger = logging.getLogger("italky-voice-enroll")
router = APIRouter(tags=["voice-enroll"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "mock").strip().lower()

def _get_bearer(auth_header: str | None) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    return token

def _get_user_id_from_token(access_token: str) -> str:
    r = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "apikey": SUPABASE_SERVICE_ROLE,
        },
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    data = r.json()
    user_id = data.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")
    return user_id

def _get_profile(user_id: str) -> dict:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=id,voice_sample_path,voice_profile_lang",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
        },
        timeout=20,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Profile fetch failed: {r.text}")

    arr = r.json()
    if not arr:
        raise HTTPException(status_code=404, detail="Profile not found")
    return arr[0]

def _parse_paths(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [str(x) for x in arr if x]
        except Exception:
            return []
    return [s]

def _update_profile(user_id: str, payload: dict):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json=payload,
        timeout=20,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"Profile update failed: {r.text}")

def _mock_enroll(user_id: str, paths: list[str]) -> dict:
    # Şimdilik gerçek provider yokken test için sahte voice_id üretir
    return {
        "provider": "mock",
        "voice_id": f"mock-{user_id[:8]}-{len(paths)}",
    }

@router.post("/voice/enroll")
def enroll_voice(authorization: Optional[str] = Header(default=None)):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise HTTPException(status_code=500, detail="Supabase env missing")

    access_token = _get_bearer(authorization)
    user_id = _get_user_id_from_token(access_token)
    profile = _get_profile(user_id)

    paths = _parse_paths(profile.get("voice_sample_path"))
    if len(paths) < 1:
        raise HTTPException(status_code=400, detail="No voice samples found")

    try:
        # Şimdilik mock. Sonra gerçek provider fonksiyonuyla değişecek.
        result = _mock_enroll(user_id, paths)

        _update_profile(user_id, {
            "tts_voice_provider": result["provider"],
            "tts_voice_id": result["voice_id"],
            "tts_voice_ready": True,
            "tts_voice_last_error": None,
            "tts_voice_updated_at": "now()"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("VOICE_ENROLL_FAIL %s", e)
        _update_profile(user_id, {
            "tts_voice_ready": False,
            "tts_voice_last_error": str(e),
        })
        raise HTTPException(status_code=500, detail=f"Voice enroll failed: {e}")

    return {
        "ok": True,
        "provider": result["provider"],
        "voice_id": result["voice_id"],
    }
