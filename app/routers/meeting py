from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(prefix="/api/meeting", tags=["meeting"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# =========================================================
# MODELS
# =========================================================
class CreateMeetingBody(BaseModel):
    title: str = Field(default="Yeni Meeting", max_length=120)
    lang_code: str = Field(default="tr", max_length=12)


class JoinMeetingBody(BaseModel):
    meeting_code: str = Field(min_length=4, max_length=32)
    member_no: str = Field(min_length=1, max_length=64)
    lang_code: str = Field(default="tr", max_length=12)


class SendMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


class UpdateLanguageBody(BaseModel):
    lang_code: str = Field(min_length=2, max_length=12)


class LeaveMeetingBody(BaseModel):
    meeting_id: str


# =========================================================
# HELPERS
# =========================================================
def _clean_lang(v: str | None) -> str:
    raw = str(v or "tr").strip().lower()
    return raw[:12] or "tr"


def _clean_text(v: str | None) -> str:
    return " ".join(str(v or "").strip().split())


def _clean_code(v: str | None) -> str:
    return str(v or "").strip().upper()


def _get_bearer(authorization: str | None) -> str:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
      raise HTTPException(status_code=401, detail="Missing bearer token")
    token = raw[7:].strip()
    if not token:
      raise HTTPException(status_code=401, detail="Empty bearer token")
    return token


def _auth_user(authorization: str | None) -> dict[str, Any]:
    token = _get_bearer(authorization)
    try:
        auth_resp = supabase.auth.get_user(token)
        user = getattr(auth_resp, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid session")
        return user.model_dump() if hasattr(user, "model_dump") else dict(user)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth failed: {e}")


def _first_nonempty(*values: Any) -> str | None:
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return None


def _cached_user() -> dict[str, Any]:
    # Backend tarafında local storage yok; boş döner.
    return {}


def _build_member_no(user: dict[str, Any], profile: dict[str, Any] | None) -> str:
    meta = user.get("user_metadata") or {}
    cached = _cached_user()
    member_no = _first_nonempty(
        profile.get("member_no") if profile else None,
        profile.get("membership_no") if profile else None,
        profile.get("user_no") if profile else None,
        profile.get("public_user_id") if profile else None,
        profile.get("short_id") if profile else None,
        meta.get("membership_no"),
        meta.get("member_no"),
        cached.get("membership_no"),
        cached.get("uyelik_no"),
    )
    if member_no:
        return member_no
    uid = str(user.get("id") or "").replace("-", "").upper()
    return uid[:8] if uid else "UNKNOWN"


def _get_profile(user_id: str) -> dict[str, Any] | None:
    try:
        resp = (
            supabase
            .from_("profiles")
            .select("*")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        return resp.data if resp and getattr(resp, "data", None) else None
    except Exception:
        return None


def _display_name(user: dict[str, Any], profile: dict[str, Any] | None) -> str:
    meta = user.get("user_metadata") or {}
    return (
        _first_nonempty(
            profile.get("full_name") if profile else None,
            meta.get("full_name"),
            meta.get("name"),
            user.get("email", "").split("@")[0] if user.get("email") else None,
        )
        or "Kullanıcı"
    )


def _avatar_url(user: dict[str, Any], profile: dict[str, Any] | None) -> str | None:
    meta = user.get("user_metadata") or {}
    return _first_nonempty(
        profile.get("avatar_url") if profile else None,
        meta.get("avatar_url"),
        meta.get("picture"),
    )


def _meeting_for_user(meeting_id: str, user_id: str) -> bool:
    try:
        resp = (
            supabase
            .from_("meeting_participants")
            .select("id")
            .eq("meeting_id", meeting_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False


# =========================================================
# ENDPOINTS
# =========================================================
@router.post("/create")
def create_meeting(
    body: CreateMeetingBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])
    profile = _get_profile(user_id)

    member_no = _build_member_no(user, profile)
    display_name = _display_name(user, profile)
    avatar_url = _avatar_url(user, profile)
    lang_code = _clean_lang(body.lang_code)
    title = _clean_text(body.title) or "Yeni Meeting"

    try:
        rpc = supabase.rpc(
            "create_meeting",
            {
                "p_host_user_id": user_id,
                "p_host_member_no": member_no,
                "p_host_display_name": display_name,
                "p_host_avatar_url": avatar_url,
                "p_lang_code": lang_code,
                "p_title": title,
            },
        ).execute()

        row = (rpc.data or [{}])[0]
        return {
            "ok": True,
            "meeting_id": row.get("meeting_id"),
            "meeting_code": row.get("meeting_code"),
            "title": row.get("title"),
            "host_member_no": member_no,
            "host_display_name": display_name,
            "lang_code": lang_code,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create_meeting_failed: {e}")


@router.post("/join")
def join_meeting(
    body: JoinMeetingBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])
    profile = _get_profile(user_id)

    actual_member_no = _build_member_no(user, profile)
    display_name = _display_name(user, profile)
    avatar_url = _avatar_url(user, profile)

    # Güvenlik için kullanıcının kendi gerçek üyelik numarası ile eşleşmesini bekliyoruz.
    requested_member_no = _clean_text(body.member_no)
    if requested_member_no and requested_member_no != actual_member_no:
        raise HTTPException(
            status_code=400,
            detail="Girilen üyelik numarası aktif kullanıcıya ait değil",
        )

    try:
        rpc = supabase.rpc(
            "join_meeting",
            {
                "p_meeting_code": _clean_code(body.meeting_code),
                "p_user_id": user_id,
                "p_member_no": actual_member_no,
                "p_display_name": display_name,
                "p_avatar_url": avatar_url,
                "p_lang_code": _clean_lang(body.lang_code),
            },
        ).execute()

        row = (rpc.data or [{}])[0]
        return {
            "ok": True,
            "meeting_id": row.get("meeting_id"),
            "joined_now": bool(row.get("joined")),
            "member_no": actual_member_no,
            "display_name": display_name,
        }
    except Exception as e:
        detail = str(e)
        if "MEETING_NOT_FOUND" in detail:
            raise HTTPException(status_code=404, detail="Meeting bulunamadı")
        raise HTTPException(status_code=500, detail=f"join_meeting_failed: {e}")


@router.post("/leave")
def leave_meeting(
    body: LeaveMeetingBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    if not _meeting_for_user(body.meeting_id, user_id):
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    try:
        rpc = supabase.rpc(
            "leave_meeting",
            {
                "p_meeting_id": body.meeting_id,
                "p_user_id": user_id,
            },
        ).execute()

        return {
            "ok": True,
            "left": bool(rpc.data),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"leave_meeting_failed: {e}")


@router.get("/{meeting_id}")
def get_meeting(
    meeting_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    if not _meeting_for_user(meeting_id, user_id):
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    try:
        meeting_resp = (
            supabase
            .from_("meeting_overview")
            .select("*")
            .eq("id", meeting_id)
            .maybe_single()
            .execute()
        )
        meeting = meeting_resp.data or None
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting bulunamadı")

        participants_resp = (
            supabase
            .from_("meeting_participants")
            .select(
                "id, meeting_id, user_id, member_no, display_name, avatar_url, "
                "lang_code, color_key, is_host, is_active, joined_at, last_seen_at, left_at"
            )
            .eq("meeting_id", meeting_id)
            .order("joined_at", desc=False)
            .execute()
        )

        return {
            "ok": True,
            "meeting": meeting,
            "participants": participants_resp.data or [],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"get_meeting_failed: {e}")


@router.get("/{meeting_id}/messages")
def get_meeting_messages(
    meeting_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    if not _meeting_for_user(meeting_id, user_id):
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    try:
        participant_resp = (
            supabase
            .from_("meeting_participants")
            .select("lang_code")
            .eq("meeting_id", meeting_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        viewer_lang = _clean_lang((participant_resp.data or {}).get("lang_code") or "tr")

        messages_resp = (
            supabase
            .from_("meeting_messages")
            .select(
                "id, meeting_id, sender_user_id, sender_member_no, sender_name, "
                "sender_lang, color_key, message_type, event_type, original_text, created_at"
            )
            .eq("meeting_id", meeting_id)
            .order("created_at", desc=False)
            .execute()
        )

        rows = messages_resp.data or []
        for row in rows:
            original_text = row.get("original_text") or ""
            original_lang = _clean_lang(row.get("sender_lang") or "tr")
            if row.get("message_type") == "system":
                row["translated_text"] = original_text
            else:
                row["translated_text"] = (
                    original_text
                    if original_lang == viewer_lang
                    else f"[{viewer_lang.upper()}] {original_text}"
                )

        return {
            "ok": True,
            "viewer_lang": viewer_lang,
            "messages": rows,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"get_messages_failed: {e}")


@router.post("/{meeting_id}/message")
def send_meeting_message(
    meeting_id: str,
    body: SendMessageBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    if not _meeting_for_user(meeting_id, user_id):
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    text = _clean_text(body.text)
    if not text:
        raise HTTPException(status_code=400, detail="Mesaj boş olamaz")

    try:
        rpc = supabase.rpc(
            "send_meeting_message",
            {
                "p_meeting_id": meeting_id,
                "p_user_id": user_id,
                "p_original_text": text,
            },
        ).execute()

        return {
            "ok": True,
            "message_id": rpc.data,
        }
    except Exception as e:
        detail = str(e)
        if "PARTICIPANT_NOT_FOUND" in detail:
            raise HTTPException(status_code=404, detail="Katılımcı bulunamadı")
        raise HTTPException(status_code=500, detail=f"send_message_failed: {e}")


@router.patch("/{meeting_id}/my-language")
def update_my_meeting_language(
    meeting_id: str,
    body: UpdateLanguageBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    if not _meeting_for_user(meeting_id, user_id):
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    try:
        rpc = supabase.rpc(
            "update_meeting_language",
            {
                "p_meeting_id": meeting_id,
                "p_user_id": user_id,
                "p_lang_code": _clean_lang(body.lang_code),
            },
        ).execute()

        return {
            "ok": True,
            "updated": bool(rpc.data),
            "lang_code": _clean_lang(body.lang_code),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_language_failed: {e}")
