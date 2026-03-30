from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from supabase import create_client, Client
import google.generativeai as genai

router = APIRouter(tags=["practice-ai"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY", "").strip()
    or os.getenv("GOOGLE_API_KEY", "").strip()
)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
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


def _trial_active(profile: dict) -> bool:
    if profile.get("trial_active") is True:
        return True

    days = profile.get("trial_days_left")
    try:
        if days is not None and int(days) > 0:
            return True
    except Exception:
        pass

    end_at = _parse_dt(profile.get("trial_ends_at") or profile.get("trial_end_at"))
    if end_at and end_at > _now_utc():
        return True

    return False


def _package_name(profile: dict) -> str:
    return str(
        profile.get("package_name")
        or profile.get("package_code")
        or profile.get("active_package")
        or profile.get("membership_type")
        or profile.get("plan")
        or profile.get("current_package")
        or ""
    ).strip().lower()


def _has_allowed_package(profile: dict) -> bool:
    name = _package_name(profile)

    if "translate" in name:
        return False
    if "education" in name:
        return True
    if "egitim" in name:
        return True
    if "edu" in name:
        return True
    if "premium" in name:
        return True

    return False


def _current_tokens(profile: dict) -> int:
    try:
        return int(profile.get("tokens") or profile.get("jeton_balance") or 0)
    except Exception:
        return 0


def _deduct_token(user_id: str, current_tokens: int) -> int:
    new_tokens = max(0, current_tokens - 1)

    payload = {"tokens": new_tokens}
    try:
        supabase.table("profiles").update(payload).eq("id", user_id).execute()
    except Exception:
        # some projects may use jeton_balance instead of tokens
        supabase.table("profiles").update({"jeton_balance": new_tokens}).eq("id", user_id).execute()

    return new_tokens


def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _display_name(profile: dict) -> str:
    hitap = str(profile.get("hitap") or "").strip()
    if hitap:
        return hitap

    name = str(profile.get("name") or "").strip()
    if name:
        return name.split(" ")[0]

    full_name = str(profile.get("full_name") or "").strip()
    if full_name:
        return full_name.split(" ")[0]

    email = str(profile.get("email") or "").strip()
    if email and "@" in email:
        return email.split("@")[0]

    return ""


def _profile_level_for_lang(profile: dict, lang: str) -> str:
    levels = profile.get("levels") or {}
    if isinstance(levels, dict):
        return str(levels.get(lang) or levels.get(lang.upper()) or "").strip()
    return ""


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
    return {
        "last_topic": str(parsed.get("topic") or parsed.get("lesson_stage") or "").strip(),
        "last_level_estimate": str(parsed.get("level_estimate") or "").strip(),
        "last_target_phrase": str(parsed.get("target_phrase") or "").strip(),
        "last_session_summary": str(parsed.get("reply_tr") or "").strip(),
        "last_teacher_message": str(parsed.get("reply") or "").strip(),
    }


@router.post("/api/practice/chat")
async def practice_chat(body: PracticeChatBody, request: Request):
    token = _extract_bearer(request)
    user = _get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")

    profile = _load_profile(user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="profile_not_found")

    if _trial_active(profile):
        raise HTTPException(status_code=403, detail="practice_ai_closed_for_trial")

    if not _has_allowed_package(profile):
        raise HTTPException(status_code=403, detail="practice_ai_requires_education_or_premium")

    tokens = _current_tokens(profile)
    if tokens <= 0:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    tokens_after = _deduct_token(user.id, tokens)

    display_name = _display_name(profile)
    profile_level = _profile_level_for_lang(profile, body.lang)
    memory = _load_memory(user.id, body.lang)

    model = genai.GenerativeModel(GEMINI_MODEL)

    final_prompt = (
        f"{body.system_prompt}\n\n"
        f"Student display name: {display_name or 'student'}\n"
        f"Profile level for selected language: {profile_level or 'unknown'}\n"
        f"{_summarize_memory_for_prompt(memory)}\n\n"
        f"{body.prompt}\n\n"
        f"Important teacher behavior:\n"
        f"- If previous lesson memory exists, greet the student naturally by name if available.\n"
        f"- If previous lesson memory exists, briefly refer to the last topic before continuing.\n"
        f"- Example style: 'Hi Oğuz. Last time we practiced greetings. Shall we continue?'\n"
        f"- If there is no previous memory, start with a short placement greeting.\n"
        f"- Keep replies short."
    )

    try:
        resp = model.generate_content(
            final_prompt,
            generation_config={
                "temperature": 0.7,
                "max_output_tokens": 350,
                "response_mime_type": "application/json",
            },
        )

        raw_text = getattr(resp, "text", "") or ""
        parsed = _safe_json(raw_text)

        if not parsed:
            parsed = {
                "reply": "",
                "reply_tr": "",
                "target_phrase": "",
                "should_repeat": False,
                "lesson_stage": "practice",
            }

        reply = str(parsed.get("reply") or "")
        bad_terms = [
            "gemini", "openai", "chatgpt", "google", "model", "api",
            "artificial intelligence", " ai "
        ]
        low = f" {reply.lower()} "
        if any(term in low for term in bad_terms):
            parsed["reply"] = ""
            parsed["reply_tr"] = ""
            parsed["target_phrase"] = ""
            parsed["should_repeat"] = False
            parsed["lesson_stage"] = "practice"

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
            "tokens_after": tokens_after,
            "text": json.dumps(parsed, ensure_ascii=False),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"gemini_error: {e}")
