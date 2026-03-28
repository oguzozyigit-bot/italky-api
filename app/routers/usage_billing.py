from __future__ import annotations

import math
import os
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import Client, create_client

router = APIRouter(tags=["usage-billing"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

FREE_STANDARD_LIMIT = 10_000
CHARS_PER_TOKEN = 1_000

STANDARD_FREE_MODULES = {
    "text_standard",
    "facetoface_standard",
    "eartoear_standard",
}

ALWAYS_PAID_MODULES = {
    "text_ai",
    "facetoface_ai",
    "eartoear_ai",
    "practic_ai",
}

UsageMode = Literal["standard", "ai"]


class UsageBillingReq(BaseModel):
    user_id: str = Field(min_length=1)
    module: str = Field(min_length=1)
    char_count: int = Field(gt=0, le=500_000)
    mode: UsageMode
    note: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class UsageBillingPreview(BaseModel):
    ok: bool
    module: str
    mode: UsageMode
    char_count: int
    free_applied_chars: int
    paid_chars: int
    tokens_to_charge: int
    tokens_before: int
    tokens_after: int
    standard_char_used_before: int
    standard_char_used_after: int
    free_limit: int
    chars_per_token: int
    reason: str


def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _get_profile_or_404(user_id: str) -> Dict[str, Any]:
    res = (
        supabase.table("profiles")
        .select("id,tokens,standard_char_used,blocked,package_active,package_ends_at")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(res) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")
    return rows[0] or {}


def _ceil_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return int(math.ceil(chars / CHARS_PER_TOKEN))


def _normalize_module(module: str, mode: UsageMode) -> str:
    raw = str(module or "").strip().lower()

    aliases = {
        "text": "text_standard" if mode == "standard" else "text_ai",
        "texttotext": "text_standard" if mode == "standard" else "text_ai",
        "facetoface": "facetoface_standard" if mode == "standard" else "facetoface_ai",
        "face_to_face": "facetoface_standard" if mode == "standard" else "facetoface_ai",
        "f2f": "facetoface_standard" if mode == "standard" else "facetoface_ai",
        "sidetoside": "eartoear_standard" if mode == "standard" else "eartoear_ai",
        "eartoear": "eartoear_standard" if mode == "standard" else "eartoear_ai",
        "ear_to_ear": "eartoear_standard" if mode == "standard" else "eartoear_ai",
        "practic": "practic_ai",
        "practice": "practic_ai",
    }

    return aliases.get(raw, raw)


def _reason_for(module: str, mode: UsageMode, char_count: int, free_chars: int, paid_chars: int) -> str:
    labels = {
        "text_standard": "Standart TextToText",
        "text_ai": "AI / Kültürel TextToText",
        "facetoface_standard": "Standart FaceToFace",
        "facetoface_ai": "AI / Özel Ses FaceToFace",
        "eartoear_standard": "Standart EarToEar",
        "eartoear_ai": "AI / Özel Ses EarToEar",
        "practic_ai": "Practic AI Sohbet",
    }
    base = labels.get(module, module)

    if mode == "standard":
        if paid_chars <= 0:
            return f"{base} ücretsiz kullanım ({char_count} karakter)"
        return f"{base} ücretsiz limit aşıldı ({free_chars} ücretsiz, {paid_chars} ücretli karakter)"

    return f"{base} jetonlu kullanım ({char_count} karakter)"


def _preview_usage(profile: Dict[str, Any], req: UsageBillingReq) -> UsageBillingPreview:
    module = _normalize_module(req.module, req.mode)
    char_count = int(req.char_count)
    tokens_before = int(profile.get("tokens") or 0)
    std_before = int(profile.get("standard_char_used") or 0)

    if req.mode == "standard":
        if module not in STANDARD_FREE_MODULES:
            raise HTTPException(
                status_code=400,
                detail=f"invalid standard module: {module}"
            )

        remaining_free = max(0, FREE_STANDARD_LIMIT - std_before)
        free_applied = min(char_count, remaining_free)
        paid_chars = max(0, char_count - free_applied)
        tokens_to_charge = _ceil_tokens(paid_chars)
        std_after = std_before + char_count
    else:
        if module not in ALWAYS_PAID_MODULES:
            raise HTTPException(
                status_code=400,
                detail=f"invalid ai module: {module}"
            )

        free_applied = 0
        paid_chars = char_count
        tokens_to_charge = _ceil_tokens(char_count)
        std_after = std_before

    tokens_after = tokens_before - tokens_to_charge
    if tokens_after < 0:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "INSUFFICIENT_TOKENS",
                "tokens_before": tokens_before,
                "tokens_needed": tokens_to_charge,
                "tokens_after": tokens_after,
            },
        )

    return UsageBillingPreview(
        ok=True,
        module=module,
        mode=req.mode,
        char_count=char_count,
        free_applied_chars=free_applied,
        paid_chars=paid_chars,
        tokens_to_charge=tokens_to_charge,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        standard_char_used_before=std_before,
        standard_char_used_after=std_after,
        free_limit=FREE_STANDARD_LIMIT,
        chars_per_token=CHARS_PER_TOKEN,
        reason=_reason_for(module, req.mode, char_count, free_applied, paid_chars),
    )


def _wallet_type_for(module: str) -> str:
    mapping = {
        "text_standard": "usage_text",
        "text_ai": "usage_text",
        "facetoface_standard": "usage_face_to_face",
        "facetoface_ai": "usage_face_to_face",
        "eartoear_standard": "usage_side_to_side",
        "eartoear_ai": "usage_side_to_side",
        "practic_ai": "usage_teacher",
    }
    return mapping.get(module, "usage_text")


def _apply_standard_char_usage(user_id: str, new_value: int):
    supabase.table("profiles").update(
        {"standard_char_used": int(new_value)}
    ).eq("id", user_id).execute()


def _apply_wallet(user_id: str, wallet_type: str, amount: int, reason: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    rpc_res = supabase.rpc(
        "apply_wallet_tx",
        {
            "p_user_id": user_id,
            "p_type": wallet_type,
            "p_amount": amount,
            "p_reason": reason,
            "p_meta": meta,
        }
    ).execute()

    data = _safe_data(rpc_res)
    if not data:
        raise HTTPException(status_code=500, detail="wallet rpc failed")
    return data


@router.post("/api/usage/preview")
async def usage_preview(req: UsageBillingReq):
    user_id = str(req.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    profile = _get_profile_or_404(user_id)

    if bool(profile.get("blocked")):
        raise HTTPException(status_code=403, detail="user blocked")

    preview = _preview_usage(profile, req)
    return preview.model_dump()


@router.post("/api/usage/commit")
async def usage_commit(req: UsageBillingReq):
    user_id = str(req.user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    profile = _get_profile_or_404(user_id)

    if bool(profile.get("blocked")):
        raise HTTPException(status_code=403, detail="user blocked")

    preview = _preview_usage(profile, req)

    # standard ücretsiz kullanım sayacı her zaman kullanıcı bazlı ilerler
    if req.mode == "standard":
        _apply_standard_char_usage(user_id, preview.standard_char_used_after)

    wallet_meta = {
        "module": preview.module,
        "mode": preview.mode,
        "char_count": preview.char_count,
        "free_applied_chars": preview.free_applied_chars,
        "paid_chars": preview.paid_chars,
        "free_limit": preview.free_limit,
        "chars_per_token": preview.chars_per_token,
        **(req.meta or {}),
    }

    wallet_result: Optional[Dict[str, Any]] = None

    if preview.tokens_to_charge > 0:
        wallet_result = _apply_wallet(
            user_id=user_id,
            wallet_type=_wallet_type_for(preview.module),
            amount=-preview.tokens_to_charge,
            reason=req.note or preview.reason,
            meta=wallet_meta,
        )
    else:
        # ücretsiz kullanımda da kayıt düşelim ki geçmiş net olsun
        try:
            supabase.table("wallet_tx").insert(
                {
                    "user_id": user_id,
                    "type": _wallet_type_for(preview.module),
                    "amount": 0,
                    "reason": req.note or preview.reason,
                    "meta": {
                        **wallet_meta,
                        "balance_after": preview.tokens_before,
                        "free_only": True,
                    },
                }
            ).execute()
        except Exception:
            # amount=0 constraint varsa ücretsiz log zorunlu değil
            pass

    latest_profile = _get_profile_or_404(user_id)

    return {
        "ok": True,
        "module": preview.module,
        "mode": preview.mode,
        "char_count": preview.char_count,
        "free_applied_chars": preview.free_applied_chars,
        "paid_chars": preview.paid_chars,
        "tokens_charged": preview.tokens_to_charge,
        "tokens_before": preview.tokens_before,
        "tokens_after": int(latest_profile.get("tokens") or 0),
        "standard_char_used_before": preview.standard_char_used_before,
        "standard_char_used_after": int(latest_profile.get("standard_char_used") or 0),
        "wallet_result": wallet_result,
        "reason": req.note or preview.reason,
    }
