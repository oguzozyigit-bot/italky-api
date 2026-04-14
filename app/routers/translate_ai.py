from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional, Tuple

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


def canonical(code: str) -> str:
    return str(code or "").strip().lower().split("-")[0]


def normalize_text(text: str) -> str:
    return str(text or "").strip()


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

    words = re.findall(r"\S+", s)
    word_count = len(words)
    char_count = len(s)

    if char_count <= 60 and word_count <= 8:
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

    if style in {"warm", "social"} and char_count >= 50:
        return True

    if any(x in low for x in emotional_hits):
        return True

    if any(x in low for x in idiom_hits):
        return True

    if any(x in low for x in formal_hits):
        return True

    if char_count >= 180:
        return True

    if word_count >= 20:
        return True

    if "!" in s or "?" in s:
        if char_count >= 70:
            return True

    return False


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
    return translated.strip()


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
    return str(translated).strip()


def openai_cultural_translate(
    text: str,
    source: str,
    target: str,
    tone: str = "neutral",
    style: str = "balanced"
) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing")

    tone = canonical_tone(tone)
    style = canonical_style(style)
    register_hint = detect_register_hint(text)

    tone_map = {
        "neutral": "Keep the tone natural, clear, smooth and human.",
        "happy": "Keep the tone warm, friendly, positive and lively.",
        "angry": "Keep the emotional intensity, but make it sound natural, socially appropriate and human.",
        "sad": "Keep the tone soft, sincere, empathetic and emotionally gentle.",
        "excited": "Keep the tone energetic, vivid, enthusiastic and natural."
    }

    style_map = {
        "balanced": "Use balanced, natural, everyday phrasing.",
        "warm": "Prefer warmer, softer, more human phrasing when possible.",
        "clear": "Prefer simpler, clearer, easier-to-understand phrasing.",
        "social": "Prefer slightly more conversational spoken-language phrasing."
    }

    register_map = {
        "formal": "If the source sounds formal, keep it respectful but do not make it stiff or robotic.",
        "casual": "If the source sounds casual, keep it relaxed, natural and conversational.",
        "neutral": "Prefer natural daily phrasing over overly formal or rigid wording."
    }

    tone_rule = tone_map.get(tone, tone_map["neutral"])
    style_rule = style_map.get(style, style_map["balanced"])
    register_rule = register_map.get(register_hint, register_map["neutral"])

    prompt = f"""
You are a highly natural conversational translator.

Translate the following text from "{source}" to "{target}".

Core rules:
- Preserve the exact meaning.
- Do not sound robotic, stiff, or overly literal.
- Make the result feel like something a real person would naturally say.
- Keep the speaker's emotional tone.
- Keep the social intent and politeness level.
- Prefer fluent, human, culturally appropriate phrasing.
- If a sentence sounds too formal in the target language, soften it slightly into natural spoken language without losing meaning.
- If the source is already casual, keep it casual and natural.
- Do not add extra explanation.
- Do not add quotation marks.
- Do not mention translation systems, AI tools, model names, brands, or technology.
- Return only the translated text.

Tone guidance:
{tone_rule}

Style guidance:
{style_rule}

Register guidance:
{register_rule}

Text:
{text}
""".strip()

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
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

    out = "".join(chunks).strip()
    if not out:
        out = str(data.get("output_text") or data.get("text") or "").strip()

    if not out:
        raise RuntimeError("OpenAI empty text")

    return out


def gemini_cultural_translate(
    text: str,
    source: str,
    target: str,
    tone: str = "neutral",
    style: str = "balanced"
) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")

    tone = canonical_tone(tone)
    style = canonical_style(style)
    register_hint = detect_register_hint(text)

    tone_map = {
        "neutral": "Keep the tone natural, clear, smooth and human.",
        "happy": "Keep the tone warm, friendly, positive and lively.",
        "angry": "Keep the emotional intensity, but make it sound natural, socially appropriate and human.",
        "sad": "Keep the tone soft, sincere, empathetic and emotionally gentle.",
        "excited": "Keep the tone energetic, vivid, enthusiastic and natural."
    }

    style_map = {
        "balanced": "Use balanced, natural, everyday phrasing.",
        "warm": "Prefer warmer, softer, more human phrasing when possible.",
        "clear": "Prefer simpler, clearer, easier-to-understand phrasing.",
        "social": "Prefer slightly more conversational spoken-language phrasing."
    }

    register_map = {
        "formal": "If the source sounds formal, keep it respectful but do not make it stiff or robotic.",
        "casual": "If the source sounds casual, keep it relaxed, natural and conversational.",
        "neutral": "Prefer natural daily phrasing over overly formal or rigid wording."
    }

    tone_rule = tone_map.get(tone, tone_map["neutral"])
    style_rule = style_map.get(style, style_map["balanced"])
    register_rule = register_map.get(register_hint, register_map["neutral"])

    prompt = f"""
You are a highly natural conversational translator.

Translate the following text from "{source}" to "{target}".

Core rules:
- Preserve the exact meaning.
- Do not sound robotic, stiff, or overly literal.
- Make the result feel like something a real person would naturally say.
- Keep the speaker's emotional tone.
- Keep the social intent and politeness level.
- Prefer fluent, human, culturally appropriate phrasing.
- If a sentence sounds too formal in the target language, soften it slightly into natural spoken language without losing meaning.
- If the source is already casual, keep it casual and natural.
- Do not add extra explanation.
- Do not add quotation marks.
- Do not mention translation systems, AI tools, model names, brands, or technology.
- Return only the translated text.

Tone guidance:
{tone_rule}

Style guidance:
{style_rule}

Register guidance:
{register_rule}

Text:
{text}
""".strip()

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.42,
            "topP": 0.9,
            "maxOutputTokens": 512
        }
    }

    r = requests.post(url, json=payload, timeout=16)
    r.raise_for_status()
    data = r.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini empty candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    out = "".join(str(p.get("text", "")) for p in parts).strip()

    if not out:
        raise RuntimeError("Gemini empty text")

    return out


def fast_translate_fallback(text: str, source: str, target: str) -> str:
    try:
        print("[translate_ai] trying google free")
        translated = google_translate_free(text, source, target)
        if translated:
            return translated
    except Exception as e1:
        print("[translate_ai] google_free failed:", e1)

    print("[translate_ai] trying google official fallback")
    translated = google_translate_official(text, source, target)
    return translated


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


def _precheck_text_charge(user_id: str, char_count: int) -> Dict[str, Any]:
    summary = _get_wallet_summary(user_id)

    tokens = int(summary.get("tokens") or 0)
    text_bucket = int(summary.get("text_bucket") or 0)

    total = text_bucket + max(0, int(char_count))
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


def _try_gemini_then_openai(
    text: str,
    source: str,
    target: str,
    tone: str,
    style: str
) -> Tuple[str, str]:
    try:
        print("[translate_ai] trying gemini cultural")
        translated = gemini_cultural_translate(text, source, target, tone, style)
        if translated:
            return translated, "gemini"
    except Exception as e1:
        print("[translate_ai] gemini failed:", e1)

    try:
        print("[translate_ai] trying openai cultural fallback")
        translated = openai_cultural_translate(text, source, target, tone, style)
        if translated:
            return translated, "openai"
    except Exception as e2:
        print("[translate_ai] openai failed:", e2)

    raise RuntimeError("ai_translate_failed")


@router.get("/translate_ai/health")
def translate_ai_health():
    return {"ok": True, "service": "translate_ai"}


@router.post("/translate_ai")
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

    print("[translate_ai] request:", {
        "text": text,
        "source": source,
        "target": target,
        "mode": mode,
        "tone": tone,
        "style": style,
    })

    if not text:
        return {"ok": False, "error": "empty_text"}

    if not source or not target:
        return {"ok": False, "error": "missing_lang"}

    if source == target:
        return {
            "ok": True,
            "translated": text,
            "provider": "none",
            "ai_used": False,
            "charged": False,
        }

    if mode == "normal":
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
            print("[translate_ai] normal failed:", e)

        return {"ok": False, "error": "normal_translate_failed"}

    if mode == "cultural":
        if not should_use_ai_for_cultural(text, tone, style):
            try:
                print("[translate_ai] cultural fast path -> google")
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
                print("[translate_ai] cultural fast path failed:", e0)

        jwt_token = _get_bearer(authorization)
        user = _get_user_from_jwt(jwt_token)
        user_id = user["id"]

        char_count = len(text)
        precheck = _precheck_text_charge(user_id, char_count)

        if not precheck["can_afford"]:
            return {
                "ok": False,
                "error": "insufficient_tokens",
                "mode": "cultural",
                "usage_kind": "text",
                "chars_used": char_count,
                "jetons_needed": precheck["jetons_needed"],
                "tokens_before": precheck["tokens"],
                "text_bucket_before": precheck["text_bucket"],
            }

        try:
            translated, provider = _try_gemini_then_openai(text, source, target, tone, style)
        except Exception:
            try:
                print("[translate_ai] trying google official fallback")
                translated = google_translate_official(text, source, target)
                if translated:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google_official",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": char_count,
                    }
            except Exception as e3:
                print("[translate_ai] google_official fallback failed:", e3)

            try:
                print("[translate_ai] trying google free fallback")
                translated = google_translate_free(text, source, target)
                if translated:
                    return {
                        "ok": True,
                        "translated": translated,
                        "provider": "google_free",
                        "ai_used": False,
                        "charged": False,
                        "chars_used": char_count,
                    }
            except Exception as e4:
                print("[translate_ai] google_free fallback failed:", e4)

            return {"ok": False, "error": "cultural_translate_failed"}

        charge = _charge_text_usage(
            user_id=user_id,
            chars_used=char_count,
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
                "chars_used": char_count,
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
            "chars_used": char_count,
            "jetons_spent": int(charge.get("jetons_spent") or 0),
            "tokens_before": int(charge.get("tokens_before") or precheck["tokens"]),
            "tokens_after": int(charge.get("tokens_after") or precheck["tokens"]),
            "text_bucket": int(charge.get("text_bucket") or 0),
            "voice_bucket": int(charge.get("voice_bucket") or 0),
        }

    return {"ok": False, "error": "invalid_mode"}
