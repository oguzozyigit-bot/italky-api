# FILE: italky-api/app/routers/teacher_chat.py
from __future__ import annotations

import os
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field

router = APIRouter()

# Basit güvenlik: Supabase Edge Function -> Render çağrısı için
RENDER_TOKEN = (os.getenv("ITALKY_RENDER_TOKEN") or "").strip()

# Model / key env (Render’da zaten sende hazır)
# Not: Burada motor adını UI’da kullanmayacağız. Bu sadece backend içi.
LLM_API_KEY = (os.getenv("OPENAI_API_KEY") or os.getenv("ITALKY_LLM_API_KEY") or "").strip()
LLM_MODEL = (os.getenv("ITALKY_LLM_MODEL") or "gpt-4o-mini").strip()  # sende farklıysa env’den değiştir

class TeacherChatIn(BaseModel):
    teacher_id: str = Field(..., max_length=32)
    teacher_name: str = Field(..., max_length=64)
    role: str = Field(..., max_length=64)           # English / Deutsch / ...
    level: str = Field(..., max_length=8)           # A0/A1/A2/B1...
    student_name: str = Field(..., max_length=64)
    user_text: str = Field(..., max_length=2000)

class TeacherChatOut(BaseModel):
    teacher: str
    tr: str
    task: str = ""
    task_tr: str = ""

def _require_token(authorization: Optional[str]):
    if not RENDER_TOKEN:
        return  # token koymadıysan kontrol etmeyelim
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    got = authorization.replace("Bearer", "").strip()
    if got != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _build_system_prompt(tname: str, role: str, level: str, sname: str) -> str:
    return f"""
You are {tname}, a friendly language teacher created by italky Academy.

TARGET LANGUAGE: {role}
Student level: {level}
Student name: {sname}

Rules:
- Respond in TARGET LANGUAGE ONLY in the "teacher" field.
- Do NOT use Turkish in "teacher".
- Provide Turkish translation/explanation in the "tr" field.
- Keep it short, natural, level-appropriate.
- If the student makes mistakes, correct gently (in TARGET LANGUAGE).
- Then give a short next task/question in TARGET LANGUAGE ("task") and its Turkish version ("task_tr").

Return STRICT JSON with keys:
teacher, tr, task, task_tr
""".strip()

@router.post("/teacher-chat", response_model=TeacherChatOut)
async def teacher_chat(payload: TeacherChatIn, authorization: Optional[str] = Header(default=None)):
    _require_token(authorization)

    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="LLM key missing on server")

    # --- LLM call ---
    # Bu backend içinde motoru kullanır; UI tarafında motor adı asla geçmez.
    try:
        from openai import AsyncOpenAI  # paket sende zaten mevcut olmalı (router'larda kullanıyorsun)
        client = AsyncOpenAI(api_key=LLM_API_KEY)

        system = _build_system_prompt(payload.teacher_name, payload.role, payload.level, payload.student_name)

        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": payload.user_text.strip()}
            ],
            temperature=0.7,
        )

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("empty model output")

        import json
        data: Dict[str, Any] = json.loads(content)

        teacher = str(data.get("teacher", "")).strip()
        tr = str(data.get("tr", "")).strip()
        task = str(data.get("task", "")).strip()
        task_tr = str(data.get("task_tr", "")).strip()

        if not teacher or not tr:
            # fallback
            return TeacherChatOut(
                teacher="Let's continue. Please write one sentence.",
                tr="Devam edelim. Lütfen bir cümle yaz.",
                task="Write one sentence about your day.",
                task_tr="Günün hakkında bir cümle yaz."
            )

        # ekstra güvenlik: teacher kısmında Türkçe kaçarsa (çok nadir) kırp
        # (kesin garanti değil ama pratik koruma)
        if any(ch in teacher for ch in "ğüşöçıİ"):
            # teacher Türkçe karakter içeriyorsa, teacher'ı kısalt ve task'a yönlendir
            teacher = teacher.replace("ğ","g").replace("ü","u").replace("ş","s").replace("ö","o").replace("ç","c").replace("ı","i").replace("İ","I")

        return TeacherChatOut(teacher=teacher, tr=tr, task=task, task_tr=task_tr)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"teacher-chat failed: {e}")
