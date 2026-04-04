from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(tags=["usage-billing"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

DEFAULT_CHARS_PER_JETON = 3000
PRACTICE_CHARS_PER_JETON = 1500

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

FREE_VOICE_MODULES = {
    "voice_preset_preview",
}


class UsageBillingReq(BaseModel):
    user_id: str = Field(min_length=1)
    module: str = Field(min_length=1)
    char_count: int = Field(gt=0, le=500_000)
    usage_kind: str = Field(min_length=1)  # text | voice | text_in | text_out | voice_out
    note: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    chars_per_jeton: Optional[int] = None


def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _get_profile_or_404(user_id: str) -> Dict[str, Any]:
    res = (
        supabase.table("profiles")
        .select(
            "id,tokens,blocked,"
            "char_text_remaining,"
            "char_voice_remaining"
        )
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(res) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")
    return rows[0] or {}


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


def _counter_field_for_kind(kind: str) -> str:
    if kind in {"text", "text_in", "text_out"}:
        return "char_text_remaining"
    return "char_voice_remaining"


def _wallet_type_for(module: str, kind: str) -> str:
    if module == "practice_ai":
        if kind == "text_in":
            return "usage_teacher"
        if kind == "text_out":
            return "usage_teacher"
        if kind in {"voice", "voice_out"}:
            return "usage_teacher"
        return "usage_teacher"

    if kind in {"voice", "voice_out"}:
        return "usage_voice"

    mapping = {
        "text_ai": "usage_text",
        "facetoface_ai": "usage_face_to_face",
        "eartoear_ai": "usage_side_to_side",
        "text_translate_paid": "usage_text",
        "culture_translate": "usage_text",
    }
    return mapping.get(module, "usage_text")


def _requires_billing(module: str, kind: str) -> bool:
    if module in FREE_VOICE_MODULES:
        return False

    if kind in {"text", "text_in", "text_out"}:
        return module in PAID_TEXT_MODULES

    if kind in {"voice", "voice_out"}:
        return module in PAID_VOICE_MODULES

    return False


def _ensure_counter_field_exists(profile: Dict[str, Any], field_name: str) -> int:
    value = profile.get(field_name)
    try:
        return int(value or 0)
    except Exception:
        return 0


def _insert_wallet_tx(user_id: str, tx_type: str, amount: int, reason: str, meta: Dict[str, Any]):
    return supabase.table("wallet_tx").insert(
        {
            "user_id": user_id,
            "type": tx_type,
            "amount": amount,
            "reason": reason,
            "meta": meta,
        }
    ).execute()


def _update_profile_fields(user_id: str, payload: Dict[str, Any]):
    return supabase.table("profiles").update(payload).eq("id", user_id).execute()


def _reason_for(module: str, kind: str) -> str:
    if module == "practice_ai":
        if kind == "text_in":
            return "Practice AI • Öğrenci konuşması"
        if kind == "text_out":
            return "Practice AI • Öğretmen cevabı"
        if kind in {"voice", "voice_out"}:
            return "Practice AI • Öğretmen sesi"
        return "Practice AI"

    if module == "voice_clone_preview":
        return "Kendi Sesim Önizleme"
    if module == "voice_preset_use":
        return "Özel Ses"
    if module == "voice_clone":
        return "Kendi Sesim"
    if module == "text_ai":
        return "Kültürel Translate"
    if module == "facetoface_ai":
        return "FaceToFace"
    if module == "eartoear_ai":
        return "SideToSide"

    if kind in {"voice", "voice_out"}:
        return "Ses Kullanımı"

    return "Jeton Kullanımı"


def _resolve_chars_per_jeton(module: str, req_value: Optional[int]) -> int:
    if req_value and int(req_value) > 0:
        return int(req_value)

    if module == "practice_ai":
        return PRACTICE_CHARS_PER_JETON

    return DEFAULT_CHARS_PER_JETON


@router.post("/api/usage/commit")
async def usage_commit(req: UsageBillingReq):
    user_id = str(req.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    module = _normalize_module(req.module)
    usage_kind = _normalize_kind(req.usage_kind)
    char_count = int(req.char_count or 0)
    chars_per_jeton = _resolve_chars_per_jeton(module, req.chars_per_jeton)

    if char_count <= 0:
        raise HTTPException(status_code=422, detail="char_count required")

    profile = _get_profile_or_404(user_id)

    if bool(profile.get("blocked")):
        raise HTTPException(status_code=403, detail="user blocked")

    if not _requires_billing(module, usage_kind):
        try:
            _insert_wallet_tx(
                user_id=user_id,
                tx_type=_wallet_type_for(module, usage_kind),
                amount=0,
                reason=req.note or _reason_for(module, usage_kind),
                meta={
                    "module": module,
                    "usage_kind": usage_kind,
                    "char_count": char_count,
                    "free_only": True,
                    **(req.meta or {}),
                },
            )
        except Exception:
            pass

        return {
            "ok": True,
            "module": module,
            "usage_kind": usage_kind,
            "char_count": char_count,
            "tokens_charged": 0,
            "free_only": True,
        }

    counter_field = _counter_field_for_kind(usage_kind)
    consumed_chars = _ensure_counter_field_exists(profile, counter_field)
    tokens_before = int(profile.get("tokens") or 0)

    charged_tokens = 0

    if consumed_chars == 0:
        charged_tokens += 1

    old_step = consumed_chars // chars_per_jeton
    new_total = consumed_chars + char_count
    new_step = new_total // chars_per_jeton

    if new_step > old_step:
        charged_tokens += (new_step - old_step)

    tokens_after = tokens_before - charged_tokens
    if tokens_after < 0:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "INSUFFICIENT_TOKENS",
                "tokens_before": tokens_before,
                "tokens_needed": charged_tokens,
                "tokens_after": tokens_after,
            },
        )

    _update_profile_fields(
        user_id,
        {
            "tokens": tokens_after,
            counter_field: new_total,
        },
    )

    if charged_tokens > 0:
        temp_balance = tokens_before
        first_consumed = consumed_chars == 0
        old_step2 = consumed_chars // chars_per_jeton
        new_step2 = new_total // chars_per_jeton

        if first_consumed:
            temp_balance -= 1
            _insert_wallet_tx(
                user_id=user_id,
                tx_type=_wallet_type_for(module, usage_kind),
                amount=-1,
                reason=req.note or _reason_for(module, usage_kind),
                meta={
                    "module": module,
                    "usage_kind": usage_kind,
                    "char_count": char_count,
                    "charge_type": "startup",
                    "balance_after": temp_balance,
                    **(req.meta or {}),
                },
            )

        if new_step2 > old_step2:
            for step_index in range(new_step2 - old_step2):
                temp_balance -= 1
                _insert_wallet_tx(
                    user_id=user_id,
                    tx_type=_wallet_type_for(module, usage_kind),
                    amount=-1,
                    reason=req.note or _reason_for(module, usage_kind),
                    meta={
                        "module": module,
                        "usage_kind": usage_kind,
                        "char_count": char_count,
                        "charge_type": "step",
                        "step_index": step_index + 1,
                        "chars_per_jeton": chars_per_jeton,
                        "balance_after": temp_balance,
                        **(req.meta or {}),
                    },
                )

    return {
        "ok": True,
        "module": module,
        "usage_kind": usage_kind,
        "char_count": char_count,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_charged": charged_tokens,
        "counter_field": counter_field,
        "counter_before": consumed_chars,
        "counter_after": new_total,
        "chars_per_jeton": chars_per_jeton,
    }
