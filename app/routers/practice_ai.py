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


def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


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


def _deduct_token(user_id: str, current_tokens: int) -> int:
    new_tokens = max(0, current_tokens - 1)
    supabase.table("profiles").update({"tokens": new_tokens}).eq("id", user_id).execute()
    return new_tokens


def _profile_membership_code(profile: dict) -> str:
    selected_code = str(profile.get("selected_package_code") or "").strip().lower()
    if selected_code:
        return selected_code

    plan = str(profile.get("plan") or "").strip().lower()
    if plan:
        return plan

    nfc_code = str(profile.get("nfc_package_code") or "").strip().lower()
    if nfc_code:
        return nfc_code

    return "none"


def _profile_has_paid_access(profile: dict) -> bool:
    package_active = bool(profile.get("package_active") is True)

    package_ends_at = _parse_dt(profile.get("package_ends_at"))
    package_valid = bool(package_ends_at and package_ends_at > _now_utc())

    plan = str(profile.get("plan") or "").strip().lower()
    selected_code = str(profile.get("selected_package_code") or "").strip().lower()

    paid_name_hit = any(
        key in f"{plan} {selected_code}"
        for key in ["premium", "edu", "education", "egitim", "translate"]
    )

    return package_active and (package_valid or paid_name_hit)


def _profile_trial_active(profile: dict) -> bool:
    if _profile_has_paid_access(profile):
        return False

    trial_ends_at = _parse_dt(profile.get("trial_ends_at"))
    if trial_ends_at and trial_ends_at > _now_utc():
        return True

    return False


def _profile_can_practice(profile: dict) -> bool:
    if not _profile_has_paid_access(profile):
        return False

    plan = str(profile.get("plan") or "").strip().lower()
    selected_code = str(profile.get("selected_package_code") or "").strip().lower()
    combo = f"{plan} {selected_code}"

    if "translate" in combo:
        return False

    if any(k in combo for k in ["premium", "edu", "education", "egitim"]):
        return True

    return False


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


def _opening_examples_for_lang(lang: str) -> str:
    lang = str(lang or "").strip().lower()

    examples = {
        "en": (
            "Opening style examples in English:\n"
            "- Hi Oğuz. Last time we practiced greetings. Shall we continue?\n"
            "- Hi Oğuz. Today we will practice daily questions.\n"
            "- Hello Oğuz. Let us start with a simple question. What is your name?\n"
            "- Good to see you again. Yesterday we worked on introductions. Ready?\n"
            "- Today we will practice shopping sentences. Are you ready?"
        ),
        "de": (
            "Opening style examples in German:\n"
            "- Hallo Oğuz. Letztes Mal haben wir Begrüßungen geübt. Wollen wir weitermachen?\n"
            "- Hallo Oğuz. Heute üben wir einfache Alltagsfragen.\n"
            "- Guten Tag Oğuz. Wir beginnen mit einer leichten Frage. Wie heißt du?\n"
            "- Schön, dich wiederzusehen. Gestern haben wir Vorstellungen geübt. Bist du bereit?\n"
            "- Heute üben wir Sätze zum Einkaufen. Bist du bereit?"
        ),
        "fr": (
            "Opening style examples in French:\n"
            "- Bonjour Oğuz. La dernière fois, nous avons travaillé les salutations. On continue ?\n"
            "- Bonjour Oğuz. Aujourd'hui, nous allons pratiquer des questions simples de la vie quotidienne.\n"
            "- Salut Oğuz. Nous commençons avec une question facile. Comment tu t'appelles ?\n"
            "- Ravi de te revoir. Hier, nous avons travaillé les présentations. Tu es prêt ?\n"
            "- Aujourd'hui, nous allons pratiquer des phrases pour faire des achats. Tu es prêt ?"
        ),
        "es": (
            "Opening style examples in Spanish:\n"
            "- Hola Oğuz. La última vez practicamos los saludos. ¿Seguimos?\n"
            "- Hola Oğuz. Hoy vamos a practicar preguntas simples de la vida diaria.\n"
            "- Hola Oğuz. Empezamos con una pregunta fácil. ¿Cómo te llamas?\n"
            "- Me alegra verte otra vez. Ayer practicamos las presentaciones. ¿Estás listo?\n"
            "- Hoy vamos a practicar frases para ir de compras. ¿Estás listo?"
        ),
        "it": (
            "Opening style examples in Italian:\n"
            "- Ciao Oğuz. L'ultima volta abbiamo praticato i saluti. Continuiamo?\n"
            "- Ciao Oğuz. Oggi pratichiamo domande semplici della vita quotidiana.\n"
            "- Ciao Oğuz. Cominciamo con una domanda facile. Come ti chiami?\n"
            "- È bello rivederti. Ieri abbiamo praticato le presentazioni. Sei pronto?\n"
            "- Oggi pratichiamo frasi per fare shopping. Sei pronto?"
        ),
    }

    return examples.get(lang, examples["en"])


def _extract_summary_fields(parsed: dict) -> dict:
    lesson_stage = str(parsed.get("lesson_stage") or "").strip()
    target_phrase = str(parsed.get("target_phrase") or "").strip()
    reply_tr = str(parsed.get("reply_tr") or "").strip()
    reply = str(parsed.get("reply") or "").strip()

    topic_map = {
        "placement": "placement",
        "practice": "daily practice",
        "repeat": "pronunciation repeat",
        "correction": "pronunciation correction",
    }

    return {
        "last_topic": topic_map.get(lesson_stage, lesson_stage or "daily practice"),
        "last_level_estimate": str(parsed.get("level_estimate") or "").strip(),
        "last_target_phrase": target_phrase,
        "last_session_summary": reply_tr,
        "last_teacher_message": reply,
    }


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


def _extract_json_from_text(raw_text: str) -> dict | None:
    raw_text = str(raw_text or "").strip()
    if not raw_text:
        return None

    parsed = _safe_json(raw_text)
    if isinstance(parsed, dict):
        return parsed

    if "```" in raw_text:
        parts = raw_text.split("```")
        for part in parts:
            cleaned = part.replace("json", "", 1).strip()
            parsed = _safe_json(cleaned)
            if isinstance(parsed, dict):
                return parsed

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw_text[start:end + 1]
        parsed = _safe_json(candidate)
        if isinstance(parsed, dict):
            return parsed

    return None


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
    print("SUPABASE_URL:", SUPABASE_URL)

    if not profile:
        raise HTTPException(status_code=404, detail="profile_not_found")

    membership_code = _profile_membership_code(profile)
    trial_active = _profile_trial_active(profile)
    can_practice = _profile_can_practice(profile)
    tokens = _profile_tokens(profile)

    print("USER:", getattr(user, "id", None))
    print("DISPLAY_NAME:", _display_name(profile))
    print("MEMBERSHIP_CODE:", membership_code)
    print("TRIAL_ACTIVE:", trial_active)
    print("CAN_PRACTICE:", can_practice)
    print("TOKENS:", tokens)

    if trial_active:
        raise HTTPException(status_code=403, detail="practice_ai_closed_for_trial")

    if "translate" in membership_code:
        raise HTTPException(status_code=403, detail="practice_ai_closed_for_translate")

    if not can_practice:
        raise HTTPException(status_code=403, detail="practice_ai_requires_education_or_premium")

    TEST_BYPASS_TOKEN = False

    if tokens <= 0 and not TEST_BYPASS_TOKEN:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    tokens_after = tokens if TEST_BYPASS_TOKEN else _deduct_token(user.id, tokens)

    display_name = _display_name(profile)
    profile_level = _profile_level_for_lang(profile, body.lang)
    memory = _load_memory(user.id, body.lang)

    model = genai.GenerativeModel(GEMINI_MODEL)

    final_prompt = (
        f"{body.system_prompt}\n\n"
        f"Student display name: {display_name or 'student'}\n"
        f"Profile level for selected language: {profile_level or 'unknown'}\n"
        f"{_summarize_memory_for_prompt(memory)}\n\n"
        f"{_opening_examples_for_lang(body.lang)}\n\n"
        f"{body.prompt}\n\n"
        f"Important teacher behavior:\n"
        f"- If previous lesson memory exists, greet the student naturally by name if available.\n"
        f"- If previous lesson memory exists, briefly refer to the last topic before continuing.\n"
        f"- If there is no previous memory, start with a short placement greeting.\n"
        f"- Use the student display name when natural.\n"
        f"- Keep replies short.\n"
        f"- The teacher must guide the lesson actively.\n"
        f"- The teacher must choose the next small step.\n"
        f"- The teacher must ask one short question at a time.\n"
        f"- The teacher must feel like a real teacher leading the session.\n"
        f"- First reply should feel like a real teacher opening, not a robot waiting screen.\n"
        f"- Good examples: greeting + one short reminder or one short lesson goal + one short question.\n"
    )

    print("FINAL PROMPT READY")

    try:
        resp = model.generate_content(
            final_prompt,
            generation_config={
                "temperature": 0.55,
                "max_output_tokens": 220,
            },
        )

        raw_text = _extract_text_from_gemini_response(resp)
        print("RAW GEMINI TEXT:", raw_text)

        parsed = _extract_json_from_text(raw_text)
        print("PARSED GEMINI JSON:", parsed)

        if not isinstance(parsed, dict):
            raise HTTPException(status_code=502, detail="practice_ai_invalid_model_json")

        reply = str(parsed.get("reply") or "").strip()
        reply_tr = str(parsed.get("reply_tr") or "").strip()

        if not reply:
            raise HTTPException(status_code=502, detail="practice_ai_empty_reply")

        parsed["reply"] = reply
        parsed["reply_tr"] = reply_tr
        parsed["target_phrase"] = str(parsed.get("target_phrase") or "").strip()
        parsed["should_repeat"] = bool(parsed.get("should_repeat"))
        parsed["lesson_stage"] = str(parsed.get("lesson_stage") or "practice").strip()

        bad_terms = [
            "gemini", "openai", "chatgpt", "google", "model", "api",
            "artificial intelligence", " ai "
        ]
        low = f" {parsed['reply'].lower()} "
        if any(term in low for term in bad_terms):
            raise HTTPException(status_code=502, detail="practice_ai_blocked_model_reply")

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

    except HTTPException:
        raise
    except Exception as e:
        print("PRACTICE_AI ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=f"gemini_error: {e}")
