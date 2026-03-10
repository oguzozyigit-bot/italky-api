from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.routers.token_engine import spend_chars

router = APIRouter(tags=["meeting-billing"])


class MeetingSpendReq(BaseModel):
    user_id: str
    used_chars: int


@router.post("/api/meeting/spend")
async def meeting_spend(req: MeetingSpendReq):
    user_id = (req.user_id or "").strip()
    used_chars = int(req.used_chars or 0)

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")

    return spend_chars(
        user_id=user_id,
        module_key="meeting",
        used_chars=used_chars,
    )
