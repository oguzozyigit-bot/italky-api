# FILE: italky-api/app/routers/command_parse.py
from __future__ import annotations

import json
import os
import re
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(tags=["command-parse"])

# --- Keys ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Gemini (fast/cheap)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent"
# OpenAI Responses
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

# --- Supported language codes (frontend LANGS ile uyumlu tut) ---
SUPPORTED_LANGS = {
    "tr","en","de","fr","it","es",
    "pt","pt-br","nl","sv","no","da","fi","pl","cs","sk","hu","ro","bg","el",
    "uk","ru","ar","he","fa","ur","hi","bn","id","ms","vi","th","zh","ja","ko",
    "ka"  # ✅ Georgian
}

ALIAS = {
    "pt_br": "pt-br",
    "pt-br": "pt-br",
    "pt": "pt",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh": "zh",
    "georgian": "ka",
    "ka-ge": "ka",
    "ka": "ka",
    "turkish": "tr",
    "english": "en",
    "german": "de",
    "french": "fr",
    "italian": "it",
    "spanish": "es",
}

# Basit dil adı sözlüğü (TR + EN) — hızlı yakalama için
LANG_NAME_TO_CODE = {
    # Turkish
    "türkçe": "tr",
    "turkce": "tr",
    "ingilizce": "en",
    "almanca": "de",
    "fransızca": "fr",
    "fransizca": "fr",
    "italyanca": "it",
    "ispanyolca": "es",
    "portekizce": "pt",
    "rusça": "ru",
    "rusca": "ru",
    "arapça": "ar",
    "arapca": "ar",
    "farsça": "fa",
    "farsca": "fa",
    "ibranice": "he",
    "hintçe": "hi",
    "hintce": "hi",
    "gürcüce": "ka",
    "gurcuce": "ka",
    "japonca": "ja",
    "çince": "zh",
    "cince": "zh",
    "korece": "ko",
    # English
    "turkish": "tr",
    "english": "en",
    "german": "de",
    "french": "fr",
    "italian": "it",
    "spanish": "es",
    "portuguese": "pt",
    "russian": "ru",
    "arabic": "ar",
    "persian": "fa",
    "hebrew": "he",
    "hindi": "hi",
    "georgian": "ka",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
}

# Komut niyeti için TR/EN hızlı kelimeler
INTENT_KEYS = [
    "dil değiştir", "dil degistir", "dili değiştir", "dili degistir",
    "çevir", "cevir", "çevirir misin", "cevirir misin",
    "translate to", "switch to", "change language", "language change", "target language"
]

CONF_THRESHOLD = 0.40  # ✅ daha toleranslı (kısa komutları kaçırmasın)

def _canon_lang(code: str) -> Optional[str]:
    c = (code or "").strip().lower()
    c = c.replace("_", "-")
    c = ALIAS.get(c, c)
    if c in SUPPORTED_LANGS:
        return c
    if "-" in c:
        base = c.split("-")[0]
        base = ALIAS.get(base, base)
        if base in SUPPORTED_LANGS:
            return base
    return None

class CommandParseRequest(BaseModel):
    text: str = Field(..., min_length=1)
    ui_lang: Optional[str] = Field(default=None)

class CommandParseResponse(BaseModel):
    is_command: bool
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    confidence: float = 0.0
    provider_used: str = "none"

def _extract_json_loose(s: str) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    s = s.strip()
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def _quick_parse_local(text: str) -> Optional[CommandParseResponse]:
    """
    AI’ye gitmeden önce hızlı yakalama:
    - Eğer cümlede niyet kelimesi + bir dil adı varsa komut say.
    - source_lang: bilinmiyorsa None bırak (frontend konuşmacı dilini zaten seçiyor olabilir),
      ama senin isteğine göre source’u "konuşulan dil" olarak AI’dan almak daha iyi.
      Burada sadece target’ı garanti yakalıyoruz.
    """
    t = (text or "").strip().lower()
    if not t:
        return None

    has_intent = any(k in t for k in INTENT_KEYS)
    if not has_intent:
        return None

    # Dil adı yakala
    found = None
    # en uzun eşleşme önce gelsin diye
    keys_sorted = sorted(LANG_NAME_TO_CODE.keys(), key=len, reverse=True)
    for name in keys_sorted:
        if name in t:
            found = LANG_NAME_TO_CODE[name]
            break

    if not found:
        return None

    return CommandParseResponse(
        is_command=True,
        source_lang=None,
        target_lang=found,
        confidence=0.85,
        provider_used="local"
    )

async def _call_gemini_parse(text: str, ui_lang: Optional[str]) -> Optional[CommandParseResponse]:
    if not GEMINI_API_KEY:
        return None

    prompt = f"""
You are a command parser for a multilingual translation app.

Goal:
Decide whether the user utterance is a language switching command (change target language).

IMPORTANT DECISION RULE (be strict and useful):
- If the utterance contains an intent to change/translate AND contains a language name (English, German, Georgian, etc.),
  then it IS a command.
- Short commands like "Dil değiştir İngilizce" MUST be treated as a command.

Return STRICT JSON ONLY:
{{
  "is_command": true/false,
  "source_lang": "xx" or null,
  "target_lang": "yy" or null,
  "confidence": 0.0
}}

Notes:
- source_lang is the language the user is speaking in (ISO 639-1).
- target_lang is the requested target (ISO 639-1).
- Supported language codes: {sorted(list(SUPPORTED_LANGS))}
- UI language hint: {ui_lang}

Utterance:
{text}
""".strip()

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if r.status_code >= 400:
            return None

        data = r.json()
        out = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        obj = _extract_json_loose(out)
        if not obj:
            return None

        is_cmd = bool(obj.get("is_command", False))
        conf = float(obj.get("confidence", 0.0) or 0.0)

        src = _canon_lang(obj.get("source_lang") or "")
        tgt = _canon_lang(obj.get("target_lang") or "")

        # eğer komut dedi ama target yoksa güveni düşür
        if is_cmd and not tgt:
            conf = min(conf, 0.35)

        ok = is_cmd and conf >= CONF_THRESHOLD and tgt is not None
        return CommandParseResponse(
            is_command=ok,
            source_lang=src if ok else None,
            target_lang=tgt if ok else None,
            confidence=conf,
            provider_used="gemini",
        )
    except Exception:
        return None

async def _call_openai_parse(text: str, ui_lang: Optional[str]) -> Optional[CommandParseResponse]:
    if not OPENAI_API_KEY:
        return None

    instructions = f"""
You are a command parser for a multilingual translation app.

Goal:
Detect language switching commands.

Decision rule:
- If utterance contains intent to change/translate AND a language name, it IS a command.
- Short commands like "Dil değiştir İngilizce" MUST be treated as a command.

Return STRICT JSON ONLY (no markdown):
{{
  "is_command": true/false,
  "source_lang": "xx" or null,
  "target_lang": "yy" or null,
  "confidence": 0.0
}}

Supported language codes: {sorted(list(SUPPORTED_LANGS))}
UI language hint: {ui_lang}

Utterance:
{text}
""".strip()

    payload: Dict[str, Any] = {
        "model": "gpt-4o-mini",
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": text},
        ],
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(OPENAI_RESPONSES_URL, headers=headers, json=payload)
        if r.status_code >= 400:
            return None

        data = r.json()
        out = (data.get("output_text") or "").strip()

        if not out:
            output = data.get("output") or []
            buf = []
            for item in output:
                for c in (item.get("content") or []):
                    if c.get("type") == "output_text":
                        buf.append(c.get("text", ""))
            out = "".join(buf).strip()

        obj = _extract_json_loose(out)
        if not obj:
            return None

        is_cmd = bool(obj.get("is_command", False))
        conf = float(obj.get("confidence", 0.0) or 0.0)

        src = _canon_lang(obj.get("source_lang") or "")
        tgt = _canon_lang(obj.get("target_lang") or "")

        if is_cmd and not tgt:
            conf = min(conf, 0.35)

        ok = is_cmd and conf >= CONF_THRESHOLD and tgt is not None
        return CommandParseResponse(
            is_command=ok,
            source_lang=src if ok else None,
            target_lang=tgt if ok else None,
            confidence=conf,
            provider_used="openai",
        )
    except Exception:
        return None

@router.post("/command_parse", response_model=CommandParseResponse)
async def command_parse(req: CommandParseRequest) -> CommandParseResponse:
    text = (req.text or "").strip()
    if not text:
        return CommandParseResponse(is_command=False, confidence=0.0, provider_used="none")

    ui_lang = (req.ui_lang or "").strip().lower() or None

    # ✅ 0) Önce local hızlı yakalama (Türkçe/İngilizce komutlar burada cuk oturur)
    local = _quick_parse_local(text)
    if local:
        return local

    # ✅ 1) Auto: Gemini first
    g = await _call_gemini_parse(text, ui_lang)
    if g and g.is_command:
        return g

    # ✅ 2) OpenAI fallback
    o = await _call_openai_parse(text, ui_lang)
    if o and o.is_command:
        return o

    return CommandParseResponse(is_command=False, confidence=0.0, provider_used=(o.provider_used if o else "none"))
