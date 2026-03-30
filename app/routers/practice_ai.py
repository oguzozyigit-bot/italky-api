from __future__ import annotations

import json
import os
import re
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
        user = getattr(res, "user", None)
        return user
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

    if "premium" in name:
        return True

    return False


def _current_tokens(profile: dict) -> int:
    try:
        return int(profile.get("tokens") or 0)
    except Exception:
        return 0


def _deduct_token(user_id: str, current_tokens: int) -> int:
    new_tokens = max(0, current_tokens - 1)
    supabase.table("profiles").update({"tokens": new_tokens}).eq("id", user_id).execute()
    return new_tokens


def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _normalize(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _levenshtein(a: str, b: str) -> int:
    a = _normalize(a)
    b = _normalize(b)
    if not a:
        return len(b)
    if not b:
        return len(a)

    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j

    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    return dp[-1][-1]


def _score_pronunciation(spoken: str, target: str) -> int:
    a = _normalize(spoken)
    b = _normalize(target)
    if not a or not b:
        return 0
    if a == b:
        return 100

    dist = _levenshtein(a, b)
    max_len = max(len(a), len(b)) or 1
    score = round((1 - dist / max_len) * 100)
    return max(0, score)


@router.post("/api/practice/chat")
async def practice_chat(body: PracticeChatBody, request: Request):
    token = _extract_bearer(request)
    user = _get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")

    profile = _load_profile(user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="profile_not_found")

    # ACCESS RULES
    if _trial_active(profile):
      raise HTTPException(status_code=403, detail="practice_ai_closed_for_trial")

    if not _has_allowed_package(profile):
      raise HTTPException(status_code=403, detail="practice_ai_requires_education_or_premium")

    tokens = _current_tokens(profile)
    if tokens <= 0:
      raise HTTPException(status_code=402, detail="insufficient_tokens")

    # Deduct 1 token per turn
    tokens_after = _deduct_token(user.id, tokens)

    model = genai.GenerativeModel(GEMINI_MODEL)

    final_prompt = f"{body.system_prompt}\n\n{body.prompt}"

    try:
        resp = model.generate_content(
            final_prompt,
            generation_config={
                "temperature": 0.7,
                "max_output_tokens": 400,
                "response_mime_type": "application/json",
            },
        )

        raw_text = getattr(resp, "text", "") or ""
        parsed = _safe_json(raw_text)

        if not parsed:
            # fail-safe JSON
            parsed = {
                "reply": "",
                "reply_tr": "",
                "target_phrase": "",
                "should_repeat": False,
                "lesson_stage": "practice",
            }

        # hard safety cleanup: no AI/model names
        bad_terms = [
            "gemini", "openai", "chatgpt", "google", "model", "api", "artificial intelligence", "ai"
        ]
        reply = str(parsed.get("reply") or "")
        low = reply.lower()
        if any(term in low for term in bad_terms):
            parsed["reply"] = ""
            parsed["reply_tr"] = ""
            parsed["target_phrase"] = ""
            parsed["should_repeat"] = False
            parsed["lesson_stage"] = "practice"

        return {
            "ok": True,
            "tokens_after": tokens_after,
            "text": json.dumps(parsed, ensure_ascii=False),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"gemini_error: {e}")
