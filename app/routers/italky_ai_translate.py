# app/routers/italky_ai_translate.py
import os
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from groq import Groq
from sentence_transformers import SentenceTransformer
import requests # Supabase REST API için

router = APIRouter(tags=["italky AI"])

# Ortam Değişkenleri
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") # Yetkili anahtar

# Modelleri yükle
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
embed_model = SentenceTransformer('all-MiniLM-L6-v2')

class TranslateRequest(BaseModel):
    text: str
    target_lang: str = "en"

@router.post("/italky/cultural-translate")
async def cultural_translate(req: TranslateRequest):
    if not groq_client:
        raise HTTPException(status_code=500, detail="Groq API Key missing")

    # 1. ANLAMSAL VEKTÖRÜ OLUŞTUR
    vector = embed_model.encode(req.text).tolist()

    # 2. SUPABASE HAFIZA KONTROLÜ (RPC üzerinden SQL fonksiyonunu çağırıyoruz)
    try:
        rpc_url = f"{SUPABASE_URL}/rest/v1/rpc/match_cultural_memory"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"
        }
        rpc_payload = {
            "query_embedding": vector,
            "match_threshold": 0.88,
            "match_count": 1
        }
        memory_resp = requests.post(rpc_url, json=rpc_payload, headers=headers, timeout=5)
        memory_data = memory_resp.json()

        if memory_data and len(memory_data) > 0:
            return {
                "result": memory_data[0]['cultural_output'],
                "source": "italky_memory",
                "confidence": memory_data[0].get('similarity')
            }
    except Exception as e:
        print(f"Hafıza hatası: {e}")

    # 3. HAFIZADA YOKSA LLAMA 3.2 İLE ÇEVİR
    prompt = f"Sen bir kültürel çeviri uzmanısın. '{req.text}' ifadesini {req.target_lang} diline kültürel karşılığıyla çevir. Sadece çeviriyi yaz."
    
    try:
        chat = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.2-3b-preview",
            temperature=0.2
        )
        ai_result = chat.choices[0].message.content.strip()

        # 4. YENİ BİLGİYİ HAFIZAYA KAYDET
        save_url = f"{SUPABASE_URL}/rest/v1/italky_cultural_memory"
        save_payload = {
            "source_text": req.text,
            "target_lang": req.target_lang,
            "cultural_output": ai_result,
            "embedding": vector
        }
        requests.post(save_url, json=save_payload, headers=headers)

        return {"result": ai_result, "source": "italky_ai_engine"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI Engine Error: {str(e)}")
