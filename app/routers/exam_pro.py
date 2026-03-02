from __future__ import annotations

import os
import re
import json
import time
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("exam-pro")
router = APIRouter(tags=["exam-pro"])

# =============================
# ENV
# =============================
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("EXAM_OPENAI_MODEL") or "gpt-4o-mini").strip()

GOOGLE_VISION_API_KEY = (os.getenv("GOOGLE_VISION_API_KEY") or "").strip()
GOOGLE_CSE_API_KEY = (os.getenv("GOOGLE_CSE_API_KEY") or "").strip()
GOOGLE_CSE_CX = (os.getenv("GOOGLE_CSE_CX") or "").strip()

if not OPENAI_API_KEY:
    logger.warning("EXAM_PRO: OPENAI_API_KEY missing (router will 500 on OpenAI requests)")

# =============================
# OpenAI client (singleton)
# =============================
_client = None
def get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client


# =============================
# Schemas - EXISTING (solve_text)
# =============================
class SolveTextReq(BaseModel):
    text: str = Field(..., min_length=5, max_length=20000)
    grade: str = Field(default="lise", max_length=32)  # "ortaokul" | "lise"
    locale: str = Field(default="tr", max_length=8)
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


# =============================
# NEW Schema - solve_v4 (image)
# =============================
class SolveV4Req(BaseModel):
    image: str = Field(..., min_length=50, max_length=25_000_000)  # base64 dataURL
    engine_mode: str = Field(default="italky_hybrid", max_length=32)
    locale: str = Field(default="tr", max_length=8)
    grade: str = Field(default="lise", max_length=32)  # ortaokul/lise (ops)
    student_answer: Optional[str] = Field(default=None, max_length=6000)


class SolveV4Resp(BaseModel):
    status: str
    topic: str
    explanation_steps: List[str]
    final_answer: str
    teacher_hook: bool


# =============================
# Helpers: split multi-question text (existing)
# =============================
_NUM_SPLIT = re.compile(r"(?m)^\s*(\d{1,2})\s*[\)\.\-:]\s+")
_BULLET_SPLIT = re.compile(r"(?m)^\s*[-•]\s+")
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.S)

def _normalize_text(t: str) -> str:
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

def split_questions(raw: str, mode: str = "auto") -> List[Dict[str, Any]]:
    text = _normalize_text(raw)

    if mode == "single":
        return [{"q_no": 1, "text": text}]

    hits = list(_NUM_SPLIT.finditer(text))
    if hits:
        parts: List[Dict[str, Any]] = []
        for i, m in enumerate(hits):
            start = m.start()
            end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
            q_no = int(m.group(1))
            block = text[start:end].strip()
            block = _NUM_SPLIT.sub("", block, count=1).strip()
            if len(block) >= 20:
                parts.append({"q_no": q_no, "text": block})
        if parts:
            return parts

    if mode in ("auto", "multi") and _BULLET_SPLIT.search(text):
        chunks = re.split(_BULLET_SPLIT, text)
        chunks = [c.strip() for c in chunks if c.strip()]
        if len(chunks) >= 2:
            out = []
            for i, c in enumerate(chunks[:20], start=1):
                out.append({"q_no": i, "text": c})
            return out

    return [{"q_no": 1, "text": text}]


# =============================
# Student answer parsing (existing)
# =============================
def parse_student_answers(raw: Optional[str]) -> Dict[int, str]:
    if not raw:
        return {}
    s = raw.strip()
    if not s:
        return {}
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
    return {1: s}


# =============================
# Common small utils
# =============================
def extract_json_from_text(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    if not t:
        raise ValueError("empty model output")
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)
    m = _JSON_OBJ_RE.search(t)
    if not m:
        raise ValueError("JSON not found in model output")
    return json.loads(m.group(0))

def _strip_dataurl(b64: str) -> str:
    s = (b64 or "").strip()
    if "," in s and s.lower().startswith("data:"):
        return s.split(",", 1)[1].strip()
    return s

def _is_question_like(text: str) -> bool:
    t = (text or "").lower()
    if len(t.strip()) < 10:
        return False
    signals = ["?", "kaç", "bul", "çöz", "a)", "b)", "c)", "d)", "eşittir", "x", "y", "√", "log", "sin", "cos", "türev", "integral", "∫", "Δ"]
    return any(s in t for s in signals)

def make_offer(topic: str, grade: str) -> str:
    g = (grade or "lise").lower().strip()
    if g == "ortaokul":
        return f'Bu konu (**{topic}**) için 15 dakikalık mini ders ister misin?'
    return f'Bu konu (**{topic}**) için 15 dakikalık PRO özel ders ister misin?'


# =============================
# OpenAI - existing solve_text JSON prompt
# =============================
def build_system_prompt(grade: str, locale: str, has_student_answer: bool) -> str:
    g = (grade or "lise").lower().strip()
    audience = "Türkiye lise (9-12) düzeyi" if g == "lise" else "Türkiye ortaokul (5-8) düzeyi"

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

async def solve_with_openai_json(system: str, user: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")

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


# =============================
# OpenAI - NEW: solve_v4 JSON (no Google/OpenAI names in output)
# =============================
def _system_prompt_solve_v4(locale: str, grade: str) -> str:
    g = (grade or "lise").lower().strip()
    audience = "Türkiye lise (9-12) düzeyi" if g == "lise" else "Türkiye ortaokul (5-8) düzeyi"
    return f"""
Sen italkyAI Kuantum Çözücü motorusun.
Hedef: {audience}.
Dil: {locale} (çıktıyı Türkçe ver).

ÇIKTI KURALI:
- SADECE geçerli JSON döndür. Markdown yok, açıklama yok.
- Adım adım öğretici çözüm: 4–10 adım.
- final_answer kısa ve net.

JSON ŞEMASI:
{{
  "status":"success",
  "topic":"Konu • Alt Başlık",
  "explanation_steps":["Adım 1: ...","Adım 2: ..."],
  "final_answer":"Cevap: ...",
  "teacher_hook": true
}}

KURAL:
- UI’da hiçbir üçüncü taraf servis adı geçmeyecek. Sadece italkyAI.
""".strip()

def _user_prompt_solve_v4(ocr_text: str, google_snippets: Optional[List[str]] = None) -> str:
    snippets = ""
    if google_snippets:
        snippets = "\n\nKAYNAK KIRINTILARI (kısa):\n" + "\n".join([f"- {s}" for s in google_snippets[:5] if s.strip()])
    return f"""
Aşağıdaki metin bir ders sorusudur. Adım adım öğretici şekilde çöz.

SORU METNİ (OCR):
{ocr_text}
{snippets}
""".strip()

async def solve_v4_with_openai_from_text(ocr_text: str, locale: str, grade: str, google_snippets: Optional[List[str]] = None) -> Dict[str, Any]:
    system = _system_prompt_solve_v4(locale, grade)
    user = _user_prompt_solve_v4(ocr_text, google_snippets)
    return await solve_with_openai_json(system, user)

async def solve_v4_with_openai_from_image(image_dataurl: str, locale: str, grade: str) -> Dict[str, Any]:
    """
    OCR tamamen çökerse: görseli direkt OpenAI'ye yedek olarak gönderir.
    (Google öncelik kuralına uyuyoruz; bu sadece fallback.)
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")

    client = get_client()
    system = _system_prompt_solve_v4(locale, grade)

    # chat.completions image content format
    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": "Bu görseldeki ders sorusunu adım adım çöz ve sadece JSON döndür."},
                {"type": "image_url", "image_url": {"url": image_dataurl}},
            ]},
        ],
    )

    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("empty model output")
    try:
        return json.loads(content)
    except Exception:
        return extract_json_from_text(content)


# =============================
# Google Vision OCR (PRIMARY)
# =============================
async def google_vision_ocr_text(image_base64_noheader: str) -> str:
    """
    Returns extracted full text. Requires GOOGLE_VISION_API_KEY.
    """
    if not GOOGLE_VISION_API_KEY:
        return ""

    import httpx

    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
    payload = {
        "requests": [{
            "image": {"content": image_base64_noheader},
            "features": [{"type": "TEXT_DETECTION"}],
        }]
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code != 200:
                logger.warning("GOOGLE_VISION_OCR_FAIL status=%s body=%s", r.status_code, r.text[:300])
                return ""
            data = r.json()
    except Exception as e:
        logger.warning("GOOGLE_VISION_OCR_EXC %s", e)
        return ""

    try:
        resp0 = (data.get("responses") or [{}])[0]
        # fullTextAnnotation > text is best
        fta = resp0.get("fullTextAnnotation") or {}
        txt = (fta.get("text") or "").strip()
        if txt:
            return txt

        # fallback: first textAnnotation
        anns = resp0.get("textAnnotations") or []
        if anns and isinstance(anns, list):
            t0 = (anns[0].get("description") or "").strip()
            return t0
    except Exception:
        return ""

    return ""


# =============================
# Optional: Google Custom Search snippets (still PRIMARY-side)
# =============================
async def google_cse_snippets(query_text: str, locale: str = "tr") -> List[str]:
    if not (GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX):
        return []

    import httpx
    q = (query_text or "").strip()
    if len(q) < 12:
        return []

    # query'yi kısalt: çok uzun OCR kötü sonuç verir
    q = re.sub(r"\s+", " ", q)
    q = q[:260]

    params = {
        "key": GOOGLE_CSE_API_KEY,
        "cx": GOOGLE_CSE_CX,
        "q": q,
        "num": 3,
        "hl": "tr" if (locale or "tr").startswith("tr") else "en",
        "safe": "active",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://www.googleapis.com/customsearch/v1", params=params)
            if r.status_code != 200:
                return []
            data = r.json()
    except Exception:
        return []

    out: List[str] = []
    items = data.get("items") or []
    for it in items:
        snippet = (it.get("snippet") or "").strip()
        if snippet:
            out.append(snippet)
    return out


# =============================
# Route: EXISTING /exam/solve_text (unchanged behavior)
# =============================
@router.post("/exam/solve_text", response_model=SolveTextResp)
async def exam_solve_text(req: SolveTextReq) -> SolveTextResp:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing on server")

    blocks = split_questions(req.text, mode=req.mode)
    blocks = blocks[:8]

    answer_map = parse_student_answers(req.student_answer)

    solved: List[SolvedOne] = []

    for b in blocks:
        q_no = int(b["q_no"])
        q_text = str(b["text"])

        sa = answer_map.get(q_no) or None
        system = build_system_prompt(req.grade, req.locale, has_student_answer=bool(sa))
        user_prompt = build_user_prompt(q_text, sa)

        data = await solve_with_openai_json(system, user_prompt)

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


# =============================
# Route: NEW /exam/solve_v4  (Google first, OpenAI fallback)
# =============================
@router.post("/exam/solve_v4", response_model=SolveV4Resp)
async def exam_solve_v4(req: SolveV4Req) -> SolveV4Resp:
    t0 = time.time()

    if not req.image:
        raise HTTPException(status_code=400, detail="image missing")

    # 1) Google OCR (PRIMARY)
    img_b64 = _strip_dataurl(req.image)
    ocr_text = await google_vision_ocr_text(img_b64)

    # 2) Validation (ders sorusu değilse asla fallback yakmayalım)
    if ocr_text and not _is_question_like(ocr_text):
        raise HTTPException(
            status_code=422,
            detail="italkyAI sadece eğitim odaklı soruları analiz eder. Lütfen bir soru görseli yükleyin."
        )

    # 3) Primary çözüm: Google OCR + (opsiyonel) Google snippets + OpenAI stepify
    # Not: Adım adım anlatımı en iyi yine LLM veriyor.
    # Google burada "öncelik": OCR ve kaynak kırıntıları tarafında.
    google_snips: List[str] = []
    if ocr_text:
        # İstersen bunu kapatabilirsin; env yoksa zaten boş döner.
        google_snips = await google_cse_snippets(ocr_text, locale=req.locale)

    # 4) Eğer OCR geldiyse: OpenAI ile metinden çöz (hybrid)
    if ocr_text:
        try:
            data = await solve_v4_with_openai_from_text(
                ocr_text=ocr_text,
                locale=req.locale,
                grade=req.grade,
                google_snippets=google_snips
            )
            # normalize output
            status = str(data.get("status") or "success")
            topic = str(data.get("topic") or "italkyAI").strip()
            steps = data.get("explanation_steps") if isinstance(data.get("explanation_steps"), list) else []
            steps = [str(x).strip() for x in steps if str(x).strip()]
            final_answer = str(data.get("final_answer") or "").strip()
            teacher_hook = bool(data.get("teacher_hook", True))

            if not steps or not final_answer:
                raise RuntimeError("weak_result")

            return SolveV4Resp(
                status="success",
                topic=topic,
                explanation_steps=steps,
                final_answer=final_answer,
                teacher_hook=teacher_hook
            )
        except Exception as e:
            logger.warning("SOLVE_V4_TEXT_PATH_FAIL: %s", e)

    # 5) OCR boşsa veya text-path çöktüyse: OpenAI vision fallback
    # Google OCR yoksa mecburen görseli yedek motorla çözdürüyoruz.
    try:
        data = await solve_v4_with_openai_from_image(
            image_dataurl=req.image,
            locale=req.locale,
            grade=req.grade
        )

        status = str(data.get("status") or "success")
        topic = str(data.get("topic") or "italkyAI").strip()
        steps = data.get("explanation_steps") if isinstance(data.get("explanation_steps"), list) else []
        steps = [str(x).strip() for x in steps if str(x).strip()]
        final_answer = str(data.get("final_answer") or "").strip()
        teacher_hook = bool(data.get("teacher_hook", True))

        if not steps or not final_answer:
            raise RuntimeError("weak_result")

        return SolveV4Resp(
            status="success",
            topic=topic,
            explanation_steps=steps,
            final_answer=final_answer,
            teacher_hook=teacher_hook
        )
    except Exception as e:
        logger.exception("SOLVE_V4_FALLBACK_FAIL: %s", e)
        raise HTTPException(status_code=500, detail="Çözüm motoru yoğun. Lütfen tekrar dene.")
