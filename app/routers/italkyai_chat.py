from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client
import os
import math

router = APIRouter(tags=["italkyai-chat"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

FREE_LIMIT = 100
CHARS_PER_JETON = 1000

class ItalkyAiChatReq(BaseModel):
    user_id: str
    text: str
    persona_mode: str = "dert_ortagi"
    voice_mode: str = "free_tts"
    voice_label: str = "Ücretsiz Ses"
    free_limit: int = FREE_LIMIT
    can_use_free: bool = True

def calc_needed_tokens(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return math.ceil(char_count / CHARS_PER_JETON)

def build_system_prompt(persona_mode: str) -> str:
    base = (
        "Sen resmi ve soğuk bir asistan değilsin. "
        "Samimi, doğal, Türk kültürüne yakın, sıcak ve insansı konuşursun. "
        "Robotik cümle kurmazsın. Kullanıcıyla güvenli ama canlı bir tonda konuşursun. "
    )

    if persona_mode == "anne_gibi":
        return base + "Tonun daha şefkatli, toparlayıcı ve koruyucudur."
    if persona_mode == "kanka_gibi":
        return base + "Tonun daha rahat, arkadaş canlısı ve doğal olur."
    if persona_mode == "tatli_sert":
        return base + "Tonun net, hafif sert ama kırmadan yön gösteren yapıdadır."

    return base + "Tonun dert ortağı gibi sıcak, ilgili ve destekleyicidir."

def call_primary_motor(system_prompt: str, user_text: str) -> str:
    # BURAYI SENİN BİRİNCİL MOTORUNA BAĞLAYACAĞIZ
    # Şimdilik çalışan fallback cevap
    return f"Seni anladım. Şunu daha net aç: {user_text}"

def call_fallback_motor(system_prompt: str, user_text: str) -> str:
    # BURAYI YEDEK MOTORUNA BAĞLAYACAĞIZ
    return f"Konuyu aldım. Biraz daha detay verirsen daha iyi yön veririm: {user_text}"

@router.post("/api/italkyai/chat")
def italkyai_chat(req: ItalkyAiChatReq):
    user_id = (req.user_id or "").strip()
    text = (req.text or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not text:
        raise HTTPException(status_code=422, detail="text required")

    prof = (
        supabase.table("profiles")
        .select("tokens,full_name,email")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if not prof.data:
        raise HTTPException(status_code=404, detail="profile not found")

    profile = prof.data[0] or {}
    tokens = int(profile.get("tokens") or 0)

    incoming_chars = len(text)
    outgoing_estimate = max(220, min(900, incoming_chars * 2))
    paid_mode = not bool(req.can_use_free and incoming_chars <= req.free_limit)

    needed_tokens = calc_needed_tokens(outgoing_estimate) if paid_mode else 0

    if needed_tokens > 0 and tokens < needed_tokens:
      raise HTTPException(status_code=402, detail="insufficient_tokens")

    system_prompt = build_system_prompt(req.persona_mode)

    try:
        reply = call_primary_motor(system_prompt, text)
    except Exception:
        reply = call_fallback_motor(system_prompt, text)

    reply = (reply or "").strip()
    if not reply:
        raise HTTPException(status_code=500, detail="empty_reply")

    spent = 0
    if paid_mode:
        actual_chars = len(reply)
        spent = calc_needed_tokens(actual_chars)
        next_tokens = max(0, tokens - spent)
        supabase.table("profiles").update({"tokens": next_tokens}).eq("id", user_id).execute()
    else:
        next_tokens = tokens

    try:
        supabase.table("chat_persona_messages").insert({
            "user_id": user_id,
            "role": "user",
            "message": text
        }).execute()

        supabase.table("chat_persona_messages").insert({
            "user_id": user_id,
            "role": "assistant",
            "message": reply
        }).execute()
    except Exception:
        pass

    return {
        "ok": True,
        "reply": reply,
        "spent_tokens": spent,
        "tokens": next_tokens,
        "free_used": not paid_mode
    }
