from __future__ import annotations

import os
import logging
import base64
import hashlib
from typing import Optional, Dict

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts").strip()

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()
CARTESIA_MODEL_ID = "sonic-3"

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"

TTS_CACHE_BUCKET = os.getenv("TTS_CACHE_BUCKET", "tts-cache").strip()

# Öncelikli marka isimleri
# Özel sesler OpenAI yerleşik seslerine map edilir.
PRESET_VOICE_CONFIG: Dict[str, Dict[str, str]] = {
    "huma": {
        "label": "Hüma",
        "gender": "female",
        "openai_voice": "nova",
    },
    "selden": {
        "label": "Selden",
        "gender": "female",
        "openai_voice": "sage",
    },
    "jale": {
        "label": "Jale",
        "gender": "female",
        "openai_voice": "coral",
    },
    "aysem": {
        "label": "Ayşem",
        "gender": "female",
        "openai_voice": "alloy",
    },
    "handan": {
        "label": "Handan",
        "gender": "female",
        "openai_voice": "shimmer",
    },
    "nilay": {
        "label": "Nilay",
        "gender": "female",
        "openai_voice": "marin",
    },
    "ozan": {
        "label": "Ozan",
        "gender": "male",
        "openai_voice": "onyx",
    },
    "noyan": {
        "label": "Noyan",
        "gender": "male",
        "openai_voice": "echo",
    },
    "oguz": {
        "label": "Oğuz",
        "gender": "male",
        "openai_voice": "ash",
    },
    "yavuz": {
        "label": "Yavuz",
        "gender": "male",
        "openai_voice": "fable",
    },
    "yilmaz": {
        "label": "Yılmaz",
        "gender": "male",
        "openai_voice": "cedar",
    },
}


def canon_lang(code: str) -> str:
    return (code or "tr").strip().lower().replace("_", "-")


def lang_base(code: str) -> str:
    return canon_lang(code).split("-")[0]


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


def tone_instruction(tone: str) -> str:
    t = canon_tone(tone)
    if t == "happy":
        return "Speak in a warm, cheerful, lively tone."
    if t == "angry":
        return "Speak with strong intensity, but keep it natural and socially appropriate."
    if t == "sad":
        return "Speak in a soft, gentle, slightly emotional tone."
    if t == "excited":
        return "Speak in an energetic, enthusiastic, vivid tone."
    return "Speak naturally, clearly and smoothly."


def is_preset_voice(voice: str) -> bool:
    return voice in PRESET_VOICE_CONFIG


def preset_gender(voice: str) -> str:
    cfg = PRESET_VOICE_CONFIG.get(voice) or {}
    return str(cfg.get("gender") or "female").strip().lower()


def preset_openai_voice(voice: str) -> str:
    cfg = PRESET_VOICE_CONFIG.get(voice) or {}
    return str(cfg.get("openai_voice") or "").strip()


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
        payload["generation_config"] = {
            "instruction": tone_instruction(tone)
        }

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


async def openai_tts(text: str, voice_name: str, tone: str):
    if not OPENAI_API_KEY or not voice_name:
        return None

    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": voice_name,
        "input": text,
        "format": "mp3",
        "instructions": tone_instruction(tone),
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                OPENAI_TTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )

        if r.status_code >= 400:
            logger.warning("openai_tts failed: %s", r.text[:500])
            return None

        return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        logger.warning("openai_tts exception: %s", e)
        return None


async def openai_tts_with_bytes(text: str, voice_name: str, tone: str):
    if not OPENAI_API_KEY or not voice_name:
        return None, None

    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": voice_name,
        "input": text,
        "format": "mp3",
        "instructions": tone_instruction(tone),
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                OPENAI_TTS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )

        if r.status_code >= 400:
            logger.warning("openai_tts_with_bytes failed: %s", r.text[:500])
            return None, None

        if not r.content:
            return None, None

        b64 = base64.b64encode(r.content).decode("utf-8")
        return b64, r.content
    except Exception as e:
        logger.warning("openai_tts_with_bytes exception: %s", e)
        return None, None


def build_clone_preview_text(profile: Optional[dict]) -> str:
    name = str((profile or {}).get("full_name") or "").strip()
    first_name = name.split(" ")[0] if name else "Arkadaşım"

    return (
        f"Merhaba, ben {first_name}. "
        f"italkyAI ile çevirileri kendi sesimle ve duygularımı da yansıtarak yapabiliyorum. "
        f"Böylece konuşmalarım daha doğal, daha sıcak ve bana daha yakın oluyor."
    )


def _preview_cache_key(voice: str, lang: str, text: str) -> str:
    raw = f"{voice}|{lang_base(lang)}|{text}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return f"preset-previews/{voice}/{lang_base(lang)}/{digest}.mp3"


async def _storage_download_base64(path: str) -> Optional[str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        return None

    url = f"{SUPABASE_URL}/storage/v1/object/{TTS_CACHE_BUCKET}/{path}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                url,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                },
            )

        if r.status_code != 200 or not r.content:
            return None

        return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        logger.warning("storage_download_base64 failed: %s", e)
        return None


async def _storage_upload_mp3(path: str, audio_bytes: bytes) -> bool:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE or not audio_bytes:
        return False

    url = f"{SUPABASE_URL}/storage/v1/object/{TTS_CACHE_BUCKET}/{path}"

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                url,
                content=audio_bytes,
                headers={
                    "apikey": SUPABASE_SERVICE_ROLE,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                    "Content-Type": "audio/mpeg",
                    "x-upsert": "true",
                },
            )

        if r.status_code not in (200, 201):
            logger.warning("storage_upload_mp3 failed: %s", r.text[:500])
            return False

        return True
    except Exception as e:
        logger.warning("storage_upload_mp3 exception: %s", e)
        return False


@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest):
    text = (req.text or "").strip()
    tone = canon_tone(req.tone)
    voice = canon_voice(req.voice)
    module = str(req.module or "facetoface").strip().lower()

    profile = await get_user_profile(req.user_id)
    voice_ready = bool(profile and profile.get("tts_voice_ready"))
    voice_id = str((profile or {}).get("tts_voice_id") or "").strip()

    # clone preview'da metin boşsa adla örnek üret
    if module == "clone_preview" and not text:
        text = build_clone_preview_text(profile)

    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    # 1) Özel sesler -> OpenAI
    if is_preset_voice(voice):
        openai_voice = preset_openai_voice(voice)

        # preview ise cache kullan
        if module == "voice_preset_preview":
            cache_path = _preview_cache_key(voice, req.lang, text)

            cached_audio = await _storage_download_base64(cache_path)
            if cached_audio:
                return TTSResponse(
                    ok=True,
                    audio_base64=cached_audio,
                    provider_used=f"cache-preset-{voice}"
                )

            audio_b64, audio_bytes = await openai_tts_with_bytes(text, openai_voice, tone)
            if audio_b64:
                if audio_bytes:
                    await _storage_upload_mp3(cache_path, audio_bytes)

                return TTSResponse(
                    ok=True,
                    audio_base64=audio_b64,
                    provider_used=f"openai-preset-{voice}"
                )

        # normal preset kullanım
        audio = await openai_tts(text, openai_voice, tone)
        if audio:
            return TTSResponse(
                ok=True,
                audio_base64=audio,
                provider_used=f"openai-preset-{voice}"
            )

    # 2) Kendi Sesim / clone -> Cartesia
    if voice == "clone" and voice_ready and voice_id:
        audio = await cartesia_tts(text, req.lang, voice_id, tone, True)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="clone-tone")

        audio = await cartesia_tts(text, req.lang, voice_id, tone, False)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="clone")

    # 3) Generic male/female -> OpenAI
    if voice == "male":
        audio = await openai_tts(text, "onyx", tone)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="openai-male")

    if voice == "female":
        audio = await openai_tts(text, "nova", tone)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="openai-female")

    # 4) auto -> önce clone varsa Cartesia, yoksa frontend cihaz sesi kullansın
    if voice == "auto" and voice_ready and voice_id:
        audio = await cartesia_tts(text, req.lang, voice_id, tone, True)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="auto-clone-tone")

        audio = await cartesia_tts(text, req.lang, voice_id, tone, False)
        if audio:
            return TTSResponse(ok=True, audio_base64=audio, provider_used="auto-clone")

    # 5) Ücretsiz / cihaz TTS fallback için backend boş dönsün
    return TTSResponse(ok=False, error="TTS_UNAVAILABLE")
