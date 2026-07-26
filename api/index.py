from __future__ import annotations

import os
import traceback

from fastapi import FastAPI

BOOT_OK = False
BOOT_ERROR = ""

# Vercel Python runtime requires a top-level variable named `app`,
# `application`, or `handler` that it can detect statically during build.
# Keep this unconditional fallback app at module top level, then replace it
# with the real FastAPI app if app.main imports successfully.
app = FastAPI(
    title="italkyAI API Boot Fallback",
    version=os.getenv("APP_VERSION", "vercel-boot-fallback"),
)


@app.get("/")
def root():
    return {
        "ok": BOOT_OK,
        "service": "italky-api",
        "boot_ok": BOOT_OK,
        "message": "Backend import failed. Open /__boot_error to see the traceback." if not BOOT_OK else "Backend online.",
    }


@app.get("/healthz")
def healthz():
    return {
        "status": "ok" if BOOT_OK else "boot_error",
        "ok": BOOT_OK,
        "boot_ok": BOOT_OK,
        "message": "Vercel function is alive." if BOOT_OK else "Vercel function is alive, but app.main import failed.",
    }


@app.get("/api/healthz")
def api_healthz():
    return {
        "status": "ok" if BOOT_OK else "boot_error",
        "ok": BOOT_OK,
        "boot_ok": BOOT_OK,
        "message": "Vercel function is alive." if BOOT_OK else "Vercel function is alive, but app.main import failed.",
    }


@app.get("/__boot_error")
def boot_error():
    return {
        "ok": BOOT_OK,
        "boot_ok": BOOT_OK,
        "error": BOOT_ERROR,
    }


try:
    from app.main import app as real_app  # type: ignore
    app = real_app
    BOOT_OK = True
except Exception:
    BOOT_ERROR = traceback.format_exc()

# Some runtimes look for `application` instead of `app`.
application = app
