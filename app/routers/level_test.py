# FILE: italky-api/app/routers/level_test.py
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

router = APIRouter(tags=["level-test"])
logger = logging.getLogger("level-test")
logger.setLevel(logging.INFO)

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL missing")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY missing")

try:
    sb_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
except Exception as e:
    raise RuntimeError(f"supabase init failed: {str(e)}")

# ✅ Storage public path (bucket public)
# https://auth.italky.ai/storage/v1/object/public/tests/level_tests/en_v1.json
PUBLIC_STORAGE_BASE = (os.getenv("PUBLIC_STORAGE_BASE") or "https://auth.italky.ai").rstrip("/")
TESTS_BUCKET = "tests"
TESTS_DIR = "level_tests"
TESTS_VER = "v1"

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=20.0, pool=10.0)


class GenReq(BaseModel):
    test_id: str = Field(..., min_length=6)
    lang: str = Field("en", min_length=2, max_length=16)


class EnsureExamAccessReq(BaseModel):
    user_id: str = Field(..., min_length=6)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_lang(lang: str) -> str:
    return (lang or "en").strip().lower()


def _public_test_url(lang: str) -> str:
    L = _norm_lang(lang)
    return f"{PUBLIC_STORAGE_BASE}/storage/v1/object/public/{TESTS_BUCKET}/{TESTS_DIR}/{L}_{TESTS_VER}.json"


def validate_questions_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Beklenen dosya formatı:
    {
      "version": 1,
      "lang": "en",
      "questions": [ ... 50 adet ... ]
    }
    """
    if not isinstance(doc, dict):
        raise ValueError("doc is not an object")

    qs = doc.get("questions")
    if not isinstance(qs, list):
        raise ValueError("questions is not a list")

    if len(qs) != 50:
        raise ValueError(f"questions length must be 50, got {len(qs)}")

    for i, q in enumerate(qs, start=1):
        if not isinstance(q, dict):
            raise ValueError(f"question[{i}] is not an object")
        opts = q.get("options")
        ci = q.get("correct_index")
        if not isinstance(opts, list) or len(opts) != 4:
            raise ValueError(f"question[{i}] options must be 4 items")
        if not isinstance(ci, int) or not (0 <= ci <= 3):
            raise ValueError(f"question[{i}] correct_index must be 0..3")
        if not q.get("question"):
            raise ValueError(f"question[{i}] missing question text")
        if q.get("level") not in ("A1", "A2", "B1", "B2", "C1"):
            raise ValueError(f"question[{i}] invalid level")
    return doc


async def load_test_from_public_storage(lang: str) -> Dict[str, Any]:
    url = _public_test_url(lang)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url)

    if r.status_code != 200:
        raise HTTPException(status_code=404, detail=f"test file not found: {url}")

    try:
        doc = r.json()
    except Exception:
        raise HTTPException(status_code=500, detail="invalid json in test file")

    try:
        doc = validate_questions_doc(doc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"invalid test file format: {str(e)}")

    return doc


@router.post("/level_test/ensure_access")
async def ensure_level_test_access(req: EnsureExamAccessReq):
    user_id = req.user_id.strip()
    now = _utc_now()
    now_iso = now.isoformat()

    # 1) Aktif 7 günlük erişim var mı?
    try:
        access_row = (
            sb_admin.table("level_exam_access")
            .select("id, starts_at, ends_at, token_spent")
            .eq("user_id", user_id)
            .eq("access_type", "level_test")
            .gt("ends_at", now_iso)
            .order("ends_at", desc=True)
            .limit(1)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logger.exception("LEVEL_TEST_ACCESS_READ_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"access read failed: {str(e)}")

    if access_row.data:
        return {
            "ok": True,
            "access_open": True,
            "used_token": False,
            "valid_until": access_row.data.get("ends_at"),
            "message": "existing_access_active"
        }

    # 2) Kullanıcının jetonunu oku
    try:
        prof = (
            sb_admin.table("profiles")
            .select("id,tokens")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logger.exception("LEVEL_TEST_PROFILE_READ_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"profile read failed: {str(e)}")

    if not prof.data:
        raise HTTPException(status_code=404, detail="user profile not found")

    current_tokens = int(prof.data.get("tokens") or 0)

    # 3) Jeton yoksa reddet
    if current_tokens < 1:
        return {
            "ok": False,
            "access_open": False,
            "reason": "INSUFFICIENT_TOKENS",
            "required_tokens": 1,
            "tokens": current_tokens,
            "message": "level_test_requires_1_token_for_7_days"
        }

    new_tokens = current_tokens - 1
    ends_at = (now + timedelta(days=7)).isoformat()

    # 4) Jeton düş
    try:
        sb_admin.table("profiles").update({
            "tokens": new_tokens
        }).eq("id", user_id).execute()
    except Exception as e:
        logger.exception("LEVEL_TEST_TOKEN_UPDATE_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"token update failed: {str(e)}")

    # 5) 7 günlük erişim aç
    try:
        sb_admin.table("level_exam_access").insert({
            "user_id": user_id,
            "access_type": "level_test",
            "token_spent": 1,
            "starts_at": now_iso,
            "ends_at": ends_at
        }).execute()
    except Exception as e:
        logger.exception("LEVEL_TEST_ACCESS_INSERT_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"access insert failed: {str(e)}")

    return {
        "ok": True,
        "access_open": True,
        "used_token": True,
        "required_tokens": 1,
        "tokens_after": new_tokens,
        "valid_until": ends_at,
        "message": "level_test_access_opened_for_7_days"
    }


@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = req.test_id.strip()
    lang = _norm_lang(req.lang)

    # 1) test var mı?
    try:
        row = (
            sb_admin.table("level_tests")
            .select("id,status,questions,language_code,user_id")
            .eq("id", test_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logger.exception("SB_SELECT_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"db read failed: {str(e)}")

    if not row.data:
        raise HTTPException(status_code=404, detail="test not found")

    db_lang = _norm_lang(row.data.get("language_code") or lang or "en")

    # 2) DB’de questions varsa çık
    if row.data.get("questions"):
        if not row.data.get("language_code"):
            try:
                sb_admin.table("level_tests").update({"language_code": db_lang}).eq("id", test_id).execute()
            except Exception:
                pass
        return {"ok": True, "status": row.data.get("status", "ready"), "source": "db"}

    # 3) Storage public JSON oku
    doc = await load_test_from_public_storage(db_lang)

    # 4) DB’ye yaz
    try:
        sb_admin.table("level_tests").update(
            {"questions": doc, "status": "ready", "language_code": db_lang}
        ).eq("id", test_id).execute()
    except Exception as e:
        logger.exception("SB_UPDATE_FAIL(storage) %s", e)
        raise HTTPException(status_code=500, detail=f"db update failed: {str(e)}")

    return {"ok": True, "status": "ready", "source": "storage"}
