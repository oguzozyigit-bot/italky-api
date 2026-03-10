from __future__ import annotations

import os

from fastapi import HTTPException
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

MODULE_LIMITS = {
    "face_clone": ("char_face_clone_remaining", 120),
    "interpreter": ("char_interpreter_remaining", 250),
    "interpreter_clone": ("char_interpreter_clone_remaining", 100),
    "text": ("char_text_remaining", 600),
    "meeting": ("char_meeting_remaining", 200),
}


def spend_chars(user_id: str, module_key: str, used_chars: int):
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    if module_key not in MODULE_LIMITS:
        raise HTTPException(status_code=400, detail="invalid module_key")

    if used_chars <= 0:
        return {"ok": True, "charged_tokens": 0, "remaining_chars": 0}

    field_name, refill_amount = MODULE_LIMITS[module_key]

    prof = (
        supabase.table("profiles")
        .select(f"id,tokens,{field_name}")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(status_code=404, detail="profile not found")

    row = prof.data[0]
    tokens = int(row.get("tokens") or 0)
    remaining = int(row.get(field_name) or 0)

    charged = 0
    need = int(used_chars)

    while remaining < need:
        if tokens <= 0:
            raise HTTPException(status_code=402, detail="insufficient_tokens")
        tokens -= 1
        remaining += refill_amount
        charged += 1

    remaining -= need

    update_res = (
        supabase.table("profiles")
        .update({
            "tokens": tokens,
            field_name: remaining
        })
        .eq("id", user_id)
        .execute()
    )

    print("TOKEN ENGINE UPDATE:", update_res)

    return {
        "ok": True,
        "charged_tokens": charged,
        "remaining_chars": remaining,
        "module": module_key,
    }
