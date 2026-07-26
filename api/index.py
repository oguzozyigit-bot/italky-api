from __future__ import annotations

import os
import traceback

from fastapi import FastAPI

BOOT_OK = False
BOOT_ERROR = ""

try:
    from app.main import app as app  # type: ignore
    BOOT_OK = True
except Exception:
    BOOT_ERROR = traceback.format_exc()
    app = FastAPI(
        title="italkyAI API Boot Fallback",
        version=os.getenv("APP_VERSION", "vercel-boot-fallback"),
    )

    @app.get("/")
    def root():
        return {
            "ok": False,
            "service": "italky-api",
            "boot_ok": False,
            "message": "Backend import failed. Open /__boot_error to see the traceback.",
        }

    @app.get("/healthz")
    def healthz():
        return {
            "status": "boot_error",
            "ok": False,
            "boot_ok": False,
            "message": "Vercel function is alive, but app.main import failed.",
        }

    @app.get("/api/healthz")
    def api_healthz():
        return {
            "status": "boot_error",
            "ok": False,
            "boot_ok": False,
            "message": "Vercel function is alive, but app.main import failed.",
        }

    @app.get("/__boot_error")
    def boot_error():
        return {
            "ok": False,
            "boot_ok": False,
            "error": BOOT_ERROR,
        }
