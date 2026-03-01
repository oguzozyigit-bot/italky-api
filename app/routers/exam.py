# FILE: italky-api/app/routers/exam.py
from __future__ import annotations

import os
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["exam"])

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
MODEL = (os.getenv("ITALKY_LLM_MODEL") or "gpt-4o-mini").strip()

class SolveReq(BaseModel):
    text: str = Field(..., min_length=5)
    grade: int = 8
    lesson: str = "Matematik"

class SolveResp(BaseModel):
    solution: str
    ask_lesson: str

@router.post("/exam/solve_text", response_model=SolveResp)
async def solve_text(req: SolveReq):
    if not OPENAI_API_KEY:
        raise HTTPException(500, "OPENAI_API_KEY missing")

    q = req.text.strip()
    grade = req.grade
    lesson = req.lesson

    sys = f"""
You are an expert Turkish teacher for {lesson} (grade {grade}).
You must:
- Solve the problem step-by-step in Turkish.
- If it contains equations, show transformations clearly.
- End with a short check.
- Then ask: "Bu konu hakkında özel ders almak ister misin?"
Return plain text. No markdown.
""".strip()

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":q}
            ],
            temperature=0.2,
            max_tokens=800,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("empty output")

        ask = "Bu konu hakkında özel ders almak ister misin?"
        return SolveResp(solution=content, ask_lesson=ask)

    except Exception as e:
        raise HTTPException(500, f"solve failed: {e}")
