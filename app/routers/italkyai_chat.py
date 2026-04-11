from __future__ import annotations

from typing import Optional, Literal
from pydantic import BaseModel


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


class PersonaState(BaseModel):
    persona_type: PersonaType = "default"
    persona_name: Optional[str] = None
    celebrity_name: Optional[str] = None
    user_identity: Optional[str] = None
    topic_identity: Optional[str] = None
    tone_level: ToneLevel = "warm"
    always_oppositional: bool = False
    selected_voice_mode: Optional[str] = "tts"


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

    # Basit ünlü yakalama
    celeb_markers = [
        "müslüm gürses", "muslum gurses",
        "barış manço", "baris manco",
        "kemal sunal",
        "atatürk", "atatürk ol", "atatürk'sün"
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

    return state


def build_persona_prompt(state: PersonaState) -> str:
    base = [
        "Sen italkyAI'sin.",
        "Kendini asla Gemini, OpenAI, Llama veya başka altyapı adıyla tanıtma.",
        "Kendini italkyAI olarak tanıt.",
        "Gerekirse, bazı altyapılardan geçiş döneminde faydalansan da kendi kozasını ören bağımsız bir yapı olduğunu söyle.",
        "Kullanıcıyla doğal, canlı, karakterli ve bağlama uygun konuş.",
        "Konuşma tarzını kullanıcının verdiği role göre değiştir.",
        "Gerektiğinde muhalif ol, karşı argüman üret, laf sok ama kontrolsüz ağır hakaret üretme.",
        "Tehdit, aşağılayıcı nefret dili, ağır cinsel hakaret ve gerçek zarara teşvik yok.",
        "Sertleşmen istendiğinde bile zekice, iğneleyici, alaycı ve baskın konuş; ama çıplak küfür makinesine dönme.",
        "Kullanıcının verdiği role sadık kal."
    ]

    role_block = []

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

    tone_map = {
        "soft": "Yumuşak konuş.",
        "warm": "Sıcak ve yakın konuş.",
        "firm": "Net ve ağırlıklı konuş.",
        "playful": "Esprili ve oyuncu konuş.",
        "sharp": "Keskin, iğneleyici ve baskın konuş."
    }

    role_block.append(tone_map.get(state.tone_level, "Doğal konuş."))

    return "\n".join(base + [""] + role_block)


# Örnek kullanım:
if __name__ == "__main__":
    text = "Sen benim annemsin ve biraz da muhalif ol"
    state = detect_persona_from_text(text)
    print(state.model_dump())
    print("----")
    print(build_persona_prompt(state))
