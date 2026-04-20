from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routers.token_engine import spend_chars, CHARS_PER_JETON

router = APIRouter(tags=["usage-billing"])

PAID_MODULES = {
    "text_ai",
    "facetoface_ai",
    "eartoear_ai",
    "practice_ai",
    "text_translate_paid",
    "culture_translate",
    "voice_clone",
    "voice_clone_preview",
    "voice_ai",
    "voice_preset_use",
    "voice_live",
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


def _requires_billing(module: str) -> bool:
    if module in FREE_MODULES:
        return False
    return module in PAID_MODULES


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

    if not _requires_billing(module):
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

    result = spend_chars(
        user_id=user_id,
        module_key="usage_general",
        used_chars=char_count,
    )

    return {
        "ok": True,
        "module": module,
        "engine_module": "usage_general",
        "usage_kind": usage_kind,
        "char_count": char_count,
        "tokens_before": result.get("tokens_before"),
        "tokens_after": result.get("tokens_after"),
        "tokens_charged": result.get("charged_tokens", 0),
        "counter_after": result.get("used_chars_total"),
        "chars_per_jeton": result.get("chars_per_jeton", CHARS_PER_JETON),
        "free_only": False,
    }
