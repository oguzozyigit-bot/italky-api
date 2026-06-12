from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.routers.session import supabase
from app.services.store_purchases import revoke_purchase_entitlement

router = APIRouter(prefix="/api/admin/store-purchases", tags=["store-purchase-admin"])

ALLOWED_REVOKE_STATUSES = {"refunded", "voided", "revoked", "cancelled"}


class ManualRevokePayload(BaseModel):
    reason: str = "manual_test_revoke"
    status: str = "revoked"
    raw_payload: dict[str, Any] | None = None


def _require_admin_secret(x_admin_secret: str | None) -> None:
    expected = os.getenv("STORE_PURCHASE_ADMIN_SECRET", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="admin_secret_not_configured")
    if not x_admin_secret or x_admin_secret.strip() != expected:
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/{purchase_id}/revoke")
def revoke_store_purchase(
    purchase_id: str,
    payload: ManualRevokePayload,
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
):
    _require_admin_secret(x_admin_secret)

    status = str(payload.status or "").strip().lower()
    if status not in ALLOWED_REVOKE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid_revoke_status")

    result = revoke_purchase_entitlement(
        supabase,
        purchase_id=purchase_id,
        reason=str(payload.reason or "manual_test_revoke").strip() or "manual_test_revoke",
        new_status=status,
        raw_payload=payload.raw_payload or {"source": "manual_revoke_endpoint"},
    )
    if not result.get("ok") and result.get("error") == "purchase_not_found":
        raise HTTPException(status_code=404, detail="purchase_not_found")
    if not result.get("ok") and result.get("error") == "profile_not_found":
        raise HTTPException(status_code=404, detail="profile_not_found")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "revoke_failed")
    return result
