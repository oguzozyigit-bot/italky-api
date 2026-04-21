from __future__ import annotations

import os
import uuid
import requests
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
MAX_FILE_SIZE = 10 * 1024 * 1024
VOICE_LIMITS = {
    "mine": 5,
    "second": 5,
    "memory": 5,
}


def _safe_name(value: Optional[str], fallback: str) -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    return text[:10]


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


def _public_url_for_path(path: str) -> str:
    return supabase.storage.from_(VOICE_BUCKET).get_public_url(path)


def _signed_url_for_path(path: Optional[str], expires_in: int = 3600) -> Optional[str]:
    p = (path or "").strip()
    if not p:
        return None

    try:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/{VOICE_BUCKET}/{p}",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            json={"expiresIn": expires_in},
            timeout=20,
        )

        if r.status_code not in (200, 201):
            return None

        data = r.json()
        signed = data.get("signedURL") or data.get("signedUrl")
        if not signed:
            return None

        if str(signed).startswith("http://") or str(signed).startswith("https://"):
            return signed

        return f"{SUPABASE_URL}/storage/v1{signed}"
    except Exception:
        return None


def _insert_voice_library(
    user_id: str,
    voice_type: str,
    voice_name: str,
    storage_path: str,
    content_type: str,
    file_ext: str,
    sample_size_bytes: int,
) -> dict:
    current_count = _count_voice_kind(user_id, voice_type)
    if current_count >= VOICE_LIMITS[voice_type]:
        raise HTTPException(
            status_code=400,
            detail=f"{VOICE_LIMITS[voice_type]} ses eklenebilir. Yeni ses eklemek için önce kayıtlı seslerinizden birini silmeniz gereklidir."
        )

    public_url = _public_url_for_path(storage_path)

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
            "preview_ready": False,
        })
        .execute()
    )
    row = (getattr(res, "data", None) or [{}])[0]

    return {
        "row": row,
        "audio_url": public_url,
    }


@router.post("/api/italkyai/voice/upload")
async def upload_voice(
    user_id: str = Form(...),
    voice_type: str = Form(...),
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
        "Kendi Ses" if voice_type == "mine" else ("Tanıdık" if voice_type == "second" else "Hatıra")
    )

    try:
        result = _insert_voice_library(
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

    mine_items = _list_voice_kind(user_id, "mine")
    second_items = _list_voice_kind(user_id, "second")
    memory_items = _list_voice_kind(user_id, "memory")

    def enrich(items: list[dict]) -> list[dict]:
        out = []
        for item in items:
            item = dict(item)
            item["sample_signed_url"] = _signed_url_for_path(item.get("sample_path"))
            item["preview_signed_url"] = _signed_url_for_path(item.get("preview_audio_path"))
            out.append(item)
        return out

    return {
        "ok": True,
        "mine_items": enrich(mine_items),
        "second_items": enrich(second_items),
        "memory_items": enrich(memory_items),
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

    return {
        "ok": True,
        "deleted_voice_id": voice_id,
        "voice_kind": row.get("voice_kind"),
    }
