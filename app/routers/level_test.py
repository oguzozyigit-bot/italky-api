# FILE: app/routers/level_test.py
from __future__ import annotations

import os
import json
import re
from typing import Optional, Tuple, Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from supabase import create_client
from supabase.lib.client_options import ClientOptions

router = APIRouter(tags=["level-test"])


# =========================================================
# ✅ Lazy Supabase Admin Client (deploy çökmesin)
# =========================================================

_sb_admin = None
_sb_err: Optional[str] = None

def _init_supabase_admin() -> Tuple[Optional[Any], Optional[str]]:
    """
    Returns (client, error). Never raises (so app can boot).
    """
    global _sb_admin, _sb_err

    if _sb_admin is not None:
        return _sb_admin, None

    if _sb_err is not None:
        return None, _sb_err

    url = (os.getenv("SUPABASE_URL", "") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

    if not url:
        _sb_err = "SUPABASE_URL missing"
        return None, _sb_err

    if not key:
        _sb_err = "SUPABASE_SERVICE_ROLE_KEY missing"
        return None, _sb_err

    # ✅ Supabase new keys: MUST be sb_secret_... for service/admin
    # (publishable key burada çalışmaz)
    try:
        _sb_admin = create_client(
            url,
            key,
            options=ClientOptions(auto_refresh_token=False, persist_session=False),
        )
        return _sb_admin, None
    except Exception as e:
        _sb_err = f"Supabase init failed: {str(e)}"
        return None, _sb_err


# =========================================================
# 📦 Request Model
# =========================================================

class GenReq(BaseModel):
    test_id: str
    lang: str


# =========================================================
# 🧠 Prompt
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
""".strip()


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON not found")
    return json.loads(m.group(0))


# =========================================================
# 🤖 Provider placeholders (senin mevcut çağrı kodun ile dolduracaksın)
# =========================================================

async def call_gemini(prompt: str) -> Optional[dict]:
    key = (os.getenv("GEMINI_API_KEY", "") or "").strip()
    if not key:
        return None
    # TODO: mevcut gemini çağrın
    return None


async def call_openai(prompt: str) -> Optional[dict]:
    key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
    if not key:
        return None
    # TODO: mevcut openai çağrın
    return None


# =========================================================
# 🚀 Route
# =========================================================

@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    sb_admin, err = _init_supabase_admin()
    if err or sb_admin is None:
        raise HTTPException(status_code=503, detail=f"supabase_admin_unavailable: {err}")

    test_id = (req.test_id or "").strip()
    lang = (req.lang or "en").strip().lower()

    if not test_id:
        raise HTTPException(status_code=422, detail="test_id required")

    # 1) test var mı?
    row = (
        sb_admin.table("level_tests")
        .select("id,status,questions")
        .eq("id", test_id)
        .maybe_single()
        .execute()
    )

    if not row.data:
        raise HTTPException(status_code=404, detail="test not found")

    if row.data.get("questions"):
        return {"ok": True, "status": row.data.get("status", "ready")}

    prompt = build_prompt(lang)

    # 2) Gemini -> OpenAI fallback
    data = await call_gemini(prompt)
    if data is None:
        data = await call_openai(prompt)

    if data is None:
        raise HTTPException(status_code=502, detail="No provider available")

    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or len(questions) != 50:
        raise HTTPException(status_code=500, detail="invalid questions payload")

    # 3) DB’ye yaz
    try:
        sb_admin.table("level_tests").update({
            "questions": {"questions": questions},
            "status": "ready",
        }).eq("id", test_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB update failed: {str(e)}")

    return {"ok": True, "status": "ready"}
