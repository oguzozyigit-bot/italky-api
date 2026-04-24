from __future__ import annotations

import os
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
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
class BootstrapBody(BaseModel):
    membership_no: str
    display_name: str
    avatar_url: str | None = None
    lang: str = "tr"


class JoinBody(BaseModel):
    room_id: str
    target_membership_no: str
    inviter_membership_no: str | None = None


class MessageBody(BaseModel):
    room_id: str
    text: str
    sender_lang: str = "tr"
    target_lang: str = "tr"


class LanguageBody(BaseModel):
    room_id: str
    lang: str


class LeaveBody(BaseModel):
    room_id: str


# =========================================================
# HELPERS
# =========================================================
def _clean(v: Any) -> str:
    return " ".join(str(v or "").strip().split())


def _clean_lang(v: Any) -> str:
    s = str(v or "tr").strip().lower()
    return s[:12] or "tr"


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


def _build_member_no(user: dict[str, Any], profile: dict[str, Any] | None) -> str:
    meta = user.get("user_metadata") or {}
    v = _first_nonempty(
        profile.get("member_no") if profile else None,
        profile.get("membership_no") if profile else None,
        profile.get("user_no") if profile else None,
        profile.get("public_user_id") if profile else None,
        profile.get("short_id") if profile else None,
        meta.get("membership_no"),
        meta.get("member_no"),
    )
    if v:
        return v
    uid = str(user.get("id") or "").replace("-", "").upper()
    return uid[:8] if uid else "UNKNOWN"


def _display_name(user: dict[str, Any], profile: dict[str, Any] | None) -> str:
    meta = user.get("user_metadata") or {}
    return (
        _first_nonempty(
            profile.get("hitap") if profile else None,
            profile.get("display_name") if profile else None,
            profile.get("full_name") if profile else None,
            profile.get("name") if profile else None,
            meta.get("hitap"),
            meta.get("display_name"),
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
        profile.get("picture") if profile else None,
        profile.get("avatar") if profile else None,
        meta.get("avatar_url"),
        meta.get("picture"),
        meta.get("avatar"),
    )


def _participant_room(room_id: str, user_id: str) -> dict[str, Any] | None:
    try:
        resp = (
            supabase
            .from_("meeting_participants")
            .select("*")
            .eq("meeting_id", room_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return resp.data if resp and getattr(resp, "data", None) else None
    except Exception:
        return None


def _room_exists(room_id: str) -> bool:
    try:
        resp = (
            supabase
            .from_("meetings")
            .select("id")
            .eq("id", room_id)
            .maybe_single()
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False


def _translate_for_viewer(text: str, sender_lang: str, viewer_lang: str) -> str:
    text = _clean(text)
    if not text:
        return ""
    sender_lang = _clean_lang(sender_lang)
    viewer_lang = _clean_lang(viewer_lang)
    if sender_lang == viewer_lang:
        return text
    return f"[{viewer_lang.upper()}] {text}"


def _participant_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "user_id": row.get("user_id"),
        "membership_no": row.get("member_no"),
        "display_name": row.get("display_name"),
        "avatar_url": row.get("avatar_url"),
        "lang": row.get("lang_code") or "tr",
        "is_host": bool(row.get("is_host")),
        "is_active": bool(row.get("is_active")),
        "joined_at": row.get("joined_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


def _generate_meeting_code(seed: str) -> str:
    base = (seed or uuid.uuid4().hex).replace("-", "").upper()
    return f"M{base[:7]}"


def _pick_next_color_key(room_id: str) -> str:
    palette = ["c1", "c2", "c3", "c4", "c5", "c6"]
    try:
        resp = (
            supabase
            .from_("meeting_participants")
            .select("id", count="exact")
            .eq("meeting_id", room_id)
            .execute()
        )
        count = int(getattr(resp, "count", 0) or 0)
        return palette[count % len(palette)]
    except Exception:
        return "c1"


def _insert_system_message(
    room_id: str,
    sender_user_id: str | None,
    sender_member_no: str | None,
    sender_name: str | None,
    sender_lang: str | None,
    color_key: str | None,
    event_type: str,
    text: str,
) -> None:
    try:
        supabase.from_("meeting_messages").insert(
            {
                "meeting_id": room_id,
                "sender_user_id": sender_user_id,
                "sender_member_no": sender_member_no,
                "sender_name": sender_name,
                "sender_lang": sender_lang,
                "color_key": color_key,
                "message_type": "system",
                "event_type": event_type,
                "original_text": text,
            }
        ).execute()
    except Exception:
        pass


def _ensure_creator_participant(
    room_id: str,
    user_id: str,
    member_no: str,
    display_name: str,
    avatar_url: str | None,
    lang: str,
) -> None:
    existing = _participant_room(room_id, user_id)
    if existing:
        try:
            supabase.from_("meeting_participants").update(
                {
                    "member_no": member_no,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "lang_code": lang,
                    "is_host": True,
                    "is_active": True,
                    "left_at": None,
                }
            ).eq("meeting_id", room_id).eq("user_id", user_id).execute()
        except Exception:
            pass
        return

    try:
        supabase.from_("meeting_participants").insert(
            {
                "meeting_id": room_id,
                "user_id": user_id,
                "member_no": member_no,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "lang_code": lang,
                "color_key": "c1",
                "is_host": True,
                "is_active": True,
            }
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"host_participant_insert_failed: {e}")


# =========================================================
# ENDPOINTS
# =========================================================
@router.post("/bootstrap")
def bootstrap_meeting(
    body: BootstrapBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])
    profile = _get_profile(user_id)

    member_no = _build_member_no(user, profile)
    display_name = _display_name(user, profile)
    avatar_url = _avatar_url(user, profile)
    lang = _clean_lang(body.lang)

    existing_room_id = None
    existing_room_code = None

    # 1) Aktif host olduğu oda var mı?
    try:
        participant_resp = (
            supabase
            .from_("meeting_participants")
            .select("meeting_id,is_host,is_active")
            .eq("user_id", user_id)
            .eq("is_host", True)
            .eq("is_active", True)
            .order("joined_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = participant_resp.data or []
        if rows:
            existing_room_id = rows[0].get("meeting_id")
    except Exception:
        existing_room_id = None

    # 2) Odaya ait meeting_code bul
    if existing_room_id:
        try:
            meeting_resp = (
                supabase
                .from_("meetings")
                .select("id,meeting_code,title")
                .eq("id", existing_room_id)
                .maybe_single()
                .execute()
            )
            if meeting_resp.data:
                existing_room_code = meeting_resp.data.get("meeting_code")
        except Exception:
            existing_room_code = None

    # 3) RPC ile oluşturmayı dene
    if not existing_room_id:
        try:
            rpc = supabase.rpc(
                "create_meeting",
                {
                    "p_host_user_id": user_id,
                    "p_host_member_no": member_no,
                    "p_host_display_name": display_name,
                    "p_host_avatar_url": avatar_url,
                    "p_lang_code": lang,
                    "p_title": "Yeni Meeting",
                },
            ).execute()

            row = (rpc.data or [{}])[0]
            existing_room_id = row.get("meeting_id")
            existing_room_code = row.get("meeting_code")
        except Exception:
            existing_room_id = None
            existing_room_code = None

    # 4) RPC çalışmadıysa fallback: direkt tabloya insert
    if not existing_room_id:
        try:
            meeting_code = _generate_meeting_code(member_no or user_id)
            meeting_insert = (
                supabase
                .from_("meetings")
                .insert(
                    {
                        "meeting_code": meeting_code,
                        "title": "Yeni Meeting",
                        "host_user_id": user_id,
                        "status": "active",
                    }
                )
                .execute()
            )

            inserted = (meeting_insert.data or [{}])[0]
            existing_room_id = inserted.get("id")
            existing_room_code = inserted.get("meeting_code") or meeting_code

            if existing_room_id:
                _ensure_creator_participant(
                    existing_room_id,
                    user_id,
                    member_no,
                    display_name,
                    avatar_url,
                    lang,
                )
                _insert_system_message(
                    existing_room_id,
                    user_id,
                    member_no,
                    display_name,
                    lang,
                    "c1",
                    "joined",
                    f"{display_name} katıldı",
                )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"meeting_create_fallback_failed: {e}")

    # 5) Hâlâ room yoksa sert hata
    if not existing_room_id:
        raise HTTPException(status_code=500, detail="room_id üretilemedi")

    # 6) Dil güncelle
    try:
        supabase.rpc(
            "update_meeting_language",
            {
                "p_meeting_id": existing_room_id,
                "p_user_id": user_id,
                "p_lang_code": lang,
            },
        ).execute()
    except Exception:
        try:
            supabase.from_("meeting_participants").update(
                {"lang_code": lang}
            ).eq("meeting_id", existing_room_id).eq("user_id", user_id).execute()
        except Exception:
            pass

    # 7) Katılımcıları çek
    try:
        participants_resp = (
            supabase
            .from_("meeting_participants")
            .select("*")
            .eq("meeting_id", existing_room_id)
            .eq("is_active", True)
            .order("joined_at", desc=False)
            .execute()
        )
        participants = [_participant_public(x) for x in (participants_resp.data or [])]
    except Exception:
        participants = []

    # 8) Mesajları çek
    try:
        messages_resp = (
            supabase
            .from_("meeting_messages")
            .select("*")
            .eq("meeting_id", existing_room_id)
            .order("created_at", desc=False)
            .execute()
        )

        messages = []
        for row in (messages_resp.data or []):
            sender_lang = _clean_lang(row.get("sender_lang") or "tr")
            original_text = _clean(row.get("original_text"))
            translated = (
                original_text
                if row.get("message_type") == "system"
                else _translate_for_viewer(original_text, sender_lang, lang)
            )
            messages.append(
                {
                    "id": row.get("id"),
                    "sender_id": row.get("sender_user_id"),
                    "sender_name": row.get("sender_name"),
                    "sender_member_no": row.get("sender_member_no"),
                    "message_type": row.get("message_type"),
                    "event_type": row.get("event_type"),
                    "original_text": original_text,
                    "translated_text": translated,
                    "created_at": row.get("created_at"),
                }
            )
    except Exception:
        messages = []

    return {
        "ok": True,
        "room_id": existing_room_id,
        "room_code": existing_room_code,
        "participants": participants,
        "messages": messages,
    }


@router.get("/state")
def room_state(
    room_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    participant = _participant_room(room_id, user_id)
    if not participant:
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    viewer_lang = _clean_lang(participant.get("lang_code") or "tr")

    try:
        participants_resp = (
            supabase
            .from_("meeting_participants")
            .select("*")
            .eq("meeting_id", room_id)
            .eq("is_active", True)
            .order("joined_at", desc=False)
            .execute()
        )
        participants = [_participant_public(x) for x in (participants_resp.data or [])]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"participants_fetch_failed: {e}")

    try:
        messages_resp = (
            supabase
            .from_("meeting_messages")
            .select("*")
            .eq("meeting_id", room_id)
            .order("created_at", desc=False)
            .execute()
        )

        messages = []
        for row in (messages_resp.data or []):
            sender_lang = _clean_lang(row.get("sender_lang") or "tr")
            original_text = _clean(row.get("original_text"))
            translated = (
                original_text
                if row.get("message_type") == "system"
                else _translate_for_viewer(original_text, sender_lang, viewer_lang)
            )
            messages
