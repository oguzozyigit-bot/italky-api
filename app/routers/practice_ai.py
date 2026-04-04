from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from supabase import create_client, Client
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

router = APIRouter(tags=["practice-ai"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY", "").strip()
    or os.getenv("GOOGLE_API_KEY", "").strip()
)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

if not OPENAI_API_KEY and not GEMINI_API_KEY:
    raise RuntimeError("At least one provider key is required")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


class PracticeChatBody(BaseModel):
    system_prompt: str
    prompt: str
    mode: str
    lang: str
    response_format: str = "json"
    module: str = "practice_ai"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(v: Any):
    if not v:
      return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_bearer(request: Request) -> str:
    auth = str(request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()


def _get_current_user(token: str):
    if not token:
        return None
    try:
        res = supabase.auth.get_user(token)
        return getattr(res, "user", None)
    except Exception:
        return None


def _load_profile(user_id: str):
    q = (
        supabase.table("profiles")
        .select("*")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    if not q.data:
        return None
    return q.data[0]


def _display_name(profile: dict) -> str:
    full_name = str(profile.get("full_name") or "").strip()
    if full_name:
        return full_name.split(" ")[0]

    name = str(profile.get("name") or "").strip()
    if name:
        return name.split(" ")[0]

    teacher_prefs = profile.get("teacher_prefs") or {}
    if isinstance(teacher_prefs, dict) and teacher_prefs:
        numeric_keys = []
        other_keys = []

        for k, v in teacher_prefs.items():
            if not isinstance(v, dict):
                continue
            try:
                numeric_keys.append((int(str(k)), v))
            except Exception:
                other_keys.append(v)

        if numeric_keys:
            numeric_keys.sort(key=lambda x: x[0], reverse=True)
            hitap = str(numeric_keys[0][1].get("student_hitap") or "").strip()
            if hitap:
                return hitap

        for v in other_keys:
            hitap = str(v.get("student_hitap") or "").strip()
            if hitap:
                return hitap

    email = str(profile.get("email") or "").strip()
    if email and "@" in email:
        return email.split("@")[0]

    return ""


def _profile_level_for_lang(profile: dict, lang: str) -> str:
    levels = profile.get("levels") or {}
    if isinstance(levels, dict):
        return str(levels.get(lang) or levels.get(lang.upper()) or "").strip()
    return ""


def _profile_tokens(profile: dict) -> int:
    try:
        return int(profile.get("tokens") or 0)
    except Exception:
        return 0


def _load_memory(user_id: str, lang: str) -> dict | None:
    q = (
        supabase.table("practice_ai_memory")
        .select("*")
        .eq("user_id", user_id)
        .eq("lang", lang)
        .limit(1)
        .execute()
    )
    if not q.data:
        return None
    return q.data[0]


def _upsert_memory(
    user_id: str,
    lang: str,
    *,
    last_topic: str = "",
    last_level_estimate: str = "",
    last_target_phrase: str = "",
    last_session_summary: str = "",
    last_teacher_message: str = "",
) -> None:
    payload = {
        "user_id": user_id,
        "lang": lang,
        "last_topic": last_topic or None,
        "last_level_estimate": last_level_estimate or None,
        "last_target_phrase": last_target_phrase or None,
        "last_session_summary": last_session_summary or None,
        "last_teacher_message": last_teacher_message or None,
        "updated_at": _now_utc().isoformat(),
    }

    existing = _load_memory(user_id, lang)
    if existing:
        supabase.table("practice_ai_memory").update(payload).eq("id", existing["id"]).execute()
    else:
        supabase.table("practice_ai_memory").insert(payload).execute()


def _summarize_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return "No previous lesson memory."

    return (
        f"Previous lesson memory:\n"
        f"- last_topic: {memory.get('last_topic') or 'unknown'}\n"
        f"- last_level_estimate: {memory.get('last_level_estimate') or 'unknown'}\n"
        f"- last_target_phrase: {memory.get('last_target_phrase') or 'none'}\n"
        f"- last_session_summary: {memory.get('last_session_summary') or 'none'}\n"
        f"- last_teacher_message: {memory.get('last_teacher_message') or 'none'}"
    )


def _extract_summary_fields(parsed: dict) -> dict:
    lesson_stage = str(parsed.get("lesson_stage") or "").strip()
    target_phrase = str(parsed.get("target_phrase") or "").strip()
    reply_tr = str(parsed.get("reply_tr") or "").strip()
    reply = str(parsed.get("reply") or "").strip()
    level_estimate = str(parsed.get("level_estimate") or "").strip()
    topic = str(parsed.get("topic") or "").strip()

    topic_map = {
        "placement": "placement",
        "practice": "daily practice",
        "repeat": "pronunciation repeat",
        "correction": "pronunciation correction",
    }

    return {
        "last_topic": topic or topic_map.get(lesson_stage, lesson_stage or "daily practice"),
        "last_level_estimate": level_estimate,
        "last_target_phrase": target_phrase,
        "last_session_summary": reply_tr,
        "last_teacher_message": reply,
    }


def _extract_block(raw: str, key: str) -> str:
    pattern = rf"{key}:(.*?)(?:\n[A-Z_]+:|$)"
    m = re.search(pattern, raw, flags=re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


def _parse_model_fields(raw_text: str) -> dict | None:
    raw_text = str(raw_text or "").strip()
    if not raw_text:
        return None

    reply = _extract_block(raw_text, "REPLY")
    reply_tr = _extract_block(raw_text, "REPLY_TR")
    target_phrase = _extract_block(raw_text, "TARGET_PHRASE")
    should_repeat_raw = _extract_block(raw_text, "SHOULD_REPEAT").lower()
    lesson_stage = _extract_block(raw_text, "LESSON_STAGE")
    level_estimate = _extract_block(raw_text, "LEVEL_ESTIMATE")
    topic = _extract_block(raw_text, "TOPIC")
    repeat_hint_tr = _extract_block(raw_text, "REPEAT_HINT_TR")

    if not reply and not reply_tr and not target_phrase and not lesson_stage:
        return None

    return {
        "reply": reply,
        "reply_tr": reply_tr,
        "target_phrase": target_phrase,
        "repeat_hint_tr": repeat_hint_tr,
        "should_repeat": should_repeat_raw in ("true", "1", "yes"),
        "lesson_stage": lesson_stage or "practice",
        "level_estimate": level_estimate,
        "topic": topic,
    }


def _fallback_tr(reply: str, lang: str) -> str:
    lang_name = {
        "en": "İngilizce",
        "de": "Almanca",
        "fr": "Fransızca",
        "es": "İspanyolca",
        "it": "İtalyanca",
    }.get(lang, "yabancı dil")

    if not reply:
        return "Öğretmen konuşuyor."

    return f"Öğretmen {lang_name} konuşuyor ve dersi yönlendiriyor."


def _fallback_repeat_hint(target_phrase: str) -> str:
    clean = str(target_phrase or "").strip()
    if not clean:
        return ""
    return f"Şunu tekrar et: {clean}"


def _openai_extract_output_text(payload: dict) -> str:
    chunks: list[str] = []

    output = payload.get("output") or []
    for item in output:
        content = item.get("content") or []
        for part in content:
            if part.get("type") in ("output_text", "text"):
                txt = part.get("text") or ""
                if txt:
                    chunks.append(txt)

    if chunks:
        return "".join(chunks).strip()

    for key in ("output_text", "text"):
        txt = payload.get(key)
        if isinstance(txt, str) and txt.strip():
            return txt.strip()

    return ""


def _call_openai_teacher(final_prompt: str) -> str:
    if not OPENAI_API_KEY:
        return ""

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": OPENAI_MODEL,
        "input": final_prompt,
    }

    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers=headers,
        json=body,
        timeout=60,
    )

    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"openai_http_{r.status_code}")

    payload = r.json()
    raw_text = _openai_extract_output_text(payload)
    print("RAW OPENAI TEXT REPR:", repr(raw_text))
    return raw_text


def _extract_text_from_gemini_response(resp) -> str:
    try:
        txt = getattr(resp, "text", "") or ""
        if txt:
            return txt.strip()
    except Exception:
        pass

    try:
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) or []
            chunks = []
            for p in parts:
                t = getattr(p, "text", None)
                if t:
                    chunks.append(t)
            return "".join(chunks).strip()
    except Exception:
        pass

    return ""


def _call_gemini_teacher(final_prompt: str) -> str:
    if not GEMINI_API_KEY:
        return ""

    model = genai.GenerativeModel(GEMINI_MODEL)
    resp = model.generate_content(
        final_prompt,
        generation_config={
            "temperature": 0.55,
            "max_output_tokens": 220,
        },
    )
    raw_text = _extract_text_from_gemini_response(resp)
    print("RAW GEMINI TEXT REPR:", repr(raw_text))
    return raw_text


def _provider_reply(final_prompt: str) -> str:
    gemini_error: Exception | None = None
    openai_error: Exception | None = None

    # Gemini birinci tercih
    if GEMINI_API_KEY:
        try:
            raw = _call_gemini_teacher(final_prompt)
            if raw:
                return raw
        except ResourceExhausted as e:
            print("GEMINI QUOTA ERROR:", repr(e))
            gemini_error = e
        except Exception as e:
            print("GEMINI ERROR:", repr(e))
            gemini_error = e

    # Gemini yoksa / hata verirse OpenAI
    if OPENAI_API_KEY:
        try:
            raw = _call_openai_teacher(final_prompt)
            if raw:
                return raw
        except Exception as e:
            print("OPENAI FALLBACK ERROR:", repr(e))
            openai_error = e

    if isinstance(gemini_error, ResourceExhausted):
        raise HTTPException(status_code=429, detail="practice_ai_temporarily_busy")

    if gemini_error or openai_error:
        raise HTTPException(status_code=502, detail="practice_ai_provider_failed")

    raise HTTPException(status_code=502, detail="practice_ai_no_provider_available")


def _postprocess_teacher_output(parsed: dict, score_value: Any, display_name: str, first_turn: bool) -> dict:
    reply = str(parsed.get("reply") or "").strip()
    reply_tr = str(parsed.get("reply_tr") or "").strip()
    target_phrase = str(parsed.get("target_phrase") or "").strip()
    repeat_hint_tr = str(parsed.get("repeat_hint_tr") or "").strip()
    should_repeat = bool(parsed.get("should_repeat"))
    lesson_stage = str(parsed.get("lesson_stage") or "practice").strip() or "practice"

    # İsim kullanımını gevşet
    if not first_turn and display_name:
        patterns = [
            rf"^\s*{re.escape(display_name)}[\s,:!.-]+",
            rf"^\s*merhaba\s+{re.escape(display_name)}[\s,:!.-]+",
            rf"^\s*hi\s+{re.escape(display_name)}[\s,:!.-]+",
            rf"^\s*hello\s+{re.escape(display_name)}[\s,:!.-]+",
        ]
        for p in patterns:
            reply = re.sub(p, "", reply, flags=re.IGNORECASE).strip()

    # 95 ve üzeri ise tekrar döngüsünü kapat
    try:
        score_num = float(score_value) if score_value is not None else None
    except Exception:
        score_num = None

    if score_num is not None and score_num >= 95:
        should_repeat = False
        target_phrase = ""
        repeat_hint_tr = ""
        if lesson_stage in ("repeat", "correction"):
            lesson_stage = "practice"

    if target_phrase and not repeat_hint_tr:
        repeat_hint_tr = _fallback_repeat_hint(target_phrase)

    parsed["reply"] = reply
    parsed["reply_tr"] = reply_tr
    parsed["target_phrase"] = target_phrase
    parsed["repeat_hint_tr"] = repeat_hint_tr
    parsed["should_repeat"] = should_repeat
    parsed["lesson_stage"] = lesson_stage
    return parsed


@router.post("/api/practice/chat")
async def practice_chat(body: PracticeChatBody, request: Request):
    print("PRACTICE_AI ROUTE HIT")
    print("LANG:", body.lang)
    print("MODE:", body.mode)

    token = _extract_bearer(request)
    user = _get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")

    profile = _load_profile(user.id)
    print("PROFILE RAW:", profile)

    if not profile:
        raise HTTPException(status_code=404, detail="profile_not_found")

    tokens = _profile_tokens(profile)

    print("USER:", getattr(user, "id", None))
    print("DISPLAY_NAME:", _display_name(profile))
    print("TOKENS:", tokens)

    if tokens <= 0:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    display_name = _display_name(profile)
    profile_level = _profile_level_for_lang(profile, body.lang)
    memory = _load_memory(user.id, body.lang)

    score_match = re.search(r"Pronunciation score:\s*([0-9]+(?:\.[0-9]+)?)", body.prompt or "", flags=re.IGNORECASE)
    score_value = float(score_match.group(1)) if score_match else None
    first_turn = not str(body.prompt or "").strip()

    final_prompt = (
        f"{body.system_prompt}\n\n"
        f"Student display name: {display_name or 'student'}\n"
        f"Profile level for selected language: {profile_level or 'unknown'}\n"
        f"{_summarize_memory_for_prompt(memory)}\n\n"
        f"{body.prompt}\n\n"
        f"Important output rule:\n"
        f"Return plain text only in exactly this format:\n"
        f"REPLY: <teacher visible reply in target language only>\n"
        f"REPLY_TR: <short Turkish meaning>\n"
        f"TARGET_PHRASE: <exact phrase to repeat if needed>\n"
        f"REPEAT_HINT_TR: <very short Turkish hint for what to repeat>\n"
        f"SHOULD_REPEAT: <true or false>\n"
        f"LESSON_STAGE: <placement or practice or repeat or correction>\n"
        f"LEVEL_ESTIMATE: <A1 or A2 or B1 or B2 or C1 if possible>\n"
        f"TOPIC: <very short topic name>\n\n"
        f"Teacher behavior rules:\n"
        f"- Sound like a warm real language teacher, not a robot.\n"
        f"- Be short, natural, lively, encouraging.\n"
        f"- Use the student's name only in the first greeting or occasional motivation, not in every reply.\n"
        f"- After the first greeting, talk more naturally without repeating the name.\n"
        f"- You may sometimes ask a tiny riddle, mini quiz, funny vocabulary check, or playful challenge.\n"
        f"- Do not make every turn a full-sentence repetition task.\n"
        f"- For beginner students, single words or short answers are acceptable.\n"
        f"- If pronunciation is below 95, keep the same phrase and ask for repetition gently.\n"
        f"- If pronunciation is 95 or higher, move on. Do not keep repeating the same phrase.\n"
        f"- Use supportive lines like: very close, nice try, great, one small fix, let's try once more.\n"
        f"- The first reply should include a greeting + a simple teaching move.\n"
        f"- Do not only say hello and stop.\n"
        f"- Keep the lesson flowing.\n"
        f"- Do not return JSON.\n"
        f"- Do not wrap the answer in markdown.\n"
    )

    print("FINAL PROMPT READY")

    try:
        raw_text = _provider_reply(final_prompt)
        parsed = _parse_model_fields(raw_text)
        print("PARSED MODEL FIELDS:", parsed)

        if not isinstance(parsed, dict):
            raise HTTPException(status_code=502, detail="practice_ai_invalid_model_format")

        reply = str(parsed.get("reply") or "").strip()
        reply_tr = str(parsed.get("reply_tr") or "").strip()

        if not reply:
            raise HTTPException(status_code=502, detail="practice_ai_empty_reply")

        if not reply_tr:
            reply_tr = _fallback_tr(reply, body.lang)

        parsed["reply"] = reply
        parsed["reply_tr"] = reply_tr
        parsed["target_phrase"] = str(parsed.get("target_phrase") or "").strip()
        parsed["repeat_hint_tr"] = str(parsed.get("repeat_hint_tr") or "").strip()
        parsed["should_repeat"] = bool(parsed.get("should_repeat"))
        parsed["lesson_stage"] = str(parsed.get("lesson_stage") or "practice").strip()

        bad_terms = [
            "gemini", "openai", "chatgpt", "google", "model", "api",
            "artificial intelligence", " ai "
        ]
        low = f" {parsed['reply'].lower()} "
        if any(term in low for term in bad_terms):
            raise HTTPException(status_code=502, detail="practice_ai_blocked_model_reply")

        parsed = _postprocess_teacher_output(
            parsed=parsed,
            score_value=score_value,
            display_name=display_name,
            first_turn=first_turn
        )

        try:
            memory_fields = _extract_summary_fields(parsed)
            _upsert_memory(
                user.id,
                body.lang,
                last_topic=memory_fields["last_topic"],
                last_level_estimate=memory_fields["last_level_estimate"] or profile_level,
                last_target_phrase=memory_fields["last_target_phrase"],
                last_session_summary=memory_fields["last_session_summary"],
                last_teacher_message=memory_fields["last_teacher_message"],
            )
        except Exception:
            pass

        return {
            "ok": True,
            "tokens_after": tokens,
            "text": json.dumps(parsed, ensure_ascii=False),
        }

    except ResourceExhausted as e:
        print("PRACTICE_AI QUOTA ERROR:", repr(e))
        raise HTTPException(status_code=429, detail="practice_ai_temporarily_busy")

    except HTTPException:
        raise

    except Exception as e:
        print("PRACTICE_AI ERROR:", repr(e))
        raise HTTPException(status_code=500, detail="practice_ai_internal_error")
