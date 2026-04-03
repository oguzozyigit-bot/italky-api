# FILE: italky-api/app/routers/nfc_tokens.py

from __future__ import annotations

import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

router = APIRouter(tags=["nfc-tokens"])

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

sb_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

class RedeemUidReq(BaseModel):
    uid: str = Field(..., min_length=6)
    user_id: str = Field(..., min_length=6)

class RedeemManualReq(BaseModel):
    manual_code: str = Field(..., min_length=6, max_length=6)
    user_id: str = Field(..., min_length=6)

@router.post("/nfc/redeem-token-card")
def redeem_token_card(req: RedeemUidReq):
    try:
        res = sb_admin.rpc("redeem_nfc_token_card", {
            "p_uid": req.uid.strip(),
            "p_user_id": req.user_id.strip()
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rpc_failed: {str(e)}")

    return res.data or {"ok": False, "reason": "EMPTY_RESPONSE"}

@router.post("/nfc/redeem-manual-code")
def redeem_manual_code(req: RedeemManualReq):
    try:
        res = sb_admin.rpc("redeem_nfc_manual_code", {
            "p_manual_code": req.manual_code.strip(),
            "p_user_id": req.user_id.strip()
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rpc_failed: {str(e)}")

    return res.data or {"ok": False, "reason": "EMPTY_RESPONSE"}
