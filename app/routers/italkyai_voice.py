from __future__ import annotations

import os
import uuid
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from supabase import create_client

router = APIRouter(tags=["italkyai-voice"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

VOICE_BUCKET = "voice-profiles"


@router.post("/api/italkyai/voice/upload")
async def upload_voice(
    user_id: str = Form(...),
    voice_type: str = Form(...),
    voice_name: str = Form(""),
    audio_file: UploadFile = File(...)
):
    if voice_type not in {"mine", "second", "memory"}:
        raise HTTPException(status_code=400, detail="invalid_voice_type")

    content = await audio_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty_audio")

    # dosya boyutu: 10 MB
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file_too_large")

    original_name = (audio_file.filename or "").lower()
    ext = "mp3" if original_name.endswith(".mp3") else "webm"
    filename = f"{uuid.uuid4().hex}.{ext}"
    storage_path = f"{user_id}/{voice_type}/{filename}"

    try:
        supabase.storage.from_(VOICE_BUCKET).upload(
            storage_path,
            content,
            {"content-type": audio_file.content_type or ("audio/mpeg" if ext == "mp3" else "audio/webm")}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload_failed: {str(e)}")

    public_url = supabase.storage.from_(VOICE_BUCKET).get_public_url(storage_path)

    # ham kayıt bilgisi istersen burada ayrı tabloya da yazılabilir
    try:
        supabase.table("voice_profiles").upsert({
            "user_id": user_id,
            "voice_type": voice_type,
            "voice_name": voice_name.strip() if voice_name else None,
            "storage_path": storage_path,
            "audio_url": public_url
        }, on_conflict="user_id,voice_type").execute()
    except Exception:
        pass

    # profiles ortak havuzu güncelle
    try:
        if voice_type == "mine":
            supabase.table("profiles").update({
                "voice_sample_path": storage_path,
                "tts_voice_ready": True,
                "tts_voice_id": storage_path
            }).eq("id", user_id).execute()

        elif voice_type == "second":
            supabase.table("profiles").update({
                "second_voice_name": voice_name.strip() if voice_name else None,
                "second_voice_sample_path": storage_path,
                "second_tts_voice_ready": True,
                "second_tts_voice_id": storage_path
            }).eq("id", user_id).execute()

        elif voice_type == "memory":
            supabase.table("profiles").update({
                "memory_voice_name": voice_name.strip() if voice_name else None,
                "memory_voice_sample_path": storage_path,
                "memory_tts_voice_ready": True,
                "memory_tts_voice_id": storage_path
            }).eq("id", user_id).execute()
    except Exception:
        pass

    return {
        "ok": True,
        "voice_type": voice_type,
        "voice_name": voice_name,
        "audio_url": public_url,
        "storage_path": storage_path
    }
