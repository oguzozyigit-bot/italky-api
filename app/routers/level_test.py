# FILE: italky-api/app/routers/level_test.py
from __future__ import annotations

import os
import json
import re
from typing import Any, Dict, Optional, List

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["level-test"])

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

# OpenAI / Gemini
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY", "") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "").strip()

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL", "gemini-1.5-flash") or "").strip()

# ---- helpers ----
def sb_headers() -> Dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        # app çökmeyecek, endpoint 503 verecek
        return {}
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }

def require_sb():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=503,
            detail="supabase_admin_unavailable: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing",
        )

def norm_lang(code: str) -> str:
    c = (code or "en").strip().lower().replace("_", "-")
    # en-US -> en
    if "-" in c:
        c = c.split("-")[0]
    return c or "en"

class GenReq(BaseModel):
    test_id: str
    lang: str = "en"

def build_prompt(lang: str) -> str:
    # 40 dk / 50 soru: gerçek placement mix A1..C1
    return f"""
You are generating a CEFR placement test for language code "{lang}".
Return STRICT JSON ONLY. No markdown, no commentary.

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
- Questions must be written in the TARGET language (not English unless lang=en).
- Options must be plausible.
- correct_index must be 0..3.
"""

def extract_json_from_text(text: str) -> Dict[str, Any]:
    # model bazen ön/son metin ekleyebilir
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON not found in model output")
    return json.loads(m.group(0))

def validate_questions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    qs = payload.get("questions")
    if not isinstance(qs, list) or len(qs) != 50:
        raise ValueError("invalid questions payload: questions must be list of length 50")

    out = []
    for i, q in enumerate(qs, start=1):
        if not isinstance(q, dict):
            raise ValueError(f"question[{i}] not object")

        options = q.get("options")
        if not isinstance(options, list) or len(options) != 4:
            raise ValueError(f"question[{i}] options must be 4 items")

        ci = q.get("correct_index")
        if not isinstance(ci, int) or ci < 0 or ci > 3:
            raise ValueError(f"question[{i}] correct_index must be 0..3")

        lvl = str(q.get("level") or "").upper().strip()
        if lvl not in ("A1", "A2", "B1", "B2", "C1"):
            raise ValueError(f"question[{i}] invalid level")

        out.append({
            "id": int(q.get("id") or i),
            "level": lvl,
            "type": "mcq",
            "question": str(q.get("question") or "").strip(),
            "options": [str(x).strip() for x in options],
            "correct_index": ci,
        })

    return out

async def call_gemini(prompt: str) -> Optional[Dict[str, Any]]:
    if not GEMINI_API_KEY:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 8192,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(url, json=body)
        if r.status_code >= 400:
            return None

        data = r.json()
        # gemini response -> candidates[0].content.parts[0].text
        text = ""
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return None

        return extract_json_from_text(text)
    except Exception:
        return None

async def call_openai(prompt: str) -> Optional[Dict[str, Any]]:
    if not OPENAI_API_KEY:
        return None

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You output only JSON."},
            {"role": "user", "content": prompt},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            return None

        j = r.json()
        text = j["choices"][0]["message"]["content"]
        return json.loads(text)
    except Exception:
        return None

async def sb_get_level_test(test_id: str) -> Dict[str, Any]:
    require_sb()
    url = f"{SUPABASE_URL}/rest/v1/level_tests"
    params = {
        "select": "id,status,questions,language_code",
        "id": f"eq.{test_id}",
        "limit": "1",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=sb_headers(), params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"supabase_read_failed HTTP {r.status_code}: {r.text[:300]}")
    rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="test not found")
    return rows[0]

async def sb_update_level_test(test_id: str, patch: Dict[str, Any]) -> None:
    require_sb()
    url = f"{SUPABASE_URL}/rest/v1/level_tests?id=eq.{test_id}"
    headers = sb_headers()
    headers["Prefer"] = "return=minimal"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.patch(url, headers=headers, json=patch)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"supabase_update_failed HTTP {r.status_code}: {r.text[:300]}")

@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = (req.test_id or "").strip()
    if not test_id:
        raise HTTPException(status_code=422, detail="test_id is required")

    lang = norm_lang(req.lang)

    # 1) test var mı?
    row = await sb_get_level_test(test_id)

    # zaten varsa dokunma
    if row.get("questions"):
        return {"ok": True, "status": row.get("status") or "ready"}

    prompt = build_prompt(lang)

    # 2) Gemini -> OpenAI fallback
    data = await call_gemini(prompt)
    if data is None:
        data = await call_openai(prompt)
    if data is None:
        raise HTTPException(status_code=502, detail="No provider available (GEMINI_API_KEY/OPENAI_API_KEY)")

    # 3) validate
    try:
        questions = validate_questions(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"invalid questions payload: {str(e)}")

    # 4) DB’ye yaz
    await sb_update_level_test(test_id, {
        "questions": {"questions": questions},
        "status": "ready",
    })

    return {"ok": True, "status": "ready"}
