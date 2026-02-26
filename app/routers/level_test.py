# FILE: app/routers/level_test.py

from __future__ import annotations

import os
import json
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client
from supabase.lib.client_options import ClientOptions

router = APIRouter(tags=["level-test"])

# =========================================================
# 🔐 ENV SAFE LOAD
# =========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL missing in environment")

if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY missing in environment")

try:
    sb_admin = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY,
        options=ClientOptions(auto_refresh_token=False, persist_session=False)
    )
except Exception as e:
    raise RuntimeError(f"Supabase init failed: {str(e)}")


# =========================================================
# 📦 REQUEST MODEL
# =========================================================

class GenReq(BaseModel):
    test_id: str
    lang: str


# =========================================================
# 🧠 PROMPT BUILDER
# =========================================================

def build_prompt(lang: str) -> str:
    return f"""
You are generating a CEFR placement test for language code "{lang}".
Return STRICT JSON ONLY. No markdown.

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
- Questions must be in the target language.
- Options must be plausible.
"""


# =========================================================
# 🤖 MODEL CALL PLACEHOLDERS
# =========================================================

async def call_gemini(prompt: str) -> Optional[dict]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    # Buraya gerçek Gemini çağrını koy
    return None


async def call_openai(prompt: str) -> Optional[dict]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    # Buraya gerçek OpenAI çağrını koy
    return None


# =========================================================
# 🧹 JSON CLEANER
# =========================================================

def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("JSON not found in model response")
    return json.loads(match.group(0))


# =========================================================
# 🚀 GENERATE ENDPOINT
# =========================================================

@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):

    test_id = req.test_id.strip()
    lang = (req.lang or "en").strip().lower()

    # 1️⃣ Test var mı?
    row = (
        sb_admin
        .table("level_tests")
        .select("id,status,questions")
        .eq("id", test_id)
        .maybe_single()
        .execute()
    )

    if not row.data:
        raise HTTPException(status_code=404, detail="test not found")

    # Zaten questions varsa tekrar üretme
    if row.data.get("questions"):
        return {"ok": True, "status": row.data.get("status", "ready")}

    prompt = build_prompt(lang)

    # 2️⃣ Gemini → OpenAI fallback
    data = await call_gemini(prompt)
    if data is None:
        data = await call_openai(prompt)

    if data is None:
        raise HTTPException(status_code=502, detail="No provider available")

    questions = data.get("questions") if isinstance(data, dict) else None

    if not isinstance(questions, list) or len(questions) != 50:
        raise HTTPException(status_code=500, detail="Invalid questions payload")

    # 3️⃣ DB update
    try:
        sb_admin.table("level_tests").update({
            "questions": {"questions": questions},
            "status": "ready"
        }).eq("id", test_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB update failed: {str(e)}")

    return {"ok": True, "status": "ready"}
