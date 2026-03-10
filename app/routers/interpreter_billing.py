from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.token_engine import spend_chars

router = APIRouter(tags=["interpreter-billing"])


class InterpreterSpendReq(BaseModel):
    payer_user_id: str
    used_chars: int


@router.post("/api/interpreter/spend")
async def interpreter_spend(req: InterpreterSpendReq):
    payer_user_id = (req.payer_user_id or "").strip()
    used_chars = int(req.used_chars or 0)

    if not payer_user_id:
        raise HTTPException(status_code=422, detail="payer_user_id required")

    return spend_chars(
        user_id=payer_user_id,
        module_key="interpreter",
        used_chars=used_chars,
    )
