from __future__ import annotations

import os
import logging
from typing import Optional
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

PREVIEW_TEXTS = {
    "mine": "Merhaba ben senin sesinin benzeriyim.",
    "second": "Merhaba. Artık dil çevirilerini ve sohbeti benim bu sesimle yapabilirsin.",
    "memory": "Ben senin hatıralarından gelen sesim."
}


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
        .select("id,voice_profile_lang,plan")
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


def _update_voice_library(voice_id: str, payload: dict) -> None:
    (
        supabase.table("voice_library")
        .update(payload)
        .eq("id", voice_id)
        .execute()
    )


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


def _cartesia_preview_tts(voice_id: str, text: str, lang: str) -> bytes:
    if not CARTESIA_API_KEY:
        raise HTTPException(status_code=500, detail="CARTESIA_API_KEY missing")

    payload = {
        "model_id": "sonic-3",
        "transcript": text,
        "voice": {
            "mode": "id",
            "id": voice_id,
        },
        "output_format": {
            "container": "mp3",
            "bit_rate": 128000,
            "sample_rate": 44100,
        },
        "language": (lang or "en").split("-")[0].lower(),
    }

    r = requests.post(
        "https://api.cartesia.ai/tts/bytes",
        json=payload,
        headers={
            "Authorization": f"Bearer {CARTESIA_API_KEY}",
            "Cartesia-Version": CARTESIA_VERSION,
            "Content-Type": "application/json",
        },
        timeout=60,
    )

    if r.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"Cartesia preview failed: {r.text}")

    if not r.content:
        raise HTTPException(status_code=500, detail="Cartesia preview empty")

    return r.content


def _upload_preview_audio(user_id: str, voice_type: str, voice_row_id: str, content: bytes) -> str:
    path = f"{user_id}/{voice_type}/preview_{voice_row_id}.mp3"
    try:
        supabase.storage.from_(VOICE_BUCKET).upload(
            path,
            content,
            {"content-type": "audio/mpeg", "x-upsert": "true"}
        )
    except Exception:
        try:
            supabase.storage.from_(VOICE_BUCKET).remove([path])
        except Exception:
            pass
        supabase.storage.from_(VOICE_BUCKET).upload(
            path,
            content,
            {"content-type": "audio/mpeg"}
        )
    return path


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

        lang = str(profile.get("voice_profile_lang") or "en")
        sample_url = _signed_url_for_storage_path(sample_path)

        clone = _cartesia_clone(
            user_id=user_id,
            sample_url=sample_url,
            lang=lang,
        )

        preview_text = PREVIEW_TEXTS.get(voice_type, "Merhaba")
        preview_bytes = _cartesia_preview_tts(
            voice_id=clone["voice_id"],
            text=preview_text,
            lang=lang,
        )
        preview_path = _upload_preview_audio(
            user_id=user_id,
            voice_type=voice_type,
            voice_row_id=str(row["id"]),
            content=preview_bytes,
        )

        _update_voice_library(
            row["id"],
            {
                "tts_voice_id": clone["voice_id"],
                "tts_voice_ready": True,
                "tts_voice_last_error": None,
                "preview_audio_path": preview_path,
                "preview_text_key": voice_type,
                "preview_ready": True,
                "generation_chars_used": len(preview_text),
                "generation_jetons_spent": 1 if len(preview_text) > 0 else 0,
            }
        )

    except HTTPException as e:
        try:
            _update_voice_library(
                row["id"],
                {
                    "tts_voice_ready": False,
                    "tts_voice_last_error": str(e.detail),
                    "preview_ready": False,
                }
            )
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
                    "preview_ready": False,
                }
            )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Voice enroll failed: {e}")

    return {
        "ok": True,
        "provider": clone["provider"],
        "voice_id": clone["voice_id"],
        "voice_type": voice_type,
        "voice_library_id": row["id"],
        "voice_name": row.get("voice_name"),
        "preview_audio_path": preview_path,
        "preview_ready": True,
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
