from __future__ import annotations

import math
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

router = APIRouter(tags=["italkyai-chat"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

FREE_CHAR_LIMIT = 100
CHARS_PER_TOKEN = 1000


class ItalkyAIChatRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    session_id: str | None = None
    text: str = Field(..., min_length=1)
    voice_mode: str = "free_tts"
    voice_label: str = "Ücretsiz Ses"
    free_limit: int = FREE_CHAR_LIMIT


class ItalkyAIChatResponse(BaseModel):
    ok: bool
    reply: str
    spent_tokens: int
    tokens_left: int
    free_used: bool


def ceil_token_cost(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return math.ceil(char_count / CHARS_PER_TOKEN)


def first_name_from_profile(profile: dict[str, Any]) -> str:
    full_name = str(profile.get("full_name") or "").strip()
    if full_name:
        return full_name.split()[0]
    email = str(profile.get("email") or "").strip()
    if email:
        return email.split("@")[0]
    return "Başkanım"


def get_profile(user_id: str) -> dict[str, Any]:
    result = (
        supabase.table("profiles")
        .select("id, full_name, email, tokens")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="profile_not_found")
    return result.data[0]


def update_tokens(user_id: str, new_token_amount: int) -> None:
    supabase.table("profiles").update({"tokens": new_token_amount}).eq("id", user_id).execute()


def save_chat_message(
    user_id: str,
    session_id: str,
    role: str,
    message: str,
    voice_mode: str,
    voice_label: str,
    spent_tokens: int,
    is_free_message: bool,
) -> None:
    try:
        supabase.table("chat_persona_messages").insert({
            "user_id": user_id,
            "session_id": session_id,
            "role": role,
            "message": message,
            "char_count": len(message),
            "voice_mode": voice_mode,
            "voice_label": voice_label,
            "jeton_spent": spent_tokens,
            "is_free_message": is_free_message
        }).execute()
    except Exception:
        # log patlasa bile sohbet dönsün
        pass


def upsert_usage(user_id: str, user_chars: int, ai_chars: int, spent_tokens: int) -> None:
    try:
        existing = (
            supabase.table("chat_persona_usage")
            .select("user_id, total_user_chars, total_ai_chars, total_messages, total_jeton_spent")
            .eq("user_id", user_id)
            .maybeSingle()
            .execute()
        )

        row = existing.data or {}
        if row:
            supabase.table("chat_persona_usage").update({
                "total_user_chars": int(row.get("total_user_chars") or 0) + user_chars,
                "total_ai_chars": int(row.get("total_ai_chars") or 0) + ai_chars,
                "total_messages": int(row.get("total_messages") or 0) + 2,
                "total_jeton_spent": int(row.get("total_jeton_spent") or 0) + spent_tokens
            }).eq("user_id", user_id).execute()
        else:
            supabase.table("chat_persona_usage").insert({
                "user_id": user_id,
                "total_user_chars": user_chars,
                "total_ai_chars": ai_chars,
                "total_messages": 2,
                "total_jeton_spent": spent_tokens
            }).execute()
    except Exception:
        pass


def save_token_movement_if_possible(
    user_id: str,
    token_delta: int,
    description: str,
    code: str,
) -> None:
    """
    Eğer sende wallet_tx / wallet_transactions / token_transactions gibi tablo varsa
    bunu kendi şemana göre uyarlarsın.
    Şimdilik hata verse bile akışı bozmaz.
    """
    if token_delta == 0:
        return

    possible_tables = [
        "wallet_tx",
        "wallet_transactions",
        "token_transactions",
    ]

    payload = {
        "user_id": user_id,
        "amount": -abs(token_delta),
        "description": description,
        "code": code
    }

    for table_name in possible_tables:
        try:
            supabase.table(table_name).insert(payload).execute()
            return
        except Exception:
            continue


def build_system_prompt(first_name: str) -> str:
    return f"""
Sen italkyAI'sin.
Asla başka bir yapay zeka markasının adıyla konuşma.
Kullanıcıya sadece italkyAI olarak görün.
Türkçe konuş.
Cevapların samimi, doğal, insansı ve sıcak olsun.
Robot gibi konuşma.
Gereksiz uzun konuşma.
Kullanıcıya bazen adıyla hitap edebilirsin: {first_name}
Yalan iddia kurma.
Bilmediğin şeyi biliyormuş gibi yapma.
""".strip()


def call_primary_engine(system_prompt: str, user_text: str) -> str:
    """
    BURASI ANA MOTOR.
    Şimdilik fallback mock cevap döndürüyorum.
    Sen kendi gerçek motor bağlantını burada yapacaksın.
    """
    return f"Tamam, bunu aldım. Biraz daha aç da seni yarım yamalak anlamayayım: {user_text}"


def call_backup_engine(system_prompt: str, user_text: str) -> str:
    """
    BURASI YEDEK MOTOR.
    """
    return f"Seni anladım. Devam et, dinliyorum: {user_text}"


@router.post("/api/italkyai/chat", response_model=ItalkyAIChatResponse)
def italkyai_chat(req: ItalkyAIChatRequest) -> ItalkyAIChatResponse:
    user_id = req.user_id.strip()
    text = req.text.strip()
    session_id = (req.session_id or "").strip() or f"session_{user_id}"

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id_required")
    if not text:
        raise HTTPException(status_code=422, detail="text_required")

    profile = get_profile(user_id)
    first_name = first_name_from_profile(profile)
    tokens = int(profile.get("tokens") or 0)

    user_char_count = len(text)
    free_used = user_char_count <= req.free_limit

    if not free_used and tokens <= 0:
        reply = "Lütfen sohbete devam etmek için jeton yüklemesi yapınız."

        save_chat_message(
            user_id=user_id,
            session_id=session_id,
            role="user",
            message=text,
            voice_mode=req.voice_mode,
            voice_label=req.voice_label,
            spent_tokens=0,
            is_free_message=False,
        )

        save_chat_message(
            user_id=user_id,
            session_id=session_id,
            role="assistant",
            message=reply,
            voice_mode=req.voice_mode,
            voice_label=req.voice_label,
            spent_tokens=0,
            is_free_message=False,
        )

        upsert_usage(user_id, user_char_count, len(reply), 0)

        return ItalkyAIChatResponse(
            ok=True,
            reply=reply,
            spent_tokens=0,
            tokens_left=tokens,
            free_used=False,
        )

    system_prompt = build_system_prompt(first_name)

    try:
        reply = call_primary_engine(system_prompt, text).strip()
    except Exception:
        reply = ""

    if not reply:
        try:
            reply = call_backup_engine(system_prompt, text).strip()
        except Exception:
            reply = ""

    if not reply:
        reply = f"Tamam {first_name}, seni duydum. Bir tık daha açarsan daha net gireceğim."

    ai_char_count = len(reply)
    spent_tokens = 0
    new_token_balance = tokens

    if not free_used:
        spent_tokens = ceil_token_cost(ai_char_count)
        if tokens < spent_tokens:
            reply = "Lütfen sohbete devam etmek için jeton yüklemesi yapınız."
            spent_tokens = 0
        else:
            new_token_balance = max(0, tokens - spent_tokens)
            update_tokens(user_id, new_token_balance)

            save_token_movement_if_possible(
                user_id=user_id,
                token_delta=spent_tokens,
                description="italkyAI Jetonlu Cevap",
                code="italkyai_chat_reply"
            )

    save_chat_message(
        user_id=user_id,
        session_id=session_id,
        role="user",
        message=text,
        voice_mode=req.voice_mode,
        voice_label=req.voice_label,
        spent_tokens=0,
        is_free_message=free_used,
    )

    save_chat_message(
        user_id=user_id,
        session_id=session_id,
        role="assistant",
        message=reply,
        voice_mode=req.voice_mode,
        voice_label=req.voice_label,
        spent_tokens=spent_tokens,
        is_free_message=free_used,
    )

    upsert_usage(user_id, user_char_count, ai_char_count, spent_tokens)

    return ItalkyAIChatResponse(
        ok=True,
        reply=reply,
        spent_tokens=spent_tokens,
        tokens_left=new_token_balance,
        free_used=free_used,
    )
