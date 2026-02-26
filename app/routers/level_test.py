from __future__ import annotations

import os
import json
import re
import logging
from typing import Any, Dict, Optional, List

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from supabase import create_client

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["level-test"])

# -------------------------
# ENV
# -------------------------
SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "").strip()

HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=75.0, write=20.0, pool=10.0)

# -------------------------
# Schemas
# -------------------------
class GenReq(BaseModel):
    test_id: str = Field(..., min_length=6)
    lang: str = Field("en", min_length=2, max_length=16)

# -------------------------
# Helpers
# -------------------------
def sb_admin():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    if not SUPABASE_SERVICE_ROLE_KEY.startswith("eyJ"):
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY must start with 'eyJ' (JWT)")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

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

# -------------------------
# OpenAI only
# -------------------------
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

# -------------------------
# Route
# -------------------------
@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = req.test_id.strip()
    lang = (req.lang or "en").strip().lower()

    # 1) supabase admin (request-time)
    try:
        sb = sb_admin()
    except Exception as e:
        logger.exception("SUPABASE_INIT_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"supabase init failed: {str(e)}")

    # 2) test var mı?
    try:
        row = sb.table("level_tests").select("id,status,questions").eq("id", test_id).maybe_single().execute()
    except Exception as e:
        logger.exception("SB_SELECT_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"db read failed: {str(e)}")

    if not row.data:
        raise HTTPException(status_code=404, detail="test not found")

    # zaten varsa
    if row.data.get("questions"):
        return {"ok": True, "status": row.data.get("status", "ready")}

    # 3) OpenAI only
    prompt = build_prompt(lang)
    data = await call_openai(prompt)

    if data is None:
        raise HTTPException(status_code=502, detail="OpenAI not available or failed (no questions generated)")

    # 4) validate
    try:
        questions = validate_questions(data)
    except Exception as e:
        logger.error("QUESTIONS_INVALID %s payload=%s", e, str(data)[:800])
        raise HTTPException(status_code=500, detail=f"invalid questions payload: {str(e)}")

    # 5) write DB
    try:
        sb.table("level_tests").update({
            "questions": {"questions": questions},
            "status": "ready"
        }).eq("id", test_id).execute()
    except Exception as e:
        logger.exception("SB_UPDATE_FAIL %s", e)
        raise HTTPException(status_code=500, detail=f"db update failed: {str(e)}")

    return {"ok": True, "status": "ready"}
