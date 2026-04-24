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
        profile.get("user_no") if profile else None,
        profile.get("public_user_id") if profile else None,
        profile.get("short_id") if profile else None,
        meta.get("member_no"),
        meta.get("user_no"),
        meta.get("public_user_id"),
        meta.get("short_id"),
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


def _translate_for_viewer(text: str, sender_lang: str, viewer_lang: str) -> str:
    text = _clean(text)
    if not text:
        return ""
    return text


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

    if not existing_room_id:
        raise HTTPException(status_code=500, detail="room_id üretilemedi")

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
            original_text = _clean(row.get("original_text"))
            translated = (
                original_text
                if row.get("message_type") == "system"
                else _translate_for_viewer(
                    original_text,
                    row.get("sender_lang") or "tr",
                    lang,
                )
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
            original_text = _clean(row.get("original_text"))
            translated = (
                original_text
                if row.get("message_type") == "system"
                else _translate_for_viewer(
                    original_text,
                    row.get("sender_lang") or "tr",
                    viewer_lang,
                )
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"messages_fetch_failed: {e}")

    return {
        "ok": True,
        "participants": participants,
        "messages": messages,
    }


@router.post("/join")
def join_meeting_by_membership(
    body: JoinBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    my_participant = _participant_room(body.room_id, user_id)
    if not my_participant:
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    target_member_no = _clean(body.target_membership_no).upper()
    if not target_member_no:
        raise HTTPException(status_code=400, detail="Üyelik numarası boş olamaz")

    target_profile = None
    target_user_id = None

    try:
        resp = (
            supabase
            .from_("profiles")
            .select("*")
            .or_(
                f"member_no.eq.{target_member_no},user_no.eq.{target_member_no},public_user_id.eq.{target_member_no},short_id.eq.{target_member_no}"
            )
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if rows:
            target_profile = rows[0]
            target_user_id = str(target_profile.get("id") or target_profile.get("user_id") or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"profile_lookup_failed: {e}")

    if not target_profile or not target_user_id:
        raise HTTPException(status_code=404, detail="Üyelik numarası bulunamadı")

    existing = _participant_room(body.room_id, target_user_id)
    if existing and existing.get("is_active"):
        return {"ok": True, "already_joined": True}

    display_name = _first_nonempty(
        target_profile.get("hitap"),
        target_profile.get("display_name"),
        target_profile.get("full_name"),
        target_profile.get("name"),
        "Kullanıcı",
    )
    avatar_url = _first_nonempty(
        target_profile.get("avatar_url"),
        target_profile.get("picture"),
        target_profile.get("avatar"),
    )
    lang = _clean_lang(target_profile.get("lang_code") or "tr")

    try:
        meeting_resp = (
            supabase
            .from_("meetings")
            .select("meeting_code")
            .eq("id", body.room_id)
            .maybe_single()
            .execute()
        )
        meeting_code = _clean(meeting_resp.data.get("meeting_code") if meeting_resp.data else "")
        if not meeting_code:
            raise HTTPException(status_code=404, detail="Meeting code bulunamadı")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"meeting_code_fetch_failed: {e}")

    try:
        rpc = supabase.rpc(
            "join_meeting",
            {
                "p_meeting_code": meeting_code,
                "p_user_id": target_user_id,
                "p_member_no": target_member_no,
                "p_display_name": display_name,
                "p_avatar_url": avatar_url,
                "p_lang_code": lang,
            },
        ).execute()

        row = (rpc.data or [{}])[0]
        return {
            "ok": True,
            "meeting_id": row.get("meeting_id") or body.room_id,
            "joined_now": bool(row.get("joined", True)),
            "member_no": target_member_no,
            "display_name": display_name,
        }
    except Exception:
        pass

    try:
        color_key = _pick_next_color_key(body.room_id)
        supabase.from_("meeting_participants").insert(
            {
                "meeting_id": body.room_id,
                "user_id": target_user_id,
                "member_no": target_member_no,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "lang_code": lang,
                "color_key": color_key,
                "is_host": False,
                "is_active": True,
            }
        ).execute()

        _insert_system_message(
            body.room_id,
            target_user_id,
            target_member_no,
            display_name,
            lang,
            color_key,
            "joined",
            f"{display_name} katıldı",
        )

        return {
            "ok": True,
            "meeting_id": body.room_id,
            "joined_now": True,
            "member_no": target_member_no,
            "display_name": display_name,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"join_meeting_failed: {e}")


@router.post("/message")
def send_message(
    body: MessageBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    if not body.room_id:
      raise HTTPException(status_code=400, detail="room_id boş")

    participant = _participant_room(body.room_id, user_id)
    if not participant:
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    text = _clean(body.text)
    if not text:
        raise HTTPException(status_code=400, detail="Mesaj boş olamaz")

    try:
        rpc = supabase.rpc(
            "send_meeting_message",
            {
                "p_meeting_id": body.room_id,
                "p_user_id": user_id,
                "p_original_text": text,
            },
        ).execute()

        return {
            "ok": True,
            "message_id": rpc.data,
        }
    except Exception:
        pass

    try:
        insert_resp = supabase.from_("meeting_messages").insert(
            {
                "meeting_id": body.room_id,
                "sender_user_id": user_id,
                "sender_member_no": participant.get("member_no"),
                "sender_name": participant.get("display_name") or "Kullanıcı",
                "sender_lang": participant.get("lang_code") or _clean_lang(body.sender_lang),
                "color_key": participant.get("color_key") or "c1",
                "message_type": "text",
                "original_text": text,
            }
        ).execute()

        row = (insert_resp.data or [{}])[0]
        return {
            "ok": True,
            "message_id": row.get("id"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"send_message_failed: {e}")


@router.post("/language")
def update_language(
    body: LanguageBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    participant = _participant_room(body.room_id, user_id)
    if not participant:
        raise HTTPException(status_code=403, detail="Bu meeting'e erişiminiz yok")

    try:
        rpc = supabase.rpc(
            "update_meeting_language",
            {
                "p_meeting_id": body.room_id,
                "p_user_id": user_id,
                "p_lang_code": _clean_lang(body.lang),
            },
        ).execute()

        return {
            "ok": True,
            "updated": bool(rpc.data),
            "lang": _clean_lang(body.lang),
        }
    except Exception:
        pass

    try:
        supabase.from_("meeting_participants").update(
            {"lang_code": _clean_lang(body.lang)}
        ).eq("meeting_id", body.room_id).eq("user_id", user_id).execute()

        return {
            "ok": True,
            "updated": True,
            "lang": _clean_lang(body.lang),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update_language_failed: {e}")


@router.post("/leave")
def leave_meeting(
    body: LeaveBody,
    authorization: Optional[str] = Header(default=None),
):
    user = _auth_user(authorization)
    user_id = str(user["id"])

    participant = _participant_room(body.room_id, user_id)
    if not participant:
        return {"ok": True, "left": False}

    try:
        rpc = supabase.rpc(
            "leave_meeting",
            {
                "p_meeting_id": body.room_id,
                "p_user_id": user_id,
            },
        ).execute()

        return {
            "ok": True,
            "left": bool(rpc.data),
        }
    except Exception:
        pass

    try:
        supabase.from_("meeting_participants").update(
            {
                "is_active": False,
            }
        ).eq("meeting_id", body.room_id).eq("user_id", user_id).execute()

        _insert_system_message(
            body.room_id,
            user_id,
            participant.get("member_no"),
            participant.get("display_name"),
            participant.get("lang_code"),
            participant.get("color_key"),
            "left",
            f'{participant.get("display_name") or "Kullanıcı"} ayrıldı',
        )

        return {
            "ok": True,
            "left": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"leave_meeting_failed: {e}")
