from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.routers.trendyol import resolve_activation_token

logger = logging.getLogger(__name__)

activation_router = APIRouter(prefix="/api/activation-links", tags=["Activation Links"])


@activation_router.get("/{token}")
def get_activation_link(token: str) -> dict[str, Any]:
    cleaned = str(token or "").strip().upper()
    logger.info("activation link request path=/api/activation-links/%s", cleaned)
    try:
        result = resolve_activation_token(token)
        logger.info(
            "activation link hit token=%s code_value=%s",
            cleaned,
            result.get("code_value"),
        )
        return result
    except HTTPException as exc:
        logger.warning(
            "activation link miss token=%s http_status=%s reason=%s",
            cleaned,
            exc.status_code,
            exc.detail,
        )
        return {"ok": False, "reason": str(exc.detail), "code_value": None}
