from __future__ import annotations

import os
import uuid
from typing import Optional, Any

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Header
from supabase import create_client

router = APIRouter(tags=["italkyai-voice"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

VOICE_BUCKET = "voice-samples"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
VOICE_LIMITS = {
    "mine": 1,
    "second": 5,
    "memory": 5,
}


def _safe_name(value: Optional[str], fallback: str) -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    return text[:60]


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = parts[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    return token


def _get_user_from_token(access_token: str) -> dict[str, Any]:
    res = supabase.auth.get_user(access_token)
    user = getattr(res, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return {"id": str(user.id), "email": getattr(user, "email", None)}


def _delete_old_storage_path(path: Optional[str]) -> None:
    old_path = (path or "").strip()
    if not old_path:
        return
    try:
        supabase.storage.from_(VOICE_BUCKET).remove([old_path])
    except Exception:
        pass


def _fetch_profile(user_id: str) -> dict:
    try:
        res = (
            supabase.table("profiles")
            .select(
                "id,"
                "voice_sample_path,tts_voice_id,tts_voice_ready,tts_voice_last_error,"
                "second_voice_name,second_voice_sample_path,second_tts_voice_id,second_tts_voice_ready,"
                "memory_voice_name,memory_voice_sample_path,memory_tts_voice_id,memory_tts_voice_ready"
            )
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        return getattr(res, "data", None) or {}
    except Exception:
        return {}


def _count_voice_kind(user_id: str, voice_kind: str) -> int:
    res = (
        supabase.table("voice_library")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("voice_kind", voice_kind)
        .is_("deleted_at", "null")
        .execute()
    )
    return int(getattr(res, "count", 0) or 0)


def _list_voice_kind(user_id: str, voice_kind: str) -> list[dict]:
    res = (
        supabase.table("voice_library")
        .select(
            "id,voice_name,voice_kind,source_type,sample_path,preview_audio_path,"
            "preview_ready,tts_voice_id,tts_voice_ready,is_active,created_at,updated_at"
        )
        .eq("user_id", user_id)
        .eq("voice_kind", voice_kind)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    return getattr(res, "data", None) or []


def _get_voice_row_by_id(user_id: str, voice_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("voice_library")
            .select("*")
            .eq("id", voice_id)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return getattr(res, "data", None) or None
    except Exception:
        return None


def _get_current_mine_row(user_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("voice_library")
            .select("*")
            .eq("user_id", user_id)
            .eq("voice_kind", "mine")
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return getattr(res, "data", None) or None
    except Exception:
        return None


def _public_url_for_path(path: str) -> str:
    return supabase.storage.from_(VOICE_BUCKET).get_public_url(path)


def _sync_profile_after_upload(
    user_id: str,
    voice_type: str,
    voice_name: str,
    storage_path: str,
) -> None:
    try:
        if voice_type == "mine":
            (
                supabase.table("profiles")
                .update({
                    "voice_sample_path": storage_path,
                    "tts_voice_ready": False,
                    "tts_voice_id": None,
                    "tts_voice_last_error": None,
                })
                .eq("id", user_id)
                .execute()
            )

        elif voice_type == "second":
            (
                supabase.table("profiles")
                .update({
                    "second_voice_name": voice_name,
                    "second_voice_sample_path": storage_path,
                    "second_tts_voice_ready": False,
                    "second_tts_voice_id": None,
                })
                .eq("id", user_id)
                .execute()
            )

        elif voice_type == "memory":
            (
                supabase.table("profiles")
                .update({
                    "memory_voice_name": voice_name,
                    "memory_voice_sample_path": storage_path,
                    "memory_tts_voice_ready": False,
                    "memory_tts_voice_id": None,
                })
                .eq("id", user_id)
                .execute()
            )
    except Exception:
        pass


def _insert_or_update_voice_library(
    user_id: str,
    voice_type: str,
    voice_name: str,
    storage_path: str,
    content_type: str,
    file_ext: str,
    sample_size_bytes: int,
) -> dict:
    public_url = _public_url_for_path(storage_path)

    if voice_type == "mine":
        existing = _get_current_mine_row(user_id)
        if existing:
            res = (
                supabase.table("voice_library")
                .update({
                    "voice_name": voice_name,
                    "source_type": "record" if file_ext == "webm" else "mp3",
                    "sample_path": storage_path,
                    "mime_type": content_type,
                    "file_ext": file_ext,
                    "sample_size_bytes": sample_size_bytes,
                    "tts_voice_id": None,
                    "tts_voice_ready": False,
                    "tts_voice_last_error": None,
                    "preview_audio_path": None,
                    "preview_ready": False,
                })
                .eq("id", existing["id"])
                .execute()
            )
            row = (getattr(res, "data", None) or [existing])[0]
            return {
                "row": row,
                "audio_url": public_url,
                "old_storage_path": existing.get("sample_path"),
            }

        res = (
            supabase.table("voice_library")
            .insert({
                "user_id": user_id,
                "voice_name": voice_name,
                "voice_kind": "mine",
                "source_type": "record" if file_ext == "webm" else "mp3",
                "sample_path": storage_path,
                "mime_type": content_type,
                "file_ext": file_ext,
                "sample_size_bytes": sample_size_bytes,
                "is_active": True,
            })
            .execute()
        )
        row = (getattr(res, "data", None) or [{}])[0]
        return {
            "row": row,
            "audio_url": public_url,
            "old_storage_path": None,
        }

    current_count = _count_voice_kind(user_id, voice_type)
    if current_count >= VOICE_LIMITS[voice_type]:
        raise HTTPException(
            status_code=400,
            detail=f"{VOICE_LIMITS[voice_type]} ses eklenebilir. Yeni ses eklemek için önce kayıtlı seslerden birini silmeniz gereklidir."
        )

    res = (
        supabase.table("voice_library")
        .insert({
            "user_id": user_id,
            "voice_name": voice_name,
            "voice_kind": voice_type,
            "source_type": "record" if file_ext == "webm" else "mp3",
            "sample_path": storage_path,
            "mime_type": content_type,
            "file_ext": file_ext,
            "sample_size_bytes": sample_size_bytes,
            "is_active": False,
        })
        .execute()
    )
    row = (getattr(res, "data", None) or [{}])[0]
    return {
        "row": row,
        "audio_url": public_url,
        "old_storage_path": None,
    }


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

    profile_data = _fetch_profile(user_id)

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

    display_name = _safe_name(
        voice_name,
        "Kendi Sesim" if voice_type == "mine" else ("Tanıdık Ses" if voice_type == "second" else "Hatıra Ses")
    )

    try:
        result = _insert_or_update_voice_library(
            user_id=user_id,
            voice_type=voice_type,
            voice_name=display_name,
            storage_path=storage_path,
            content_type=final_content_type,
            file_ext=ext,
            sample_size_bytes=len(content),
        )
    except Exception:
        _delete_old_storage_path(storage_path)
        raise

    _sync_profile_after_upload(
        user_id=user_id,
        voice_type=voice_type,
        voice_name=display_name,
        storage_path=storage_path,
    )

    if voice_type == "mine":
        _delete_old_storage_path(result.get("old_storage_path"))

    return {
        "ok": True,
        "message": "voice_saved",
        "voice_type": voice_type,
        "voice_name": display_name,
        "audio_url": result["audio_url"],
        "storage_path": storage_path,
        "voice_id": result["row"].get("id"),
    }


@router.get("/api/italkyai/voice/library")
def get_voice_library(authorization: Optional[str] = Header(default=None)):
    access_token = _get_bearer(authorization)
    user = _get_user_from_token(access_token)
    user_id = user["id"]

    mine_row = _get_current_mine_row(user_id)
    second_items = _list_voice_kind(user_id, "second")
    memory_items = _list_voice_kind(user_id, "memory")

    return {
      "ok": True,
      "mine": mine_row,
      "second_items": second_items,
      "memory_items": memory_items,
      "limits": VOICE_LIMITS,
    }


@router.post("/api/italkyai/voice/delete")
def delete_voice(
    voice_id: str = Form(...),
    authorization: Optional[str] = Header(default=None)
):
    access_token = _get_bearer(authorization)
    user = _get_user_from_token(access_token)
    user_id = user["id"]

    row = _get_voice_row_by_id(user_id, voice_id)
    if not row:
        raise HTTPException(status_code=404, detail="voice_not_found")

    try:
        (
            supabase.table("voice_library")
            .update({
                "deleted_at": "now()",
                "is_active": False,
            })
            .eq("id", voice_id)
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"delete_failed: {str(e)}")

    _delete_old_storage_path(row.get("sample_path"))
    _delete_old_storage_path(row.get("preview_audio_path"))

    try:
        if row.get("voice_kind") == "mine":
            (
                supabase.table("profiles")
                .update({
                    "voice_sample_path": None,
                    "tts_voice_id": None,
                    "tts_voice_ready": False,
                    "tts_voice_last_error": None,
                })
                .eq("id", user_id)
                .execute()
            )
        elif row.get("voice_kind") == "second":
            (
                supabase.table("profiles")
                .update({
                    "second_voice_sample_path": None,
                    "second_tts_voice_id": None,
                    "second_tts_voice_ready": False,
                    "second_voice_name": None,
                })
                .eq("id", user_id)
                .execute()
            )
        elif row.get("voice_kind") == "memory":
            (
                supabase.table("profiles")
                .update({
                    "memory_voice_sample_path": None,
                    "memory_tts_voice_id": None,
                    "memory_tts_voice_ready": False,
                    "memory_voice_name": None,
                })
                .eq("id", user_id)
                .execute()
            )
    except Exception:
        pass

    return {
        "ok": True,
        "deleted_voice_id": voice_id,
        "voice_kind": row.get("voice_kind"),
    }
