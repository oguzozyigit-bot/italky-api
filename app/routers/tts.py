from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

# =========================
# ENV
# =========================
GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

# =========================
# LANG MAP
# =========================
LANG_BCP47 = {
    "tr": "tr-TR",
    "en": "en-US",
    "en-gb": "en-GB",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "ru": "ru-RU",
    "pt": "pt-PT",
    "pt-br": "pt-BR",
    "nl": "nl-NL",
    "sv": "sv-SE",
    "no": "nb-NO",
    "nb": "nb-NO",
    "da": "da-DK",
    "fi": "fi-FI",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "sk": "sk-SK",
    "hu": "hu-HU",
    "ro": "ro-RO",
    "bg": "bg-BG",
    "el": "el-GR",
    "uk": "uk-UA",
    "ar": "ar-XA",
    "he": "he-IL",
    "fa": "fa-IR",
    "ur": "ur-PK",
    "hi": "hi-IN",
    "bn": "bn-BD",
    "id": "id-ID",
    "ms": "ms-MY",
    "vi": "vi-VN",
    "th": "th-TH",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "ka": "ka-GE",
}

# =========================
# LANGUAGE -> GOOGLE VOICE MAP
# erkek / kadın / otomatik
# =========================
GOOGLE_VOICE_MAP = {
    "tr": {
        "male": "tr-TR-Standard-B",
        "female": "tr-TR-Standard-A",
    },
    "en": {
        "male": "en-US-Standard-D",
        "female": "en-US-Standard-F",
    },
    "de": {
        "male": "de-DE-Standard-B",
        "female": "de-DE-Standard-A",
    },
    "fr": {
        "male": "fr-FR-Standard-B",
        "female": "fr-FR-Standard-A",
    },
    "it": {
        "male": "it-IT-Standard-C",
        "female": "it-IT-Standard-A",
    },
    "es": {
        "male": "es-ES-Standard-B",
        "female": "es-ES-Standard-A",
    },
    "ru": {
        "male": "ru-RU-Standard-B",
        "female": "ru-RU-Standard-A",
    },
}

def canon_lang(code: str) -> str:
    c = (code or "tr").strip().lower().replace("_", "-")
    if c == "pt-br":
        return "pt-br"
    if "-" in c and len(c) >= 4:
        base = c.split("-")[0]
        region = c.split("-")[1].upper()
        return f"{base}-{region}"
    return c

def lang_to_bcp47(code: str) -> str:
    c = canon_lang(code)
    if "-" in c and len(c.split("-")[1]) == 2:
        return c
    return LANG_BCP47.get(c, "en-US")

def lang_base(code: str) -> str:
    return canon_lang(code).split("-")[0]

# =========================
# SCHEMAS
# =========================
class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr"
    voice: Optional[str] = None     # auto / male / female / direct voice-name
    speaking_rate: float = 1.0
    pitch: float = 0.0
    user_id: Optional[str] = None

class TTSResponse(FlexibleModel):
    ok: bool
    audio_base64: Optional[str] = None
    provider_used: Optional[str] = None
    error: Optional[str] = None

# =========================
# PROFILE LOOKUP
# =========================
async def get_user_tts_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id:
      return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
      return None

    url = (
        f"{SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}"
        f"&select=id,tts_voice_provider,tts_voice_id,tts_voice_ready"
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                url,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                },
            )

        if r.status_code >= 400:
            logger.error("TTS_PROFILE_FETCH_FAIL %s %s", r.status_code, r.text[:400])
            return None

        arr = r.json()
        if not arr:
            return None

        row = arr[0] or {}
        if row.get("tts_voice_ready") and row.get("tts_voice_id"):
            return row

        return None

    except Exception as e:
        logger.exception("TTS_PROFILE_FETCH_EXCEPTION: %s", e)
        return None

# =========================
# CUSTOM VOICE HOOK
# Şimdilik gerçek clone provider yok
# Hazırsa işaretleyelim, sonra provider bağlanacak
# =========================
async def custom_voice_tts_if_ready(
    text: str,
    lang: str,
    custom_profile: Optional[dict],
) -> Optional[str]:
    if not custom_profile:
        return None

    provider = str(custom_profile.get("tts_voice_provider") or "").strip().lower()
    voice_id = str(custom_profile.get("tts_voice_id") or "").strip()

    if not provider or not voice_id:
        return None

    # ŞİMDİLİK gerçek custom TTS yok.
    # İleride ElevenLabs / Cartesia / başka provider bağlanınca buraya koyacağız.
    logger.info("CUSTOM_VOICE_READY provider=%s voice_id=%s lang=%s", provider, voice_id, lang)
    return None

# =========================
# GOOGLE VOICE CHOOSER
# =========================
def pick_google_voice_and_gender(lang: str, voice: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    returns: (voice_name, ssmlGender)
    voice input can be:
      - male
      - female
      - auto
      - direct google voice name
      - None
    """
    raw_voice = (voice or "auto").strip()
    low_voice = raw_voice.lower()
    base = lang_base(lang)

    # Kullanıcı direkt voice adı gönderdiyse
    if raw_voice and raw_voice not in ("auto", "male", "female"):
        return raw_voice, None

    if low_voice == "male":
        name = GOOGLE_VOICE_MAP.get(base, {}).get("male")
        return name, "MALE"

    if low_voice == "female":
        name = GOOGLE_VOICE_MAP.get(base, {}).get("female")
        return name, "FEMALE"

    # auto
    return None, None

# =========================
# GOOGLE TTS
# =========================
async def google_tts(
    text: str,
    lang: str,
    voice: Optional[str],
    speaking_rate: float,
    pitch: float
) -> Optional[str]:
    if not GOOGLE_API_KEY:
        logger.warning("TTS_GOOGLE: GOOGLE_API_KEY missing -> skip")
        return None

    bcp47 = lang_to_bcp47(lang)
    voice_name, gender = pick_google_voice_and_gender(lang, voice)

    voice_cfg: Dict[str, Any] = {
        "languageCode": bcp47
    }

    if voice_name:
        voice_cfg["name"] = voice_name
    elif gender:
        voice_cfg["ssmlGender"] = gender

    payload: Dict[str, Any] = {
        "input": {"text": text},
        "voice": voice_cfg,
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": float(speaking_rate or 1.0),
            "pitch": float(pitch or 0.0),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                f"{GOOGLE_TTS_URL}?key={GOOGLE_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if r.status_code >= 400:
            logger.error("TTS_FAIL_GOOGLE %s %s", r.status_code, r.text[:700])
            return None

        data = r.json()
        audio_b64 = (data.get("audioContent") or "").strip()
        return audio_b64 or None

    except Exception as e:
        logger.exception("TTS_GOOGLE_EXCEPTION: %s", e)
        return None

# =========================
# ROUTE
# =========================
@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest) -> TTSResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    # 1) custom voice profile var mı?
    custom_profile = await get_user_tts_profile(req.user_id)

    # 2) ileride gerçek custom voice buradan üretilecek
    custom_audio = await custom_voice_tts_if_ready(text, req.lang, custom_profile)
    if custom_audio:
        return TTSResponse(
            ok=True,
            audio_base64=custom_audio,
            provider_used="custom"
        )

    # 3) Google TTS fallback
    g = await google_tts(
        text=text,
        lang=req.lang,
        voice=req.voice,
        speaking_rate=req.speaking_rate,
        pitch=req.pitch
    )

    if g:
        if custom_profile:
            return TTSResponse(
                ok=True,
                audio_base64=g,
                provider_used="custom-ready+google"
            )
        return TTSResponse(
            ok=True,
            audio_base64=g,
            provider_used="google"
        )

    # 4) hiçbiri olmadı
    return TTSResponse(
        ok=False,
        provider_used="none",
        error="TTS_UNAVAILABLE"
    )
