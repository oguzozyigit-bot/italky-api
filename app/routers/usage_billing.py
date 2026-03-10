from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.routers.token_engine import spend_chars

router = APIRouter(tags=["usage-billing"])


class UsageSpendReq(BaseModel):
    user_id: str
    module_key: str
    used_chars: int


@router.post("/api/usage/spend")
async def usage_spend(req: UsageSpendReq):
    return spend_chars(
        user_id=(req.user_id or "").strip(),
        module_key=(req.module_key or "").strip(),
        used_chars=int(req.used_chars or 0),
    )
