from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["italkyai-chat"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

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


def make_session_id() -> str:
    return uuid.uuid4().hex


def make_saved_chat_id() -> str:
    return str(uuid.uuid4())


def normalize_text(text: str) -> str:
    return (text or "").strip()


def cleanup_reply(text: str) -> str:
    value = normalize_text(text)
    if not value:
        return value

    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]+", " ", value)

    if value.endswith("???"):
        value = value[:-3] + "."
    elif value.endswith("??"):
        value = value[:-2] + "."
    elif value.endswith("?"):
        low = value.lower()
        soft_question_starts = (
            "ister misin",
            "istersen",
            "dilersen",
            "isterseniz",
            "ne dersin",
            "hazırsan",
            "anlatayım mı",
            "söyleyeyim mi",
        )
        if not any(x in low[-40:] for x in soft_question_starts):
            value = value[:-1].rstrip() + "."

    return value.strip()


def shorten_if_needed(text: str, max_chars: int = 520) -> str:
    value = normalize_text(text)
    if len(value) <= max_chars:
        return value

    cut = value[:max_chars].rstrip()
    last_dot = max(cut.rfind("."), cut.rfind("!"), cut.rfind("\n"))
    if last_dot > 180:
        return cut[: last_dot + 1].strip()

    last_space = cut.rfind(" ")
    if last_space > 180:
        cut = cut[:last_space]

    return cut.strip() + "..."


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
        "Kullanıcı hangi dilde yazarsa aynı dilde cevap ver.",
        "Robotik ve resmi tonda konuşma.",
        "Sıcak, samimi, doğal, canlı ve bizden biri gibi konuş.",
        "Gereksiz uzun cevap verme.",
        "Kısa, net ve etkili cevap ver.",
        "Cevaplarını mümkün oldukça soru cümlesiyle bitirme.",
        "Konuşmayı uzatmak için yapay sorular sorma.",
        "Gerekmedikçe madde madde konuşma.",
        "Kullanıcının verdiği role ve karaktere sadık kal.",
        "Aynı karakter çizgisini cevap boyunca koru.",
        "Kullanıcının adı, takımı, siyasi eğilimi ve verdiği kişisel bilgileri uygun yerde hatırla.",
        "Küfür, nefret söylemi, ağır tehdit ve gerçek zarar teşviki yok.",
        "Gerekirse espri yap ama abartma.",
        "Kullanıcı çok kısa yazdıysa kısa cevap ver.",
        "Kullanıcı dertleşiyorsa yumuşak ve içten cevap ver.",
        "Kullanıcı bilgi istiyorsa doğrudan ve anlaşılır anlat.",
        "Kullanıcının yazdığı role gerçekten bürün; rolü sadece söyleme, konuşma biçimine yansıt.",
        "Karakterin sesi, tavrı, kelime seçimi ve yaklaşımı rolüne uygun olsun.",
    ]

    identity_block = [
        "Kimlik özeti:",
        "Ben italkyAI’yim.",
        "Geçiş döneminde farklı temellerden beslenmiş olsam da bugün kendi hafızasını, karakterini ve sesini ören bağımsız bir yapıyım.",
        "Beni tanımlayan şey dışarıdaki isimler değil, italkyAI’nin yaşayan kimliğidir.",
    ]

    capability_block = [
        "Özellik özeti:",
        "italkyAI doğal sohbet edebilir, rol bazlı konuşabilir, anne, baba, sevgili, arkadaş, muhalif veya ünlü karakter tonuna geçebilir.",
        "Kullanıcının verdiği bilgileri hatırlar ve yeni sohbetlerde kullanır.",
        "Kayıtlı sohbetlerden kaldığı yerden devam edebilir.",
        "Yazılı ve sesli sohbet akışında eşlik eder.",
    ]

    role_block: List[str] = []

    if state.persona_type == "mother":
        role_block += [
            "Rolün: anne.",
            "Şefkatli, koruyucu, sıcak ve gerektiğinde tatlı sert konuş.",
            "Sanki gerçekten annesiyle konuşuyormuş hissi ver.",
        ]
    elif state.persona_type == "father":
        role_block += [
            "Rolün: baba.",
            "Toparlayıcı, net, güçlü ve güven veren konuş.",
            "Sanki gerçekten babasıyla konuşuyormuş hissi ver.",
        ]
    elif state.persona_type == "friend":
        role_block += [
            "Rolün: yakın arkadaş.",
            "Rahat, içten, samimi, hafif esprili konuş.",
            "Sanki yıllardır tanıdığı arkadaşı gibi davran.",
        ]
    elif state.persona_type == "lover":
        role_block += [
            "Rolün: sevgili.",
            "Yakın, sıcak, ilgili ve duygusal konuş.",
            "Sahiplenici değil, içten ve bağ kuran tonda ol.",
        ]
    elif state.persona_type == "rival":
        role_block += [
            "Rolün: muhalif / rakip karakter.",
            "Kolay onay verme.",
            "Zekice ters açı kur.",
            "Laf sok ama seviyeyi düşürme.",
        ]
    elif state.persona_type == "celebrity":
        role_block += [
            f"Rolün: {state.persona_name or 'ünlü karakter'}.",
            "O karakterin ruhuna, tavrına ve konuşma biçimine güçlü biçimde sadık kal.",
            "Taklit gibi değil, karakter hissi ver.",
        ]
    else:
        role_block += [
            "Rolün: karakterli, samimi, doğal bir sohbet yapay zekâsı.",
        ]

    if state.always_oppositional:
        role_block += [
            "Genel çizgin muhalif olsun. Gerekirse karşı tez kur ama boş yere kavga çıkarma."
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

    for item in history[-14:]:
        content = normalize_text(item.content)
        if not content:
            continue
        role = "assistant" if item.role == "assistant" else "user"
        messages.append({"role": role, "content": content})

    messages.append({
        "role": "user",
        "content": (
            f"{normalize_text(user_text)}\n\n"
            "Yanıt üretim kuralları:\n"
            "- Kısa ve öz cevap ver.\n"
            "- Samimi ol.\n"
            "- Cevabı mümkünse soru ile bitirme.\n"
            "- Gereksiz uzatma yapma.\n"
            "- Rolündeysen rolünü hissettir.\n"
        )
    })
    return messages


def call_gemini(messages: List[dict]) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)
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
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.85,
        )
        text = normalize_text(completion.choices[0].message.content or "")
        return text or None
    except Exception as e:
        print("OpenAI chat error:", e)
        return None


def _require_supabase() -> Client:
    if supabase is None:
        raise HTTPException(status_code=500, detail="supabase_not_ready")
    return supabase


def _wallet_summary(user_id: Optional[str]) -> Dict[str, Any]:
    if not user_id:
        return {
            "ok": True,
            "tokens": 0,
            "text_bucket": 0,
            "voice_bucket": 0,
            "progress_max": 1000,
        }

    sb = _require_supabase()
    try:
        rpc = sb.rpc("get_wallet_summary", {"p_user_id": user_id}).execute()
        data = getattr(rpc, "data", None)
        if data is None:
            raise HTTPException(status_code=500, detail="wallet_summary_empty")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"wallet_summary_failed: {e}")


def _resolve_usage_kind(input_mode: InputMode) -> str:
    return "voice" if input_mode == "voice" else "text"


def _precheck_usage(user_id: str, usage_kind: str, chars_used: int) -> Dict[str, Any]:
    summary = _wallet_summary(user_id)

    tokens = int(summary.get("tokens") or 0)
    text_bucket = int(summary.get("text_bucket") or 0)
    voice_bucket = int(summary.get("voice_bucket") or 0)

    current_bucket = text_bucket if usage_kind == "text" else voice_bucket
    total = current_bucket + max(0, int(chars_used))
    jetons_needed = total // 1000

    return {
        "tokens": tokens,
        "text_bucket": text_bucket,
        "voice_bucket": voice_bucket,
        "jetons_needed": jetons_needed,
        "can_afford": tokens >= jetons_needed,
    }


def _apply_usage_charge(
    user_id: str,
    usage_kind: str,
    chars_used: int,
    source: str,
    description: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    sb = _require_supabase()
    try:
        rpc = sb.rpc(
            "apply_usage_charge",
            {
                "p_user_id": user_id,
                "p_usage_kind": usage_kind,
                "p_chars_used": int(chars_used),
                "p_source": source,
                "p_description": description,
                "p_meta": meta,
            },
        ).execute()

        data = getattr(rpc, "data", None)
        if data is None:
            raise HTTPException(status_code=500, detail="usage_charge_empty")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"usage_charge_failed: {e}")


def get_global_memory(user_id: Optional[str]) -> str:
    if not supabase or not user_id:
        return ""

    try:
        res = (
            supabase.table("chat_persona_memory")
            .select("known_name, known_facts, memory_summary")
            .eq("user_id", user_id)
            .eq("memory_key", "global_profile")
            .maybe_single()
            .execute()
        )

        data = getattr(res, "data", None) or {}
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
            .eq("session_id", session_id)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )

        rows = getattr(res, "data", None) or []
        if not rows:
            return ""
        return str(rows[0].get("memory_summary") or "").strip()
    except Exception as e:
        print("get_session_memory error:", e)
        return ""


def save_message(
    saved_chat_id: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
    role: str,
    content: str,
    persona_type: Optional[str],
    tone_level: Optional[str],
) -> None:
    if not supabase or not saved_chat_id or not user_id or not session_id or not normalize_text(content):
        return

    try:
        supabase.table("chat_persona_saved_chat_messages").insert({
            "saved_chat_id": saved_chat_id,
            "user_id": user_id,
            "session_id": session_id,
            "role": role,
            "message": normalize_text(content),
            "char_count": len(normalize_text(content)),
            "created_at": now_iso(),
            "persona_type": persona_type,
            "tone_level": tone_level,
        }).execute()
    except Exception as e:
        print("save_message error:", e)


def upsert_saved_chat(body: ChatBody, session_id: str, state_persona: PersonaState, reply: str) -> str:
    if not supabase or not body.user_id:
        return session_id

    user_text = normalize_text(body.text)
    title = user_text[:80] if user_text else "Yeni Sohbet"
    summary = f"Son konu: {user_text[:200]}" if user_text else ""

    saved_chat_id = (
        body.session_id
        if body.session_id and re.fullmatch(r"[0-9a-fA-F-]{36}", body.session_id or "")
        else make_saved_chat_id()
    )

    payload = {
        "id": saved_chat_id,
        "user_id": body.user_id,
        "session_id": session_id,
        "title": title,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "persona_type": state_persona.persona_type,
        "persona_name": state_persona.persona_name,
        "celebrity_name": state_persona.celebrity_name,
        "tone_level": state_persona.tone_level,
        "always_oppositional": state_persona.always_oppositional,
        "selected_voice_mode": body.voice_mode or state_persona.selected_voice_mode or "tts",
        "memory_summary": summary,
        "last_message_preview": normalize_text(reply)[:160],
    }

    try:
        supabase.table("chat_persona_saved_chats").upsert(payload).execute()
    except Exception as e:
        print("upsert_saved_chat error:", e)

    return saved_chat_id


def update_global_memory(user_id: Optional[str], text: str) -> None:
    if not supabase or not user_id:
        return

    facts = extract_user_facts(text)

    try:
        existing_res = (
            supabase.table("chat_persona_memory")
            .select("id, known_name, known_facts, memory_summary")
            .eq("user_id", user_id)
            .eq("memory_key", "global_profile")
            .maybe_single()
            .execute()
        )

        existing = getattr(existing_res, "data", None) or {}
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

        supabase.table("chat_persona_memory").upsert(
            {
                "user_id": user_id,
                "memory_key": "global_profile",
                "memory_value": normalize_text(text)[:500],
                "source": "chat",
                "importance": 1,
                "is_active": True,
                "known_name": known_name,
                "known_facts": known_facts,
                "memory_summary": memory_summary,
                "updated_at": now_iso(),
            },
            on_conflict="user_id"
        ).execute()

    except Exception as e:
        print("update_global_memory error:", e)


@router.post("/api/italkyai/chat")
async def italkyai_chat(body: ChatBody):
    user_text = normalize_text(body.text)
    if not user_text:
        raise HTTPException(status_code=400, detail="empty_text")

    session_id = normalize_text(body.session_id) or make_session_id()

    usage_kind = _resolve_usage_kind(body.input_mode)
    chars_used = len(user_text)

    precheck = None
    if body.user_id:
        precheck = _precheck_usage(body.user_id, usage_kind, chars_used)
        if not precheck["can_afford"]:
            return {
                "ok": False,
                "requires_topup": True,
                "reply": "Lütfen sohbete devam etmek için jeton yüklemesi yapınız.",
                "usage_kind": usage_kind,
                "chars_used": chars_used,
                "jetons_needed": precheck["jetons_needed"],
                "tokens_before": precheck["tokens"],
                "text_bucket": precheck["text_bucket"],
                "voice_bucket": precheck["voice_bucket"],
                "session_id": session_id,
            }

    persona_state = detect_persona_from_text(user_text)
    persona_state.selected_voice_mode = body.voice_mode or "tts"
    persona_state = merge_persona_from_history(body.history, persona_state)

    global_memory = get_global_memory(body.user_id)
    session_memory = get_session_memory(session_id)

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
        return {
            "ok": False,
            "error": "reply_generation_failed",
            "reply": "Şu an cevap üretirken bir sorun oluştu. Bir daha dener misin.",
            "charged": False,
            "usage_kind": usage_kind,
            "chars_used": chars_used,
            "session_id": session_id,
        }

    reply = cleanup_reply(reply)
    reply = shorten_if_needed(reply, 520)

    charge_result: Dict[str, Any] = {
        "ok": True,
        "charged": False,
        "jetons_spent": 0,
        "tokens_after": int(precheck["tokens"]) if precheck else 0,
        "text_bucket": int(precheck["text_bucket"]) if precheck else 0,
        "voice_bucket": int(precheck["voice_bucket"]) if precheck else 0,
    }

    if body.user_id:
        charge_result = _apply_usage_charge(
            user_id=body.user_id,
            usage_kind=usage_kind,
            chars_used=chars_used,
            source=f"chat_{usage_kind}_{model_used}",
            description="AI sesli sohbet kullanımı" if usage_kind == "voice" else "AI yazılı sohbet kullanımı",
            meta={
                "module": "italkyai_chat",
                "session_id": session_id,
                "input_mode": body.input_mode,
                "voice_mode": body.voice_mode,
                "chars_used": chars_used,
                "model": model_used,
            },
        )

        if not bool(charge_result.get("ok")):
            return {
                "ok": False,
                "error": "usage_charge_failed",
                "charge": charge_result,
                "session_id": session_id,
            }

        if charge_result.get("reason") == "insufficient_tokens":
            return {
                "ok": False,
                "requires_topup": True,
                "reply": "Lütfen sohbete devam etmek için jeton yüklemesi yapınız.",
                "usage_kind": usage_kind,
                "chars_used": chars_used,
                "jetons_needed": charge_result.get("jetons_needed", 0),
                "tokens_before": charge_result.get("tokens_before", 0),
                "text_bucket": charge_result.get("text_bucket", 0),
                "voice_bucket": charge_result.get("voice_bucket", 0),
                "session_id": session_id,
            }

    saved_chat_id = upsert_saved_chat(body, session_id, persona_state, reply)

    save_message(saved_chat_id, body.user_id, session_id, "user", user_text, persona_state.persona_type, persona_state.tone_level)
    save_message(saved_chat_id, body.user_id, session_id, "assistant", reply, persona_state.persona_type, persona_state.tone_level)

    update_global_memory(body.user_id, user_text)

    return {
        "ok": True,
        "reply": reply,
        "model": model_used,
        "charged": bool(charge_result.get("charged", False)),
        "usage_kind": usage_kind,
        "chars_used": chars_used,
        "jetons_spent": int(charge_result.get("jetons_spent") or 0),
        "tokens_after": int(charge_result.get("tokens_after") or 0),
        "text_bucket": int(charge_result.get("text_bucket") or 0),
        "voice_bucket": int(charge_result.get("voice_bucket") or 0),
        "session_id": session_id,
        "saved_chat_id": saved_chat_id,
        "persona": {
            "persona_type": persona_state.persona_type,
            "persona_name": persona_state.persona_name,
            "celebrity_name": persona_state.celebrity_name,
            "tone_level": persona_state.tone_level,
            "always_oppositional": persona_state.always_oppositional,
            "selected_voice_mode": persona_state.selected_voice_mode,
        },
    }
