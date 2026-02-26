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
from supabase import create_client

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["level-test"])

# -------------------------
# ENV (lazy init)
# -------------------------
SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").strip()
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

def get_sb_admin():
    """
    Render deploy sırasında env boş/yanlışsa app'i düşürmemek için lazy init.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing in env"
        )
    try:
        return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception as e:
        # 500 yerine detaylı hata dönelim (invalid key vb.)
        raise HTTPException(status_code=500, detail=f"Supabase init failed: {str(e)}")


class GenReq(BaseModel):
    test_id: str
    lang: str = "en"


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
- Options must be plausible and same language.
- correct_index must be 0..3.
"""


def extract_json_from_text(text: str) -> Dict[str, Any]:
    """
    Model bazen JSON dışında yazı dökerse diye en dıştaki { ... } bloğunu çeker.
    """
    if not text:
        raise ValueError("empty provider response")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON block not found in provider response")
    return json.loads(m.group(0))


async def call_gemini(prompt: str) -> Optional[Dict[str, Any]]:
    """
    Gemini REST çağrısı (varsa).
    """
    key = (os.getenv("GEMINI_API_KEY", "") or "").strip()
    model = (os.getenv("GEMINI_MODEL", "gemini-1.5-flash") or "").strip()
    if not key:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 8192,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            logger.error("GEMINI_FAIL %s %s", r.status_code, r.text[:400])
            return None

        j = r.json()
        # candidates[0].content.parts[0].text
        txt = ""
        try:
            txt = j["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            txt = ""

        if not txt:
            return None

        return extract_json_from_text(txt)
    except Exception as e:
        logger.exception("GEMINI_EXCEPTION: %s", e)
        return None


async def call_openai(prompt: str) -> Optional[Dict[str, Any]]:
    """
    OpenAI chat çağrısı (Gemini yoksa fallback).
    """
    key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
    model = (os.getenv("OPENAI_TEST_MODEL", "gpt-4o-mini") or "").strip()
    if not key:
        return None

    url = "https://api.openai.com/v1/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return STRICT JSON only. No markdown."},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(url, json=payload, headers=headers)

        if r.status_code >= 400:
            logger.error("OPENAI_FAIL %s %s", r.status_code, r.text[:400])
            return None

        j = r.json()
        txt = ""
        try:
            txt = j["choices"][0]["message"]["content"]
        except Exception:
            txt = ""

        if not txt:
            return None

        # OpenAI json_object modunda zaten JSON döner ama yine de sağlam kalsın:
        return json.loads(txt)
    except Exception as e:
        logger.exception("OPENAI_EXCEPTION: %s", e)
        return None


def normalize_questions(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    q = data.get("questions")
    if not isinstance(q, list):
        raise ValueError("questions is not a list")

    if len(q) != 50:
        raise ValueError(f"questions length must be 50, got {len(q)}")

    # hafif doğrulama
    for i, item in enumerate(q, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"question[{i}] not object")
        if "question" not in item or "options" not in item:
            raise ValueError(f"question[{i}] missing fields")
        opts = item.get("options")
        if not isinstance(opts, list) or len(opts) != 4:
            raise ValueError(f"question[{i}] options must be 4")
        ci = item.get("correct_index")
        if not isinstance(ci, int) or not (0 <= ci <= 3):
            raise ValueError(f"question[{i}] correct_index must be 0..3")

    return q


@router.post("/level_test/generate")
async def generate_level_test(req: GenReq):
    test_id = (req.test_id or "").strip()
    lang = (req.lang or "en").strip().lower()

    if not test_id:
        raise HTTPException(status_code=422, detail="test_id required")

    sb_admin = get_sb_admin()

    # 1) test var mı?
    try:
        res = sb_admin.table("level_tests").select("id,status,questions").eq("id", test_id).execute()
        row = None
        if isinstance(res.data, list):
            row = res.data[0] if res.data else None
        elif isinstance(res.data, dict):
            row = res.data
        if not row:
            raise HTTPException(status_code=404, detail="test not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("SUPABASE_SELECT_FAIL: %s", e)
        raise HTTPException(status_code=500, detail=f"db select failed: {str(e)}")

    # zaten hazırsa
    if row.get("questions"):
        return {"ok": True, "status": row.get("status", "ready")}

    prompt = build_prompt(lang)

    # 2) Gemini -> OpenAI fallback
    data = await call_gemini(prompt)
    if data is None:
        data = await call_openai(prompt)

    if data is None:
        raise HTTPException(status_code=502, detail="No provider available (GEMINI_API_KEY or OPENAI_API_KEY missing/failed)")

    try:
        questions = normalize_questions(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"invalid questions payload: {str(e)}")

    # 3) DB’ye yaz
    try:
        sb_admin.table("level_tests").update({
            "questions": {"questions": questions},
            "status": "ready",
        }).eq("id", test_id).execute()
    except Exception as e:
        logger.exception("SUPABASE_UPDATE_FAIL: %s", e)
        raise HTTPException(status_code=500, detail=f"db update failed: {str(e)}")

    return {"ok": True, "status": "ready"}
