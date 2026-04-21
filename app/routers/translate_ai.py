
from __future__ import annotations

import json
import os
import re
import html
from typing import Any, Dict, Optional, Tuple, List

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["translate_ai"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class TranslateBody(BaseModel):
    text: str
    from_lang: str
    to_lang: str
    mode: Optional[str] = "normal"      # normal | cultural
    tone: Optional[str] = "neutral"     # neutral | happy | angry | sad | excited
    style: Optional[str] = "balanced"   # balanced | warm | clear | social

    atalar_mode: Optional[bool] = False
    atalar_source: Optional[str] = None
    atalar_target: Optional[str] = None

    historical_mode: Optional[bool] = False
    reading_mode: Optional[bool] = False


# =========================================================
# BASIC HELPERS
# =========================================================

def canonical(code: str) -> str:
    return str(code or "").strip().lower().split("-")[0]


def normalize_text(text: str) -> str:
    s = str(text or "")
    s = s.replace("\u200b", "")
    s = s.replace("\ufeff", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def canonical_tone(tone: str) -> str:
    v = str(tone or "neutral").strip().lower()
    if v in {"neutral", "happy", "angry", "sad", "excited"}:
        return v
    return "neutral"


def canonical_style(style: str) -> str:
    v = str(style or "balanced").strip().lower()
    if v in {"balanced", "warm", "clear", "social"}:
        return v
    return "balanced"


def lang_display(code: str) -> str:
    c = canonical(code)
    return LANG_DISPLAY_NAMES.get(c, c.upper())


def safe_json_loads(raw: str) -> Dict[str, Any]:
    s = normalize_text(raw)
    if not s:
        return {}

    try:
        return json.loads(s)
    except Exception:
        pass

    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.DOTALL).strip()
    if fenced != s:
        try:
            return json.loads(fenced)
        except Exception:
            pass

    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}

    return {}


def strip_outer_quotes(s: str) -> str:
    t = normalize_text(s)
    if len(t) >= 2 and (
        (t.startswith('"') and t.endswith('"')) or
        (t.startswith("'") and t.endswith("'"))
    ):
        return t[1:-1].strip()
    return t


def cleanup_translation_text(s: str) -> str:
    t = normalize_text(s)
    t = strip_outer_quotes(t)
    t = html.unescape(t)
    t = re.sub(r"\s+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", normalize_text(text)))


def char_count(text: str) -> int:
    return len(normalize_text(text))


def normalize_compare_key(s: str) -> str:
    t = normalize_text(s).lower()
    t = t.replace("’", "'").replace("`", "'")
    t = re.sub(r"[^\wçğıöşüâîû'-]+", "", t, flags=re.UNICODE)
    return t


def is_single_token_like(text: str) -> bool:
    s = normalize_text(text)
    if not s:
        return False
    tokens = re.findall(r"\S+", s)
    return len(tokens) <= 2 and len(s) <= 24


SHORT_UTTERANCE_SET = {
    "evet", "hayır", "hayir", "tamam", "olur", "peki", "tabii", "tabi", "yok",
    "var", "tamamdır", "tamamdir", "aynen", "doğru", "dogru", "yanlış", "yanlis",
    "teşekkürler", "tesekkurler", "teşekkür ederim", "tesekkur ederim",
    "merhaba", "selam", "günaydın", "gunaydin", "iyi geceler", "iyi akşamlar",
    "nasılsın", "nasilsin", "iyiyim", "üzgünüm", "uzgunum", "özür dilerim",
    "ozur dilerim", "olmaz", "istemiyorum", "istiyorum", "bekle", "dur"
}

COMMON_GREETING_TRANSLATIONS = {
    "tr": {
        "selam",
        "merhaba",
        "nasılsın",
        "nasilsin",
        "iyi geceler",
        "iyi akşamlar",
        "gunaydin",
        "günaydın",
        "teşekkürler",
        "tesekkurler",
        "teşekkür ederim",
        "tesekkur ederim",
        "evet",
        "hayır",
        "hayir",
        "tamam",
        "olur",
    }
}


def is_short_utterance(text: str) -> bool:
    s = normalize_text(text).lower()
    if not s:
        return False

    if s in SHORT_UTTERANCE_SET:
        return True

    wc = word_count(s)
    cc = char_count(s)
    if wc <= 2 and cc <= 18:
        return True

    return False


def is_common_greeting_like(text: str, source: str) -> bool:
    s = normalize_text(text).lower()
    src = canonical(source)
    if not s:
        return False
    return s in COMMON_GREETING_TRANSLATIONS.get(src, set())


def contains_forbidden_meta_output(text: str) -> bool:
    s = normalize_text(text).lower()
    forbidden_markers = [
        "translation:",
        "translated text:",
        "here is the translation",
        "çeviri:",
        "işte çeviri",
        "the translation is",
        "ai",
        "model",
        "gemini",
        "openai",
        "google translate",
    ]
    return any(x in s for x in forbidden_markers)


def probably_expanded_too_much(src: str, out: str, source: str = "", target: str = "") -> bool:
    src_wc = word_count(src)
    out_wc = word_count(out)

    if not src_wc or not out_wc:
        return False

    if is_common_greeting_like(src, source):
        if out_wc <= 6 and char_count(out) <= 36:
            return False

    if src_wc == 1:
        if out_wc >= src_wc + 3:
            return True
        if char_count(src) <= 8 and char_count(out) >= 24:
            return True
        return False

    if src_wc <= 3:
        if out_wc >= src_wc + 5:
            return True
        if char_count(src) <= 14 and char_count(out) >= 42:
            return True
        return False

    if out_wc >= max(src_wc * 3, src_wc + 8):
        return True

    return False


def validate_translation_output(
    src_text: str,
    out_text: str,
    source: str,
    target: str,
    strict_short: bool = False,
) -> Tuple[bool, str]:
    out = cleanup_translation_text(out_text)

    if not out:
        return False, "empty_output"

    if contains_forbidden_meta_output(out):
        return False, "meta_output"

    if strict_short or is_short_utterance(src_text):
        if probably_expanded_too_much(src_text, out, source, target):
            return False, "expanded_short_text"

    if canonical(source) != canonical(target):
        if normalize_compare_key(src_text) == normalize_compare_key(out):
            if word_count(src_text) == 1 and char_count(src_text) >= 3:
                pass
            elif is_short_utterance(src_text) or word_count(src_text) <= 3:
                return False, "same_as_input"

    return True, "ok"


def should_force_literal_mode(text: str, tone: str, style: str) -> bool:
    if is_short_utterance(text):
        return True
    if word_count(text) <= 3 and tone == "neutral" and style in {"balanced", "clear"}:
        return True
    return False


# =========================================================
# LANGUAGE MAPS
# =========================================================

UNSUPPORTED_LOCAL_LANGS = {
    "lzz": "Lazca",
    "ady": "Çerkesce",
    "ab": "Abhazca",
    "kbd": "Kabardeyce",
    "ce": "Çeçence",
    "os": "Osetçe",
    "lez": "Lezgice",
    "av": "Avarca",
    "kmr": "Kürtçe (Kurmancî)",
    "ckb": "Kürtçe (Soranî)",
    "zza": "Zazaca",
    "syc": "Süryanice",
    "gag": "Gagavuzca",
    "crh": "Kırım Tatarcası",
    "nog": "Nogayca",
    "ba": "Başkurtça",
}

LANG_DISPLAY_NAMES = {
    "tr": "Türkçe",
    "en": "İngilizce",
    "de": "Almanca",
    "fr": "Fransızca",
    "it": "İtalyanca",
    "es": "İspanyolca",
    "sq": "Arnavutça",
    "bs": "Boşnakça",
    "sr": "Sırpça",
    "hr": "Hırvatça",
    "mk": "Makedonca",
    "bg": "Bulgarca",
    "ro": "Romence",
    "el": "Yunanca",
    "ku": "Kürtçe",
    "kmr": "Kürtçe (Kurmancî)",
    "ckb": "Kürtçe (Soranî)",
    "zza": "Zazaca",
    "syc": "Süryanice",
    "he": "İbranice",
    "lzz": "Lazca",
    "ab": "Abhazca",
    "ady": "Çerkesce",
    "kbd": "Kabardeyce",
    "ce": "Çeçence",
    "ka": "Gürcüce",
    "os": "Osetçe",
    "lez": "Lezgice",
    "av": "Avarca",
    "az": "Azerbaycan Türkçesi",
    "kk": "Kazakça",
    "ky": "Kırgızca",
    "uz": "Özbekçe",
    "tk": "Türkmence",
    "ug": "Uygurca",
    "tt": "Tatarca",
    "ba": "Başkurtça",
    "gag": "Gagavuzca",
    "crh": "Kırım Tatarcası",
    "nog": "Nogayca",
    "gokturk": "Göktürkçe",
}

SUPPORTED_GOOGLE_LANGS = {
    "tr", "en", "de", "fr", "it", "es",
    "sq", "bs", "sr", "hr", "mk", "bg", "ro", "el",
    "ab", "ce", "av", "os", "crh",
    "ku", "kmr", "ckb",
    "ka", "az", "kk", "ky", "uz", "tk", "ug", "tt", "ba", "gag",
    "he"
}

SHORT_PHRASE_MAP: Dict[Tuple[str, str, str], str] = {
    ("tr", "ab", "merhaba"): "Бзиа убааит",
    ("tr", "ab", "selam"): "Бзиа убааит",
    ("tr", "ab", "nasılsın"): "Ушԥаџьума?",
    ("tr", "ab", "nasilsin"): "Ушԥаџьума?",
    ("tr", "ab", "iyiyim"): "Сара сыбзиоуп",
    ("tr", "ab", "teşekkür ederim"): "Иҭабуп",
    ("tr", "ab", "tesekkur ederim"): "Иҭабуп",
    ("tr", "ab", "teşekkürler"): "Иҭабуп",
    ("tr", "ab", "tesekkurler"): "Иҭабуп",
    ("tr", "ab", "evet"): "Ааи",
    ("tr", "ab", "hayır"): "Мап",
    ("tr", "ab", "hayir"): "Мап",

    ("tr", "ckb", "merhaba"): "سڵاو",
    ("tr", "ckb", "selam"): "سڵاو",
    ("tr", "ckb", "nasılsın"): "چۆنی؟",
    ("tr", "ckb", "nasilsin"): "چۆنی؟",
    ("tr", "ckb", "iyiyim"): "باشم",
    ("tr", "ckb", "teşekkür ederim"): "سوپاس",
    ("tr", "ckb", "tesekkur ederim"): "سوپاس",
    ("tr", "ckb", "evet"): "بەڵێ",
    ("tr", "ckb", "hayır"): "نەخێر",
    ("tr", "ckb", "hayir"): "نەخێر",

    ("tr", "kmr", "merhaba"): "Silav",
    ("tr", "kmr", "selam"): "Silav",
    ("tr", "kmr", "nasılsın"): "Tu çawa yî?",
    ("tr", "kmr", "nasilsin"): "Tu çawa yî?",
    ("tr", "kmr", "iyiyim"): "Ez baş im",
    ("tr", "kmr", "teşekkür ederim"): "Spas dikim",
    ("tr", "kmr", "tesekkur ederim"): "Spas dikim",
    ("tr", "kmr", "evet"): "Erê",
    ("tr", "kmr", "hayır"): "Na",
    ("tr", "kmr", "hayir"): "Na",
}


# =========================================================
# REGISTER / MODE DECISION
# =========================================================

def detect_register_hint(text: str) -> str:
    s = normalize_text(text).lower()

    formal_markers = [
        "sayın", "saygılarımla", "arz ederim", "rica ederim", "teşekkür ederim",
        "lütfen", "bilginize", "tarafınıza", "konu hakkında", "gerekmektedir",
        "uygundur", "hususunda", "talep ediyorum", "değerlendirmenizi"
    ]

    casual_markers = [
        "ya", "abi", "abla", "kanka", "bence", "hadi", "vallahi", "valla",
        "tamam", "olur", "aynen", "şöyle", "şoyle", "yani", "off", "wow"
    ]

    formal_score = sum(1 for x in formal_markers if x in s)
    casual_score = sum(1 for x in casual_markers if x in s)

    if formal_score >= casual_score + 2:
        return "formal"
    if casual_score >= formal_score + 1:
        return "casual"
    return "neutral"


def should_use_ai_for_cultural(text: str, tone: str, style: str) -> bool:
    s = normalize_text(text)
    low = s.lower()

    if not s:
        return False

    if should_force_literal_mode(s, tone, style):
        return True

    words = re.findall(r"\S+", s)
    word_count_local = len(words)
    char_count_local = len(s)

    if char_count_local <= 60 and word_count_local <= 8:
        simple_hits = [
            "selam", "merhaba", "günaydın", "iyi geceler", "iyi akşamlar",
            "nasılsın", "nasilsin", "iyiyim", "teşekkürler", "tesekkurler",
            "tamam", "olur", "evet", "hayır", "hayir", "görüşürüz", "gorusuruz",
            "hoşça kal", "hosca kal", "kaç para", "yardım eder misin", "adres nerede"
        ]
        if any(x in low for x in simple_hits):
            return False

    emotional_hits = [
        "kırıldım", "kirildim", "alındım", "alindim", "gönül", "gonul",
        "kalbim", "içim", "icim", "ayıp oldu", "ayip oldu", "bozuldu",
        "üzgünüm", "uzgunum", "mutluyum", "çok sevindim", "cok sevindim"
    ]

    idiom_hits = [
        "lafın gelişi", "lafin gelisi", "üstü kapalı", "ustu kapali",
        "ince ince", "iğneleme", "igneleme", "ima", "imalı", "imali",
        "taş atmak", "tas atmak", "gönlünü almak", "gonlunu almak"
    ]

    formal_hits = [
        "sayın", "saygılarımla", "arz ederim", "rica ederim",
        "tarafınıza", "hususunda", "değerlendirmenizi"
    ]

    if tone != "neutral":
        return True

    if style in {"warm", "social"} and char_count_local >= 50:
        return True

    if any(x in low for x in emotional_hits):
        return True

    if any(x in low for x in idiom_hits):
        return True

    if any(x in low for x in formal_hits):
        return True

    if char_count_local >= 180:
        return True

    if word_count_local >= 20:
        return True

    if "!" in s or "?" in s:
        if char_count_local >= 70:
            return True

    return False


# =========================================================
# ATALARIN DİLİ - GÖKTÜRK DÖNÜŞÜM
# =========================================================

MULTI_CHAR_MAP = [
    ("ng", "𐰭"),
    ("ny", "𐰪"),
]

CHAR_MAP_TR_TO_GOKTURK = {
    "a": "𐰀",
    "b": "𐰉",
    "c": "𐰲",
    "ç": "𐰲",
    "d": "𐰑",
    "e": "𐰀",
    "f": "𐰯",
    "g": "𐰏",
    "ğ": "𐰍",
    "h": "𐰴",
    "ı": "𐰃",
    "i": "𐰃",
    "j": "𐰖",
    "k": "𐰚",
    "l": "𐰞",
    "m": "𐰢",
    "n": "𐰤",
    "o": "𐰆",
    "ö": "𐰇",
    "p": "𐰯",
    "q": "𐰚",
    "r": "𐰺",
    "s": "𐰽",
    "ş": "𐱁",
    "t": "𐱅",
    "u": "𐰆",
    "ü": "𐰇",
    "v": "𐰉",
    "w": "𐰉",
    "x": "𐰴𐰽",
    "y": "𐰖",
    "z": "𐰔",
}

CHAR_MAP_GOKTURK_TO_TR = {
    "𐰀": "a",
    "𐰉": "b",
    "𐰲": "ç",
    "𐰑": "d",
    "𐰏": "g",
    "𐰍": "ğ",
    "𐰴": "h",
    "𐰃": "i",
    "𐰚": "k",
    "𐰞": "l",
    "𐰢": "m",
    "𐰤": "n",
    "𐰆": "u",
    "𐰇": "ü",
    "𐰯": "p",
    "𐰺": "r",
    "𐰽": "s",
    "𐱁": "ş",
    "𐱅": "t",
    "𐰖": "y",
    "𐰔": "z",
    "𐰭": "ng",
    "𐰪": "ny",
}

WORD_OVERRIDES_TR_TO_GOKTURK = {
    "turk": {"rune": "𐱅𐰇𐰼𐰚", "read": "türk"},
    "türk": {"rune": "𐱅𐰇𐰼𐰚", "read": "türk"},
    "tanri": {"rune": "𐱅𐰭𐰼𐰃", "read": "tanrı"},
    "tanrı": {"rune": "𐱅𐰭𐰼𐰃", "read": "tanrı"},
    "gok": {"rune": "𐰚𐰇𐰚", "read": "gök"},
    "gök": {"rune": "𐰚𐰇𐰚", "read": "gök"},
    "gokturk": {"rune": "𐰚𐰇𐰚𐱅𐰇𐰼𐰚", "read": "göktürk"},
    "göktürk": {"rune": "𐰚𐰇𐰚𐱅𐰇𐰼𐰚", "read": "göktürk"},
    "bilge": {"rune": "𐰋𐰃𐰠𐰏𐰀", "read": "bilge"},
    "kagan": {"rune": "𐰴𐰍𐰣", "read": "kağan"},
    "kağan": {"rune": "𐰴𐰍𐰣", "read": "kağan"},
    "kut": {"rune": "𐰴𐰆𐱃", "read": "kut"},
    "ulu": {"rune": "𐰆𐰠𐰆", "read": "ulu"},
    "ordu": {"rune": "𐰆𐰺𐰑𐰆", "read": "ordu"},
    "yurt": {"rune": "𐰖𐰆𐰺𐱃", "read": "yurt"},
    "il": {"rune": "𐰃𐰠", "read": "il"},
    "bodun": {"rune": "𐰉𐰆𐰑𐰆𐰣", "read": "bodun"},
    "tegin": {"rune": "𐱅𐰏𐰃𐰣", "read": "tegin"},
    "ata": {"rune": "𐰀𐱃𐰀", "read": "ata"},
    "ana": {"rune": "𐰀𐰣𐰀", "read": "ana"},
    "su": {"rune": "𐰽𐰆", "read": "su"},
    "taş": {"rune": "𐱃𐰀𐱁", "read": "taş"},
    "selam": {"rune": "𐰽𐰞𐰀𐰢", "read": "selam"},
    "merhaba": {"rune": "𐰢𐰼𐰴𐰉𐰀", "read": "merhaba"},
}


def _split_keep_spaces(text: str) -> List[str]:
    return re.split(r"(\s+)", text)


def _clean_word_for_override(word: str) -> str:
    return re.sub(r"[^\wçğıöşü]", "", word, flags=re.UNICODE).lower()


def turkish_to_gokturk_with_reading(text: str) -> Dict[str, str]:
    s = normalize_text(text)
    if not s:
        return {"gokturk_text": "", "gokturk_reading": ""}

    s = s.lower()
    parts = _split_keep_spaces(s)
    converted_parts = []
    reading_parts = []

    for part in parts:
        if not part:
            continue

        if part.isspace():
            converted_parts.append(part)
            reading_parts.append(part)
            continue

        pure = _clean_word_for_override(part)

        if pure in WORD_OVERRIDES_TR_TO_GOKTURK:
            converted_parts.append(WORD_OVERRIDES_TR_TO_GOKTURK[pure]["rune"])
            reading_parts.append(WORD_OVERRIDES_TR_TO_GOKTURK[pure]["read"])
            continue

        out = []
        i = 0
        while i < len(part):
            matched = False

            for src, dst in MULTI_CHAR_MAP:
                if part.startswith(src, i):
                    out.append(dst)
                    i += len(src)
                    matched = True
                    break

            if matched:
                continue

            ch = part[i]
            out.append(CHAR_MAP_TR_TO_GOKTURK.get(ch, ch))
            i += 1

        converted_parts.append("".join(out))
        reading_parts.append(part)

    return {
        "gokturk_text": "".join(converted_parts).strip(),
        "gokturk_reading": "".join(reading_parts).strip(),
    }


def gokturk_to_turkish(text: str) -> str:
    s = normalize_text(text)
    if not s:
        return ""

    out = []
    for ch in s:
        out.append(CHAR_MAP_GOKTURK_TO_TR.get(ch, ch))

    joined = "".join(out).strip()
    return joined


def _extract_json_object(text: str) -> Dict[str, Any]:
    return safe_json_loads(text)


def _historical_prompt(text: str, literal_runes: str, literal_reading: str) -> str:
    return f"""
You are an expert in Old Turkic / Göktürk writing and historical Turkish linguistics.

We already have a literal Göktürk-script rendering of a modern Turkish input.

You must be EXTREMELY conservative.

Return STRICT JSON only.

Rules:
- If there is NO strong, historically defensible Old Turkic counterpart, return empty strings.
- Do NOT invent a poetic substitute.
- Do NOT guess.
- Do NOT paraphrase loosely.
- historical_text must only be filled if you are highly confident.
- historical_reading must match historical_text exactly.
- historical_meaning must be short modern Turkish explanation.
- If uncertain, all three fields must be empty.

Input:
modern_text = {text}
literal_runes = {literal_runes}
literal_reading = {literal_reading}

JSON:
{{
  "historical_text": "",
  "historical_reading": "",
  "historical_meaning": ""
}}
""".strip()


def _historical_from_gemini(text: str, literal_runes: str, literal_reading: str) -> Dict[str, str]:
    if not GEMINI_API_KEY:
        return {}

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )

        payload = {
            "contents": [{"parts": [{"text": _historical_prompt(text, literal_runes, literal_reading)}]}],
            "generationConfig": {
                "temperature": 0.05,
                "topP": 0.4,
                "maxOutputTokens": 220
            }
        }

        r = requests.post(url, json=payload, timeout=18)
        r.raise_for_status()
        data = r.json()

        candidates = data.get("candidates") or []
        if not candidates:
            return {}

        parts = candidates[0].get("content", {}).get("parts", [])
        raw = "".join(str(p.get("text", "")) for p in parts).strip()
        parsed = _extract_json_object(raw)
        if not parsed:
            return {}

        return {
            "historical_text": normalize_text(parsed.get("historical_text", "")),
            "historical_reading": normalize_text(parsed.get("historical_reading", "")),
            "historical_meaning": normalize_text(parsed.get("historical_meaning", "")),
        }
    except Exception as e:
        print("[translate_ai] historical gemini failed:", e)
        return {}


def _historical_from_openai(text: str, literal_runes: str, literal_reading: str) -> Dict[str, str]:
    if not OPENAI_API_KEY:
        return {}

    try:
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": OPENAI_MODEL,
            "input": _historical_prompt(text, literal_runes, literal_reading),
        }

        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=18,
        )
        r.raise_for_status()
        data = r.json()

        chunks = []
        for item in data.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in {"output_text", "text"}:
                    txt = str(content.get("text") or "").strip()
                    if txt:
                        chunks.append(txt)

        raw = "".join(chunks).strip()
        if not raw:
            raw = str(data.get("output_text") or data.get("text") or "").strip()

        parsed = _extract_json_object(raw)
        if not parsed:
            return {}

        return {
            "historical_text": normalize_text(parsed.get("historical_text", "")),
            "historical_reading": normalize_text(parsed.get("historical_reading", "")),
            "historical_meaning": normalize_text(parsed.get("historical_meaning", "")),
        }
    except Exception as e:
        print("[translate_ai] historical openai failed:", e)
        return {}


def historical_gokturk_enrichment(text: str, literal_runes: str, literal_reading: str) -> Dict[str, str]:
    data = _historical_from_gemini(text, literal_runes, literal_reading)
    if data.get("historical_text"):
        return data

    data = _historical_from_openai(text, literal_runes, literal_reading)
    if data.get("historical_text"):
        return data

    return {
        "historical_text": "",
        "historical_reading": "",
        "historical_meaning": "",
    }


# =========================================================
# PROVIDER DECISION
# =========================================================

def lookup_short_phrase(text: str, source: str, target: str) -> str:
    key = (canonical(source), canonical(target), normalize_text(text).lower())
    return SHORT_PHRASE_MAP.get(key, "").strip()


def is_google_supported_pair(source: str, target: str) -> bool:
    s = canonical(source)
    t = canonical(target)
    return s in SUPPORTED_GOOGLE_LANGS and t in SUPPORTED_GOOGLE_LANGS


def should_force_ai_for_language_pair(source: str, target: str) -> bool:
    s = canonical(source)
    t = canonical(target)

    if s == "gokturk" or t == "gokturk":
        return True

    return not is_google_supported_pair(s, t)


# =========================================================
# PROMPTS
# =========================================================

def build_strict_translation_prompt(
    text: str,
    source: str,
    target: str,
    tone: str = "neutral",
    style: str = "balanced",
    literal_mode: bool = False,
) -> str:
    source_name = lang_display(source)
    target_name = lang_display(target)
    register_hint = detect_register_hint(text)

    tone_map = {
        "neutral": "Keep the tone neutral and natural.",
        "happy": "Keep the tone warm, positive and natural.",
        "angry": "Keep the emotional force, but do not exaggerate.",
        "sad": "Keep the tone soft, sincere and gentle.",
        "excited": "Keep the tone energetic and natural."
    }

    style_map = {
        "balanced": "Use balanced, everyday phrasing.",
        "warm": "Use warm and human phrasing, but do not add content.",
        "clear": "Use clear and easy-to-understand phrasing.",
        "social": "Use lightly conversational spoken phrasing, but do not add content."
    }

    register_map = {
        "formal": "If the source is formal, keep it respectful and natural.",
        "casual": "If the source is casual, keep it casual and natural.",
        "neutral": "Do not make it overly formal or overly slangy."
    }

    literal_block = ""
    if literal_mode:
        literal_block = """
SPECIAL STRICT MODE:
- The input is a very short utterance or a minimal response.
- DO NOT expand it.
- DO NOT clarify it.
- DO NOT make it more expressive.
- DO NOT turn one word into a sentence.
- Keep it as short and as exact as the target language allows.
"""

    return f"""
You are a highly precise translator.

Translate from {source_name} to {target_name}.

Return STRICT JSON only:
{{
  "translation": ""
}}

Rules:
- Preserve the exact meaning.
- Do not explain.
- Do not add context.
- Do not add politeness unless it is already in the source.
- Do not add emotional amplification.
- Do not add quotation marks.
- Do not mention AI, translation engines, tools, models, or brands.
- Output must be only valid JSON.

{literal_block}

Tone:
{tone_map.get(tone, tone_map["neutral"])}

Style:
{style_map.get(style, style_map["balanced"])}

Register:
{register_map.get(register_hint, register_map["neutral"])}

Source text:
{text}
""".strip()


# =========================================================
# PROVIDER CALLS
# =========================================================

def _extract_translation_field(raw: str) -> str:
    parsed = safe_json_loads(raw)
    if isinstance(parsed, dict):
        val = parsed.get("translation", "")
        return cleanup_translation_text(str(val or ""))
    return ""


def _gemini_translate_structured(
    text: str,
    source: str,
    target: str,
    tone: str,
    style: str,
    literal_mode: bool = False,
) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    prompt = build_strict_translation_prompt(
        text=text,
        source=source,
        target=target,
        tone=tone,
        style=style,
        literal_mode=literal_mode,
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.05 if literal_mode else 0.18,
            "topP": 0.35 if literal_mode else 0.7,
            "maxOutputTokens": 320,
        }
    }

    r = requests.post(url, json=payload, timeout=18)
    r.raise_for_status()
    data = r.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini empty candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    raw = "".join(str(p.get("text", "")) for p in parts).strip()
    out = _extract_translation_field(raw)

    if not out:
        raise RuntimeError("Gemini invalid structured output")

    return out


def _openai_translate_structured(
    text: str,
    source: str,
    target: str,
    tone: str,
    style: str,
    literal_mode: bool = False,
) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")

    prompt = build_strict_translation_prompt(
        text=text,
        source=source,
        target=target,
        tone=tone,
        style=style,
        literal_mode=literal_mode,
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
    }

    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers=headers,
        json=payload,
        timeout=18,
    )
    r.raise_for_status()
    data = r.json()

    chunks = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"}:
                txt = str(content.get("text") or "").strip()
                if txt:
                    chunks.append(txt)

    raw = "".join(chunks).strip()
    if not raw:
        raw = str(data.get("output_text") or data.get("text") or "").strip()

    out = _extract_translation_field(raw)
    if not out:
        raise RuntimeError("OpenAI invalid structured output")

    return out


def ai_direct_translate_by_language_name(
    text: str,
    source: str,
    target: str,
    tone: str = "neutral",
    style: str = "balanced"
) -> Tuple[str, str]:
    literal_mode = should_force_literal_mode(text, tone, style)

    try:
        out = _gemini_translate_structured(text, source, target, tone, style, literal_mode=literal_mode)
        ok, reason = validate_translation_output(text, out, source, target, strict_short=literal_mode)
        if ok:
            return out, "gemini_local_lang"
        print("[translate_ai] gemini_local_lang invalid:", reason, "raw=", repr(out))
    except Exception as e:
        print("[translate_ai] gemini local-lang failed:", e)

    try:
        out = _openai_translate_structured(text, source, target, tone, style, literal_mode=literal_mode)
        ok, reason = validate_translation_output(text, out, source, target, strict_short=literal_mode)
        if ok:
            return out, "openai_local_lang"
        print("[translate_ai] openai_local_lang invalid:", reason, "raw=", repr(out))
    except Exception as e:
        print("[translate_ai] openai local-lang failed:", e)

    raise RuntimeError("local_lang_ai_translate_failed")


def google_translate_free(text: str, source: str, target: str) -> str:
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }

    r = requests.get(url, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()

    translated = ""
    if isinstance(data, list) and data and isinstance(data[0], list):
        for item in data[0]:
            if isinstance(item, list) and item:
                translated += str(item[0] or "")
    return cleanup_translation_text(translated)


def google_translate_official(text: str, source: str, target: str) -> str:
    if not GOOGLE_TRANSLATE_API_KEY:
        raise RuntimeError("GOOGLE_TRANSLATE_API_KEY missing")

    url = "https://translation.googleapis.com/language/translate/v2"
    payload = {
        "q": text,
        "source": source,
        "target": target,
        "format": "text",
        "key": GOOGLE_TRANSLATE_API_KEY,
    }

    r = requests.post(url, data=payload, timeout=12)
    r.raise_for_status()
    data = r.json()
    translated = (
        data.get("data", {})
        .get("translations", [{}])[0]
        .get("translatedText", "")
    )
    return cleanup_translation_text(str(translated))


def fast_translate_fallback(text: str, source: str, target: str) -> str:
    try:
        print("[translate_ai] trying google free")
        translated = google_translate_free(text, source, target)
        print("[translate_ai] google free raw:", repr(translated))
        ok, reason = validate_translation_output(
            text,
            translated,
            source,
            target,
            strict_short=is_short_utterance(text),
        )
        print("[translate_ai] google free validation:", {"ok": ok, "reason": reason})
        if translated and ok:
            return translated
    except Exception as e1:
        print("[translate_ai] google_free failed:", e1)

    print("[translate_ai] trying google official fallback")
    translated = google_translate_official(text, source, target)
    print("[translate_ai] google official raw:", repr(translated))
    ok, reason = validate_translation_output(
        text,
        translated,
        source,
        target,
        strict_short=is_short_utterance(text),
    )
    print("[translate_ai] google official validation:", {"ok": ok, "reason": reason})
    if not ok:
        raise RuntimeError(f"google_output_invalid:{reason}")
    return translated


def _try_gemini_then_openai(
    text: str,
    source: str,
    target: str,
    tone: str,
    style: str
) -> Tuple[str, str]:
    literal_mode = should_force_literal_mode(text, tone, style)

    try:
        print("[translate_ai] trying gemini cultural")
        translated = _gemini_translate_structured(text, source, target, tone, style, literal_mode=literal_mode)
        ok, reason = validate_translation_output(text, translated, source, target, strict_short=literal_mode)
        if ok:
            return translated, "gemini"
        print("[translate_ai] gemini invalid:", reason, "raw=", repr(translated))
    except Exception as e1:
        print("[translate_ai] gemini failed:", e1)

    try:
        print("[translate_ai] trying openai cultural fallback")
        translated = _openai_translate_structured(text, source, target, tone, style, literal_mode=literal_mode)
        ok, reason = validate_translation_output(text, translated, source, target, strict_short=literal_mode)
        if ok:
            return translated, "openai"
        print("[translate_ai] openai invalid:", reason, "raw=", repr(translated))
    except Exception as e2:
        print("[translate_ai] openai failed:", e2)

    raise RuntimeError("ai_translate_failed")


# =========================================================
# SUPABASE / BILLING
# =========================================================

def _require_supabase() -> Client:
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase ayarları eksik")
    return supabase


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="authorization_missing")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="authorization_invalid")

    return parts[1].strip()


def _get_user_from_jwt(jwt_token: str) -> Dict[str, Any]:
    sb = _require_supabase()
    try:
        res = sb.auth.get_user(jwt_token)
        user = getattr(res, "user", None)
        if not user or not getattr(user, "id", None):
            raise HTTPException(status_code=401, detail="user_not_found")
        return {
            "id": str(user.id),
            "email": getattr(user, "email", None),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"jwt_verify_failed: {e}")


def _get_wallet_summary(user_id: str) -> Dict[str, Any]:
    sb = _require_supabase()
    try:
        rpc = sb.rpc("get_wallet_summary", {"p_user_id": user_id}).execute()
        data = rpc.data
        if data is None:
            raise HTTPException(status_code=500, detail="wallet_summary_empty")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"wallet_summary_failed: {e}")


def _precheck_text_charge(user_id: str, char_count_local: int) -> Dict[str, Any]:
    summary = _get_wallet_summary(user_id)

    tokens = int(summary.get("tokens") or 0)
    text_bucket = int(summary.get("text_bucket") or 0)

    total = text_bucket + max(0, int(char_count_local))
    jetons_needed = total // 1000

    return {
        "tokens": tokens,
        "text_bucket": text_bucket,
        "jetons_needed": jetons_needed,
        "can_afford": tokens >= jetons_needed,
    }


def _charge_text_usage(user_id: str, chars_used: int, source: str, description: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    sb = _require_supabase()
    try:
        rpc = sb.rpc(
            "apply_usage_charge",
            {
                "p_user_id": user_id,
                "p_usage_kind": "text",
                "p_chars_used": int(chars_used),
                "p_source": source,
                "p_description": description,
                "p_meta": meta,
            },
        ).execute()

        data = rpc.data
        if data is None:
            raise HTTPException(status_code=500, detail="usage_charge_empty")

        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"usage_charge_failed: {e}")


# =========================================================
# ROUTES
# =========================================================

@router.get("/translate_ai/health")
@router.get("/translate-ai/health")
@router.get("/translate/health")
def translate_ai_health():
    return {"ok": True, "service": "translate_ai"}


@router.post("/translate_ai")
@router.post("/translate-ai")
@router.post("/translate")
def translate_ai(
    body: TranslateBody,
    authorization: Optional[str] = Header(default=None),
):
    text = normalize_text(body.text)
    source = canonical(body.from_lang)
    target = canonical(body.to_lang)
    mode = str(body.mode or "normal").strip().lower()
    tone = canonical_tone(body.tone or "neutral")
    style = canonical_style(body.style or "balanced")

    atalar_mode = bool(body.atalar_mode)
    atalar_source = canonical(body.atalar_source or source)
    atalar_target = canonical(body.atalar_target or target)
    historical_mode = bool(body.historical_mode)
    reading_mode = bool(body.reading_mode)

    print("[translate_ai] request:", {
        "text": text,
        "source": source,
        "target": target,
        "mode": mode,
        "tone": tone,
        "style": style,
        "atalar_mode": atalar_mode,
        "atalar_source": atalar_source,
        "atalar_target": atalar_target,
        "historical_mode": historical_mode,
        "reading_mode": reading_mode,
    })

    if not text:
        return {"ok": False, "error": "empty_text"}

    if not source or not target:
        return {"ok": False, "error": "missing_lang"}

    if atalar_mode:
        if atalar_source == "tr" and atalar_target == "gokturk":
            local = turkish_to_gokturk_with_reading(text)
            gokturk_text = local["gokturk_text"]
            gokturk_reading = local["gokturk_reading"] if reading_mode else ""

            historical = {
                "historical_text": "",
                "historical_reading": "",
                "historical_meaning": "",
            }
            if historical_mode:
                historical = historical_gokturk_enrichment(text, gokturk_text, gokturk_reading)

            return {
                "ok": True,
                "translated": gokturk_text,
                "gokturk_text": gokturk_text,
                "gokturk_reading": gokturk_reading,
                "literal_reading": gokturk_reading,
                "historical_text": historical.get("historical_text", ""),
                "historical_reading": historical.get("historical_reading", ""),
                "historical_meaning": historical.get("historical_meaning", ""),
                "provider": "atalar_local",
                "ai_used": bool(historical.get("historical_text")),
                "charged": False,
                "chars_used": len(text),
            }

        if atalar_source == "gokturk" and atalar_target == "tr":
            translated = gokturk_to_turkish(text)
            return {
                "ok": True,
                "translated": translated,
                "provider": "atalar_local",
                "ai_used": False,
                "charged": False,
                "chars_used": len(text),
            }

    if source == target:
        return {
            "ok": True,
            "translated": text,
            "provider": "none",
            "ai_used": False,
            "charged": False,
        }

    short_phrase_hit = lookup_short_phrase(text, source, target)
    if short_phrase_hit:
        return {
            "ok": True,
            "translated": short_phrase_hit,
            "provider": "phrase_map",
            "ai_used": False,
            "charged": False,
            "chars_used": len(text),
        }

    if mode == "normal":
        try:
            if is_google_supported_pair(source, target):
                translated = fast_translate_fallback(text, source, target)
                if translated:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": len(text),
                    }

            translated, provider = ai_direct_translate_by_language_name(
                text=text,
                source=source,
                target=target,
                tone=tone,
                style=style,
            )
            if translated:
                return {
                    "ok": True,
                    "translated": translated,
                    "provider": provider,
                    "ai_used": True,
                    "charged": False,
                    "chars_used": len(text),
                }
        except Exception as e:
            print("[translate_ai] normal failed:", e)

        return {"ok": False, "error": "normal_translate_failed"}

    if mode == "cultural":
        force_local_ai = should_force_ai_for_language_pair(source, target)

        short_phrase_hit = lookup_short_phrase(text, source, target)
        if short_phrase_hit:
            return {
                "ok": True,
                "translated": short_phrase_hit,
                "provider": "phrase_map",
                "ai_used": False,
                "charged": False,
                "chars_used": len(text),
            }

        if is_short_utterance(text) and is_google_supported_pair(source, target):
            try:
                translated = fast_translate_fallback(text, source, target)
                if translated:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": len(text),
                    }
            except Exception as e0:
                print("[translate_ai] short literal fallback failed:", e0)

        if not force_local_ai and is_google_supported_pair(source, target) and not should_use_ai_for_cultural(text, tone, style):
            try:
                translated = fast_translate_fallback(text, source, target)
                if translated:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": len(text),
                    }
            except Exception as e0:
                print("[translate_ai] cultural google-first failed:", e0)

        jwt_token = _get_bearer(authorization)
        user = _get_user_from_jwt(jwt_token)
        user_id = user["id"]

        cc = len(text)
        precheck = _precheck_text_charge(user_id, cc)

        if not precheck["can_afford"]:
            return {
                "ok": False,
                "error": "insufficient_tokens",
                "mode": "cultural",
                "usage_kind": "text",
                "chars_used": cc,
                "jetons_needed": precheck["jetons_needed"],
                "tokens_before": precheck["tokens"],
                "text_bucket_before": precheck["text_bucket"],
            }

        try:
            if force_local_ai:
                translated, provider = ai_direct_translate_by_language_name(
                    text=text,
                    source=source,
                    target=target,
                    tone=tone,
                    style=style,
                )
            else:
                translated, provider = _try_gemini_then_openai(text, source, target, tone, style)
        except Exception:
            try:
                print("[translate_ai] trying google official fallback")
                translated = google_translate_official(text, source, target)
                ok, reason = validate_translation_output(
                    text, translated, source, target, strict_short=is_short_utterance(text)
                )
                if ok:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google_official",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": cc,
                    }
                print("[translate_ai] google_official invalid:", reason, "raw=", repr(translated))
            except Exception as e3:
                print("[translate_ai] google_official fallback failed:", e3)

            try:
                print("[translate_ai] trying google free fallback")
                translated = google_translate_free(text, source, target)
                ok, reason = validate_translation_output(
                    text, translated, source, target, strict_short=is_short_utterance(text)
                )
                if ok:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google_free",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": cc,
                    }
                print("[translate_ai] google_free invalid:", reason, "raw=", repr(translated))
            except Exception as e4:
                print("[translate_ai] google_free fallback failed:", e4)

            return {"ok": False, "error": "cultural_translate_failed"}

        charge = _charge_text_usage(
            user_id=user_id,
            chars_used=cc,
            source=f"translate_ai_{provider}",
            description="Kültürel çeviri kullanımı",
            meta={
                "module": "translate_ai",
                "mode": "cultural",
                "provider": provider,
                "from_lang": source,
                "to_lang": target,
                "tone": tone,
                "style": style,
                "short_guard": is_short_utterance(text),
            },
        )

        if not bool(charge.get("ok")):
            return {
                "ok": False,
                "error": "usage_charge_failed",
                "charge": charge,
            }

        if charge.get("reason") == "insufficient_tokens":
            return {
                "ok": False,
                "error": "insufficient_tokens",
                "mode": "cultural",
                "usage_kind": "text",
                "chars_used": cc,
                "jetons_needed": charge.get("jetons_needed", 0),
                "tokens_before": charge.get("tokens_before", 0),
                "text_bucket_before": precheck["text_bucket"],
            }

        return {
            "ok": True,
            "translated": translated,
            "provider": provider,
            "ai_used": True,
            "charged": bool(charge.get("charged", False)),
            "usage_kind": "text",
            "chars_used": cc,
            "jetons_spent": int(charge.get("jetons_spent") or 0),
            "tokens_before": int(charge.get("tokens_before") or precheck["tokens"]),
            "tokens_after": int(charge.get("tokens_after") or precheck["tokens"]),
            "text_bucket": int(charge.get("text_bucket") or 0),
            "voice_bucket": int(charge.get("voice_bucket") or 0),
        }

    return {"ok": False, "error": "invalid_mode"}
