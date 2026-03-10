from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client
import os
import math

router = APIRouter(tags=["usage-billing"])

SUPABASE_URL = os.getenv("SUPABASE_URL","").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY","").strip()

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# 1 kontör = 4000 karakter
CHAR_PER_TOKEN = 4000


class UsageReq(BaseModel):
    user_id: str
    module: str
    characters: int


@router.post("/api/billing/usage")
async def charge_usage(req: UsageReq):

    user_id = req.user_id.strip()
    chars = int(req.characters)
    module = req.module.strip()

    if chars <= 0:
        raise HTTPException(422,"characters invalid")

    # kaç kontör
    tokens_needed = math.ceil(chars / CHAR_PER_TOKEN)

    prof = (
        supabase.table("profiles")
        .select("tokens")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(404,"profile not found")

    tokens = int(prof.data[0]["tokens"] or 0)

    if tokens < tokens_needed:
        raise HTTPException(402,"not enough tokens")

    new_tokens = tokens - tokens_needed

    supabase.table("profiles").update({
        "tokens": new_tokens
    }).eq("id",user_id).execute()

    supabase.table("usage_logs").insert({
        "user_id": user_id,
        "module": module,
        "characters": chars,
        "tokens_used": tokens_needed
    }).execute()

    return {
        "ok": True,
        "tokens_used": tokens_needed,
        "remaining": new_tokens
    }
