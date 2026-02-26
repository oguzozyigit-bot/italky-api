# FILE: italky-api/app/routers/level_test.py
from __future__ import annotations

import os
import json
import re
import logging
from typing import Any, Dict, Optional, List

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["level-test"])

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    # Render'da env eksikse net söyle
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing in env")

# Supabase PostgREST base
REST_BASE = SUPABASE_URL.rstrip("/") + "/rest/v1"

def _sb_headers() -> Dict[str, str]:
    # sb_secret_... veya eyJ... fark etmez: PostgREST header'da kullanır
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

class GenReq(BaseModel):
    test_id: str
    lang: str

def build_prompt(lang: str) -> str:
    # 40 dk / 50 soru: “gerçek seviye tespit”
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
        raise ValueError("JSON not found in model output")
    return json.loads(m.group(0))

async def sb_select_level_test(test_id: str) -> Dict[str, Any]:
    # level_tests?id=eq.<uuid>&select=id,status,questions
    url = f"{REST_BASE}/level_tests"
    params = {
        "select": "id,status,questions,language_code",
        "id": f"eq.{test_id}",
        "limit": "1",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_sb_headers(), params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"supabase_select_failed {r.status_code}: {r.text[:500]}")
    arr = r.json()
    if not arr:
        raise HTTPException(status_code=404, detail="test not found")
    return arr[0]

async def sb_update_level_test(test_id: str, patch: Dict[str, Any]) -> None:
    # PATCH level_tests?id=eq.<uuid>
    url = f"{REST_BASE}/level_tests?id=eq.{test_id}"
    headers = _sb_headers()
    # PostgREST: return=minimal
    headers["Prefer"] = "return=minimal"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.patch(url, headers=headers, content=json.dumps(patch))
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"supabase_update_failed {r.status_code}: {r.text[:500]}")

async def call_openai(prompt: str) -> Optional[dict]:
    """
    OpenAI JSON üretimi (fallback).
    Not: Senin projede farklı bir OpenAI wrapper varsa onu kullanabilirsin.
    """
    if not OPENAI_API_KEY:
        return None

    # OpenAI python 1.x “Responses” yerine basit HTTP ile gidelim (kütüphane çakışmasın)
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": "You output STRICT JSON only."},
            {"role": "user", "content": prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, headers=headers, json=payload)
    if r.status_code >= 400:
        logger.error("OPENAI_FAIL %s %s", r.status_code, r.text[:300])
        return None

    data = r.json()
    txt = data["choices"][0]["message"]["content"]
    try:
        return json.loads(txt)
    except Exception:
        return extract_json(txt)

async def call_gemini(prompt: str) -> Optional[dict]:
    """
    Gemini varsa kullan (primary).
    Burada basit REST ile koydum. Projende hazır gemini kodun varsa burayı ona bağla.
    """
    if not GEMINI_API_KEY:
        return None

    # Gemini REST (v1beta) – en basit şekilde
    # Not: model adını env’den değiştirebilirsin
    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ],
        "generationConfig": {"temperature": 0.3}
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, json=payload)
    if r.status_code >= 400:
        logger.error("GEMINI_FAIL %s %s", r.status_code, r.text[:300])
        return None

    j = r.json()
    # Çıktı textini çek
    try:
        txt = j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None

    try:
        return json.loads(txt)
    except Exception:
        return extract_json(txt)

@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = (req.test_id or "").strip()
    lang = (req.lang or "en").strip().lower()

    if not test_id:
        raise HTTPException(422, "test_id is required")

    # 1) test var mı?
    row = await sb_select_level_test(test_id)

    # zaten doluysa çık
    if row.get("questions"):
        return {"ok": True, "status": row.get("status", "ready")}

    prompt = build_prompt(lang)

    # 2) Gemini -> OpenAI fallback
    data = await call_gemini(prompt)
    if data is None:
        data = await call_openai(prompt)

    if data is None:
        raise HTTPException(502, "No provider available (GEMINI_API_KEY / OPENAI_API_KEY missing or failing)")

    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or len(questions) != 50:
        raise HTTPException(500, "invalid questions payload (must be exactly 50 questions)")

    # küçük doğrulama
    for i, q in enumerate(questions[:3]):
        if not isinstance(q, dict) or "question" not in q or "options" not in q or "correct_index" not in q:
            raise HTTPException(500, f"invalid question schema at index {i}")

    # 3) DB’ye yaz
    await sb_update_level_test(test_id, {
        "questions": {"questions": questions},
        "status": "ready",
        "language_code": lang,  # varsa tutarlı kalsın
    })

    return {"ok": True, "status": "ready"}
