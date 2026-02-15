# FILE: italky-api/app/routers/teacher_chat.py
from __future__ import annotations

import os
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field

router = APIRouter()

# ✅ Basit güvenlik (Supabase Edge Function -> Render)
RENDER_TOKEN = (os.getenv("ITALKY_RENDER_TOKEN") or "").strip()

# ✅ LLM env (Render'da mevcut)
LLM_API_KEY = (os.getenv("OPENAI_API_KEY") or os.getenv("ITALKY_LLM_API_KEY") or "").strip()
LLM_MODEL = (os.getenv("ITALKY_LLM_MODEL") or "gpt-4o-mini").strip()


class TeacherChatIn(BaseModel):
    teacher_id: str = Field(..., max_length=32)
    teacher_name: str = Field(..., max_length=32)   # kullanıcı verdiği isim
    role: str = Field(..., max_length=64)           # English / Deutsch / ...
    level: str = Field(..., max_length=8)           # A0/A1/A2/B1...
    student_name: str = Field(..., max_length=64)
    user_text: str = Field(..., max_length=2000)

    # ✅ yeni: persona
    coach_gender: Optional[str] = Field(default="female", max_length=16)   # male/female
    coach_style: Optional[str] = Field(default="friendly", max_length=16)  # friendly/cheerful/disciplined/expert
    coach_voice: Optional[str] = Field(default="voice_1", max_length=16)   # voice_1..voice_4 (şimdilik metadata)


class TeacherChatOut(BaseModel):
    teacher: str
    tr: str
    task: str = ""
    task_tr: str = ""


def _require_token(authorization: Optional[str]):
    if not RENDER_TOKEN:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    got = authorization.replace("Bearer", "").strip()
    if got != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _style_rules(style: str) -> str:
    style = (style or "friendly").strip().lower()
    if style == "cheerful":
        return (
            "- Be upbeat, encouraging, and energetic.\n"
            "- Use short motivational phrases.\n"
            "- Keep corrections gentle and positive.\n"
        )
    if style == "disciplined":
        return (
            "- Be structured, strict but respectful.\n"
            "- Correct mistakes clearly.\n"
            "- Give short drills and require precise answers.\n"
        )
    if style == "expert":
        return (
            "- Be professional, concise, and precise.\n"
            "- Explain briefly why something is wrong (in Turkish section only).\n"
            "- Provide higher-quality vocabulary and natural phrasing.\n"
        )
    # friendly
    return (
        "- Be warm and friendly.\n"
        "- Encourage the student.\n"
        "- Keep it simple and clear.\n"
    )


def _gender_voice_hint(gender: str, voice: str) -> str:
    # UI’da motor adı yok; sadece persona ipucu.
    g = (gender or "female").strip().lower()
    v = (voice or "voice_1").strip().lower()
    gtxt = "female" if g == "female" else "male"
    return f"- Persona: {gtxt}\n- Voice preset: {v} (internal)\n"


def _build_system_prompt(tname: str, role: str, level: str, sname: str, style: str, gender: str, voice: str) -> str:
    # ✅ Önemli: öğretmen hedef dilde konuşacak, TR açıklama ayrı alanda
    return f"""
You are a language teacher created by italky Academy.
Your name is: {tname}
Target language: {role}
Student level: {level}
Student name: {sname}

STRICT OUTPUT FORMAT:
Return STRICT JSON ONLY with keys:
teacher, tr, task, task_tr

Rules for "teacher":
- Write ONLY in the TARGET LANGUAGE ({role}).
- Do NOT use Turkish in "teacher".
- Be natural, level-appropriate, and helpful.
- Use the teacher name "{tname}" naturally when appropriate (e.g., greetings).

Rules for "tr":
- Provide Turkish translation/explanation of the teacher message.
- If you correct mistakes, explain briefly in Turkish (clear and kind).

Rules for "task" and "task_tr":
- Provide a short next task/question in TARGET LANGUAGE ("task").
- Provide its Turkish version ("task_tr").
- Keep tasks short and actionable.

Teacher style rules:
{_style_rules(style)}
{_gender_voice_hint(gender, voice)}

Never mention any external provider names. Always act as italky Academy.
""".strip()


@router.post("/teacher-chat", response_model=TeacherChatOut)
async def teacher_chat(payload: TeacherChatIn, authorization: Optional[str] = Header(default=None)):
    _require_token(authorization)

    if not LLM_API_KEY:
        raise HTTPException(status_code=500, detail="LLM key missing on server")

    tname = (payload.teacher_name or "Coach").strip()[:32]
    role = (payload.role or "English").strip()[:64]
    level = (payload.level or "A0").strip()[:8]
    sname = (payload.student_name or "Student").strip()[:64]
    style = (payload.coach_style or "friendly").strip()[:16]
    gender = (payload.coach_gender or "female").strip()[:16]
    voice = (payload.coach_voice or "voice_1").strip()[:16]
    user_text = (payload.user_text or "").strip()

    if not user_text:
        raise HTTPException(status_code=400, detail="Missing user_text")

    system = _build_system_prompt(tname, role, level, sname, style, gender, voice)

    try:
        # Backend içi LLM çağrısı (UI’da adı geçmez)
        from openai import AsyncOpenAI  # zaten projede kullanıyorsun
        client = AsyncOpenAI(api_key=LLM_API_KEY)

        resp = await client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text}
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
                teacher=f"Hello {sname}! I'm {tname}. Write one sentence about your day.",
                tr=f"Merhaba {sname}! Ben {tname}. Günün hakkında bir cümle yaz.",
                task="Write one sentence about your day.",
                task_tr="Günün hakkında bir cümle yaz."
            )

        return TeacherChatOut(teacher=teacher, tr=tr, task=task, task_tr=task_tr)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"teacher-chat failed: {e}")
