# FILE: italky-api/app/routers/tts.py
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
import edge_tts

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["tts"])

# Disk cache (Render'da /tmp güvenli)
CACHE_DIR = Path("/tmp/italky_tts_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 6 dil için voice seçimi (istersen artırırız)
VOICE_MAP = {
    "en": "en-US-JennyNeural",
    "de": "de-DE-KatjaNeural",
    "fr": "fr-FR-DeniseNeural",
    "it": "it-IT-ElsaNeural",
    "es": "es-ES-ElviraNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "tr": "tr-TR-EmelNeural",
}

# ---------- Schemas ----------
class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

class TTSRequest(FlexibleModel):
    # frontend bazı yerde text, bazı yerde input yolluyor
    text: Optional[str] = None
    input: Optional[str] = None

    lang: str = Field("en", min_length=2, max_length=16)

    # opsiyonel override (istersen query ile açarız)
    voice: Optional[str] = None

    # Edge TTS rate/volume/pitch SSML şeklinde; şimdilik sabit tutuyoruz.
    # speaking_rate, pitch alanlarını yok etmiyoruz ki eski client kırılmasın.
    speaking_rate: float = 1.0
    pitch: float = 0.0

def canon_lang(code: str) -> str:
    c = (code or "en").strip().lower().replace("_", "-")
    # en-us gibi gelirse en'e indir
    if "-" in c:
        c = c.split("-")[0]
    return c

def pick_voice(lang: str, voice_override: Optional[str]) -> str:
    if voice_override:
        return voice_override.strip()
    return VOICE_MAP.get(canon_lang(lang), VOICE_MAP["en"])

def cache_path(voice: str, text: str) -> Path:
    h = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.mp3"

async def edge_tts_mp3(text: str, lang: str, voice_override: Optional[str]) -> Path:
    voice = pick_voice(lang, voice_override)
    out_path = cache_path(voice, text)

    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    # Edge TTS üret
    communicate = edge_tts.Communicate(text=text, voice=voice)
    await communicate.save(str(out_path))
    return out_path

@router.post("/tts")
async def tts(req: TTSRequest):
    text = (req.text or req.input or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text (or input) is required")

    try:
        mp3_path = await edge_tts_mp3(text=text, lang=req.lang, voice_override=req.voice)
        return FileResponse(
            path=str(mp3_path),
            media_type="audio/mpeg",
            filename="tts.mp3",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.exception("TTS_EDGE_EXCEPTION: %s", e)
        # frontend bozulmasın diye 500 dönelim
        raise HTTPException(status_code=500, detail="TTS_UNAVAILABLE")
