from __future__ import annotations

import os
from fastapi import HTTPException
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Yeni sabit kural
CHARS_PER_JETON = 3000

MODULE_COUNTER_MAP = {
    "text": "char_text_remaining",
    "face_clone": "char_face_clone_remaining",
    "interpreter": "char_interpreter_remaining",
    "interpreter_clone": "char_interpreter_clone_remaining",
    "meeting": "char_meeting_remaining",
}

VOICE_MODULE_KEYS = {
    "face_clone",
    "interpreter_clone",
}

TEXT_MODULE_KEYS = {
    "text",
    "interpreter",
    "meeting",
}


def _get_profile_or_404(user_id: str):
    prof = (
        supabase.table("profiles")
        .select(
            "id,tokens,"
            "char_text_remaining,"
            "char_face_clone_remaining,"
            "char_interpreter_remaining,"
            "char_interpreter_clone_remaining,"
            "char_meeting_remaining"
        )
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    rows = getattr(prof, "data", None) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")

    return rows[0] or {}


def _wallet_type_for(module_key: str) -> str:
    if module_key in VOICE_MODULE_KEYS:
        return "usage_voice"

    mapping = {
        "text": "usage_text",
        "interpreter": "usage_side_to_side",
        "meeting": "usage_meeting",
    }
    return mapping.get(module_key, "usage_text")


def _reason_for(module_key: str, startup: bool) -> str:
    if module_key in VOICE_MODULE_KEYS:
        return "Ses kullanımı başlangıç kesintisi" if startup else "Ses kullanımı 3000 karakter kesintisi"

    return "Ücretli çeviri başlangıç kesintisi" if startup else "Ücretli çeviri 3000 karakter kesintisi"


def _insert_wallet_tx(user_id: str, tx_type: str, amount: int, reason: str, meta: dict):
    return supabase.table("wallet_tx").insert(
        {
            "user_id": user_id,
            "type": tx_type,
            "amount": amount,
            "reason": reason,
            "meta": meta,
        }
    ).execute()


def spend_chars(user_id: str, module_key: str, used_chars: int):
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if module_key not in MODULE_COUNTER_MAP:
        raise HTTPException(status_code=400, detail="invalid module_key")

    if used_chars <= 0:
        return {
            "ok": True,
            "charged_tokens": 0,
            "remaining_chars": 0,
            "module": module_key,
        }

    field_name = MODULE_COUNTER_MAP[module_key]
    row = _get_profile_or_404(user_id)

    tokens_before = int(row.get("tokens") or 0)
    consumed_chars = int(row.get(field_name) or 0)
    charged_tokens = 0

    # İlk kullanımda 1 jeton peşin
    if consumed_chars == 0:
        charged_tokens += 1

    old_step = consumed_chars // CHARS_PER_JETON
    new_total = consumed_chars + int(used_chars)
    new_step = new_total // CHARS_PER_JETON

    if new_step > old_step:
        charged_tokens += (new_step - old_step)

    tokens_after = tokens_before - charged_tokens
    if tokens_after < 0:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    (
        supabase.table("profiles")
        .update({
            "tokens": tokens_after,
            field_name: new_total
        })
        .eq("id", user_id)
        .execute()
    )

    # Hareket yaz
    if charged_tokens > 0:
        tx_type = _wallet_type_for(module_key)
        temp_balance = tokens_before

        if consumed_chars == 0:
            temp_balance -= 1
            _insert_wallet_tx(
                user_id=user_id,
                tx_type=tx_type,
                amount=-1,
                reason=_reason_for(module_key, True),
                meta={
                    "module": module_key,
                    "used_chars": used_chars,
                    "charge_type": "startup",
                    "balance_after": temp_balance,
                },
            )

        if new_step > old_step:
            for idx in range(new_step - old_step):
                temp_balance -= 1
                _insert_wallet_tx(
                    user_id=user_id,
                    tx_type=tx_type,
                    amount=-1,
                    reason=_reason_for(module_key, False),
                    meta={
                        "module": module_key,
                        "used_chars": used_chars,
                        "charge_type": "3000_step",
                        "step_index": idx + 1,
                        "balance_after": temp_balance,
                    },
                )

    return {
        "ok": True,
        "charged_tokens": charged_tokens,
        "remaining_chars": new_total,
        "module": module_key,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "chars_per_jeton": CHARS_PER_JETON,
    }
