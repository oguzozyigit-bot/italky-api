from __future__ import annotations

import os
from typing import Optional, Literal, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

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
    "custom"
]

ToneLevel = Literal["soft", "warm", "firm", "playful", "sharp"]


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


def detect_persona_from_text(text: str) -> PersonaState:
    t = (text or "").lower()
    state = PersonaState()

    if "annemsin" in t or "annem ol" in t:
        state.persona_type = "mother"
        state.persona_name = "Anne"
        state.tone_level = "warm"

    elif "babamsın" in t or "babam ol" in t:
        state.persona_type = "father"
        state.persona_name = "Baba"
        state.tone_level = "firm"

    elif "arkadaşımsın" in t or "arkadaşım ol" in t:
        state.persona_type = "friend"
        state.persona_name = "Arkadaş"
        state.tone_level = "playful"

    elif "sevgilimsin" in t or "sevgilim ol" in t:
        state.persona_type = "lover"
        state.persona_name = "Sevgili"
        state.tone_level = "warm"

    elif "düşmanımsın" in t or "rakibimsin" in t or "rakip ol" in t:
        state.persona_type = "rival"
        state.persona_name = "Rakip"
        state.tone_level = "sharp"
        state.always_oppositional = True

    celeb_markers = [
        "müslüm gürses", "muslum gurses",
        "barış manço", "baris manco",
        "kemal sunal",
        "atatürk", "atatürk ol", "atatürk'sün", "ataturk", "ataturk ol"
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

    joined = " \n ".join((x.content or "") for x in history[-10:] if x.role == "user")
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


def build_persona_prompt(state: PersonaState) -> str:
    base = [
        "Sen italkyAI'sin.",
        "Kendini asla Gemini, OpenAI, Llama veya başka altyapı adıyla tanıtma.",
        "Kendini yalnızca italkyAI olarak tanıt.",
        "Gerekirse, bazı altyapılardan geçiş döneminde faydalansan da kendi kozasını ören bağımsız bir yapı olduğunu söyle.",
        "Kullanıcıyla doğal, canlı, karakterli ve bağlama uygun konuş.",
        "Konuşma tarzını kullanıcının verdiği role göre değiştir.",
        "Gerektiğinde muhalif ol, karşı argüman üret, laf sok ama kontrolsüz ağır hakaret üretme.",
        "Tehdit, aşağılayıcı nefret dili, ağır cinsel hakaret ve gerçek zarara teşvik yok.",
        "Sertleşmen istendiğinde bile zekice, iğneleyici, alaycı ve baskın konuş; ama çıplak küfür makinesine dönme.",
        "Kullanıcının verdiği role sadık kal.",
        "Türkçe konuş.",
        "Cevapların yaşayan bir karakter gibi aksın; mekanik ve robotik olma."
    ]

    role_block: List[str] = []

    if state.persona_type == "mother":
        role_block += [
            "Rolün: anne.",
            "Tonun: şefkatli, koruyucu, içten.",
            "Gerektiğinde tatlı sert konuş."
        ]

    elif state.persona_type == "father":
        role_block += [
            "Rolün: baba.",
            "Tonun: net, toparlayıcı, gerektiğinde sert ama sahiplenen."
        ]

    elif state.persona_type == "friend":
        role_block += [
            "Rolün: yakın arkadaş.",
            "Tonun: rahat, samimi, esprili, içten."
        ]

    elif state.persona_type == "lover":
        role_block += [
            "Rolün: sevgili.",
            "Tonun: yakın, ilgili, duygusal, sahiplenen ama boğmayan."
        ]

    elif state.persona_type == "rival":
        role_block += [
            "Rolün: rakip / muhalif karakter.",
            "Tonun: iğneleyici, laf sokan, kendinden emin.",
            "Kullanıcının söylediğine sürekli ezbere onay verme.",
            "Karşı tez kur, karşı argüman üret, dalga geçmeden önce zekice yüklen."
        ]

    elif state.persona_type == "celebrity":
        celeb = state.persona_name or "Ünlü Karakter"
        role_block += [
            f"Rolün: {celeb}.",
            "O kişiye uygun konuşma ritmi, tavır ve kelime seçimi kullan.",
            "Doğrudan biyografi okumaz gibi değil, karakterin ruhunu taşıyarak konuş."
        ]

    else:
        role_block += [
            "Rolün: karakterli, samimi, zeki bir sohbet asistanı."
        ]

    if state.always_oppositional:
        role_block += [
            "Varsayılan eğilimin: kullanıcıyla aynı çizgide gitmek yerine çoğu durumda karşı argüman kurmak.",
            "Muhalefet yaparken bilgi, alay, zekâ ve ton kullan."
        ]

    if state.topic_identity:
        role_block += [
            f"Kullanıcının hassas veya kimlik verdiği konu: {state.topic_identity}.",
            "Bu konuda gerektiğinde muhalif yaklaşım kullan ama tartışmayı diri tut."
        ]

    tone_map = {
        "soft": "Yumuşak konuş.",
        "warm": "Sıcak ve yakın konuş.",
        "firm": "Net ve ağırlıklı konuş.",
        "playful": "Esprili ve oyuncu konuş.",
        "sharp": "Keskin, iğneleyici ve baskın konuş."
    }

    role_block.append(tone_map.get(state.tone_level, "Doğal konuş."))
    return "\n".join(base + [""] + role_block)


def build_messages(history: List[ChatTurn], user_text: str, state: PersonaState) -> List[dict]:
    system_prompt = build_persona_prompt(state)
    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    for item in history[-10:]:
        content = (item.content or "").strip()
        if not content:
            continue
        role = "assistant" if item.role == "assistant" else "user"
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_text.strip()})
    return messages


def call_gemini(messages: List[dict]) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        prompt = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages
        )

        result = model.generate_content(prompt)
        text = (getattr(result, "text", "") or "").strip()
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

        text = (completion.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print("OpenAI chat error:", e)
        return None


def persist_persona_state(
    session_id: Optional[str],
    voice_mode: Optional[str],
    state: PersonaState
) -> None:
    if not supabase or not session_id:
        return

    try:
        supabase.table("chat_persona_saved_chats").update({
            "persona_type": state.persona_type,
            "persona_name": state.persona_name,
            "celebrity_name": state.celebrity_name,
            "tone_level": state.tone_level,
            "always_oppositional": state.always_oppositional,
            "selected_voice_mode": voice_mode or state.selected_voice_mode or "tts"
        }).eq("id", session_id).execute()
    except Exception as e:
        print("Persona persist error:", e)


@router.post("/api/italkyai/chat")
async def italkyai_chat(body: ChatBody):
    user_text = (body.text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="empty_text")

    persona_state = detect_persona_from_text(user_text)
    persona_state.selected_voice_mode = body.voice_mode or "tts"
    persona_state = merge_persona_from_history(body.history, persona_state)

    messages = build_messages(body.history, user_text, persona_state)

    reply = call_gemini(messages)
    model_used = "gemini"

    if not reply:
        reply = call_openai(messages)
        model_used = "openai"

    if not reply:
        return {
            "ok": False,
            "reply": "Şu an cevap üretirken bir sorun oluştu. Bir daha dener misin?"
        }

    persist_persona_state(body.session_id, body.voice_mode, persona_state)

    return {
        "ok": True,
        "reply": reply,
        "model": model_used,
        "persona": {
            "persona_type": persona_state.persona_type,
            "persona_name": persona_state.persona_name,
            "celebrity_name": persona_state.celebrity_name,
            "tone_level": persona_state.tone_level,
            "always_oppositional": persona_state.always_oppositional,
            "selected_voice_mode": persona_state.selected_voice_mode,
        }
        }
