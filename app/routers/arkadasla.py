from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Literal

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.routers.push import push_to_user

router = APIRouter(prefix="/api/arkadasla", tags=["arkadasla"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY eksik.")

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
    "Content-Type": "application/json",
}

CHAT_CODE_RE = re.compile(r"^[A-Z]{2}[0-9]{4}$")
FORBIDDEN_PREFIXES = {"AK", "FG"}
VOICE_CHOICES = {"auto", "mine", "second", "memory"}


# =========================================================
# HELPERS
# =========================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_chat_code(code: str) -> str:
    return (code or "").strip().upper()


def normalize_voice_name(value: Optional[str]) -> str:
    v = str(value or "auto").strip().lower()
    if v in VOICE_CHOICES:
        return v
    if v in ("clone", "kendi", "kendi sesim"):
        return "mine"
    return "auto"


def is_valid_chat_code(code: str) -> bool:
    code = normalize_chat_code(code)
    if not CHAT_CODE_RE.fullmatch(code):
        return False
    if code[:2] in FORBIDDEN_PREFIXES:
        return False
    return True


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    return parts[1].strip()


def get_current_user_id(auth_header: Optional[str]) -> str:
    token = _get_bearer(auth_header)
    url = f"{SUPABASE_URL}/auth/v1/user"
    resp = requests.get(
        url,
        headers={
            "apikey": SUPABASE_SERVICE_ROLE,
            "Authorization": f"Bearer {token}",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Geçersiz oturum")

    data = resp.json() or {}
    uid = data.get("id")
    if not uid:
        raise HTTPException(status_code=401, detail="Kullanıcı bulunamadı")
    return uid


def sb_select(
    table: str,
    select: str = "*",
    filters: Optional[dict[str, str]] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params: dict[str, str] = {"select": select}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)

    resp = requests.get(url, headers=SB_HEADERS, params=params, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} select failed: {resp.text}")
    return resp.json() or []


def sb_insert(table: str, payload: dict | list[dict]) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} insert failed: {resp.text}")
    return resp.json() or []


def sb_patch(table: str, filters: dict[str, str], payload: dict) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    resp = requests.patch(url, headers=headers, params=filters, json=payload, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} update failed: {resp.text}")
    return resp.json() or []


def sb_upsert(table: str, payload: dict | list[dict], on_conflict: Optional[str] = None) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"}
    params: dict[str, str] = {}
    if on_conflict:
        params["on_conflict"] = on_conflict

    resp = requests.post(url, headers=headers, params=params, json=payload, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} upsert failed: {resp.text}")
    return resp.json() or []


def sb_delete(table: str, filters: dict[str, str]) -> list[dict]:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    resp = requests.delete(url, headers=headers, params=filters, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"{table} delete failed: {resp.text}")
    return resp.json() or []


def flag_from_lang(code: str) -> str:
    mapping = {
        "tr": "🇹🇷",
        "en": "🇬🇧",
        "de": "🇩🇪",
        "fr": "🇫🇷",
        "it": "🇮🇹",
        "es": "🇪🇸",
    }
    return mapping.get((code or "").lower(), "🌐")


def safe_name(value: Optional[str], fallback: str = "Karşı Taraf") -> str:
    clean = str(value or "").strip()
    return clean or fallback


# =========================================================
# MODELS
# =========================================================
class StartRequestBody(BaseModel):
    target_code: str = Field(..., min_length=6, max_length=6)
    requester_lang: str = Field(default="tr", min_length=2, max_length=8)
    requester_flag: Optional[str] = Field(default=None, min_length=1, max_length=8)
    requester_voice: Optional[str] = Field(default="auto", max_length=20)

    @field_validator("target_code")
    @classmethod
    def validate_target_code(cls, v: str) -> str:
        code = normalize_chat_code(v)
        if not is_valid_chat_code(code):
            raise ValueError("Kod formatı geçersiz. Örnek: RM4821")
        return code

    @field_validator("requester_voice")
    @classmethod
    def validate_requester_voice(cls, v: Optional[str]) -> str:
        return normalize_voice_name(v)


class RespondRequestBody(BaseModel):
    request_id: str
    action: Literal["accept", "reject"]


class SendMessageBody(BaseModel):
    conversation_id: str
    text: str = Field(..., min_length=1, max_length=4000)
    source_lang: str = Field(default="tr", min_length=2, max_length=8)
    source_flag: Optional[str] = Field(default=None, min_length=1, max_length=8)
    source_voice: Optional[str] = Field(default="auto", max_length=20)
    translated_text: Optional[str] = None

    @field_validator("source_voice")
    @classmethod
    def validate_source_voice(cls, v: Optional[str]) -> str:
        return normalize_voice_name(v)


class PresenceUpdateBody(BaseModel):
    app_state: Literal["foreground", "background", "inactive"] = "foreground"
    selected_lang: str = Field(default="tr", min_length=2, max_length=8)
    selected_flag: Optional[str] = Field(default=None, min_length=1, max_length=8)
    selected_voice: Optional[str] = Field(default="auto", max_length=20)
    is_busy: bool = False
    current_conversation_id: Optional[str] = None

    @field_validator("selected_voice")
    @classmethod
    def validate_selected_voice(cls, v: Optional[str]) -> str:
        return normalize_voice_name(v)


class AddContactBody(BaseModel):
    contact_code: str = Field(..., min_length=6, max_length=6)
    contact_name: str = Field(..., min_length=1, max_length=80)
    contact_lang: Optional[str] = Field(default=None, min_length=2, max_length=8)
    contact_flag: Optional[str] = Field(default=None, min_length=1, max_length=8)
    contact_voice: Optional[str] = Field(default="auto", max_length=20)

    @field_validator("contact_code")
    @classmethod
    def validate_contact_code(cls, v: str) -> str:
        code = normalize_chat_code(v)
        if not is_valid_chat_code(code):
            raise ValueError("Kod formatı geçersiz. Örnek: RM4821")
        return code

    @field_validator("contact_voice")
    @classmethod
    def validate_contact_voice(cls, v: Optional[str]) -> str:
        return normalize_voice_name(v)


class SaveChatBody(BaseModel):
    conversation_id: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=120)
    peer_user_id: Optional[str] = None
    peer_name: Optional[str] = None
    peer_lang: Optional[str] = Field(default=None, min_length=2, max_length=8)
    peer_flag: Optional[str] = Field(default=None, min_length=1, max_length=8)
    peer_voice: Optional[str] = Field(default="auto", max_length=20)
    messages: list[dict] = Field(default_factory=list)

    @field_validator("peer_voice")
    @classmethod
    def validate_peer_voice(cls, v: Optional[str]) -> str:
        return normalize_voice_name(v)


# =========================================================
# BASIC / ME
# =========================================================
@router.get("/me")
def arkadasla_me(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "profiles",
        select="id,full_name,email,chat_code",
        filters={"id": f"eq.{user_id}"},
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Profile bulunamadı")

    profile = rows[0]
    chat_code = normalize_chat_code(profile.get("chat_code") or "")

    return {
        "ok": True,
        "user_id": user_id,
        "chat_code": chat_code,
        "full_name": profile.get("full_name"),
        "email": profile.get("email"),
    }


# =========================================================
# PRESENCE
# =========================================================
@router.post("/presence")
def upsert_presence(
    body: PresenceUpdateBody,
    authorization: Optional[str] = Header(default=None),
):
    user_id = get_current_user_id(authorization)

    row = sb_upsert(
        "arkadasla_presence",
        {
            "user_id": user_id,
            "is_online": body.app_state in ("foreground", "background"),
            "is_busy": body.is_busy,
            "app_state": body.app_state,
            "current_conversation_id": body.current_conversation_id,
            "selected_lang": body.selected_lang,
            "selected_flag": body.selected_flag or flag_from_lang(body.selected_lang),
            "selected_voice": normalize_voice_name(body.selected_voice),
            "last_seen_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        },
        on_conflict="user_id",
    )[0]

    return {"ok": True, "presence": row}


@router.post("/presence/offline")
def set_presence_offline(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_patch(
        "arkadasla_presence",
        {"user_id": f"eq.{user_id}"},
        {
            "is_online": False,
            "is_busy": False,
            "app_state": "inactive",
            "current_conversation_id": None,
            "updated_at": utc_now_iso(),
        },
    )

    return {"ok": True, "presence": rows[0] if rows else None}


# =========================================================
# CONTACTS / REHBER
# =========================================================
@router.post("/contacts")
def add_contact(
    body: AddContactBody,
    authorization: Optional[str] = Header(default=None),
):
    owner_user_id = get_current_user_id(authorization)

    target_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"chat_code": f"eq.{body.contact_code}"},
        limit=1,
    )
    target = target_rows[0] if target_rows else None

    inserted = sb_upsert(
        "arkadasla_contacts",
        {
            "owner_user_id": owner_user_id,
            "contact_user_id": target["id"] if target else None,
            "contact_name": body.contact_name.strip(),
            "contact_code": body.contact_code,
            "contact_lang": body.contact_lang,
            "contact_flag": body.contact_flag or flag_from_lang(body.contact_lang or "tr"),
            "contact_voice": normalize_voice_name(body.contact_voice),
            "updated_at": utc_now_iso(),
        },
        on_conflict="owner_user_id,contact_code",
    )[0]

    return {"ok": True, "contact": inserted}


@router.get("/contacts")
def list_contacts(authorization: Optional[str] = Header(default=None)):
    owner_user_id = get_current_user_id(authorization)

    contacts = sb_select(
        "arkadasla_contacts",
        select="id,owner_user_id,contact_user_id,contact_name,contact_code,contact_lang,contact_flag,contact_voice,created_at,updated_at",
        filters={"owner_user_id": f"eq.{owner_user_id}"},
        order="updated_at.desc",
        limit=200,
    )

    if not contacts:
        return {"ok": True, "items": []}

    codes = [normalize_chat_code(c["contact_code"]) for c in contacts if c.get("contact_code")]
    if codes:
        code_filter = ",".join(codes)
        profiles = sb_select(
            "profiles",
            select="id,full_name,chat_code",
            filters={"chat_code": f"in.({code_filter})"},
            limit=500,
        )
    else:
        profiles = []

    profile_by_code = {
        normalize_chat_code(p["chat_code"]): p
        for p in profiles
        if p.get("chat_code")
    }

    user_ids = [p["id"] for p in profiles if p.get("id")]
    if user_ids:
        uid_filter = ",".join(user_ids)
        presences = sb_select(
            "arkadasla_presence",
            select="user_id,is_online,is_busy,app_state,current_conversation_id,selected_lang,selected_flag,selected_voice,last_seen_at,updated_at",
            filters={"user_id": f"in.({uid_filter})"},
            limit=1000,
        )
    else:
        presences = []

    presence_by_user = {p["user_id"]: p for p in presences if p.get("user_id")}

    items = []
    for c in contacts:
        code = normalize_chat_code(c["contact_code"])
        prof = profile_by_code.get(code, {})
        prs = presence_by_user.get(prof.get("id"), {})

        items.append({
            "id": c["id"],
            "contact_name": c.get("contact_name") or prof.get("full_name") or "Kişi",
            "contact_code": code,
            "contact_lang": c.get("contact_lang") or prs.get("selected_lang") or "tr",
            "contact_flag": c.get("contact_flag") or prs.get("selected_flag") or "🌐",
            "contact_voice": c.get("contact_voice") or prs.get("selected_voice") or "auto",
            "contact_user_id": prof.get("id"),
            "is_online": bool(prs.get("is_online", False)),
            "is_busy": bool(prs.get("is_busy", False)),
            "last_seen_at": prs.get("last_seen_at"),
            "updated_at": c.get("updated_at"),
        })

    return {"ok": True, "items": items}


@router.delete("/contacts/{contact_id}")
def delete_contact(contact_id: str, authorization: Optional[str] = Header(default=None)):
    owner_user_id = get_current_user_id(authorization)

    sb_delete(
        "arkadasla_contacts",
        {
            "id": f"eq.{contact_id}",
            "owner_user_id": f"eq.{owner_user_id}",
        },
    )
    return {"ok": True}


# =========================================================
# REQUESTS
# =========================================================
@router.post("/request")
def send_chat_request(
    body: StartRequestBody,
    authorization: Optional[str] = Header(default=None),
):
    requester_id = get_current_user_id(authorization)

    my_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"id": f"eq.{requester_id}"},
        limit=1,
    )
    if not my_rows:
        raise HTTPException(status_code=404, detail="Requester profile bulunamadı")

    me = my_rows[0]
    my_name = safe_name(me.get("full_name"), "Bir kullanıcı")
    my_code = normalize_chat_code(str(me.get("chat_code") or ""))
    if not is_valid_chat_code(my_code):
        raise HTTPException(status_code=400, detail="Requester chat_code eksik veya geçersiz")

    target_code = normalize_chat_code(body.target_code)

    if target_code == my_code:
        raise HTTPException(status_code=400, detail="Kendi koduna istek gönderemezsin")

    target_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"chat_code": f"eq.{target_code}"},
        limit=1,
    )
    if not target_rows:
        raise HTTPException(status_code=404, detail="Hedef kod bulunamadı")

    target = target_rows[0]
    target_user_id = target["id"]

    target_presence_rows = sb_select(
        "arkadasla_presence",
        select="user_id,is_online,is_busy,app_state,current_conversation_id,selected_lang,selected_flag,selected_voice,last_seen_at",
        filters={"user_id": f"eq.{target_user_id}"},
        limit=1,
    )
    target_presence = target_presence_rows[0] if target_presence_rows else None

    if not target_presence or not bool(target_presence.get("is_online", False)):
        raise HTTPException(status_code=409, detail="Bu kullanıcı şu anda çevrimdışı.")

    if bool(target_presence.get("is_busy", False)):
        raise HTTPException(status_code=409, detail="Bu kullanıcı şu anda meşgul.")

    existing = sb_select(
        "arkadasla_requests",
        select="id,status,requester_user_id,target_user_id",
        filters={
            "requester_user_id": f"eq.{requester_id}",
            "target_user_id": f"eq.{target_user_id}",
            "status": "eq.pending",
        },
        order="created_at.desc",
        limit=1,
    )
    if existing:
        return {
            "ok": True,
            "request_id": existing[0]["id"],
            "status": "pending",
            "message": "Zaten bekleyen bir istek var."
        }

    inserted = sb_insert("arkadasla_requests", {
        "id": str(uuid.uuid4()),
        "requester_user_id": requester_id,
        "target_user_id": target_user_id,
        "requester_code": my_code,
        "target_code": target_code,
        "requester_lang": body.requester_lang,
        "requester_flag": body.requester_flag or flag_from_lang(body.requester_lang),
        "requester_voice": normalize_voice_name(body.requester_voice),
        "status": "pending",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    })

    row = inserted[0]

    try:
        push_to_user(
            target_user_id,
            title="italkyAI",
            body=f"{my_name} seninle sohbet etmek istiyor",
            type_="arkadasla_request",
            requester_name=my_name,
            open_page="/pages/arkadasla.html",
        )
    except Exception:
        pass

    return {
        "ok": True,
        "request_id": row["id"],
        "status": row["status"],
        "target_user_id": target_user_id,
        "target_name": target.get("full_name") or "Karşı Taraf",
        "target_lang": target_presence.get("selected_lang") if target_presence else None,
        "target_flag": target_presence.get("selected_flag") if target_presence else None,
        "target_voice": target_presence.get("selected_voice") if target_presence else "auto",
    }


@router.get("/incoming")
def list_incoming_requests(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_requests",
        select="id,requester_user_id,target_user_id,requester_code,target_code,requester_lang,requester_flag,requester_voice,status,created_at",
        filters={
            "target_user_id": f"eq.{user_id}",
            "status": "eq.pending",
        },
        order="created_at.desc",
        limit=20,
    )

    if not rows:
        return {"ok": True, "items": []}

    requester_ids = ",".join(
        r["requester_user_id"] for r in rows if r.get("requester_user_id")
    )
    profiles = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"id": f"in.({requester_ids})"} if requester_ids else {},
        limit=50,
    )
    profile_map = {p["id"]: p for p in profiles}

    items = []
    for r in rows:
        rp = profile_map.get(r["requester_user_id"], {})
        items.append({
            "request_id": r["id"],
            "requester_user_id": r["requester_user_id"],
            "requester_name": rp.get("full_name") or "Karşı Taraf",
            "requester_code": normalize_chat_code(r.get("requester_code") or ""),
            "requester_lang": r.get("requester_lang") or "tr",
            "requester_flag": r.get("requester_flag") or flag_from_lang(r.get("requester_lang") or "tr"),
            "requester_voice": normalize_voice_name(r.get("requester_voice")),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
        })

    return {"ok": True, "items": items}


@router.post("/respond")
def respond_chat_request(
    body: RespondRequestBody,
    authorization: Optional[str] = Header(default=None),
):
    target_user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_requests",
        select="*",
        filters={
            "id": f"eq.{body.request_id}",
            "target_user_id": f"eq.{target_user_id}",
        },
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="İstek bulunamadı")

    req = rows[0]
    if req.get("status") != "pending":
        return {
            "ok": True,
            "request_id": req["id"],
            "status": req.get("status"),
            "message": "İstek zaten işlenmiş."
        }

    if body.action == "reject":
        updated = sb_patch(
            "arkadasla_requests",
            {"id": f"eq.{body.request_id}"},
            {"status": "rejected", "updated_at": utc_now_iso()},
        )[0]
        return {
            "ok": True,
            "request_id": updated["id"],
            "status": updated["status"],
        }

    requester_user_id = req["requester_user_id"]
    conversation_id = str(uuid.uuid4())

    conv = sb_insert("arkadasla_conversations", {
        "id": conversation_id,
        "user_a": requester_user_id,
        "user_b": target_user_id,
        "request_id": req["id"],
        "status": "active",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    })[0]

    updated = sb_patch(
        "arkadasla_requests",
        {"id": f"eq.{body.request_id}"},
        {
            "status": "accepted",
            "conversation_id": conversation_id,
            "updated_at": utc_now_iso(),
        },
    )[0]

    requester_presence = sb_select(
        "arkadasla_presence",
        select="user_id,selected_lang,selected_flag,selected_voice,app_state",
        filters={"user_id": f"eq.{requester_user_id}"},
        limit=1,
    )
    target_presence = sb_select(
        "arkadasla_presence",
        select="user_id,selected_lang,selected_flag,selected_voice,app_state",
        filters={"user_id": f"eq.{target_user_id}"},
        limit=1,
    )

    rp = requester_presence[0] if requester_presence else {}
    tp = target_presence[0] if target_presence else {}

    sb_upsert(
        "arkadasla_presence",
        [
            {
                "user_id": requester_user_id,
                "is_online": True,
                "is_busy": True,
                "app_state": rp.get("app_state") or "foreground",
                "current_conversation_id": conversation_id,
                "selected_lang": rp.get("selected_lang") or req.get("requester_lang") or "tr",
                "selected_flag": rp.get("selected_flag") or req.get("requester_flag") or flag_from_lang(req.get("requester_lang") or "tr"),
                "selected_voice": normalize_voice_name(rp.get("selected_voice") or req.get("requester_voice") or "auto"),
                "last_seen_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
            {
                "user_id": target_user_id,
                "is_online": True,
                "is_busy": True,
                "app_state": tp.get("app_state") or "foreground",
                "current_conversation_id": conversation_id,
                "selected_lang": tp.get("selected_lang") or "tr",
                "selected_flag": tp.get("selected_flag") or flag_from_lang(tp.get("selected_lang") or "tr"),
                "selected_voice": normalize_voice_name(tp.get("selected_voice") or "auto"),
                "last_seen_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        ],
        on_conflict="user_id",
    )

    try:
        accepter_rows = sb_select(
            "profiles",
            select="full_name",
            filters={"id": f"eq.{target_user_id}"},
            limit=1,
        )
        accepter_name = safe_name(
            accepter_rows[0].get("full_name") if accepter_rows else None,
            "Karşı taraf"
        )

        push_to_user(
            requester_user_id,
            title="italkyAI",
            body=f"{accepter_name} ile bağlantı kuruldu",
            type_="arkadasla_connected",
            peer_name=accepter_name,
            open_page="/pages/arkadasla.html",
        )
    except Exception:
        pass

    return {
        "ok": True,
        "request_id": updated["id"],
        "status": updated["status"],
        "conversation_id": conv["id"],
    }


# =========================================================
# CONVERSATION
# =========================================================
@router.get("/conversation/current")
def get_current_conversation(authorization: Optional[str] = Header(default=None)):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_conversations",
        select="*",
        filters={
            "status": "eq.active",
            "or": f"(user_a.eq.{user_id},user_b.eq.{user_id})",
        },
        order="updated_at.desc",
        limit=1,
    )
    if not rows:
        return {"ok": True, "conversation": None}

    conv = rows[0]
    other_user_id = conv["user_b"] if conv["user_a"] == user_id else conv["user_a"]

    profile_rows = sb_select(
        "profiles",
        select="id,full_name,chat_code",
        filters={"id": f"eq.{other_user_id}"},
        limit=1,
    )
    other = profile_rows[0] if profile_rows else {}

    other_presence_rows = sb_select(
        "arkadasla_presence",
        select="selected_lang,selected_flag,selected_voice,is_online,is_busy",
        filters={"user_id": f"eq.{other_user_id}"},
        limit=1,
    )
    other_presence = other_presence_rows[0] if other_presence_rows else {}

    return {
        "ok": True,
        "conversation": {
            "id": conv["id"],
            "status": conv["status"],
            "other_user_id": other_user_id,
            "other_name": other.get("full_name") or "Karşı Taraf",
            "other_code": normalize_chat_code(other.get("chat_code") or ""),
            "other_lang": other_presence.get("selected_lang") or "tr",
            "other_flag": other_presence.get("selected_flag") or flag_from_lang(other_presence.get("selected_lang") or "tr"),
            "other_voice": normalize_voice_name(other_presence.get("selected_voice")),
            "created_at": conv.get("created_at"),
            "updated_at": conv.get("updated_at"),
        }
    }


@router.post("/message")
def send_message(
    body: SendMessageBody,
    authorization: Optional[str] = Header(default=None),
):
    sender_user_id = get_current_user_id(authorization)

    conv_rows = sb_select(
        "arkadasla_conversations",
        select="*",
        filters={
            "id": f"eq.{body.conversation_id}",
            "status": "eq.active",
            "or": f"(user_a.eq.{sender_user_id},user_b.eq.{sender_user_id})",
        },
        limit=1,
    )
    if not conv_rows:
        raise HTTPException(status_code=404, detail="Aktif konuşma bulunamadı")

    conv = conv_rows[0]

    inserted = sb_insert("arkadasla_messages", {
        "id": str(uuid.uuid4()),
        "conversation_id": body.conversation_id,
        "sender_user_id": sender_user_id,
        "text": body.text,
        "source_lang": body.source_lang,
        "source_flag": body.source_flag or flag_from_lang(body.source_lang),
        "source_voice": normalize_voice_name(body.source_voice),
        "translated_text": body.translated_text,
        "created_at": utc_now_iso(),
    })[0]

    sb_patch(
        "arkadasla_conversations",
        {"id": f"eq.{body.conversation_id}"},
        {"updated_at": utc_now_iso()},
    )

    other_user_id = conv["user_b"] if conv["user_a"] == sender_user_id else conv["user_a"]

    try:
        sender_rows = sb_select(
            "profiles",
            select="full_name",
            filters={"id": f"eq.{sender_user_id}"},
            limit=1,
        )
        sender_name = safe_name(
            sender_rows[0].get("full_name") if sender_rows else None,
            "Karşı taraf"
        )

        push_to_user(
            other_user_id,
            title="italkyAI",
            body=f"{sender_name}: {body.translated_text or body.text}",
            type_="arkadasla_message",
            peer_name=sender_name,
            preview=body.translated_text or body.text,
            open_page="/pages/arkadasla.html",
        )
    except Exception:
        pass

    return {
        "ok": True,
        "message": inserted,
    }


@router.get("/messages")
def list_messages(
    conversation_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user_id = get_current_user_id(authorization)

    conv_rows = sb_select(
        "arkadasla_conversations",
        select="id,user_a,user_b,status",
        filters={
            "id": f"eq.{conversation_id}",
            "or": f"(user_a.eq.{user_id},user_b.eq.{user_id})",
        },
        limit=1,
    )
    if not conv_rows:
        raise HTTPException(status_code=404, detail="Konuşma bulunamadı")

    rows = sb_select(
        "arkadasla_messages",
        select="*",
        filters={"conversation_id": f"eq.{conversation_id}"},
        order="created_at.asc",
        limit=500,
    )

    return {
        "ok": True,
        "items": rows,
    }


@router.post("/end")
def end_conversation(
    conversation_id: str,
    authorization: Optional[str] = Header(default=None),
):
    user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_conversations",
        select="*",
        filters={
            "id": f"eq.{conversation_id}",
            "status": "eq.active",
            "or": f"(user_a.eq.{user_id},user_b.eq.{user_id})",
        },
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Aktif konuşma bulunamadı")

    conv = rows[0]
    updated = sb_patch(
        "arkadasla_conversations",
        {"id": f"eq.{conversation_id}"},
        {
            "status": "ended",
            "ended_by": user_id,
            "ended_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        },
    )[0]

    user_a = conv["user_a"]
    user_b = conv["user_b"]

    sb_upsert(
        "arkadasla_presence",
        [
            {
                "user_id": user_a,
                "is_online": True,
                "is_busy": False,
                "app_state": "foreground",
                "current_conversation_id": None,
                "last_seen_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
            {
                "user_id": user_b,
                "is_online": True,
                "is_busy": False,
                "app_state": "foreground",
                "current_conversation_id": None,
                "last_seen_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            },
        ],
        on_conflict="user_id",
    )

    return {
        "ok": True,
        "conversation_id": updated["id"],
        "status": updated["status"],
    }


# =========================================================
# SAVED CHATS
# =========================================================
@router.post("/saved")
def save_chat(
    body: SaveChatBody,
    authorization: Optional[str] = Header(default=None),
):
    owner_user_id = get_current_user_id(authorization)

    saved = sb_insert("arkadasla_saved_chats", {
        "id": str(uuid.uuid4()),
        "owner_user_id": owner_user_id,
        "conversation_id": body.conversation_id,
        "title": body.title.strip(),
        "peer_user_id": body.peer_user_id,
        "peer_name": body.peer_name,
        "peer_lang": body.peer_lang,
        "peer_flag": body.peer_flag or flag_from_lang(body.peer_lang or "tr"),
        "peer_voice": normalize_voice_name(body.peer_voice),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    })[0]

    message_rows = []
    for item in body.messages:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        message_rows.append({
            "id": str(uuid.uuid4()),
            "saved_chat_id": saved["id"],
            "side": item.get("side") or "left",
            "sender_name": item.get("sender_name") or item.get("name"),
            "sender_voice": normalize_voice_name(item.get("sender_voice")),
            "text": text,
            "translated_text": item.get("translated_text"),
            "meta": item.get("meta"),
            "created_at": utc_now_iso(),
        })

    if message_rows:
        sb_insert("arkadasla_saved_chat_messages", message_rows)

    return {"ok": True, "saved_chat": saved}


@router.get("/saved")
def list_saved_chats(authorization: Optional[str] = Header(default=None)):
    owner_user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_saved_chats",
        select="id,title,peer_user_id,peer_name,peer_lang,peer_flag,peer_voice,conversation_id,created_at,updated_at",
        filters={"owner_user_id": f"eq.{owner_user_id}"},
        order="updated_at.desc",
        limit=200,
    )

    return {"ok": True, "items": rows}


@router.get("/saved/{saved_chat_id}")
def get_saved_chat(saved_chat_id: str, authorization: Optional[str] = Header(default=None)):
    owner_user_id = get_current_user_id(authorization)

    rows = sb_select(
        "arkadasla_saved_chats",
        select="*",
        filters={
            "id": f"eq.{saved_chat_id}",
            "owner_user_id": f"eq.{owner_user_id}",
        },
        limit=1,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Kayıtlı sohbet bulunamadı")

    saved = rows[0]

    messages = sb_select(
        "arkadasla_saved_chat_messages",
        select="id,side,sender_name,sender_voice,text,translated_text,meta,created_at",
        filters={"saved_chat_id": f"eq.{saved_chat_id}"},
        order="created_at.asc",
        limit=1000,
    )

    return {"ok": True, "saved_chat": saved, "messages": messages}


@router.delete("/saved/{saved_chat_id}")
def delete_saved_chat(saved_chat_id: str, authorization: Optional[str] = Header(default=None)):
    owner_user_id = get_current_user_id(authorization)

    sb_delete(
        "arkadasla_saved_chats",
        {
            "id": f"eq.{saved_chat_id}",
            "owner_user_id": f"eq.{owner_user_id}",
        },
    )

    return {"ok": True}
