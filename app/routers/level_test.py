# FILE: italky-api/app/routers/level_test.py
from __future__ import annotations

import os, json, re, random
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from supabase import create_client

router = APIRouter(tags=["level-test"])

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    sb_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
else:
    sb_admin = None


class GenReq(BaseModel):
    model_config = ConfigDict(extra="ignore")
    test_id: str
    lang: str = "en"


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
- correct_index must match the correct option.
"""


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON not found")
    return json.loads(m.group(0))


def fallback_questions(lang: str) -> List[Dict[str, Any]]:
    # ✅ 50 soru garanti – basit ama çalışır (Gemini/OpenAI hazır olana kadar)
    L = (lang or "en").lower().strip()
    base = [
        ("A1", f"[{L}] Choose the correct option: I ____ a student.", ["am","is","are","be"], 0),
        ("A1", f"[{L}] Choose the correct option: She ____ coffee every day.", ["drink","drinks","drinking","to drink"], 1),

        ("A2", f"[{L}] Choose the correct option: We ____ to the cinema yesterday.", ["go","went","gone","going"], 1),
        ("A2", f"[{L}] Choose the correct option: There ____ some books on the table.", ["is","are","was","be"], 1),

        ("B1", f"[{L}] Choose the correct option: If it ____ tomorrow, we will stay home.", ["rain","rains","rained","raining"], 1),
        ("B1", f"[{L}] Choose the correct option: I have lived here ____ 2018.", ["since","for","during","from"], 0),

        ("B2", f"[{L}] Choose the correct option: The report ____ by the team last week.", ["was prepared","is prepare","prepared","has prepare"], 0),
        ("B2", f"[{L}] Choose the correct option: Hardly ____ I seen such a thing.", ["have","has","had","having"], 0),

        ("C1", f"[{L}] Choose the closest meaning: 'It is imperative that he be informed.'", ["He must be informed.","He might be informed.","He was informed.","He can inform."], 0),
        ("C1", f"[{L}] Choose the correct option: I would rather you ____ now.", ["leave","left","leaving","to leave"], 1),
    ]

    # 10 blok x 5 = 50
    out: List[Dict[str, Any]] = []
    qid = 1
    while len(out) < 50:
        lvl, q, opts, ci = random.choice(base)
        out.append({
            "id": qid,
            "level": lvl,
            "type": "mcq",
            "question": q,
            "options": opts,
            "correct_index": ci
        })
        qid += 1
    return out[:50]


async def call_gemini(prompt: str) -> Optional[dict]:
    key = (os.getenv("GEMINI_API_KEY", "") or "").strip()
    if not key:
        return None
    # ✅ Buraya projendeki gerçek gemini çağrını bağlayacaksın.
    # Şimdilik None döndürüp fallback’e düşürüyoruz.
    return None


async def call_openai(prompt: str) -> Optional[dict]:
    key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
    if not key:
        return None
    # ✅ Buraya projendeki gerçek openai çağrını bağlayacaksın.
    # Şimdilik None döndürüp fallback’e düşürüyoruz.
    return None


@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    if sb_admin is None:
        raise HTTPException(500, "Supabase service role env missing")

    test_id = (req.test_id or "").strip()
    if not test_id:
        raise HTTPException(422, "test_id required")

    lang = (req.lang or "en").strip().lower()

    # 1) test var mı?
    row = sb_admin.table("level_tests").select("id,status,questions").eq("id", test_id).maybe_single().execute()
    if not row.data:
        raise HTTPException(404, "test not found")

    # already has questions
    if row.data.get("questions"):
        return {"ok": True, "status": row.data.get("status", "ready")}

    prompt = build_prompt(lang)

    provider = "fallback"
    data = await call_gemini(prompt)
    if data is not None:
        provider = "gemini"
    else:
        data = await call_openai(prompt)
        if data is not None:
            provider = "openai"

    # normalize
    questions = None
    if isinstance(data, dict):
        questions = data.get("questions")

    # provider yoksa fallback üret
    if not isinstance(questions, list) or len(questions) != 50:
        questions = fallback_questions(lang)
        provider = "fallback"

    # 3) DB’ye yaz
    sb_admin.table("level_tests").update({
        "questions": {"questions": questions},
        "status": "ready"
    }).eq("id", test_id).execute()

    return {"ok": True, "status": "ready", "provider": provider, "count": len(questions)}
