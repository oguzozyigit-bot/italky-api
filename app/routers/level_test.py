from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os, json, re
from supabase import create_client

router = APIRouter(tags=["level-test"])

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

sb_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

class GenReq(BaseModel):
    test_id: str
    lang: str

def build_prompt(lang: str) -> str:
    # 40 dk / 50 soru: “gerçek seviye tespit” karışık (A1→C1)
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

async def call_gemini(prompt: str) -> dict | None:
    key = (os.getenv("GEMINI_API_KEY","") or "").strip()
    if not key:
        return None
    # Senin gemini çağrı kodun projede varsa onu kullan.
    # Buraya “placeholder” bırakıyorum:
    return None

async def call_openai(prompt: str) -> dict | None:
    key = (os.getenv("OPENAI_API_KEY","") or "").strip()
    if not key:
        return None
    # Senin openai çağrı kodun projede varsa onu kullan.
    # Buraya “placeholder” bırakıyorum:
    return None

def extract_json(text: str) -> dict:
    # model bazen ekstra metin dökerse diye
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON not found")
    return json.loads(m.group(0))

@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = req.test_id.strip()
    lang = (req.lang or "en").strip().lower()

    # 1) test var mı?
    row = sb_admin.table("level_tests").select("id,status,questions").eq("id", test_id).maybe_single().execute()
    if not row.data:
        raise HTTPException(404, "test not found")

    if row.data.get("questions"):
        return {"ok": True, "status": row.data.get("status", "ready")}

    prompt = build_prompt(lang)

    # 2) Gemini -> OpenAI fallback
    data = await call_gemini(prompt)
    if data is None:
        data = await call_openai(prompt)

    if data is None:
        raise HTTPException(502, "No provider available")

    # data normalize: data["questions"] olmalı
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or len(questions) != 50:
        raise HTTPException(500, "invalid questions payload")

    # 3) DB’ye yaz
    upd = sb_admin.table("level_tests").update({
        "questions": {"questions": questions},
        "status": "ready"
    }).eq("id", test_id).execute()

    return {"ok": True, "status": "ready"}
