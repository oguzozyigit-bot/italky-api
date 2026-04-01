from __future__ import annotations

import os
import logging
import base64
from typing import Optional, Dict
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
import httpx

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()
CARTESIA_MODEL_ID = "sonic-3"

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"

# Hazır özel sesler
# Not:
# - Buradaki id alanları ENV üzerinden okunur.
# - ENV boşsa ilgili preset Google fallback ile kadın/erkek sesi kullanır.
PRESET_VOICE_CONFIG: Dict[str, Dict[str, str]] = {
    "huma": {
        "label": "Hüma",
        "gender": "female",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_HUMA_ID", "").strip(),
    },
    "umay": {
        "label": "Umay",
        "gender": "female",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_UMAY_ID", "").strip(),
    },
    "jale": {
        "label": "Jale",
        "gender": "female",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_JALE_ID", "").strip(),
    },
    "mina": {
        "label": "Mina",
        "gender": "female",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_MINA_ID", "").strip(),
    },
    "beren": {
        "label": "Beren",
        "gender": "female",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_BEREN_ID", "").strip(),
    },
    "ozan": {
        "label": "Ozan",
        "gender": "male",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_OZAN_ID", "").strip(),
    },
    "kaan": {
        "label": "Kaan",
        "gender": "male",
        "cartesia_voice_id": os.getenv("PRESET_VOICE_KAAN_ID", "").strip(),
    },
}

GOOGLE_LANG_MAP = {
    "tr": "tr-TR",
    "en": "en-US",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "ru": "ru-RU",
    "el": "el-GR",
    "az": "az-AZ",
    "ka": "ka-GE",
}

GOOGLE_FEMALE_NAME = {
    "tr": "tr-TR-Standard-A",
    "en": "en-US-Standard-C",
    "de": "de-DE-Standard-A",
    "fr": "fr-FR-Standard-A",
    "it": "it-IT-Standard-A",
    "es": "es-ES-Standard-A",
}

GOOGLE_MALE_NAME = {
    "tr": "tr-TR-Standard-B",
    "en": "en-US-Standard-B",
    "de": "de-DE-Standard-B",
    "fr": "fr-FR-Standard-B",
    "it": "it-IT-Standard-B",
    "es": "es-ES-Standard-B",
}


def canon_lang(code: str) -> str:
    return (code or "tr").strip().lower().replace("_", "-")


def lang_base(code: str) -> str:
    return canon_lang(code).split("-")[0]


def google_lang_code(code: str) -> str:
    base = lang_base(code)
    return GOOGLE_LANG_MAP.get(base, "en-US")


def canon_voice(value: Optional[str]) -> str:
    v = (value or "auto").strip().lower()

    if v in ("own", "my"):
        return "clone"

    if v in ("female", "male", "clone", "auto", "preset"):
        return v

    if v in PRESET_VOICE_CONFIG:
        return v

    return "auto"


def canon_tone(value: Optional[str]) -> str:
    v = (value or "neutral").strip().lower()
    if v in ("happy", "angry", "sad", "excited", "neutral"):
        return v
    return "neutral"


def tone_config(tone: str):
    t = canon_tone(tone)

    if t == "happy":
        return {"speed": 1.15, "volume": 1.1, "emotion": "positivity:high"}

    if t == "angry":
        return {"speed": 1.25, "volume": 1.15, "emotion": "anger:high"}

    if t == "sad":
        return {"speed": 0.85, "volume": 0.9, "emotion": "sadness:high"}

    if t == "excited":
        return {"speed": 1.30, "volume": 1.15, "emotion": "excitement:high"}

    return {"speed": 1.0, "volume": 1.0, "emotion": "neutral"}


def is_preset_voice(voice: str) -> bool:
    return voice in PRESET_VOICE_CONFIG


def preset_gender(voice: str) -> str:
    cfg = PRESET_VOICE_CONFIG.get(voice) or {}
    return str(cfg.get("gender") or "female").strip().lower()


def preset_cartesia_voice_id(voice: str) -> str:
    cfg = PRESET_VOICE_CONFIG.get(voice) or {}
    return str(cfg.get("cartesia_voice_id") or "").strip()


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr"
    voice: Optional[str] = None
    tone: Optional[str] = "neutral"
    user_id: Optional[str] = None
    module: str = "facetoface"


class TTSResponse(FlexibleModel):
    ok: bool
    audio_base64: Optional[str] = None
    provider_used: Optional[str] = None
    error: Optional[str] = None


async def get_user_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        return None

    url = (
        f"{SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}"
        f"&select=id,full_name,tts_voice_id,tts_voice_ready,tts_voice,tts_voice_preference"
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
            },
        )
        if r.status_code != 200:
            return None

        data = r.json()
        return data[0] if data else None


async def cartesia_tts(text: str, lang: str, voice_id: str, tone: str, use_tone: bool = True):
    if not CARTESIA_API_KEY or not voice_id:
        return None

    payload = {
        "model_id": CARTESIA_MODEL_ID,
        "transcript": text,
        "voice": {
            "mode": "id",
            "id": voice_id,
        },
        "output_format": {
            "container": "mp3",
            "bit_rate": 128000,
            "sample_rate": 44100,
        },
        "language": lang_base(lang),
    }

    if use_tone:
        payload["generation_config"] = tone_config(tone)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                CARTESIA_TTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {CARTESIA_API_KEY}",
                    "Cartesia-Version": CARTESIA_VERSION,
                    "Content-Type": "application/json",
                },
            )

        if r.status_code >= 400:
            logger.warning("cartesia_tts failed: %s", r.text[:500])
            return None

        return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        logger.warning("cartesia_tts exception: %s", e)
        return None


async def google_tts(text: str, lang: str, voice_gender: str = "female"):
    if not GOOGLE_API_KEY:
        return None

    base = lang_base(lang)
    language_code = google_lang_code(lang)

    voice_name = (
        GOOGLE_MALE_NAME.get(base) if voice_gender == "male"
        else GOOGLE_FEMALE_NAME.get(base)
    )

    payload = {
        "input": {"text": text},
        "voice": {
            "languageCode": language_code,
            "ssmlGender": "MALE" if voice_gender == "male" else "FEMALE",
        },
        "audioConfig": {"audioEncoding": "MP3"},
    }

    if voice_name:
        payload["voice"]["name"] = voice_name

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{GOOGLE_TTS_URL}?key={GOOGLE_API_KEY}",
                json=payload,
            )

        if r.status_code != 200:
            logger.warning("google_tts failed: %s", r.text[:500])
            return None

        return r.json().get("audioContent")
    except Exception as e:
        logger.warning("google_tts exception: %s", e)
        return None


def build_clone_preview_text(profile: Optional[dict]) -> str:
    name = str((profile or {}).get("full_name") or "").strip()
    first_name = name.split(" ")[0] if name else "Arkadaşım"

    return (
        f"Merhaba, ben {first_name}. "
        f"italkyAI ile çevirileri kendi sesimle ve duygularımı da yansıtarak yapabiliyorum. "
        f"Böylece konuşmalarım daha doğal, daha sıcak ve bana daha yakın oluyor."
    )


@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    tone = canon_tone(req.tone)
    voice = canon_voice(req.voice)
    module = str(req.module or "facetoface").strip().lower()

    profile = await get_user_profile(req.user_id)
    voice_ready = bool(profile and profile.get("tts_voice_ready"))
    voice_id = str((profile or {}).get("tts_voice_id") or "").strip()

    # Kendi sesimi dinle butonunda metin boş gelirse profile adından örnek metin kur
    if module == "clone_preview" and not text:
        text = build_clone_preview_text(profile)

    # 1) Hazır özel sesler
    if is_preset_voice(voice):
        preset_id = preset_cartesia_voice_id(voice)

        if preset_id:
            audio = await cartesia_tts(text, req.lang, preset_id, tone, True)
            if audio:
                return TTSResponse(ok=True, audio_base64=audio, provider_used=f"preset-{voice}-tone")

            audio = await cartesia_tts(text, req.lang, preset_id, tone, False)
            if audio:
                return TTSResponse(ok=True, audio_base64=audio, provider_used=f"preset-{voice}")

        # ENV ile preset id yoksa Google fallback
        audio = await google_tts(text, req.lang, preset_gender(voice))
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used=f"google-preset-{voice}")

    # 2) Kendi clon sesim
    if voice == "clone" and voice_ready and voice_id:
        audio = await cartesia_tts(text, req.lang, voice_id, tone, True)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="clone-tone")

        audio = await cartesia_tts(text, req.lang, voice_id, tone, False)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="clone")

    # 3) Genel kadın / erkek seçimleri
    if voice in ("female", "male"):
        audio = await google_tts(text, req.lang, voice)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used=f"google-{voice}")

    # 4) Auto -> önce profilde seçili clone varsa onu dene
    if voice == "auto" and voice_ready and voice_id:
        audio = await cartesia_tts(text, req.lang, voice_id, tone, True)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="auto-clone-tone")

        audio = await cartesia_tts(text, req.lang, voice_id, tone, False)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="auto-clone")

    # 5) Son fallback Google kadın sesi
    audio = await google_tts(text, req.lang, "female")
    if audio:
        return TTSResponse(ok=True, audio_base64=audio, provider_used="google")

    return TTSResponse(ok=False, error="TTS_UNAVAILABLE")
