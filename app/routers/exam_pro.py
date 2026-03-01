from __future__ import annotations

import os
import re
import json
import logging
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("exam-pro")
router = APIRouter(tags=["exam-pro"])

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("EXAM_OPENAI_MODEL") or "gpt-4o-mini").strip()

if not OPENAI_API_KEY:
    logger.warning("EXAM_PRO: OPENAI_API_KEY missing (router will 500 on requests)")

# ✅ Client'ı tek kez oluştur (performans)
_client = None
def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client

# -----------------------------
# Schemas
# -----------------------------
class SolveTextReq(BaseModel):
    text: str = Field(..., min_length=5, max_length=20000)
    grade: str = Field(default="lise", max_length=32)  # "ortaokul" | "lise"
    locale: str = Field(default="tr", max_length=8)
    # ✅ tek cevap veya JSON map destek (örn {"1":"B","2":"x=3"})
    student_answer: Optional[str] = Field(default=None, max_length=6000)
    mode: str = Field(default="auto", max_length=16)  # "auto" | "single" | "multi"


class SolvedOne(BaseModel):
    q_no: int
    question: str
    choices: Optional[List[str]] = None

    topic: str
    subtopic: str
    difficulty: str

    steps: List[str]
    final_answer: str
    explanation_short: str

    student_correct: Optional[bool] = None
    why_wrong: Optional[str] = None
    step_by_step_fix: Optional[List[str]] = None

    offer: str


class SolveTextResp(BaseModel):
    ok: bool
    detected_count: int
    questions: List[SolvedOne]


# -----------------------------
# Helpers: split multi-question text
# -----------------------------
_NUM_SPLIT = re.compile(r"(?m)^\s*(\d{1,2})\s*[\)\.\-:]\s+")
_BULLET_SPLIT = re.compile(r"(?m)^\s*[-•]\s+")
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)

def _normalize_text(t: str) -> str:
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

def split_questions(raw: str, mode: str = "auto") -> List[Dict[str, Any]]:
    """
    Returns list of {"q_no": int, "text": str}
    """
    text = _normalize_text(raw)

    if mode == "single":
        return [{"q_no": 1, "text": text}]

    # 1) Numbered questions: 1) 2) 3.
    hits = list(_NUM_SPLIT.finditer(text))
    if hits:
        parts: List[Dict[str, Any]] = []
        for i, m in enumerate(hits):
            start = m.start()
            end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
            q_no = int(m.group(1))
            block = text[start:end].strip()

            # ✅ "1) " prefixi temizle
            block = _NUM_SPLIT.sub("", block, count=1).strip()

            if len(block) >= 20:
                parts.append({"q_no": q_no, "text": block})
        if parts:
            return parts

    # 2) Bullet-style fallback
    if mode in ("auto", "multi") and _BULLET_SPLIT.search(text):
        chunks = re.split(_BULLET_SPLIT, text)
        chunks = [c.strip() for c in chunks if c.strip()]
        if len(chunks) >= 2:
            out = []
            for i, c in enumerate(chunks[:20], start=1):
                out.append({"q_no": i, "text": c})
            return out

    # 3) fallback single
    return [{"q_no": 1, "text": text}]


# -----------------------------
# Student answer parsing
# -----------------------------
def parse_student_answers(raw: Optional[str]) -> Dict[int, str]:
    """
    Supports:
    - None -> {}
    - Plain text -> {1: text}
    - JSON like {"1":"B","2":"x=3"} -> {1:"B", 2:"x=3"}
    """
    if not raw:
        return {}

    s = raw.strip()
    if not s:
        return {}

    # JSON map dene
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            out: Dict[int, str] = {}
            for k, v in obj.items():
                try:
                    ki = int(str(k).strip())
                except Exception:
                    continue
                vv = str(v).strip()
                if vv:
                    out[ki] = vv
            if out:
                return out
    except Exception:
        pass

    # fallback: tek cevap, q1
    return {1: s}


# -----------------------------
# OpenAI call + JSON extraction
# -----------------------------
def build_system_prompt(grade: str, locale: str, has_student_answer: bool) -> str:
    g = (grade or "lise").lower().strip()
    audience = "Türkiye lise (9-12) düzeyi" if g == "lise" else "Türkiye ortaokul (5-8) düzeyi"

    # ✅ student_answer varsa ekstra alanları zorunlu yap
    extra = ""
    if has_student_answer:
        extra = """
EK ALANLAR (student_answer geldiğinde):
- student_correct: true/false
- why_wrong: yanlışsa kısa sebep
- step_by_step_fix: yanlışsa doğruya götüren 3-7 adım
""".strip()

    return f"""
Sen italky'nin PRO Sınav Çözüm Motorusun.
Hedef: {audience}.
Dil: {locale} (çıktıyı Türkçe ver).

ÇIKTI KURALI:
- SADECE geçerli JSON döndür. Markdown yok, açıklama yok.
- Her soru için adım adım çözüm üret.
- Matematikte işlem adımlarını açık yaz.
- Sonuç kesin olsun.

JSON ŞEMASI:
{{
  "topic": "string",
  "subtopic": "string",
  "difficulty": "Kolay|Orta|Zor",
  "final_answer": "string",
  "steps": ["string", "..."],
  "explanation_short": "string",
  "choices": ["...","..."]  // şık varsa liste, yoksa null
  {', "student_correct": true, "why_wrong": "string", "step_by_step_fix": ["..."]' if has_student_answer else ""}
}}

EK KURAL:
- Eğer metinde şıklar varsa choices dolu gelsin. Yoksa null.
- final_answer içinde sadece sonuç (ör: x=2, 24, 5√2, B şıkkı).
- steps içinde 4–10 madde arası.
{extra}
""".strip()

def build_user_prompt(q_text: str, student_answer: Optional[str]) -> str:
    sa = (student_answer or "").strip()
    sa_block = ""
    if sa:
        sa_block = f'\nÖĞRENCİ CEVABI: "{sa}"\n'
    return f"""
SORU:
{q_text}
{sa_block}
""".strip()

def extract_json_from_text(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        raise ValueError("empty model output")
    # model bazen ufak ön/arka metin eklerse
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)
    m = _JSON_OBJ_RE.search(t)
    if not m:
        raise ValueError("JSON not found in model output")
    return json.loads(m.group(0))

async def solve_with_openai(system: str, user: str) -> Dict[str, Any]:
    try:
        client = get_client()
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise RuntimeError("empty model output")
        try:
            return json.loads(content)
        except Exception:
            return extract_json_from_text(content)
    except Exception as e:
        logger.exception("OPENAI_SOLVE_FAIL: %s", e)
        raise

def make_offer(topic: str, grade: str) -> str:
    g = (grade or "lise").lower().strip()
    if g == "ortaokul":
        return f'Bu konu (**{topic}**) için 15 dakikalık mini ders ister misin?'
    return f'Bu konu (**{topic}**) için 15 dakikalık PRO özel ders ister misin?'


# -----------------------------
# Route
# -----------------------------
@router.post("/exam/solve_text", response_model=SolveTextResp)
async def exam_solve_text(req: SolveTextReq) -> SolveTextResp:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing on server")

    blocks = split_questions(req.text, mode=req.mode)
    blocks = blocks[:8]  # 1 sayfada çok soru olabilir: pro limit

    # ✅ öğrenci cevapları: map halinde
    answer_map = parse_student_answers(req.student_answer)

    solved: List[SolvedOne] = []

    for b in blocks:
        q_no = int(b["q_no"])
        q_text = str(b["text"])

        sa = answer_map.get(q_no) or None
        system = build_system_prompt(req.grade, req.locale, has_student_answer=bool(sa))
        user_prompt = build_user_prompt(q_text, sa)

        data = await solve_with_openai(system, user_prompt)

        topic = str(data.get("topic") or "Konu").strip()
        subtopic = str(data.get("subtopic") or "Alt konu").strip()
        difficulty = str(data.get("difficulty") or "Orta").strip()
        final_answer = str(data.get("final_answer") or "").strip()
        explanation_short = str(data.get("explanation_short") or "").strip()

        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        steps = [str(x).strip() for x in steps if str(x).strip()]

        choices = data.get("choices")
        if not (isinstance(choices, list) and len(choices) >= 2):
            choices = None
        else:
            choices = [str(x).strip() for x in choices if str(x).strip()]

        student_correct = None
        why_wrong = None
        step_by_step_fix = None

        if sa:
            sc = data.get("student_correct")
            if isinstance(sc, bool):
                student_correct = sc
            why_wrong = str(data.get("why_wrong") or "").strip() or None
            sfix = data.get("step_by_step_fix")
            if isinstance(sfix, list):
                step_by_step_fix = [str(x).strip() for x in sfix if str(x).strip()] or None

        solved.append(SolvedOne(
            q_no=q_no,
            question=q_text,
            choices=choices,
            topic=topic,
            subtopic=subtopic,
            difficulty=difficulty,
            steps=steps,
            final_answer=final_answer,
            explanation_short=explanation_short,
            student_correct=student_correct,
            why_wrong=why_wrong,
            step_by_step_fix=step_by_step_fix,
            offer=make_offer(topic, req.grade),
        ))

    return SolveTextResp(ok=True, detected_count=len(solved), questions=solved)
