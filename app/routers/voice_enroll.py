from __future__ import annotations

import json
import os
import logging
from typing import Optional
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, Header, HTTPException

logger = logging.getLogger("italky-voice-enroll")
router = APIRouter(tags=["voice-enroll"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "mock").strip().lower()
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()

# italkyai_voice.py ile aynı bucket olmalı
VOICE_BUCKET = os.getenv("VOICE_BUCKET", "voice-samples").strip()


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
        f"{SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}"
        f"&select=id,voice_sample_path,second_voice_sample_path,memory_voice_sample_path,voice_profile_lang,plan",
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


def _update_profile(user_id: str, payload: dict) -> None:
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


def _signed_url_for_storage_path(path: str, expires_in: int = 3600) -> str:
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{VOICE_BUCKET}/{path}",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
            "Content-Type": "application/json",
        },
        json={"expiresIn": expires_in},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Signed URL failed: {r.text}")
    data = r.json()
    signed_path = data.get("signedURL") or data.get("signedUrl")
    if not signed_path:
        raise HTTPException(status_code=500, detail="Signed URL missing")
    return f"{SUPABASE_URL}/storage/v1{signed_path}"


def _cartesia_clone(user_id: str, sample_url: str, lang: str) -> dict:
    if not CARTESIA_API_KEY:
        raise HTTPException(status_code=500, detail="CARTESIA_API_KEY missing")

    audio_resp = requests.get(sample_url, timeout=30)
    if audio_resp.status_code != 200 or not audio_resp.content:
        raise HTTPException(status_code=500, detail="Could not fetch sample audio")

    content_type = audio_resp.headers.get("content-type", "").lower()
    ext = "mp3" if "mpeg" in content_type or "mp3" in content_type else "webm"
    mime = "audio/mpeg" if ext == "mp3" else "audio/webm"

    files = {
        "clip": (f"sample.{ext}", audio_resp.content, mime)
    }
    data = {
        "name": f"italky-{user_id[:8]}",
        "description": "italky interpreter custom voice",
        "language": (lang or "en").split("-")[0].lower(),
    }

    r = requests.post(
        "https://api.cartesia.ai/voices/clone",
        headers={
            "Authorization": f"Bearer {CARTESIA_API_KEY}",
            "Cartesia-Version": CARTESIA_VERSION,
        },
        files=files,
        data=data,
        timeout=60,
    )
    voice_id = j.get("id") or j.get("voice_id")
if not voice_id:
    raise HTTPException(status_code=500, detail="Cartesia voice_id missing")

logger.warning("CARTESIA_CLONE_OK user=%s voice_id=%s", user_id, voice_id)

return {
    "provider": "cartesia",
    "voice_id": voice_id,
}

def _mock_enroll(user_id: str, paths: list[str]) -> dict:
    return {
        "provider": "mock",
        "voice_id": f"mock-{user_id[:8]}-{len(paths)}",
    }


def _resolve_paths(profile: dict, voice_type: str) -> list[str]:
    if voice_type == "mine":
        return _parse_paths(profile.get("voice_sample_path"))
    if voice_type == "second":
        return _parse_paths(profile.get("second_voice_sample_path"))
    if voice_type == "memory":
        return _parse_paths(profile.get("memory_voice_sample_path"))
    raise HTTPException(status_code=400, detail="invalid_voice_type")


def _success_payload_for_type(result: dict, voice_type: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    if voice_type == "mine":
        return {
            "tts_voice_provider": result["provider"],
            "tts_voice_id": result["voice_id"],
            "tts_voice_ready": True,
            "tts_voice_last_error": None,
            "tts_voice_updated_at": now,
        }

    if voice_type == "second":
        return {
            "second_tts_voice_id": result["voice_id"],
            "second_tts_voice_ready": True,
        }

    if voice_type == "memory":
        return {
            "memory_tts_voice_id": result["voice_id"],
            "memory_tts_voice_ready": True,
        }

    raise HTTPException(status_code=400, detail="invalid_voice_type")


def _failure_payload_for_type(detail: str, voice_type: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    if voice_type == "mine":
        return {
            "tts_voice_ready": False,
            "tts_voice_last_error": detail,
            "tts_voice_updated_at": now,
        }

    if voice_type == "second":
        return {
            "second_tts_voice_ready": False,
        }

    if voice_type == "memory":
        return {
            "memory_tts_voice_ready": False,
        }

    raise HTTPException(status_code=400, detail="invalid_voice_type")


def _enroll_voice_by_type(voice_type: str, authorization: Optional[str]) -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise HTTPException(status_code=500, detail="Supabase env missing")

    access_token = _get_bearer(authorization)
    user_id = _get_user_id_from_token(access_token)
    profile = _get_profile(user_id)

    if str(profile.get("plan") or "free").lower() == "free":
        raise HTTPException(status_code=403, detail="Custom voice is premium only")

    paths = _resolve_paths(profile, voice_type)
    if len(paths) < 1:
        raise HTTPException(status_code=400, detail="No voice samples found")

    try:
        if VOICE_PROVIDER == "cartesia":
            sample_url = _signed_url_for_storage_path(paths[0])
            result = _cartesia_clone(
                user_id=user_id,
                sample_url=sample_url,
                lang=str(profile.get("voice_profile_lang") or "en"),
            )
        else:
            result = _mock_enroll(user_id, paths)

        _update_profile(user_id, _success_payload_for_type(result, voice_type))

    except HTTPException as e:
        try:
          _update_profile(user_id, _failure_payload_for_type(str(e.detail), voice_type))
        except Exception:
            pass
        raise

    except Exception as e:
        logger.exception("VOICE_ENROLL_FAIL %s", e)
        try:
            _update_profile(user_id, _failure_payload_for_type(str(e), voice_type))
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Voice enroll failed: {e}")

    return {
        "ok": True,
        "provider": result["provider"],
        "voice_id": result["voice_id"],
        "samples": len(paths),
        "voice_type": voice_type,
    }


@router.post("/voice/enroll")
def enroll_voice(authorization: Optional[str] = Header(default=None)):
    return _enroll_voice_by_type("mine", authorization)


@router.post("/voice/enroll/mine")
def enroll_mine_voice(authorization: Optional[str] = Header(default=None)):
    return _enroll_voice_by_type("mine", authorization)


@router.post("/voice/enroll/second")
def enroll_second_voice(authorization: Optional[str] = Header(default=None)):
    return _enroll_voice_by_type("second", authorization)


@router.post("/voice/enroll/memory")
def enroll_memory_voice(authorization: Optional[str] = Header(default=None)):
    return _enroll_voice_by_type("memory", authorization)
