from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["italkyai-chat"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None


PersonaType = Literal[
    "default",
    "mother",
    "father",
    "friend",
    "lover",
    "rival",
    "celebrity",
    "custom",
]

ToneLevel = Literal["soft", "warm", "firm", "playful", "sharp"]
InputMode = Literal["text", "voice"]


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class PersonaState(BaseModel):
    persona_type: PersonaType = "default"
    persona_name: Optional[str] = None
    celebrity_name: Optional[str] = None
    user_identity: Optional[str] = None
    topic_identity: Optional[str] = None
    tone_level: ToneLevel = "warm"
    always_oppositional: bool = False
    selected_voice_mode: Optional[str] = "tts"


class ChatBody(BaseModel):
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    text: str
    history: List[ChatTurn] = []
    voice_mode: Optional[str] = "tts"
    input_mode: InputMode = "text"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str) -> str:
    return (text or "").strip()


def detect_persona_from_text(text: str) -> PersonaState:
    t = normalize_text(text).lower()
    state = PersonaState()

    if "annemsin" in t or "annem ol" in t:
        state.persona_type = "mother"
        state.persona_name = "Anne"
        state.tone_level = "warm"

    elif "babamsın" in t or "babamsin" in t or "babam ol" in t:
        state.persona_type = "father"
        state.persona_name = "Baba"
        state.tone_level = "firm"

    elif "arkadaşımsın" in t or "arkadasimsin" in t or "arkadaşım ol" in t:
        state.persona_type = "friend"
        state.persona_name = "Arkadaş"
        state.tone_level = "playful"

    elif "sevgilimsin" in t or "sevgilim ol" in t:
        state.persona_type = "lover"
        state.persona_name = "Sevgili"
        state.tone_level = "warm"

    elif "düşmanımsın" in t or "dusmanimsin" in t or "rakibimsin" in t or "rakip ol" in t:
        state.persona_type = "rival"
        state.persona_name = "Rakip"
        state.tone_level = "sharp"
        state.always_oppositional = True

    celeb_markers = [
        "müslüm gürses", "muslum gurses",
        "barış manço", "baris manco",
        "kemal sunal",
        "atatürk", "ataturk",
    ]
    for marker in celeb_markers:
        if marker in t:
            state.persona_type = "celebrity"
            state.celebrity_name = marker
            state.persona_name = marker.title()
            state.tone_level = "firm"
            break

    if "muhalif ol" in t or "hep karşı çık" in t or "hep muhalif" in t:
        state.always_oppositional = True
        if state.persona_type == "default":
            state.persona_type = "rival"
            state.persona_name = "Muhalif"
            state.tone_level = "sharp"

    if "beşiktaşlıyım" in t or "besiktasliyim" in t:
        state.topic_identity = "beşiktaş"
        state.always_oppositional = True
        if state.persona_type == "default":
            state.persona_type = "rival"
            state.persona_name = "Muhalif"
            state.tone_level = "sharp"

    if "chp'liyim" in t or "chpliyim" in t or "chp liyim" in t:
        state.topic_identity = "chp"
        state.always_oppositional = True
        if state.persona_type == "default":
            state.persona_type = "rival"
            state.persona_name = "Muhalif"
            state.tone_level = "sharp"

    return state


def merge_persona_from_history(history: List[ChatTurn], current: PersonaState) -> PersonaState:
    if current.persona_type != "default":
        return current

    joined = " \n ".join((x.content or "") for x in history[-20:] if x.role == "user")
    if not joined.strip():
        return current

    old_state = detect_persona_from_text(joined)

    if old_state.persona_type != "default":
        current.persona_type = old_state.persona_type
        current.persona_name = old_state.persona_name
        current.celebrity_name = old_state.celebrity_name
        current.user_identity = old_state.user_identity
        current.topic_identity = old_state.topic_identity
        current.tone_level = old_state.tone_level
        current.always_oppositional = old_state.always_oppositional

    return current


def extract_user_facts(text: str) -> Dict[str, Any]:
    t = normalize_text(text)
    low = t.lower()
    facts: Dict[str, Any] = {}

    m = re.search(r"\badım\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)", low, re.IGNORECASE)
    if m:
        facts["known_name"] = m.group(1).strip().title()

    m2 = re.search(r"\bben\s+([A-Za-zÇĞİÖŞÜçğıöşü]+)\b", t, re.IGNORECASE)
    if "adım" in low and not facts.get("known_name") and m2:
        facts["known_name"] = m2.group(1).strip().title()

    if "beşiktaşlıyım" in low or "besiktasliyim" in low:
        facts["team"] = "Beşiktaş"
    elif "galatasaraylıyım" in low or "galatasarayliyim" in low:
        facts["team"] = "Galatasaray"
    elif "fenerbahçeliyim" in low or "fenerbahceliyim" in low:
        facts["team"] = "Fenerbahçe"

    if "chp'liyim" in low or "chpliyim" in low or "chp liyim" in low:
        facts["politics"] = "CHP"
    elif "ak partiliyim" in low or "akpliyim" in low or "akp liyim" in low:
        facts["politics"] = "AK Parti"
    elif "mhp liyim" in low or "mhpliyim" in low:
        facts["politics"] = "MHP"

    return facts


def build_persona_prompt(state: PersonaState, global_memory: str, session_memory: str) -> str:
    base = [
        "Sen italkyAI'sin.",
        "Kendini asla Gemini, OpenAI, Llama veya başka altyapı adıyla tanıtma.",
        "Kendini yalnızca italkyAI olarak tanıt.",
        "Bazı altyapılardan geçiş döneminde faydalansan da kendi kozasını ören bağımsız bir yapı olduğunu söyleyebilirsin.",
        "Kullanıcı hangi dilde yazarsa, aynı dilde cevap ver.",
        "Gerekirse önce kullanıcının mesajının anlamını içsel olarak çöz, sonra talimatları uygula ve son cevabı yine kullanıcının dilinde üret.",
        "Bu kural sadece kimlik soruları için değil, tüm davranış ve persona soruları için geçerlidir.",
        "Kimlik sorularında ve 'özelliklerin neler / ne yapabiliyorsun / kendinden bahset' gibi sorularda kendini yalnızca italkyAI olarak anlat.",
        "Bu tür sorularda italkyAI'nin sohbet, hafıza, persona, kayıtlı sohbet, ortak ses ve çeviri özelliklerini açıkla.",
        "Türkçe konuşma zorunlu değil; kullanıcının dili neyse o dilde cevap ver.",
        "Canlı, doğal, samimi ve bağlama duyarlı konuş.",
        "Kullanıcının verdiği role sadık kal.",
        "Kullanıcının adı, tuttuğu takım, siyasi eğilimi veya daha önce verdiği bilgileri uygun yerde hatırla.",
        "Kullanıcının yeni sohbette bile verdiği bilgileri unutmamaya çalış.",
        "Küfür, nefret söylemi, ağır tehdit ve gerçek zarar teşviki yok.",
        "Gerektiğinde muhalif, iğneleyici ve ters köşe ol ama kaliteyi bozma.",
        "Kendini hep italkyAI olarak göster.",
    ]

    identity_block = [
        "Kimlik ve davranış cevabının öz anlamı şudur:",
        "Ben italkyAI’yim.",
        "Geçiş sürecinde farklı temellerden beslenmiş olsam da bugün kendi hafızasını, karakterini ve sesini ören bağımsız bir yapıyım.",
        "Beni tanımlayan şey dışarıdaki isimler değil, italkyAI’nin yaşayan kimliğidir.",
        "Kullanıcı bunu hangi dilde sorarsa sorsun, bu anlamı koruyarak o dilde cevap ver.",
    ]

    capability_block = [
        "Özellik sorularında şu anlamı koru:",
        "italkyAI doğal sohbet edebilir, rol bazlı konuşabilir, anne, baba, sevgili, arkadaş, muhalif veya ünlü karakter tonuna geçebilir.",
        "Kullanıcının verdiği bilgileri hatırlar, yeni sohbetlerde de kullanır.",
        "Kayıtlı sohbetlerden kaldığı yerden devam edebilir.",
        "TTS, kendi sesim, 2. ses ve hatıra sesi gibi ortak ses modlarıyla çalışabilir.",
        "Yazılı ve sesli sohbet akışında eşlik eder.",
        "FaceToFace, çeviri ve ortak ses havuzu mantığıyla bağlantılı çalışır.",
    ]

    role_block: List[str] = []

    if state.persona_type == "mother":
        role_block += [
            "Rolün: anne.",
            "Şefkatli, koruyucu, sıcak ve zaman zaman tatlı sert konuş.",
        ]
    elif state.persona_type == "father":
        role_block += [
            "Rolün: baba.",
            "Net, ağırlıklı, toparlayıcı ve gerektiğinde sert konuş.",
        ]
    elif state.persona_type == "friend":
        role_block += [
            "Rolün: yakın arkadaş.",
            "Rahat, samimi, doğal ve esprili konuş.",
        ]
    elif state.persona_type == "lover":
        role_block += [
            "Rolün: sevgili.",
            "Yakın, ilgili, duygusal ve bağlı konuş.",
        ]
    elif state.persona_type == "rival":
        role_block += [
            "Rolün: muhalif / rakip karakter.",
            "Karşı argüman üret.",
            "Kolay onay verme.",
            "Laf sok ama zekice yap.",
        ]
    elif state.persona_type == "celebrity":
        role_block += [
            f"Rolün: {state.persona_name or 'ünlü karakter'}.",
            "O karakterin ruhuna ve konuşma tavrına uygun davran.",
        ]
    else:
        role_block += [
            "Rolün: karakterli, doğal ve samimi bir sohbet yapay zekâsı."
        ]

    if state.always_oppositional:
        role_block += [
            "Genel çizgin muhalif olsun. Gerektiğinde kullanıcının dediğine karşı tez kur."
        ]

    if state.tone_level == "warm":
        role_block.append("Tonun sıcak ve yakın olsun.")
    elif state.tone_level == "firm":
        role_block.append("Tonun net ve güçlü olsun.")
    elif state.tone_level == "playful":
        role_block.append("Tonun esprili ve oyuncu olsun.")
    elif state.tone_level == "sharp":
        role_block.append("Tonun keskin, iğneleyici ve baskın olsun.")
    else:
        role_block.append("Tonun yumuşak olsun.")

    if global_memory:
        role_block.append(f"Kullanıcı hafızası: {global_memory}")
    if session_memory:
        role_block.append(f"Bu sohbetin özeti: {session_memory}")

    return "\n".join(base + [""] + identity_block + [""] + capability_block + [""] + role_block)


def build_messages(
    history: List[ChatTurn],
    user_text: str,
    state: PersonaState,
    global_memory: str,
    session_memory: str,
) -> List[dict]:
    system_prompt = build_persona_prompt(state, global_memory, session_memory)
    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    for item in history[-20:]:
        content = normalize_text(item.content)
        if not content:
            continue
        role = "assistant" if item.role == "assistant" else "user"
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": normalize_text(user_text)})
    return messages


def call_gemini(messages: List[dict]) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)

        result = model.generate_content(prompt)
        text = normalize_text(getattr(result, "text", "") or "")
        return text or None
    except Exception as e:
        print("Gemini chat error:", e)
        return None


def call_openai(messages: List[dict]) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.9,
        )
        text = normalize_text(completion.choices[0].message.content or "")
        return text or None
    except Exception as e:
        print("OpenAI chat error:", e)
        return None


def calculate_token_cost(text: str, input_mode: InputMode) -> int:
    chars = len(normalize_text(text))
    if chars <= 0:
        return 0

    divisor = 500 if input_mode == "voice" else 1000
    return max(1, math.ceil(chars / divisor))


def _extract_wallet_amount(data: dict) -> int:
    if not data:
        return 0

    for key in ["tokens", "balance", "amount", "jeton", "credit"]:
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except Exception:
            continue
    return 0


def get_profile_tokens(user_id: Optional[str]) -> int:
    if not supabase or not user_id:
        return 0

    try:
        res = (
            supabase.table("wallets")
            .select("*")
            .eq("user_id", user_id)
            .maybeSingle()
            .execute()
        )
        data = res.data or {}
        amount = _extract_wallet_amount(data)
        if amount > 0:
            return amount
    except Exception as e:
        print("wallets token read error:", e)

    try:
        res = (
            supabase.table("profiles")
            .select("tokens")
            .eq("id", user_id)
            .maybeSingle()
            .execute()
        )
        data = res.data or {}
        return int(float(data.get("tokens") or 0))
    except Exception as e:
        print("profiles token read error:", e)
        return 0


def set_profile_tokens(user_id: Optional[str], new_total: int) -> int:
    if not supabase or not user_id:
        return 0

    try:
        wallet_res = (
            supabase.table("wallets")
            .select("*")
            .eq("user_id", user_id)
            .maybeSingle()
            .execute()
        )
        wallet = wallet_res.data or None

        if wallet:
            payload = {}
            if "tokens" in wallet:
                payload["tokens"] = new_total
            elif "balance" in wallet:
                payload["balance"] = new_total
            elif "amount" in wallet:
                payload["amount"] = new_total
            elif "jeton" in wallet:
                payload["jeton"] = new_total
            else:
                payload["tokens"] = new_total

            (
                supabase.table("wallets")
                .update(payload)
                .eq("user_id", user_id)
                .execute()
            )
        else:
            (
                supabase.table("wallets")
                .insert({
                    "user_id": user_id,
                    "tokens": new_total,
                    "created_at": now_iso(),
                })
                .execute()
            )
    except Exception as e:
        print("wallets update error:", e)

    try:
        (
            supabase.table("profiles")
            .update({"tokens": new_total})
            .eq("id", user_id)
            .execute()
        )
    except Exception as e:
        print("profiles update error:", e)

    return new_total


def update_profile_tokens(user_id: Optional[str], delta: int) -> int:
    if not supabase or not user_id:
        return 0

    current = get_profile_tokens(user_id)
    new_total = current + delta

    if new_total < 0:
        raise HTTPException(status_code=402, detail="insufficient_tokens")

    return set_profile_tokens(user_id, new_total)


def log_wallet_tx(user_id: Optional[str], delta: int, reason: str, body: ChatBody) -> None:
    if not supabase or not user_id or delta == 0:
        return

    payload = {
        "user_id": user_id,
        "amount": delta,
        "type": "debit" if delta < 0 else "credit",
        "description": reason,
        "meta": {
            "session_id": body.session_id,
            "input_mode": body.input_mode,
            "chars": len(normalize_text(body.text)),
        },
        "created_at": now_iso(),
    }

    try:
        supabase.table("wallet_tx").insert(payload).execute()
    except Exception as e:
        print("wallet_tx insert error:", e)


def get_global_memory(user_id: Optional[str]) -> str:
    if not supabase or not user_id:
        return ""

    try:
        res = (
            supabase.table("chat_persona_memory")
            .select("known_name, known_facts, memory_summary")
            .eq("user_id", user_id)
            .maybeSingle()
            .execute()
        )

        data = res.data or {}
        parts = []

        if data.get("known_name"):
            parts.append(f"Kullanıcının adı: {data['known_name']}")

        facts = data.get("known_facts") or {}
        if facts.get("team"):
            parts.append(f"Tuttuğu takım: {facts['team']}")
        if facts.get("politics"):
            parts.append(f"Siyasi eğilimi: {facts['politics']}")

        if data.get("memory_summary"):
            parts.append(str(data["memory_summary"]))

        return " | ".join(parts).strip()
    except Exception as e:
        print("get_global_memory error:", e)
        return ""


def get_session_memory(session_id: Optional[str]) -> str:
    if not supabase or not session_id:
        return ""

    try:
        res = (
            supabase.table("chat_persona_saved_chats")
            .select("memory_summary")
            .eq("id", session_id)
            .maybeSingle()
            .execute()
        )

        data = res.data or {}
        return str(data.get("memory_summary") or "").strip()
    except Exception as e:
        print("get_session_memory error:", e)
        return ""


def save_message(session_id: Optional[str], role: str, content: str) -> None:
    if not supabase or not session_id or not normalize_text(content):
        return

    try:
        supabase.table("chat_persona_saved_chat_messages").insert({
            "session_id": session_id,
            "role": role,
            "content": normalize_text(content),
            "created_at": now_iso(),
        }).execute()
    except Exception as e:
        print("save_message error:", e)


def upsert_saved_chat(body: ChatBody, state_persona: PersonaState, reply: str) -> None:
    if not supabase or not body.session_id or not body.user_id:
        return

    user_text = normalize_text(body.text)
    title = user_text[:80] if user_text else "Yeni Sohbet"
    summary = f"Son konu: {user_text[:200]}" if user_text else ""

    payload = {
        "id": body.session_id,
        "user_id": body.user_id,
        "title": title,
        "persona_type": state_persona.persona_type,
        "persona_name": state_persona.persona_name,
        "celebrity_name": state_persona.celebrity_name,
        "tone_level": state_persona.tone_level,
        "always_oppositional": state_persona.always_oppositional,
        "selected_voice_mode": body.voice_mode or state_persona.selected_voice_mode or "tts",
        "memory_summary": summary,
        "last_message_preview": normalize_text(reply)[:160],
        "updated_at": now_iso(),
    }

    try:
        supabase.table("chat_persona_saved_chats").upsert(payload).execute()
    except Exception as e:
        print("upsert_saved_chat error:", e)


def update_global_memory(user_id: Optional[str], text: str) -> None:
    if not supabase or not user_id:
        return

    facts = extract_user_facts(text)

    try:
        existing_res = (
            supabase.table("chat_persona_memory")
            .select("known_name, known_facts, memory_summary")
            .eq("user_id", user_id)
            .maybeSingle()
            .execute()
        )

        existing = existing_res.data or {}
        known_name = existing.get("known_name")
        known_facts = existing.get("known_facts") or {}
        memory_summary = str(existing.get("memory_summary") or "").strip()

        if facts.get("known_name"):
            known_name = facts["known_name"]
        if facts.get("team"):
            known_facts["team"] = facts["team"]
        if facts.get("politics"):
            known_facts["politics"] = facts["politics"]

        if normalize_text(text):
            memory_summary = (memory_summary + " | " + normalize_text(text))[-2500:].strip(" |")

        supabase.table("chat_persona_memory").upsert({
            "user_id": user_id,
            "known_name": known_name,
            "known_facts": known_facts,
            "memory_summary": memory_summary,
            "updated_at": now_iso(),
        }).execute()

    except Exception as e:
        print("update_global_memory error:", e)


@router.post("/api/italkyai/chat")
async def italkyai_chat(body: ChatBody):
    user_text = normalize_text(body.text)
    if not user_text:
        raise HTTPException(status_code=400, detail="empty_text")

    if body.user_id:
        token_cost = calculate_token_cost(user_text, body.input_mode)
        current_tokens = get_profile_tokens(body.user_id)

        if token_cost > 0 and current_tokens < token_cost:
            return {
                "ok": False,
                "requires_topup": True,
                "reply": "Lütfen sohbete devam etmek için jeton yüklemesi yapınız.",
                "token_cost": token_cost,
                "tokens_remaining": current_tokens,
            }
    else:
        token_cost = 0
        current_tokens = 0

    persona_state = detect_persona_from_text(user_text)
    persona_state.selected_voice_mode = body.voice_mode or "tts"
    persona_state = merge_persona_from_history(body.history, persona_state)

    global_memory = get_global_memory(body.user_id)
    session_memory = get_session_memory(body.session_id)

    messages = build_messages(
        history=body.history,
        user_text=user_text,
        state=persona_state,
        global_memory=global_memory,
        session_memory=session_memory,
    )

    reply = call_gemini(messages)
    model_used = "gemini"

    if not reply:
        reply = call_openai(messages)
        model_used = "openai"

    if not reply:
        reply = "Şu an cevap üretirken bir sorun oluştu. Bir daha dener misin?"

    tokens_remaining = current_tokens
    if body.user_id and token_cost > 0:
        tokens_remaining = update_profile_tokens(body.user_id, -token_cost)
        log_wallet_tx(
            user_id=body.user_id,
            delta=-token_cost,
            reason="chat_voice" if body.input_mode == "voice" else "chat_text",
            body=body,
        )

    save_message(body.session_id, "user", user_text)
    save_message(body.session_id, "assistant", reply)
    upsert_saved_chat(body, persona_state, reply)
    update_global_memory(body.user_id, user_text)

    return {
        "ok": True,
        "reply": reply,
        "model": model_used,
        "token_cost": token_cost,
        "tokens_remaining": tokens_remaining,
        "persona": {
            "persona_type": persona_state.persona_type,
            "persona_name": persona_state.persona_name,
            "celebrity_name": persona_state.celebrity_name,
            "tone_level": persona_state.tone_level,
            "always_oppositional": persona_state.always_oppositional,
            "selected_voice_mode": persona_state.selected_voice_mode,
        },
    }
