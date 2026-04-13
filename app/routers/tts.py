from __future__ import annotations

import os
import logging
import base64
import uuid
from typing import Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "").strip()
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-03-01").strip()
CARTESIA_MODEL_ID = "sonic-3"

CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value).strip())
        return True
    except Exception:
        return False


def canon_lang(code: str) -> str:
    return (code or "tr").strip().lower().replace("_", "-")


def lang_base(code: str) -> str:
    return canon_lang(code).split("-")[0]


def canon_voice(value: Optional[str]) -> str:
    v = (value or "auto").strip().lower()

    alias_map = {
        "own": "mine",
        "my": "mine",
        "mine": "mine",
        "kendi": "mine",
        "kendi sesim": "mine",
        "clone": "mine",
        "preset": "auto",
        "auto": "auto",
        "second": "second",
        "memory": "memory",
    }
    return alias_map.get(v, "auto")


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


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TTSRequest(FlexibleModel):
    text: str
    lang: str = "tr"
    voice: Optional[str] = None
    voice_mode: Optional[str] = None
    preset_voice: Optional[str] = None
    selected_voice: Optional[str] = None
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
        f"&select="
        f"id,full_name,"
        f"tts_voice_id,tts_voice_ready,voice_sample_path,"
        f"second_tts_voice_id,second_tts_voice_ready,second_voice_sample_path,"
        f"memory_tts_voice_id,memory_tts_voice_ready,memory_voice_sample_path"
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
            logger.warning("get_user_profile failed: %s", r.text[:400])
            return None

        data = r.json()
        return data[0] if data else None


def resolve_requested_voice(req: TTSRequest) -> str:
    candidates = [
        req.selected_voice,
        req.voice,
        req.preset_voice,
        req.voice_mode,
    ]

    for item in candidates:
        v = canon_voice(item)
        if v in ("mine", "second", "memory", "auto"):
            return v

    return "auto"


def resolve_profile_voice(profile: Optional[dict], requested_voice: str) -> Tuple[str, bool]:
    if not profile:
        return "", False

    if requested_voice == "mine":
        voice_id = str(profile.get("tts_voice_id") or "").strip()
        ready = bool(profile.get("tts_voice_ready")) or bool(profile.get("voice_sample_path"))
        return voice_id, ready

    if requested_voice == "second":
        voice_id = str(profile.get("second_tts_voice_id") or "").strip()
        ready = bool(profile.get("second_tts_voice_ready")) or bool(profile.get("second_voice_sample_path"))
        return voice_id, ready

    if requested_voice == "memory":
        voice_id = str(profile.get("memory_tts_voice_id") or "").strip()
        ready = bool(profile.get("memory_tts_voice_ready")) or bool(profile.get("memory_voice_sample_path"))
        return voice_id, ready

    return "", False


async def cartesia_tts(
    text: str,
    lang: str,
    voice_id: str,
    tone: str,
    use_tone: bool = True,
) -> Optional[str]:
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

        if not r.content:
            logger.warning("cartesia_tts empty content")
            return None

        return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        logger.warning("cartesia_tts exception: %s", e)
        return None


@router.post("/tts", response_model=TTSResponse)
async def tts(req: TTSRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")

    tone = canon_tone(req.tone)
    requested_voice = resolve_requested_voice(req)
    profile = await get_user_profile(req.user_id)

    logger.info(
        "[tts] requested_voice=%s module=%s lang=%s user_id=%s",
        requested_voice,
        req.module,
        canon_lang(req.lang),
        req.user_id,
    )

    # auto -> backend ses üretmesin, frontend cihaz sesi/fallback kullansın
    if requested_voice == "auto":
        return TTSResponse(ok=False, error="TTS_UNAVAILABLE")

    voice_id, voice_ready = resolve_profile_voice(profile, requested_voice)

    if not voice_ready:
        return TTSResponse(
            ok=False,
            error=f"{requested_voice.upper()}_VOICE_NOT_READY"
        )

    if not voice_id or not is_uuid(voice_id):
        return TTSResponse(
            ok=False,
            error=f"{requested_voice.upper()}_VOICE_ID_INVALID"
        )

    # Önce tone ile dene
    audio = await cartesia_tts(
        text=text,
        lang=req.lang,
        voice_id=voice_id,
        tone=tone,
        use_tone=True,
    )
    if audio:
        return TTSResponse(
            ok=True,
            audio_base64=audio,
            provider_used=f"cartesia-{requested_voice}-tone"
        )

    # Sonra tonesuz fallback
    audio = await cartesia_tts(
        text=text,
        lang=req.lang,
        voice_id=voice_id,
        tone=tone,
        use_tone=False,
    )
    if audio:
        return TTSResponse(
            ok=True,
            audio_base64=audio,
            provider_used=f"cartesia-{requested_voice}"
        )

    return TTSResponse(
        ok=False,
        error=f"{requested_voice.upper()}_TTS_FAILED"
    )
