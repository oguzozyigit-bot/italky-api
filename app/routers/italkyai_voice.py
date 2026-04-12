from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from supabase import create_client

router = APIRouter(tags=["italkyai-voice"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

VOICE_BUCKET = "voice-samples"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def _safe_name(value: Optional[str], fallback: str) -> str:
    text = (value or "").strip()
    return text if text else fallback


def _delete_old_storage_path(path: Optional[str]) -> None:
    old_path = (path or "").strip()
    if not old_path:
      return
    try:
        supabase.storage.from_(VOICE_BUCKET).remove([old_path])
    except Exception:
        pass


@router.post("/api/italkyai/voice/upload")
async def upload_voice(
    user_id: str = Form(...),
    voice_type: str = Form(...),   # mine | second | memory
    voice_name: str = Form(""),
    audio_file: UploadFile = File(...)
):
    voice_type = (voice_type or "").strip().lower()
    if voice_type not in {"mine", "second", "memory"}:
        raise HTTPException(status_code=400, detail="invalid_voice_type")

    content = await audio_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty_audio")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="file_too_large")

    # Eski path'i önce öğren
    old_mine_path = None
    old_second_path = None
    old_memory_path = None

    try:
        profile_res = supabase.table("profiles").select(
            "voice_sample_path,second_voice_sample_path,memory_voice_sample_path"
        ).eq("id", user_id).maybeSingle().execute()

        profile_data = getattr(profile_res, "data", None) or {}
        old_mine_path = profile_data.get("voice_sample_path")
        old_second_path = profile_data.get("second_voice_sample_path")
        old_memory_path = profile_data.get("memory_voice_sample_path")
    except Exception:
        pass

    original_name = (audio_file.filename or "").lower()
    content_type = (audio_file.content_type or "").lower()

    if original_name.endswith(".mp3") or content_type == "audio/mpeg":
        ext = "mp3"
        final_content_type = "audio/mpeg"
    else:
        ext = "webm"
        final_content_type = "audio/webm"

    filename = f"{uuid.uuid4().hex}.{ext}"
    storage_path = f"{user_id}/{voice_type}/{filename}"

    try:
        supabase.storage.from_(VOICE_BUCKET).upload(
            storage_path,
            content,
            {"content-type": final_content_type}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload_failed: {str(e)}")

    public_url = supabase.storage.from_(VOICE_BUCKET).get_public_url(storage_path)

    try:
        supabase.table("voice_profiles").upsert({
            "user_id": user_id,
            "voice_type": voice_type,
            "voice_name": _safe_name(voice_name, voice_type),
            "storage_path": storage_path,
            "audio_url": public_url
        }, on_conflict="user_id,voice_type").execute()
    except Exception:
        pass

    try:
        if voice_type == "mine":
    supabase.table("profiles").update({
        "voice_sample_path": storage_path,
        "tts_voice_ready": False,
        "tts_voice_id": None,
        "tts_voice_last_error": None
    }).eq("id", user_id).execute()
    _delete_old_storage_path(old_mine_path)

        elif voice_type == "second":
    supabase.table("profiles").update({
        "second_voice_name": _safe_name(voice_name, "2. Ses"),
        "second_voice_sample_path": storage_path,
        "second_tts_voice_ready": False,
        "second_tts_voice_id": None
    }).eq("id", user_id).execute()
    _delete_old_storage_path(old_second_path)

        elif voice_type == "memory":
    supabase.table("profiles").update({
        "memory_voice_name": _safe_name(voice_name, "Hatıra Sesi"),
        "memory_voice_sample_path": storage_path,
        "memory_tts_voice_ready": False,
        "memory_tts_voice_id": None
    }).eq("id", user_id).execute()
    _delete_old_storage_path(old_memory_path)

    except Exception:
        pass

    return {
        "ok": True,
        "message": "voice_saved",
        "voice_type": voice_type,
        "voice_name": _safe_name(voice_name, voice_type),
        "audio_url": public_url,
        "storage_path": storage_path
    }
