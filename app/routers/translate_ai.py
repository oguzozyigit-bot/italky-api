from __future__ import annotations

import json
import math
import os
import re
import html
from typing import Any, Dict, Optional, Tuple, List

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["translate_ai"])

GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
CULTURAL_PROVIDER_TIMEOUT_SECONDS = float(os.getenv("CULTURAL_PROVIDER_TIMEOUT_SECONDS", "5"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class TranslateBody(BaseModel):
    text: str
    from_lang: str
    to_lang: str
    source: Optional[str] = None
    target: Optional[str] = None
    mode: Optional[str] = "normal"      # normal | cultural
    tone: Optional[str] = "neutral"     # neutral | happy | angry | sad | excited
    style: Optional[str] = "balanced"   # balanced | warm | clear | social
    use_ai: Optional[bool] = False
    cultural: Optional[bool] = False
    surface: Optional[str] = None
    google_only: Optional[bool] = False  # True ise sadece Google denenir; başka sağlayıcıya düşmez.

    # Ataların Dili
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


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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

    forbidden_patterns = [
        r"\btranslation\s*:",
        r"\btranslated text\s*:",
        r"\bhere is the translation\b",
        r"\bthe translation is\b",
        r"\bçeviri\s*:",
        r"\bişte çeviri\b",
        r"\bgoogle translate\b",
        r"\blanguage model\b",
        r"\bai model\b",
        r"\byapay zeka\b",
    ]

    return any(re.search(p, s, flags=re.IGNORECASE) for p in forbidden_patterns)


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
    allow_short_cultural_equivalent: bool = False,
) -> Tuple[bool, str]:
    out = cleanup_translation_text(out_text)

    if not out:
        return False, "empty_output"

    if contains_forbidden_meta_output(out):
        return False, "meta_output"

    if not allow_short_cultural_equivalent and (strict_short or is_short_utterance(src_text)):
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
    "ku": "Kürtçe Kurmanci",
    "ckb": "Kürtçe Sorani",
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
    "pt", "nl", "sv", "no", "da", "fi", "pl", "cs", "sk", "hu",
    "sq", "bs", "sr", "hr", "mk", "bg", "ro", "el",
    "uk", "ru",
    "ab", "ce", "av", "os", "crh",
    "ku", "ckb",
    "ka", "az", "kk", "ky", "uz", "tk", "ug", "tt", "ba", "gag",
    "he", "ar", "fa", "ur",
    "hi", "bn", "id", "ms", "vi", "th", "zh", "ja", "ko", "fil",
    "mr", "ta", "te", "gu", "kn",
    "hy", "sl", "et", "lv", "lt",
    "af", "sw",
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
        return False

    cultural_key = normalize_demo_cultural_key(s)
    cultural_markers = {
        "sakla saman",
        "damlaya damlaya",
        "bir tasla",
        "iki kus",
        "ayagini yorgan",
        "etekleri zil",
        "kulak ardi",
        "lafin gelisi",
        "gonlunu almak",
    }
    if any(marker in cultural_key for marker in cultural_markers):
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
# ATALARIN DİLİ - SADECE LATİN -> GÖKTÜRK HARF DÖNÜŞÜMÜ
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
# GOOGLE PROVIDER CALLS
# =========================================================

def google_translate_free(text: str, source: str, target: str, timeout: float = 12) -> str:
    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }

    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()

    translated = ""
    if isinstance(data, list) and data and isinstance(data[0], list):
        for item in data[0]:
            if isinstance(item, list) and item:
                translated += str(item[0] or "")
    return cleanup_translation_text(translated)


def google_translate_official(text: str, source: str, target: str, timeout: float = 12) -> str:
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

    r = requests.post(url, data=payload, timeout=timeout)
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


# =========================================================
# FACE TO FACE DEMO AI PROVIDERS
# =========================================================

def is_facetoface_demo_ai_request(body: TranslateBody, mode: str) -> bool:
    return (
        str(body.surface or "").strip().lower() == "facetoface_demo"
        and (
            mode == "cultural"
            or truthy(body.use_ai)
            or truthy(body.cultural)
        )
    )


def build_translation_response(
    translated: str,
    provider: str,
    ai_used: bool,
    source: str,
    target: str,
    chars_used: int,
) -> Dict[str, Any]:
    value = cleanup_translation_text(translated)
    return {
        "ok": True,
        "translated": value,
        "translation": value,
        "text": value,
        "provider": provider,
        "ai_used": ai_used,
        "charged": False,
        "chars_used": chars_used,
        "from_lang": source,
        "to_lang": target,
    }


DEMO_CULTURAL_OVERRIDES = {
    ("tr", "en", "sakla samanı gelir zamanı"): "Waste not, want not.",
    ("tr", "en", "sakla samani gelir zamani"): "Waste not, want not.",
    ("tr", "en", "sakla zamanı gelir zamanı"): "Waste not, want not.",
    ("tr", "en", "sakla zamani gelir zamani"): "Waste not, want not.",
}


def normalize_demo_cultural_key(text: str) -> str:
    replacements = str.maketrans({
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "i": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
        "â": "a",
        "î": "i",
        "û": "u",
    })
    s = normalize_text(text).lower().replace("\u0307", "").translate(replacements)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def lookup_demo_cultural_override(text: str, source: str, target: str) -> str:
    src = canonical(source)
    dst = canonical(target)
    keys = {
        normalize_text(text).lower(),
        normalize_demo_cultural_key(text),
    }

    for key in keys:
        translated = DEMO_CULTURAL_OVERRIDES.get((src, dst, key), "")
        if translated:
            return translated
    return ""


def cultural_translation_prompt(text: str, source: str, target: str) -> str:
    return (
        "You are a cultural proverb and conversation translator.\n"
        "Your job is not literal translation.\n"
        f"Translate the user's text from {lang_display(source)} ({source}) "
        f"to {lang_display(target)} ({target}).\n"
        "If the source text contains a proverb, idiom, joke, sarcasm, cultural phrase, "
        "or figurative expression, replace it with the closest natural equivalent in the target language.\n"
        "If the text is a proverb or idiom, choose the natural proverb/idiom in the target language.\n"
        "For Turkish \"Sakla samanı, gelir zamanı.\", translate to English as \"Waste not, want not.\"\n"
        "Never translate it as \"Save the straw\" or \"Hide the straw\".\n"
        "Return only the final translation.\n"
        "Return only the translated phrase/sentence.\n"
        "No explanation.\n\n"
        "Examples:\n"
        "Turkish → English:\n"
        "\"Sakla samanı, gelir zamanı.\" => \"Waste not, want not.\"\n"
        "\"Damlaya damlaya göl olur.\" => \"Every little bit helps.\"\n"
        "\"Bir taşla iki kuş vurmak.\" => \"Kill two birds with one stone.\"\n\n"
        f"User text:\n{text}"
    )


def call_openai_cultural_translate(text: str, source: str, target: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY, timeout=CULTURAL_PROVIDER_TIMEOUT_SECONDS)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You translate conversation naturally across cultures. "
                        "Return only the translation, with no label or explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": cultural_translation_prompt(text, source, target),
                },
            ],
            temperature=0.25,
        )
        translated = cleanup_translation_text(completion.choices[0].message.content or "")
        ok, _ = validate_translation_output(
            text,
            translated,
            source,
            target,
            allow_short_cultural_equivalent=True,
        )
        return translated if ok else None
    except Exception as e:
        print("[translate_ai] demo openai failed:", e)
        return None


def call_gemini_cultural_translate(text: str, source: str, target: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)
        result = model.generate_content(
            cultural_translation_prompt(text, source, target),
            request_options={"timeout": CULTURAL_PROVIDER_TIMEOUT_SECONDS},
        )
        translated = cleanup_translation_text(getattr(result, "text", "") or "")
        ok, _ = validate_translation_output(
            text,
            translated,
            source,
            target,
            allow_short_cultural_equivalent=True,
        )
        return translated if ok else None
    except Exception as e:
        print("[translate_ai] demo gemini failed:", e)
        return None


def demo_google_translate_fallback(text: str, source: str, target: str) -> Optional[str]:
    try:
        translated = google_translate_official(
            text,
            source,
            target,
            timeout=CULTURAL_PROVIDER_TIMEOUT_SECONDS,
        )
        ok, _ = validate_translation_output(text, translated, source, target)
        if translated and ok:
            return translated
    except Exception as e1:
        print("[translate_ai] demo google official failed:", e1)

    try:
        translated = google_translate_free(
            text,
            source,
            target,
            timeout=CULTURAL_PROVIDER_TIMEOUT_SECONDS,
        )
        ok, _ = validate_translation_output(text, translated, source, target)
        if translated and ok:
            return translated
    except Exception as e2:
        print("[translate_ai] demo google free failed:", e2)

    return None


def _with_cultural_charge(
    response: Dict[str, Any],
    user_id: str,
    cost: int,
    source_text: str,
    target_lang: str,
) -> Dict[str, Any]:
    charge = _charge_cultural_translation_tokens(
        user_id=user_id,
        cost=cost,
        source_text=source_text,
        target_lang=target_lang,
    )
    response.update(
        {
            "charged": bool(charge.get("charged", True)),
            "tokens_charged": int(charge.get("tokens_charged") or charge.get("cost") or cost or 0),
            "tokens_before": charge.get("tokens_before"),
            "tokens_after": charge.get("tokens_after"),
            "wallet": charge,
        }
    )
    return response


def translate_facetoface_demo_ai(
    text: str,
    source: str,
    target: str,
    tone: str,
    style: str,
    user_id: str,
) -> Dict[str, Any]:
    cost = cultural_translation_token_cost(text)
    _precheck_cultural_translation_tokens(user_id, cost)

    override_text = lookup_demo_cultural_override(text, source, target)
    if override_text:
        response = build_translation_response(
            override_text,
            "demo_cultural_override",
            True,
            source,
            target,
            len(text),
        )
        return _with_cultural_charge(response, user_id, cost, text, target)

    needs_ai = should_force_ai_for_language_pair(source, target) or should_use_ai_for_cultural(text, tone, style)

    if not needs_ai:
        google_text = demo_google_translate_fallback(text, source, target)
        if google_text:
            response = build_translation_response(google_text, "google", False, source, target, len(text))
            return _with_cultural_charge(response, user_id, cost, text, target)

        raise HTTPException(
            status_code=503,
            detail={
                "code": "CULTURAL_TRANSLATION_PROVIDER_FAILED",
                "reason": "google_translate_failed",
            },
        )

    openai_text = call_openai_cultural_translate(text, source, target)
    if openai_text:
        response = build_translation_response(openai_text, "openai", True, source, target, len(text))
        return _with_cultural_charge(response, user_id, cost, text, target)

    gemini_text = call_gemini_cultural_translate(text, source, target)
    if gemini_text:
        response = build_translation_response(gemini_text, "gemini", True, source, target, len(text))
        return _with_cultural_charge(response, user_id, cost, text, target)

    google_text = demo_google_translate_fallback(text, source, target)
    if google_text:
        response = build_translation_response(google_text, "google", False, source, target, len(text))
        return _with_cultural_charge(response, user_id, cost, text, target)

    raise HTTPException(
        status_code=503,
        detail={
            "code": "CULTURAL_TRANSLATION_PROVIDER_FAILED",
            "reason": "all_providers_failed",
        },
    )


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


def cultural_translation_token_cost(source_text: str) -> int:
    source_len = len(normalize_text(source_text))
    if source_len <= 0:
        return 0
    return max(1, math.ceil(source_len / 10))


def _get_profile_tokens(user_id: str) -> int:
    sb = _require_supabase()
    try:
        res = (
            sb.table("profiles")
            .select("tokens")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        if not rows:
            raise HTTPException(status_code=404, detail="profile_not_found")
        return int((rows[0] or {}).get("tokens") or 0)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"profile_tokens_failed: {e}")


def _precheck_cultural_translation_tokens(user_id: str, cost: int) -> Dict[str, Any]:
    tokens = _get_profile_tokens(user_id)
    can_afford = tokens >= int(cost or 0)
    result = {
        "tokens": tokens,
        "tokens_after": tokens - int(cost or 0) if can_afford else tokens,
        "tokens_charged": int(cost or 0) if can_afford else 0,
        "required_tokens": int(cost or 0),
        "can_afford": can_afford,
    }

    if not can_afford:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "INSUFFICIENT_TOKENS",
                "reason": "insufficient_tokens",
                **result,
            },
        )

    return result


def _charge_cultural_translation_tokens(
    user_id: str,
    cost: int,
    source_text: str,
    target_lang: str,
) -> Dict[str, Any]:
    sb = _require_supabase()
    try:
        rpc = sb.rpc(
            "charge_cultural_translation_tokens",
            {
                "p_user_id": user_id,
                "p_cost": int(cost or 0),
                "p_source_text": normalize_text(source_text),
                "p_target_lang": canonical(target_lang),
            },
        ).execute()

        data = rpc.data
        if data is None:
            raise HTTPException(status_code=500, detail="cultural_charge_empty")

        if isinstance(data, dict) and data.get("ok") is False:
            if data.get("reason") == "insufficient_tokens":
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "INSUFFICIENT_TOKENS",
                        **data,
                    },
                )
            raise HTTPException(status_code=500, detail=data)

        return data if isinstance(data, dict) else {"ok": True, "raw": data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cultural_charge_failed: {e}")


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
    source = canonical(body.from_lang or body.source)
    target = canonical(body.to_lang or body.target)
    mode = str(body.mode or "normal").strip().lower()
    tone = canonical_tone(body.tone or "neutral")
    style = canonical_style(body.style or "balanced")

    atalar_mode = bool(body.atalar_mode)
    atalar_source = canonical(body.atalar_source or source)
    atalar_target = canonical(body.atalar_target or target)
    reading_mode = bool(body.reading_mode)
    google_only = bool(body.google_only)

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
        "reading_mode": reading_mode,
        "google_only": google_only,
        "use_ai": bool(body.use_ai),
        "cultural": bool(body.cultural),
        "surface": str(body.surface or ""),
    })

    if not text:
        return {"ok": False, "error": "empty_text"}

    if not source or not target:
        return {"ok": False, "error": "missing_lang"}

    # =====================================================
    # ATALARIN DİLİ: SADECE LATİN -> GÖKTÜRK HARF ÇEVİRİSİ
    # =====================================================
    if atalar_mode:
        if atalar_source == "tr" and atalar_target == "gokturk":
            local = turkish_to_gokturk_with_reading(text)
            gokturk_text = local["gokturk_text"]
            gokturk_reading = local["gokturk_reading"] if reading_mode else ""

            return {
                "ok": True,
                "translated": gokturk_text,
                "gokturk_text": gokturk_text,
                "gokturk_reading": gokturk_reading,
                "literal_reading": gokturk_reading,
                "provider": "atalar_local",
                "ai_used": False,
                "charged": False,
                "chars_used": len(text),
            }

        return {
            "ok": False,
            "error": "atalar_mode_only_supports_tr_to_gokturk"
        }

    if source == target:
        return {
            "ok": True,
            "translated": text,
            "translation": text,
            "text": text,
            "provider": "none",
            "ai_used": False,
            "charged": False,
        }

    if is_facetoface_demo_ai_request(body, mode):
        jwt_token = _get_bearer(authorization)
        user = _get_user_from_jwt(jwt_token)
        return translate_facetoface_demo_ai(
            text=text,
            source=source,
            target=target,
            tone=tone,
            style=style,
            user_id=user["id"],
        )

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

    # =====================================================
    # GOOGLE-ONLY TRANSLATION
    # Bu routerda sadece Google fallback vardır. Google başarısızsa hata döner.
    # =====================================================
    if mode in {"normal", "cultural"}:
        if not is_google_supported_pair(source, target):
            return {
                "ok": False,
                "error": "google_unsupported_language_pair",
                "ai_used": False,
                "charged": False,
                "from_lang": source,
                "to_lang": target,
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
        except Exception as e:
            print("[translate_ai] google-only failed:", e)

        return {
            "ok": False,
            "error": "google_translate_failed",
            "ai_used": False,
            "charged": False,
            "from_lang": source,
            "to_lang": target,
        }

    return {"ok": False, "error": "invalid_mode"}
