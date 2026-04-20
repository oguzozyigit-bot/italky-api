from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routers.token_engine import spend_chars, CHARS_PER_JETON

router = APIRouter(tags=["usage-billing"])

PAID_TEXT_MODULES = {
    "text_ai",
    "facetoface_ai",
    "eartoear_ai",
    "practice_ai",
    "text_translate_paid",
    "culture_translate",
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
    "offline_translate",
    "offline_mode",
    "interpreter",
    "interpreter_live",
    "interpreter_qr",
}


class UsageBillingReq(BaseModel):
    user_id: str = Field(min_length=1)
    module: str = Field(min_length=1)
    char_count: int = Field(gt=0, le=500_000)
    usage_kind: str = Field(min_length=1)  # text | voice | text_in | text_out | voice_out
    note: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


def _normalize_module(module: str) -> str:
    value = str(module or "").strip().lower()
    if value == "practic_ai":
        return "practice_ai"
    if value in {"practice", "practiceai"}:
        return "practice_ai"
    return value


def _normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value not in {"text", "voice", "text_in", "text_out", "voice_out"}:
        raise HTTPException(status_code=400, detail="invalid usage_kind")
    return value


def _requires_billing(module: str, kind: str) -> bool:
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


@router.post("/api/usage/commit")
async def usage_commit(req: UsageBillingReq):
    user_id = str(req.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    module = _normalize_module(req.module)
    usage_kind = _normalize_kind(req.usage_kind)
    char_count = int(req.char_count or 0)

    if char_count <= 0:
        raise HTTPException(status_code=422, detail="char_count required")

    if not _requires_billing(module, usage_kind):
        return {
            "ok": True,
            "module": module,
            "usage_kind": usage_kind,
            "char_count": char_count,
            "tokens_before": None,
            "tokens_after": None,
            "tokens_charged": 0,
            "free_only": True,
            "chars_per_jeton": CHARS_PER_JETON,
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

    return {
        "ok": True,
        "module": module,
        "engine_module": usage_type,
        "usage_kind": usage_kind,
        "char_count": char_count,
        "tokens_before": result.get("tokens_before"),
        "tokens_after": result.get("tokens_after"),
        "tokens_charged": result.get("charged_tokens", 0),
        "chars_per_jeton": result.get("chars_per_jeton", CHARS_PER_JETON),
        "free_only": False,
    }
