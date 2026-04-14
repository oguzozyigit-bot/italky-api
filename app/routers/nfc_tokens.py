from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

router = APIRouter(tags=["nfc-tokens"])

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

sb_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class RedeemUidReq(BaseModel):
    uid: str = Field(..., min_length=6)
    user_id: str = Field(..., min_length=6)


class RedeemManualReq(BaseModel):
    manual_code: str = Field(..., min_length=6, max_length=6)
    user_id: str = Field(..., min_length=6)


def _to_dict(data: Any) -> dict:
    if isinstance(data, dict):
        return data
    return {}


def _extract_loaded_tokens(data: dict) -> int:
    for key in ("loaded_tokens", "token_amount", "tokens_loaded", "amount", "jetons"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except Exception:
            continue
    return 0


def _extract_tokens_after(data: dict) -> int | None:
    for key in ("tokens_after", "tokens", "balance_after"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except Exception:
            continue
    return None


def _should_skip_wallet_tx(data: dict) -> bool:
    if data.get("already_processed") is True:
        return True
    if data.get("already_redeemed") is True:
        return True
    if str(data.get("reason") or "").strip().lower() in {
        "already_processed",
        "already_redeemed",
        "card_already_used",
        "manual_code_already_used",
    }:
        return True
    return False


def _insert_wallet_credit_tx(
    user_id: str,
    amount: int,
    balance_after: int | None,
    source: str,
    description: str,
    meta: dict | None = None,
):
    if amount <= 0:
        return

    balance_before = None
    if balance_after is not None:
        balance_before = balance_after - amount

    payload = {
        "user_id": user_id,
        "tx_type": "credit",
        "source": source,
        "usage_kind": None,
        "chars_used": 0,
        "jetons": amount,
        "balance_before": balance_before if balance_before is not None else 0,
        "balance_after": balance_after if balance_after is not None else 0,
        "description": description,
        "meta": meta or {},
    }

    sb_admin.table("wallet_tx").insert(payload).execute()


@router.post("/nfc/redeem-token-card")
def redeem_token_card(req: RedeemUidReq):
    try:
        res = sb_admin.rpc(
            "redeem_nfc_token_card",
            {
                "p_uid": req.uid.strip(),
                "p_user_id": req.user_id.strip(),
            },
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rpc_failed: {str(e)}")

    data = _to_dict(res.data) if res.data is not None else {"ok": False, "reason": "EMPTY_RESPONSE"}

    if data.get("ok") is True and not _should_skip_wallet_tx(data):
        loaded_tokens = _extract_loaded_tokens(data)
        tokens_after = _extract_tokens_after(data)

        try:
            _insert_wallet_credit_tx(
                user_id=req.user_id.strip(),
                amount=loaded_tokens,
                balance_after=tokens_after,
                source="nfc_token_load",
                description="NFC kart ile jeton yükleme",
                meta={
                    "uid": req.uid.strip(),
                    "loaded_tokens": loaded_tokens,
                    "tokens_after": tokens_after,
                    "source_type": "nfc",
                },
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"wallet_tx_failed: {str(e)}")

    return data


@router.post("/nfc/redeem-manual-code")
def redeem_manual_code(req: RedeemManualReq):
    try:
        res = sb_admin.rpc(
            "redeem_nfc_manual_code",
            {
                "p_manual_code": req.manual_code.strip(),
                "p_user_id": req.user_id.strip(),
            },
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rpc_failed: {str(e)}")

    data = _to_dict(res.data) if res.data is not None else {"ok": False, "reason": "EMPTY_RESPONSE"}

    if data.get("ok") is True and not _should_skip_wallet_tx(data):
        loaded_tokens = _extract_loaded_tokens(data)
        tokens_after = _extract_tokens_after(data)

        try:
            _insert_wallet_credit_tx(
                user_id=req.user_id.strip(),
                amount=loaded_tokens,
                balance_after=tokens_after,
                source="manual_code_token_load",
                description="Kod ile jeton yükleme",
                meta={
                    "manual_code": req.manual_code.strip(),
                    "loaded_tokens": loaded_tokens,
                    "tokens_after": tokens_after,
                    "source_type": "manual_code",
                },
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"wallet_tx_failed: {str(e)}")

    return data
