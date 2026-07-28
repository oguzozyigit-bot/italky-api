from __future__ import annotations

import os
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

from app.routers.token_engine import CHARS_PER_JETON, spend_chars

router = APIRouter(tags=["usage-billing"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Yeni bireysel fiyatlandırma:
# - İlk başarılı çeviri kullanımıyla 7 günlük deneme başlar.
# - Deneme sonrasında ilk başarılı kullanım 5 jeton karşılığında 24 saat açar.
# - Aynı 24 saat içindeki diğer çeviri modülleri yeniden ücretlenmez.
TRANSLATION_MODULES = {
    "text_ai",
    "text_translate",
    "text_translate_paid",
    "document_translate",
    "document_ai",
    "photo_translate",
    "photo_ai",
    "ocr_translate",
    "facetoface_ai",
    "usage_face_to_face",
    "face_to_face",
    "facetoface",
    "eartoear_ai",
    "ear_to_ear",
    "side_to_side",
    "sidetoside",
    "culture_translate",
    "cultural_translate",
    "italky_call",
    "two_phone",
    "interpreter",
    "interpreter_live",
    "interpreter_qr",
    "regional_translate",
    "geographic_translate",
}

TRANSLATION_HINTS = (
    "translate",
    "translation",
    "facetoface",
    "face_to_face",
    "eartoear",
    "ear_to_ear",
    "side_to_side",
    "sidetoside",
    "document",
    "photo",
    "ocr",
    "interpreter",
    "italky_call",
    "two_phone",
    "culture",
    "cultural",
)

PAID_TEXT_MODULES = {
    "chat_ai",
    "practice_ai",
}

PAID_VOICE_MODULES = {
    "voice_clone",
    "voice_clone_preview",
    "voice_ai",
    "voice_preset_use",
    "voice_live",
    "practice_ai",
}

FREE_MODULES = {
    "voice_preset_preview",
    "offline",
    "offline_mode",
}


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
        return {
            "id": str(user.id),
            "email": getattr(user, "email", None),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"JWT doğrulama hatası: {exc}")


def _normalize_module(module: str) -> str:
    value = str(module or "").strip().lower()
    aliases = {
        "practic_ai": "practice_ai",
        "practice": "practice_ai",
        "practiceai": "practice_ai",
    }
    return aliases.get(value, value)


def _normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value not in {"text", "voice", "text_in", "text_out", "voice_out"}:
        raise HTTPException(status_code=400, detail="invalid usage_kind")
    return value


def _is_translation_module(module: str) -> bool:
    if module in TRANSLATION_MODULES:
        return True
    return any(hint in module for hint in TRANSLATION_HINTS)


def _requires_legacy_billing(module: str, kind: str) -> bool:
    if module in FREE_MODULES:
        return False
    if kind in {"text", "text_in", "text_out"}:
        return module in PAID_TEXT_MODULES
    if kind in {"voice", "voice_out"}:
        return module in PAID_VOICE_MODULES
    return False


def _usage_type_for(kind: str) -> str:
    if kind in {"voice", "voice_out"}:
        return "voice_tts"
    return "ai_text"


def _normalize_rpc_data(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    raise HTTPException(status_code=500, detail="RPC boş veya geçersiz cevap döndü")


@router.post("/api/usage/commit")
async def usage_commit(
    req: UsageBillingReq,
    authorization: Optional[str] = Header(default=None),
):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)
    user_id = user["id"]

    supplied_user_id = str(req.user_id or "").strip()
    if supplied_user_id and supplied_user_id != user_id:
        raise HTTPException(status_code=403, detail="user_id does not match authenticated user")

    module = _normalize_module(req.module)
    usage_kind = _normalize_kind(req.usage_kind)
    char_count = int(req.char_count or 0)

    if _is_translation_module(module):
        request_id = str(req.request_id or "").strip() or str(uuid4())
        try:
            rpc = supabase.rpc(
                "claim_translation_daily_access",
                {
                    "p_user_id": user_id,
                    "p_request_key": request_id,
                    "p_module": module,
                },
            ).execute()
            data = _normalize_rpc_data(rpc.data)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Translation access error: {exc}")

        if not data.get("ok") and data.get("reason") == "insufficient_tokens":
            raise HTTPException(
                status_code=402,
                detail={
                    **data,
                    "code": "INSUFFICIENT_TOKENS",
                    "message": "Bu çeviri günü için 5 jeton gerekli.",
                },
            )

        return {
            **data,
            "ok": True,
            "module": module,
            "usage_kind": usage_kind,
            "char_count": char_count,
            "request_id": request_id,
            "billing_model": "translation_daily_access_v1",
            "daily_access": True,
            "jetons_spent": int(data.get("tokens_charged") or 0),
            "free_only": int(data.get("tokens_charged") or 0) == 0,
        }

    if not _requires_legacy_billing(module, usage_kind):
        return {
            "ok": True,
            "module": module,
            "usage_kind": usage_kind,
            "char_count": char_count,
            "tokens_before": None,
            "tokens_after": None,
            "tokens_charged": 0,
            "jetons_spent": 0,
            "free_only": True,
            "chars_per_jeton": CHARS_PER_JETON,
            "billing_model": "free_or_unmetered",
        }

    usage_type = _usage_type_for(usage_kind)
    result = spend_chars(
        user_id=user_id,
        used_chars=char_count,
        usage_type=usage_type,
        extra_meta={
            "original_module": module,
            "usage_kind": usage_kind,
            "note": req.note or "",
            **(req.meta or {}),
        },
    )

    charged = int(result.get("charged_tokens") or 0)
    return {
        "ok": True,
        "module": module,
        "engine_module": usage_type,
        "usage_kind": usage_kind,
        "char_count": char_count,
        "tokens_before": result.get("tokens_before"),
        "tokens_after": result.get("tokens_after"),
        "tokens_charged": charged,
        "jetons_spent": charged,
        "chars_per_jeton": result.get("chars_per_jeton", CHARS_PER_JETON),
        "free_only": False,
        "billing_model": "legacy_character_usage",
    }


@router.get("/api/usage/translation-status")
def translation_status(
    authorization: Optional[str] = Header(default=None),
):
    jwt_token = _get_bearer(authorization)
    user = _get_user_from_jwt(jwt_token)

    try:
        rpc = supabase.rpc(
            "get_translation_daily_access_status",
            {"p_user_id": user["id"]},
        ).execute()
        return _normalize_rpc_data(rpc.data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Translation status error: {exc}")
