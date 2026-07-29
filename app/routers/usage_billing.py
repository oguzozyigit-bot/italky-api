from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

from app.routers.token_engine import CHARS_PER_JETON, spend_chars

router = APIRouter(tags=["usage-billing"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
ICANY_PERSONAL_SPEND_URL = "https://icany.ai/api/bridge/personal-spend"

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

TRANSLATION_MODULES = {
    "text_ai", "text_translate", "text_translate_paid", "document_translate", "document_ai",
    "photo_translate", "photo_ai", "ocr_translate", "facetoface_ai", "usage_face_to_face",
    "face_to_face", "facetoface", "eartoear_ai", "ear_to_ear", "side_to_side", "sidetoside",
    "culture_translate", "cultural_translate", "italky_call", "two_phone", "interpreter",
    "interpreter_live", "interpreter_qr", "regional_translate", "geographic_translate",
}
TRANSLATION_HINTS = (
    "translate", "translation", "facetoface", "face_to_face", "eartoear", "ear_to_ear",
    "side_to_side", "sidetoside", "document", "photo", "ocr", "interpreter",
    "italky_call", "two_phone", "culture", "cultural",
)
PAID_TEXT_MODULES = {"chat_ai", "practice_ai"}
PAID_VOICE_MODULES = {"voice_clone", "voice_clone_preview", "voice_ai", "voice_preset_use", "voice_live", "practice_ai"}
FREE_MODULES = {"voice_preset_preview", "offline", "offline_mode"}


class UsageBillingReq(BaseModel):
    user_id: Optional[str] = None
    module: str = Field(min_length=1, max_length=120)
    char_count: int = Field(gt=0, le=500_000)
    usage_kind: str = Field(min_length=1)
    request_id: Optional[str] = Field(default=None, max_length=200)
    note: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    return parts[1].strip()


def _get_user_from_jwt(jwt_token: str) -> Dict[str, Any]:
    try:
        response = supabase.auth.get_user(jwt_token)
        user = getattr(response, "user", None)
        if not user or not getattr(user, "id", None):
            raise HTTPException(status_code=401, detail="User not found from token")
        return {"id": str(user.id), "email": getattr(user, "email", None)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"JWT doğrulama hatası: {exc}")


def _normalize_module(module: str) -> str:
    value = str(module or "").strip().lower()
    return {"practic_ai": "practice_ai", "practice": "practice_ai", "practiceai": "practice_ai"}.get(value, value)


def _normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value not in {"text", "voice", "text_in", "text_out", "voice_out"}:
        raise HTTPException(status_code=400, detail="invalid usage_kind")
    return value


def _is_translation_module(module: str) -> bool:
    return module in TRANSLATION_MODULES or any(hint in module for hint in TRANSLATION_HINTS)


def _requires_legacy_billing(module: str, kind: str) -> bool:
    if module in FREE_MODULES:
        return False
    if kind in {"text", "text_in", "text_out"}:
        return module in PAID_TEXT_MODULES
    if kind in {"voice", "voice_out"}:
        return module in PAID_VOICE_MODULES
    return False


def _usage_type_for(kind: str) -> str:
    return "voice_tts" if kind in {"voice", "voice_out"} else "ai_text"


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _shared_wallet_spend(jwt_token: str, amount: int, module: str, request_id: str, note: str = "") -> Dict[str, Any]:
    body = json.dumps({
        "amount": int(amount),
        "module": module,
        "requestId": request_id,
        "note": note,
    }).encode("utf-8")
    req = Request(
        ICANY_PERSONAL_SPEND_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            data = {}
        if exc.code == 402 or data.get("code") == "INSUFFICIENT_TOKENS":
            raise HTTPException(status_code=402, detail={
                "code": "INSUFFICIENT_TOKENS",
                "message": data.get("error") or "Jeton yetersiz.",
                "required_tokens": amount,
                "tokens_after": data.get("tokenBalance", 0),
            })
        raise HTTPException(status_code=502, detail=data.get("error") or "Ortak cüzdan yanıt vermedi")
    except (URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Ortak cüzdana ulaşılamadı: {exc}")
    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=data.get("error") or "Ortak cüzdan işlemi başarısız")
    return data


def _translation_access(user_id: str, module: str, request_id: str, jwt_token: str) -> Dict[str, Any]:
    previous = (
        supabase.table("translation_access_requests")
        .select("result")
        .eq("user_id", user_id)
        .eq("request_key", request_id)
        .limit(1)
        .execute()
    )
    rows = getattr(previous, "data", None) or []
    if rows and isinstance(rows[0].get("result"), dict):
        return {**rows[0]["result"], "idempotent_replay": True}

    state_res = (
        supabase.table("translation_access_state")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    state_rows = getattr(state_res, "data", None) or []
    now = datetime.now(timezone.utc)

    if not state_rows:
        trial_end = now + timedelta(days=7)
        state = {
            "user_id": user_id,
            "trial_started_at": now.isoformat(),
            "trial_ends_at": trial_end.isoformat(),
            "updated_at": now.isoformat(),
        }
        supabase.table("translation_access_state").insert(state).execute()
        result = {
            "ok": True, "charged": False, "access_mode": "trial_started", "tokens_charged": 0,
            "trial_started_at": state["trial_started_at"], "trial_ends_at": state["trial_ends_at"],
            "access_ends_at": state["trial_ends_at"], "module": module, "request_key": request_id,
        }
    else:
        state = state_rows[0]
        trial_end = _parse_time(state.get("trial_ends_at"))
        paid_end = _parse_time(state.get("paid_access_ends_at"))
        if trial_end and now < trial_end:
            result = {
                "ok": True, "charged": False, "access_mode": "trial_active", "tokens_charged": 0,
                "trial_started_at": state.get("trial_started_at"), "trial_ends_at": state.get("trial_ends_at"),
                "access_ends_at": state.get("trial_ends_at"), "module": module, "request_key": request_id,
            }
        elif paid_end and now < paid_end:
            result = {
                "ok": True, "charged": False, "access_mode": "paid_active", "tokens_charged": 0,
                "paid_access_started_at": state.get("paid_access_started_at"),
                "paid_access_ends_at": state.get("paid_access_ends_at"),
                "access_ends_at": state.get("paid_access_ends_at"), "module": module, "request_key": request_id,
            }
        else:
            wallet = _shared_wallet_spend(jwt_token, 5, "translation_day", request_id, "Tüm çeviri modülleri - 24 saat")
            paid_end = now + timedelta(hours=24)
            supabase.table("translation_access_state").update({
                "paid_access_started_at": now.isoformat(),
                "paid_access_ends_at": paid_end.isoformat(),
                "updated_at": now.isoformat(),
            }).eq("user_id", user_id).execute()
            result = {
                "ok": True, "charged": True, "access_mode": "paid_started", "tokens_charged": 5,
                "tokens_before": wallet.get("tokensBefore"), "tokens_after": wallet.get("tokensAfter"),
                "paid_access_started_at": now.isoformat(), "paid_access_ends_at": paid_end.isoformat(),
                "access_ends_at": paid_end.isoformat(), "module": module, "request_key": request_id,
                "wallet": "icany_personal",
            }

    supabase.table("translation_access_requests").insert({
        "user_id": user_id,
        "request_key": request_id,
        "module": module,
        "access_mode": result.get("access_mode", "unknown"),
        "tokens_charged": int(result.get("tokens_charged") or 0),
        "access_ends_at": result.get("access_ends_at"),
        "result": result,
    }).execute()
    return result


@router.post("/api/usage/commit")
async def usage_commit(req: UsageBillingReq, authorization: Optional[str] = Header(default=None)):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)
    user_id = user["id"]

    supplied_user_id = str(req.user_id or "").strip()
    if supplied_user_id and supplied_user_id != user_id:
        raise HTTPException(status_code=403, detail="user_id does not match authenticated user")

    module = _normalize_module(req.module)
    usage_kind = _normalize_kind(req.usage_kind)
    char_count = int(req.char_count or 0)
    request_id = str(req.request_id or "").strip() or str(uuid4())

    if _is_translation_module(module):
        data = _translation_access(user_id, module, request_id, jwt_token)
        return {
            **data,
            "ok": True,
            "module": module,
            "usage_kind": usage_kind,
            "char_count": char_count,
            "request_id": request_id,
            "billing_model": "translation_daily_access_shared_wallet_v2",
            "daily_access": True,
            "jetons_spent": int(data.get("tokens_charged") or 0),
            "free_only": int(data.get("tokens_charged") or 0) == 0,
        }

    if not _requires_legacy_billing(module, usage_kind):
        return {
            "ok": True, "module": module, "usage_kind": usage_kind, "char_count": char_count,
            "tokens_before": None, "tokens_after": None, "tokens_charged": 0, "jetons_spent": 0,
            "free_only": True, "chars_per_jeton": CHARS_PER_JETON, "billing_model": "free_or_unmetered",
        }

    usage_type = _usage_type_for(usage_kind)
    result = spend_chars(
        user_id=user_id,
        used_chars=char_count,
        usage_type=usage_type,
        jwt_token=jwt_token,
        request_id=request_id,
        extra_meta={
            "original_module": module,
            "usage_kind": usage_kind,
            "note": req.note or "",
            **(req.meta or {}),
        },
    )
    charged = int(result.get("charged_tokens") or 0)
    return {
        "ok": True, "module": module, "engine_module": usage_type, "usage_kind": usage_kind,
        "char_count": char_count, "tokens_before": result.get("tokens_before"),
        "tokens_after": result.get("tokens_after"), "tokens_charged": charged,
        "jetons_spent": charged, "chars_per_jeton": result.get("chars_per_jeton", CHARS_PER_JETON),
        "free_only": False, "billing_model": "shared_personal_wallet_v2", "wallet": "icany_personal",
    }


@router.get("/api/usage/translation-status")
def translation_status(authorization: Optional[str] = Header(default=None)):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)
    state_res = (
        supabase.table("translation_access_state")
        .select("*")
        .eq("user_id", user["id"])
        .limit(1)
        .execute()
    )
    rows = getattr(state_res, "data", None) or []
    now = datetime.now(timezone.utc)
    if not rows:
        return {"ok": True, "access_mode": "not_started", "access_active": False, "daily_cost": 5}
    state = rows[0]
    trial_end = _parse_time(state.get("trial_ends_at"))
    paid_end = _parse_time(state.get("paid_access_ends_at"))
    if trial_end and now < trial_end:
        mode, end = "trial_active", trial_end
    elif paid_end and now < paid_end:
        mode, end = "paid_active", paid_end
    else:
        mode, end = "payment_required", None
    return {
        "ok": True,
        "access_mode": mode,
        "access_active": end is not None,
        "access_ends_at": end.isoformat() if end else None,
        "trial_started_at": state.get("trial_started_at"),
        "trial_ends_at": state.get("trial_ends_at"),
        "paid_access_started_at": state.get("paid_access_started_at"),
        "paid_access_ends_at": state.get("paid_access_ends_at"),
        "daily_cost": 5,
        "wallet": "icany_personal",
    }
