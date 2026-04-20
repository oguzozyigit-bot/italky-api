from __future__ import annotations

import os
import logging
import base64
import uuid
from typing import Optional, Tuple, Any, Dict

import httpx
from fastapi import APIRouter, HTTPException, Header
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
    charged: Optional[bool] = None
    usage_kind: Optional[str] = None
    chars_used: Optional[int] = None
    jetons_spent: Optional[int] = None
    tokens_after: Optional[int] = None
    text_bucket: Optional[int] = None
    voice_bucket: Optional[int] = None


async def get_user_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        return None

    url = (
        f"{SUPABASE_URL}/rest/v1/profiles"
        f"?id=eq.{user_id}"
        f"&select="
        f"id,full_name,"
        f"tts_voice_id,tts_voice_ready,voice_sample_path,tts_voice_last_error,"
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
        ready = bool(profile.get("tts_voice_ready")) and bool(voice_id) and is_uuid(voice_id)
        return voice_id, ready

    if requested_voice == "second":
        voice_id = str(profile.get("second_tts_voice_id") or "").strip()
        ready = bool(profile.get("second_tts_voice_ready")) and bool(voice_id) and is_uuid(voice_id)
        return voice_id, ready

    if requested_voice == "memory":
        voice_id = str(profile.get("memory_tts_voice_id") or "").strip()
        ready = bool(profile.get("memory_tts_voice_ready")) and bool(voice_id) and is_uuid(voice_id)
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


def _get_bearer(auth_header: Optional[str]) -> str:
    if not auth_header:
        raise HTTPException(status_code=401, detail="authorization_missing")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="authorization_invalid")

    return parts[1].strip()


async def _get_user_from_jwt(jwt_token: str) -> Dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise HTTPException(status_code=500, detail="supabase_not_ready")

    url = f"{SUPABASE_URL}/auth/v1/user"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE,
                "Authorization": f"Bearer {jwt_token}",
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid_session")

    data = r.json() or {}
    user_id = str(data.get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="user_not_found")

    return {
        "id": user_id,
        "email": data.get("email"),
    }


async def _get_wallet_summary(user_id: str) -> Dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise HTTPException(status_code=500, detail="supabase_not_ready")

    url = f"{SUPABASE_URL}/rest/v1/rpc/get_wallet_summary"

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                "Content-Type": "application/json",
            },
            json={"p_user_id": user_id},
        )

    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"wallet_summary_failed: {r.text[:300]}")

    data = r.json()
    if data is None:
        raise HTTPException(status_code=500, detail="wallet_summary_empty")
    return data


async def _precheck_voice_charge(user_id: str, char_count: int) -> Dict[str, Any]:
    summary = await _get_wallet_summary(user_id)

    tokens = int(summary.get("tokens") or 0)
    text_bucket = int(summary.get("text_bucket") or 0)
    voice_bucket = int(summary.get("voice_bucket") or 0)

    total = voice_bucket + max(0, int(char_count))
    jetons_needed = total // 1000

    return {
        "tokens": tokens,
        "text_bucket": text_bucket,
        "voice_bucket": voice_bucket,
        "jetons_needed": jetons_needed,
        "can_afford": tokens >= jetons_needed,
    }


async def _apply_voice_charge(
    user_id: str,
    chars_used: int,
    source: str,
    description: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
        raise HTTPException(status_code=500, detail="supabase_not_ready")

    url = f"{SUPABASE_URL}/rest/v1/rpc/apply_usage_charge"

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            url,
            headers={
                "apikey": SUPABASE_SERVICE_ROLE,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
                "Content-Type": "application/json",
            },
            json={
                "p_user_id": user_id,
                "p_usage_kind": "voice",
                "p_chars_used": int(chars_used),
                "p_source": source,
                "p_description": description,
                "p_meta": meta,
            },
        )

    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"usage_charge_failed: {r.text[:300]}")

    data = r.json()
    if data is None:
        raise HTTPException(status_code=500, detail="usage_charge_empty")

    return data


@router.post("/tts", response_model=TTSResponse)
async def tts(
    req: TTSRequest,
    authorization: Optional[str] = Header(default=None),
):
    try:
        text = (req.text or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="text is required")

        tone = canon_tone(req.tone)
        requested_voice = resolve_requested_voice(req)
        chars_used = len(text)

        logger.info(
            "[tts] requested_voice=%s module=%s lang=%s user_id=%s",
            requested_voice,
            req.module,
            canon_lang(req.lang),
            req.user_id,
        )

        if requested_voice == "auto":
            return TTSResponse(
                ok=False,
                error="TTS_UNAVAILABLE",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
            )

        jwt_user_id = None
        if authorization:
            jwt_token = _get_bearer(authorization)
            jwt_user = await _get_user_from_jwt(jwt_token)
            jwt_user_id = jwt_user["id"]

        if req.user_id and jwt_user_id and str(req.user_id).strip() != str(jwt_user_id).strip():
            raise HTTPException(status_code=403, detail="user_mismatch")

        effective_user_id = jwt_user_id or (str(req.user_id).strip() if req.user_id else None)
        if not effective_user_id:
            raise HTTPException(status_code=401, detail="user_required")

        profile = await get_user_profile(effective_user_id)
        voice_id, voice_ready = resolve_profile_voice(profile, requested_voice)

        logger.info(
            "[tts-debug] selected_voice=%s voice=%s preset_voice=%s voice_mode=%s requested=%s user_id=%s",
            req.selected_voice,
            req.voice,
            req.preset_voice,
            req.voice_mode,
            requested_voice,
            effective_user_id,
        )
        logger.info(
            "[tts-debug] resolved voice_id=%s ready=%s profile=%s",
            voice_id,
            voice_ready,
            profile,
        )

        if not voice_ready:
            return TTSResponse(
                ok=False,
                error=f"{requested_voice.upper()}_VOICE_NOT_READY",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
            )

        if not voice_id or not is_uuid(voice_id):
            return TTSResponse(
                ok=False,
                error=f"{requested_voice.upper()}_VOICE_ID_INVALID",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
            )

        logger.info("[tts-step] before_precheck user_id=%s chars=%s", effective_user_id, chars_used)
        precheck = await _precheck_voice_charge(effective_user_id, chars_used)
        logger.info("[tts-step] after_precheck precheck=%s", precheck)

        if not precheck["can_afford"]:
            return TTSResponse(
                ok=False,
                error="INSUFFICIENT_TOKENS",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
                tokens_after=int(precheck["tokens"]),
                text_bucket=int(precheck["text_bucket"]),
                voice_bucket=int(precheck["voice_bucket"]),
            )

        logger.info("[tts-step] before_cartesia voice_id=%s lang=%s tone=%s", voice_id, req.lang, tone)
        audio = await cartesia_tts(
            text=text,
            lang=req.lang,
            voice_id=voice_id,
            tone=tone,
            use_tone=True,
        )
        provider_used = None

        if audio:
            provider_used = f"cartesia-{requested_voice}-tone"
        else:
            audio = await cartesia_tts(
                text=text,
                lang=req.lang,
                voice_id=voice_id,
                tone=tone,
                use_tone=False,
            )
            if audio:
                provider_used = f"cartesia-{requested_voice}"

        logger.info("[tts-step] after_cartesia provider=%s audio_ok=%s", provider_used, bool(audio))

        if not audio:
            return TTSResponse(
                ok=False,
                error=f"{requested_voice.upper()}_TTS_FAILED",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
                tokens_after=int(precheck["tokens"]),
                text_bucket=int(precheck["text_bucket"]),
                voice_bucket=int(precheck["voice_bucket"]),
            )

        logger.info("[tts-step] before_apply_charge user_id=%s chars=%s", effective_user_id, chars_used)
        charge = await _apply_voice_charge(
            user_id=effective_user_id,
            chars_used=chars_used,
            source=f"tts_{requested_voice}",
            description=f"Özel ses TTS kullanımı ({requested_voice})",
            meta={
                "module": req.module,
                "voice_mode": requested_voice,
                "lang": canon_lang(req.lang),
                "tone": tone,
                "provider": provider_used,
                "chars_used": chars_used,
            },
        )
        logger.info("[tts-step] after_apply_charge charge=%s", charge)

        if not bool(charge.get("ok")):
            return TTSResponse(
                ok=False,
                error="USAGE_CHARGE_FAILED",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
            )

        if charge.get("reason") == "insufficient_tokens":
            return TTSResponse(
                ok=False,
                error="INSUFFICIENT_TOKENS",
                charged=False,
                usage_kind="voice",
                chars_used=chars_used,
                jetons_spent=0,
                tokens_after=int(charge.get("tokens_after") or precheck["tokens"]),
                text_bucket=int(charge.get("text_bucket") or precheck["text_bucket"]),
                voice_bucket=int(charge.get("voice_bucket") or precheck["voice_bucket"]),
            )

        return TTSResponse(
            ok=True,
            audio_base64=audio,
            provider_used=provider_used,
            charged=bool(charge.get("charged", False)),
            usage_kind="voice",
            chars_used=chars_used,
            jetons_spent=int(charge.get("jetons_spent") or 0),
            tokens_after=int(charge.get("tokens_after") or precheck["tokens"]),
            text_bucket=int(charge.get("text_bucket") or precheck["text_bucket"]),
            voice_bucket=int(charge.get("voice_bucket") or precheck["voice_bucket"]),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[tts-fatal] unhandled exception: %s", e)
        raise HTTPException(status_code=500, detail=f"tts_internal_error: {e}")
