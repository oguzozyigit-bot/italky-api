# app/routers/italky_ai_translate.py
from __future__ import annotations
import os
import logging
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from groq import Groq
from sentence_transformers import SentenceTransformer

# Senin tts.py içindeki çalışan fonksiyonları kullanıyoruz
from app.routers.tts import cartesia_tts, get_user_profile

logger = logging.getLogger("uvicorn.error")
router = APIRouter(tags=["italky-ai-engine"])

# AYARLAR (Render Environment Variables)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# Modeller
embed_model = SentenceTransformer('all-MiniLM-L6-v2')
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class CulturalTranslateRequest(BaseModel):
    text: str
    target_lang: str = "en"
    user_id: str = None
    tone: str = "neutral"

@router.post("/italky/cultural-translate-voice")
async def italky_cultural_translate_voice(req: CulturalTranslateRequest):
    if not req.text:
        raise HTTPException(status_code=400, detail="Text is required")

    # 1. ANLAMSAL VEKTÖR OLUŞTUR
    query_vector = embed_model.encode(req.text).tolist()

    # 2. SUPABASE HAFIZA KONTROLÜ (Requests ile - app.core bağımlılığı olmadan)
    memory_result = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            rpc_resp = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/match_cultural_memory",
                json={
                    "query_embedding": query_vector,
                    "match_threshold": 0.88,
                    "match_count": 1
                },
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                }
            )
            if rpc_resp.status_code == 200:
                data = rpc_resp.json()
                if data:
                    memory_result = data[0]
    except Exception as e:
        logger.error(f"Hafiza hatasi: {str(e)}")

    # 3. ÇEVİRİ MANTIĞI
    translated_text = ""
    source_info = ""

    if memory_result:
        translated_text = memory_result['cultural_output']
        source_info = "italky_memory"
    else:
        if not groq_client:
            raise HTTPException(status_code=500, detail="Groq API Key missing")

        prompt = f"Sen bir kültürel çeviri uzmanısın. '{req.text}' ifadesini {req.target_lang} diline kültürel karşılığıyla çevir. Sadece sonucu yaz."
        
        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.2-3b-preview",
            temperature=0.2,
        )
        translated_text = chat.choices[0].message.content.strip()
        source_info = "italky_ai_engine"

        # Kayıt (Async)
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/italky_cultural_memory",
                    json={
                        "source_text": req.text,
                        "target_lang": req.target_lang,
                        "cultural_output": translated_text,
                        "embedding": query_vector
                    },
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
                )
        except:
            pass

    # 4. SESLENDİRME (TTS)
    audio_base64 = None
    profile = await get_user_profile(req.user_id)
    voice_id = (profile or {}).get("tts_voice_id")
    voice_ready = bool(profile and profile.get("tts_voice_ready"))

    if voice_ready and voice_id:
        audio_base64 = await cartesia_tts(
            text=translated_text,
            lang=req.target_lang,
            voice_id=voice_id,
            tone=req.tone,
            use_tone=True
        )

    return {
        "ok": True,
        "source": source_info,
        "output": translated_text,
        "audio_base64": audio_base64
    }
