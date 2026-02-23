from __future__ import annotations

import json
import os
import re
from typing import Optional, Dict, Any, Tuple

import httpx
from fastapi import APIRouter, HTTPException
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

# Basit alias: bazı modeller pt-br yerine pt yazabilir.
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
}

def _canon_lang(code: str) -> Optional[str]:
    c = (code or "").strip().lower()
    c = c.replace("_", "-")
    c = ALIAS.get(c, c)
    # sadece 2 harfli veya pt-br gibi
    if c in SUPPORTED_LANGS:
        return c
    # bazıları "en-us" gibi gelebilir -> "en"
    if "-" in c:
        base = c.split("-")[0]
        base = ALIAS.get(base, base)
        if base in SUPPORTED_LANGS:
            return base
    return None

class CommandParseRequest(BaseModel):
    text: str = Field(..., min_length=1)
    # UI dili opsiyonel (komut ipuçları için)
    ui_lang: Optional[str] = Field(default=None)

class CommandParseResponse(BaseModel):
    is_command: bool
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    confidence: float = 0.0
    provider_used: str = "none"

def _extract_json_loose(s: str) -> Optional[Dict[str, Any]]:
    """
    Model bazen JSON'u ``` ile veya metinle birlikte döndürebilir.
    İlk JSON objesini yakalamaya çalışır.
    """
    if not s:
        return None
    s = s.strip()

    # ```json ... ```
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    chunk = m.group(0)
    try:
        return json.loads(chunk)
    except Exception:
        return None

async def _call_gemini_parse(text: str, ui_lang: Optional[str]) -> Optional[CommandParseResponse]:
    if not GEMINI_API_KEY:
        return None

    prompt = f"""
You are a command parser for a multilingual translation app.

Task:
- Determine if the user utterance is a "change target language" command.
- If YES:
  - source_lang: the language the user is speaking in (ISO 639-1 code, e.g., "tr", "en", "ka")
  - target_lang: the target language requested (ISO 639-1 code, e.g., "en", "de", "fr", "ka")
  - confidence: 0.0 to 1.0
- If NO:
  - is_command=false, confidence low

Important:
- Users may say it in ANY language (e.g., Turkish, Georgian, Russian).
- Examples of command intent:
  - "Dil değiştir İngilizce"
  - "Translate to German"
  - "Переведи на английский"
  - "Cambiar idioma a francés"
- Not a command if they are just speaking normally.

Output STRICT JSON ONLY:
{{
  "is_command": true/false,
  "source_lang": "xx" or null,
  "target_lang": "yy" or null,
  "confidence": 0.0
}}

Supported language codes:
{sorted(list(SUPPORTED_LANGS))}

UI language hint (may be null): {ui_lang}

Utterance:
{text}
""".strip()

    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ]
    }

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

        # Eğer komut dedi ama diller boşsa -> güven düşür
        if is_cmd and (not src or not tgt):
            conf = min(conf, 0.35)

        return CommandParseResponse(
            is_command=is_cmd and conf >= 0.55,
            source_lang=src if conf >= 0.55 else None,
            target_lang=tgt if conf >= 0.55 else None,
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

Return STRICT JSON ONLY. No markdown.

Schema:
{{
  "is_command": true/false,
  "source_lang": "xx" or null,
  "target_lang": "yy" or null,
  "confidence": 0.0
}}

Rules:
- is_command is TRUE only if user intent is "change target language".
- source_lang: language user is speaking in (ISO 639-1).
- target_lang: requested target (ISO 639-1).
- If uncertain, set is_command=false and low confidence.
- Supported language codes: {sorted(list(SUPPORTED_LANGS))}
- UI language hint: {ui_lang}

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
            # yedek parse
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

        if is_cmd and (not src or not tgt):
            conf = min(conf, 0.35)

        return CommandParseResponse(
            is_command=is_cmd and conf >= 0.55,
            source_lang=src if conf >= 0.55 else None,
            target_lang=tgt if conf >= 0.55 else None,
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

    # ✅ Auto: Gemini first, then OpenAI
    g = await _call_gemini_parse(text, ui_lang)
    if g and g.is_command:
        return g

    o = await _call_openai_parse(text, ui_lang)
    if o:
        return o

    # hiçbir provider parse edemezse: komut değil say
    return CommandParseResponse(is_command=False, confidence=0.0, provider_used="none")
