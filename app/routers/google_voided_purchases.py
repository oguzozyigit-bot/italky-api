from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/cron/google", tags=["google-voided-purchases"])


def _require_cron_secret(x_cron_secret: str | None) -> None:
    expected = (
        os.getenv("GOOGLE_VOIDED_PURCHASES_CRON_SECRET", "").strip()
        or os.getenv("CRON_SECRET", "").strip()
    )
    if not expected:
        raise HTTPException(status_code=503, detail="cron_secret_not_configured")
    if not x_cron_secret or x_cron_secret.strip() != expected:
        raise HTTPException(status_code=403, detail="forbidden")


def _google_credentials_configured() -> bool:
    raw_json = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    package_name = (
        os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "").strip()
        or os.getenv("ANDROID_PACKAGE_NAME", "").strip()
    )
    return bool(package_name and (raw_json or credentials_file))


@router.get("/voided-purchases")
def google_voided_purchases_cron(
    x_cron_secret: str | None = Header(default=None, alias="X-Cron-Secret"),
):
    _require_cron_secret(x_cron_secret)

    if not _google_credentials_configured():
        return {"ok": False, "error": "google_credentials_not_configured"}

    return {
        "ok": False,
        "error": "google_voided_purchases_not_implemented",
        "message": "Voided Purchases API integration will call Google and revoke matching store_purchases here.",
    }
