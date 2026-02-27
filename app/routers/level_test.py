# FILE: italky-api/app/routers/level_test.py
from __future__ import annotations

import io
import json
import logging
import os
import re
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

# Storage cache
TESTS_BUCKET = "tests"
TESTS_DIR = "level_tests"          # bucket içindeki klasör
TESTS_VER = "v1"                   # dosya versiyonu

# OpenAI
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=75.0, write=20.0, pool=10.0)


class GenReq(BaseModel):
    test_id: str = Field(..., min_length=6)
    lang: str = Field("en", min_length=2, max_length=16)


def _norm_lang(lang: str) -> str:
    return (lang or "en").strip().lower()


def build_prompt(lang: str) -> str:
    return f"""
You are generating a CEFR placement test for language code "{lang}".
Return STRICT JSON ONLY. No markdown, no explanation.

Schema:
{{
  "questions": [
    {{
      "id": 1,
      "level": "A1|A2|B1|B2|C1",
      "type": "mcq",
      "question": "string",
      "options": ["A","B","C","D"],
      "correct_index": 0
    }}
  ]
}}

Rules:
- Total exactly 50 questions.
- Mix levels roughly: A1 10, A2 10, B1 10, B2 10, C1 10.
- Questions and options MUST be in the target language.
- Options must be plausible.
- correct_index must be 0..3
""".strip()


def extract_json_from_text(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        raise ValueError("empty text")
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("JSON not found in model output")
    return json.loads(m.group(0))


def validate_questions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    qs = payload.get("questions")
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
    return qs


def _storage_path(lang: str) -> str:
    # tests/level_tests/en_v1.json
    L = _norm_lang(lang)
    return f"{TESTS_DIR}/{L}_{TESTS_VER}.json"


def load_cached_test_from_storage(lang: str) -> Optional[Dict[str, Any]]:
    """
    Storage'dan { questions:[...] } JSON döndürür.
    Bulamazsa None.
    """
    path = _storage_path(lang)
    try:
        res = sb_admin.storage.from_(TESTS_BUCKET).download(path)
        # supabase-py download bazen bytes döner, bazen response benzeri
        if hasattr(res, "read"):
            raw = res.read()
        else:
            raw = res
        if not raw:
            return None
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        # normalize: dosya zaten {"questions":[...]} olmalı
        if isinstance(data.get("questions"), list):
            validate_questions(data)
            return data
        return None
    except Exception as e:
        # 404 vs her şeyi None kabul ediyoruz (cache yok)
        logger.warning("CACHE_MISS storage download failed path=%s err=%s", path, str(e)[:200])
        return None


def upload_test_to_storage(lang: str, payload: Dict[str, Any]) -> None:
    """
    Üretilen testi storage'a upsert eder.
    """
    path = _storage_path(lang)
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        f = io.BytesIO(body)
        # supabase-py upload options: upsert=True
        sb_admin.storage.from_(TESTS_BUCKET).upload(
            path,
            f,
            {"content-type": "application/json", "upsert": "true"},
        )
        logger.info("CACHE_WRITE ok path=%s", path)
    except Exception as e:
        logger.warning("CACHE_WRITE failed path=%s err=%s", path, str(e)[:200])


async def call_openai(prompt: str) -> Optional[Dict[str, Any]]:
    if not OPENAI_API_KEY:
        return None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": OPENAI_MODEL,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "You must output ONLY valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }

            r = await client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                logger.error("OPENAI_FAIL %s %s", r.status_code, r.text[:800])
                return None

            j = r.json()
            text = ""
            try:
                text = j["choices"][0]["message"]["content"]
            except Exception:
                text = ""

            if not text:
                logger.error("OPENAI_EMPTY_TEXT %s", str(j)[:800])
                return None

            return extract_json_from_text(text)

        except httpx.ReadTimeout:
            logger.error("OPENAI_TIMEOUT")
            return None
        except Exception as e:
            logger.exception("OPENAI_EXCEPTION %s", e)
            return None


@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = req.test_id.strip()
    lang = _norm_lang(req.lang)

    # 1) test var mı?
    try:
        row = (
            sb_admin.table("level_tests")
            .select("id,status,questions,language_code")
            .eq("id", test_id)
            .maybe_single()
            .execute()
        )
    except Exception as e:
        logger.exception("SB_SELECT_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"db read failed: {str(e)}")

    if not row.data:
        raise HTTPException(status_code=404, detail="test not found")

    # dil kolonunu garantiye alalım (NULL olan kayıtlar var sende)
    db_lang = _norm_lang(row.data.get("language_code") or lang or "en")

    # 2) DB’de questions varsa çık
    if row.data.get("questions"):
        return {"ok": True, "status": row.data.get("status", "ready")}

    # 3) Storage cache var mı? varsa DB’ye yaz
    cached = load_cached_test_from_storage(db_lang)
    if cached:
        try:
            sb_admin.table("level_tests").update(
                {"questions": cached, "status": "ready", "language_code": db_lang}
            ).eq("id", test_id).execute()
            return {"ok": True, "status": "ready", "source": "storage"}
        except Exception as e:
            logger.exception("SB_UPDATE_FAIL(storage) %s", e)
            raise HTTPException(status_code=500, detail=f"db update failed: {str(e)}")

    # 4) Cache yok → OpenAI üret
    prompt = build_prompt(db_lang)
    data = await call_openai(prompt)
    if data is None:
        raise HTTPException(status_code=502, detail="OpenAI failed (no questions generated)")

    # 5) validate
    try:
        questions = validate_questions(data)
    except Exception as e:
        logger.error("QUESTIONS_INVALID %s payload=%s", e, str(data)[:800])
        raise HTTPException(status_code=500, detail=f"invalid questions payload: {str(e)}")

    payload = {"questions": questions}

    # 6) DB’ye yaz
    try:
        sb_admin.table("level_tests").update(
            {"questions": payload, "status": "ready", "language_code": db_lang}
        ).eq("id", test_id).execute()
    except Exception as e:
        logger.exception("SB_UPDATE_FAIL(openai) %s", e)
        raise HTTPException(status_code=500, detail=f"db update failed: {str(e)}")

    # 7) Storage’a cache yaz (hata olursa süreç bozulmasın)
    upload_test_to_storage(db_lang, payload)

    return {"ok": True, "status": "ready", "source": "openai"}
