from __future__ import annotations

import math
import os
from typing import Any, Dict, Optional

from fastapi import HTTPException
from supabase import Client, create_client

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Tek sabit kural:
# AI metin = 1000 karakter / 1 jeton
# Ses/TTS   = 1000 karakter / 1 jeton
CHARS_PER_JETON = 1000

# İleride farklı tarifeler istersek tek yerden açarız.
VALID_USAGE_TYPES = {
    "ai_text",
    "voice_tts",
    "general",
}


def _get_profile_or_404(user_id: str) -> Dict[str, Any]:
    prof = (
        supabase.table("profiles")
        .select("id,tokens")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    rows = getattr(prof, "data", None) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")

    return rows[0] or {}


def _reason_for(usage_type: str) -> str:
    if usage_type == "voice_tts":
        return f"Ses kullanımı ({CHARS_PER_JETON} karakter = 1 jeton)"
    if usage_type == "ai_text":
        return f"AI metin kullanımı ({CHARS_PER_JETON} karakter = 1 jeton)"
    return f"Kullanım kesintisi ({CHARS_PER_JETON} karakter = 1 jeton)"


def _wallet_tx_type_for(usage_type: str) -> str:
    if usage_type == "voice_tts":
        return "usage_voice"
    if usage_type == "ai_text":
        return "usage_text"
    return "usage_general"


def _insert_wallet_tx(
    user_id: str,
    tx_type: str,
    amount: int,
    reason: str,
    meta: Dict[str, Any],
) -> None:
    supabase.table("wallet_tx").insert(
        {
            "user_id": user_id,
            "type": tx_type,
            "amount": amount,
            "reason": reason,
            "meta": meta,
        }
    ).execute()


def calc_tokens_for_chars(used_chars: int) -> int:
    used_chars = int(used_chars or 0)
    if used_chars <= 0:
        return 0
    return math.ceil(used_chars / CHARS_PER_JETON)


def spend_chars(
    user_id: str,
    used_chars: int,
    usage_type: str = "general",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if usage_type not in VALID_USAGE_TYPES:
        raise HTTPException(status_code=400, detail="invalid usage_type")

    used_chars = int(used_chars or 0)
    if used_chars <= 0:
        return {
            "ok": True,
            "charged_tokens": 0,
            "module": usage_type,
            "tokens_before": None,
            "tokens_after": None,
            "chars_per_jeton": CHARS_PER_JETON,
        }

    extra_meta = extra_meta or {}

    row = _get_profile_or_404(user_id)

    tokens_before = int(row.get("tokens") or 0)
    charged_tokens = calc_tokens_for_chars(used_chars)
    tokens_after = tokens_before - charged_tokens

    if tokens_after < 0:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    (
        supabase.table("profiles")
        .update({
            "tokens": tokens_after,
        })
        .eq("id", user_id)
        .execute()
    )

    if charged_tokens > 0:
        tx_type = _wallet_tx_type_for(usage_type)
        reason = _reason_for(usage_type)
        temp_balance = tokens_before

        for idx in range(charged_tokens):
            temp_balance -= 1
            _insert_wallet_tx(
                user_id=user_id,
                tx_type=tx_type,
                amount=-1,
                reason=reason,
                meta={
                    "usage_type": usage_type,
                    "used_chars": used_chars,
                    "charge_type": "per_request_ceil_1000",
                    "step_index": idx + 1,
                    "charged_tokens_total": charged_tokens,
                    "chars_per_jeton": CHARS_PER_JETON,
                    "balance_after": temp_balance,
                    **extra_meta,
                },
            )

    return {
        "ok": True,
        "charged_tokens": charged_tokens,
        "module": usage_type,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "chars_per_jeton": CHARS_PER_JETON,
    }
