from __future__ import annotations

import os
import logging
from typing import Optional, Any
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, Header, HTTPException, Query
from supabase import create_client

logger = logging.getLogger("italky-voice-enroll")
router = APIRouter(tags=["voice-enroll"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "cartesia").strip().lower()
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()

VOICE_BUCKET = os.getenv("VOICE_BUCKET", "voice-samples").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)


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
    res = supabase.auth.get_user(access_token)
    user = getattr(res, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return str(user.id)


def _get_profile(user_id: str) -> dict:
    res = (
        supabase.table("profiles")
        .select(
            "id,voice_profile_lang,plan,"
            "voice_sample_path,tts_voice_id,tts_voice_ready,"
            "second_voice_name,second_voice_sample_path,second_tts_voice_id,second_tts_voice_ready,"
            "memory_voice_name,memory_voice_sample_path,memory_tts_voice_id,memory_tts_voice_ready"
        )
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    profile = getattr(res, "data", None) or {}
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _get_voice_row(user_id: str, voice_type: str, voice_id: Optional[str] = None) -> dict:
    query = (
        supabase.table("voice_library")
        .select("*")
        .eq("user_id", user_id)
        .eq("voice_kind", voice_type)
        .is_("deleted_at", "null")
    )

    if voice_id:
        query = query.eq("id", voice_id).limit(1)
    else:
        query = query.order("created_at", desc=True).limit(1)

    res = query.execute()
    rows = getattr(res, "data", None) or []
    if not rows:
        raise HTTPException(status_code=400, detail="No voice samples found")
    return rows[0]


def _update_profile(user_id: str, payload: dict) -> None:
    res = (
        supabase.table("profiles")
        .update(payload)
        .eq("id", user_id)
        .execute()
    )
    _ = res


def _update_voice_library(voice_id: str, payload: dict) -> None:
    res = (
        supabase.table("voice_library")
        .update(payload)
        .eq("id", voice_id)
        .execute()
    )
    _ = res


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


def uuid_safe_tail(user_id: str) -> str:
    cleaned = str(user_id or "").replace("-", "")
    return cleaned[-8:] if cleaned else "voice"


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
        "name": f"italky-{user_id[:8]}-{uuid_safe_tail(user_id)}",
        "description": f"italky {user_id[:8]} custom voice",
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

    if r.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"Cartesia clone failed: {r.text}")

    try:
        j = r.json()
    except Exception:
        raise HTTPException(status_code=500, detail="Cartesia clone response is not valid JSON")

    voice_id = j.get("id") or j.get("voice_id")
    if not voice_id:
        raise HTTPException(status_code=500, detail="Cartesia voice_id missing")

    logger.warning("CARTESIA_CLONE_OK user=%s voice_id=%s", user_id, voice_id)

    return {
        "provider": "cartesia",
        "voice_id": voice_id,
    }


def _success_profile_payload(voice_type: str, row: dict, result: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()

    if voice_type == "mine":
        return {
            "voice_sample_path": row.get("sample_path"),
            "tts_voice_provider": result["provider"],
            "tts_voice_id": result["voice_id"],
            "tts_voice_ready": True,
            "tts_voice_last_error": None,
            "tts_voice_updated_at": now,
        }

    if voice_type == "second":
        return {
            "second_voice_name": row.get("voice_name"),
            "second_voice_sample_path": row.get("sample_path"),
            "second_tts_voice_id": result["voice_id"],
            "second_tts_voice_ready": True,
        }

    if voice_type == "memory":
        return {
            "memory_voice_name": row.get("voice_name"),
            "memory_voice_sample_path": row.get("sample_path"),
            "memory_tts_voice_id": result["voice_id"],
            "memory_tts_voice_ready": True,
        }

    raise HTTPException(status_code=400, detail="invalid_voice_type")


def _failure_profile_payload(voice_type: str, detail: str) -> dict:
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


def _enroll_voice_by_type(
    voice_type: str,
    authorization: Optional[str],
    voice_id: Optional[str] = None,
) -> dict:
    access_token = _get_bearer(authorization)
    user_id = _get_user_id_from_token(access_token)
    profile = _get_profile(user_id)

    if str(profile.get("plan") or "free").lower() == "free":
        raise HTTPException(status_code=403, detail="Custom voice is premium only")

    row = _get_voice_row(user_id=user_id, voice_type=voice_type, voice_id=voice_id)
    sample_path = str(row.get("sample_path") or "").strip()
    if not sample_path:
        raise HTTPException(status_code=400, detail="No voice sample path found")

    try:
        if VOICE_PROVIDER != "cartesia":
            raise HTTPException(status_code=500, detail="VOICE_PROVIDER must be cartesia")

        sample_url = _signed_url_for_storage_path(sample_path)
        result = _cartesia_clone(
            user_id=user_id,
            sample_url=sample_url,
            lang=str(profile.get("voice_profile_lang") or "en"),
        )

        _update_voice_library(
            row["id"],
            {
                "tts_voice_id": result["voice_id"],
                "tts_voice_ready": True,
                "tts_voice_last_error": None,
            }
        )

        _update_profile(
            user_id,
            _success_profile_payload(voice_type, row, result)
        )

    except HTTPException as e:
        try:
            _update_voice_library(
                row["id"],
                {
                    "tts_voice_ready": False,
                    "tts_voice_last_error": str(e.detail),
                }
            )
        except Exception:
            pass

        try:
            _update_profile(user_id, _failure_profile_payload(voice_type, str(e.detail)))
        except Exception:
            pass
        raise

    except Exception as e:
        logger.exception("VOICE_ENROLL_FAIL %s", e)

        try:
            _update_voice_library(
                row["id"],
                {
                    "tts_voice_ready": False,
                    "tts_voice_last_error": str(e),
                }
            )
        except Exception:
            pass

        try:
            _update_profile(user_id, _failure_profile_payload(voice_type, str(e)))
        except Exception:
            pass

        raise HTTPException(status_code=500, detail=f"Voice enroll failed: {e}")

    return {
        "ok": True,
        "provider": result["provider"],
        "voice_id": result["voice_id"],
        "voice_type": voice_type,
        "voice_library_id": row["id"],
        "voice_name": row.get("voice_name"),
    }


@router.post("/voice/enroll")
def enroll_voice(
    authorization: Optional[str] = Header(default=None),
    voice_id: Optional[str] = Query(default=None),
):
    return _enroll_voice_by_type("mine", authorization, voice_id)


@router.post("/voice/enroll/mine")
def enroll_mine_voice(
    authorization: Optional[str] = Header(default=None),
    voice_id: Optional[str] = Query(default=None),
):
    return _enroll_voice_by_type("mine", authorization, voice_id)


@router.post("/voice/enroll/second")
def enroll_second_voice(
    authorization: Optional[str] = Header(default=None),
    voice_id: Optional[str] = Query(default=None),
):
    return _enroll_voice_by_type("second", authorization, voice_id)


@router.post("/voice/enroll/memory")
def enroll_memory_voice(
    authorization: Optional[str] = Header(default=None),
    voice_id: Optional[str] = Query(default=None),
):
    return _enroll_voice_by_type("memory", authorization, voice_id)
