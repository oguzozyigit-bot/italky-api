from __future__ import annotations

import os
import logging
import base64
from typing import Optional, Dict, Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

GOOGLE_API_KEY = (os.getenv("GOOGLE_API_KEY", "") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()

GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_MODEL_ID = "sonic-2"

LANG_BCP47 = {
    "tr": "tr-TR",
    "en": "en-US",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
    "es": "es-ES",
    "ru": "ru-RU",
    "el": "el-GR",
    "ka": "ka-GE",
}

GOOGLE_VOICE_MAP = {
    "tr": {"male": "tr-TR-Standard-B", "female": "tr-TR-Standard-A"},
    "en": {"male": "en-US-Standard-D", "female": "en-US-Standard-F"},
    "de": {"male": "de-DE-Standard-B", "female": "de-DE-Standard-A"},
    "fr": {"male": "fr-FR-Standard-B", "female": "fr-FR-Standard-A"},
    "it": {"male": "it-IT-Standard-C", "female": "it-IT-Standard-A"},
    "es": {"male": "es-ES-Standard-B", "female": "es-ES-Standard-A"},
}


def canon_lang(code: str) -> str:
    return (code or "tr").strip().lower().replace("_", "-")


def lang_base(code: str) -> str:
    return canon_lang(code).split("-")[0]


def lang_to_bcp47(code: str) -> str:
    c = lang_base(code)
    return LANG_BCP47.get(c, "en-US")


def canon_voice(value: Optional[str]) -> str:
    v = (value or "auto").strip().lower()
    if v in ("own", "my"):
        return "clone"
    if v in ("female", "male", "clone"):
        return v
    return "auto"


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr"
    voice: Optional[str] = None   # auto / male / female / clone
    speaking_rate: float = 1.0
    pitch: float = 0.0
    user_id: Optional[str] = None
    module: str = "facetoface"    # facetoface / interpreter / walkie / chat


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
        f"&select=id,plan,tts_voice_provider,tts_voice_id,tts_voice_ready"
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
        return arr[0] if arr else None
    except Exception as e:
        logger.exception("TTS_PROFILE_FETCH_EXCEPTION: %s", e)
        return None


def pick_google_voice(lang: str, voice: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    base = lang_base(lang)
    v = canon_voice(voice)

    if v == "male":
        return GOOGLE_VOICE_MAP.get(base, {}).get("male"), "MALE"

    if v == "female":
        return GOOGLE_VOICE_MAP.get(base, {}).get("female"), "FEMALE"

    return None, None


async def google_tts(
    text: str,
    lang: str,
    voice: Optional[str],
    speaking_rate: float,
    pitch: float
) -> Optional[str]:
    if not GOOGLE_API_KEY:
        logger.warning("TTS_GOOGLE: GOOGLE_API_KEY missing")
        return None

    bcp47 = lang_to_bcp47(lang)
    voice_name, gender = pick_google_voice(lang, voice)

    voice_cfg: Dict[str, Any] = {"languageCode": bcp47}
    if voice_name:
        voice_cfg["name"] = voice_name
    elif gender:
        voice_cfg["ssmlGender"] = gender

    payload = {
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
        return (data.get("audioContent") or "").strip() or None
    except Exception as e:
        logger.exception("TTS_GOOGLE_EXCEPTION: %s", e)
        return None


async def cartesia_tts(text: str, lang: str, voice_id: str) -> Optional[str]:
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

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
            logger.error("TTS_FAIL_CARTESIA %s %s", r.status_code, r.text[:500])
            return None
        if not r.content:
            return None
        return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        logger.exception("TTS_CARTESIA_EXCEPTION: %s", e)
        return None


@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest) -> TTSResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    module = str(req.module or "facetoface").lower().strip()
    voice = canon_voice(req.voice)

    profile = await get_user_profile(req.user_id)

    voice_ready = bool((profile or {}).get("tts_voice_ready"))
    voice_id = str((profile or {}).get("tts_voice_id") or "").strip()

    # 1) Kullanıcı özellikle "Benim Sesim" seçtiyse önce custom sesi dene
    if voice == "clone" and voice_ready and voice_id:
        audio = await cartesia_tts(
            text=text,
            lang=req.lang,
            voice_id=voice_id
        )
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="cartesia-clone")

    # 2) FaceToFace / Interpreter için hazır özel ses varsa otomatik de olsa custom öncelikli olabilir
    if module in ("facetoface", "interpreter") and voice_ready and voice_id and voice in ("auto", "clone"):
        audio = await cartesia_tts(
            text=text,
            lang=req.lang,
            voice_id=voice_id
        )
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="cartesia")

    # 3) Google fallback
    g = await google_tts(
        text=text,
        lang=req.lang,
        voice=voice,
        speaking_rate=req.speaking_rate,
        pitch=req.pitch
    )
    if g:
        return TTSResponse(ok=True, audio_base64=g, provider_used="google")

    return TTSResponse(ok=False, provider_used="none", error="TTS_UNAVAILABLE")
