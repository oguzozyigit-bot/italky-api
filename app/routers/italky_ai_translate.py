# app/models/italky_translate.py
import os
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db_sa import get_db
from groq import Groq
from sentence_transformers import SentenceTransformer
import httpx

router = APIRouter()

# Ayarlar
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# Not: Embedding modeli ilk açılışta 100-200MB indirebilir, Render'da cache'lenir.
embed_model = SentenceTransformer('all-MiniLM-L6-v2') 
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

@router.post("/italky/translate")
async def italky_cultural_translate(req: dict, db: Session = Depends(get_db)):
    source_text = req.get("text", "").strip()
    target_lang = req.get("target_lang", "en").strip()

    if not source_text:
        return {"error": "Metin boş olamaz evladım."}

    # 1. ANLAMSAL VEKTÖR OLUŞTUR
    query_vector = embed_model.encode(source_text).tolist()

    # 2. SUPABASE HAFIZA KONTROLÜ (Daha önce kurduğumuz SQL fonksiyonunu çağırıyoruz)
    # Not: Doğrudan SQL execute ile hafızaya bakıyoruz
    try:
        from sqlalchemy import text as sa_text
        # match_cultural_memory fonksiyonunu çağırıyoruz
        sql = sa_text("SELECT * FROM match_cultural_memory(:vec, 0.85, 1)")
        result = db.execute(sql, {"vec": query_vector}).fetchone()
        
        if result:
            return {
                "result": result.cultural_output,
                "source": "italky_memory",
                "explanation": result.meaning_description
            }
    except Exception as e:
        print(f"Hafıza arama hatası: {e}")

    # 3. HAFIZADA YOKSA GROQ (LLAMA 3.2) İLE KÜLTÜREL ÇEVİRİ
    if not groq_client:
        return {"error": "Groq API anahtarı ayarlanmamış."}

    prompt = f"""Sen bir kültürel çeviri uzmanısın. 
    Kaynak Metin: "{source_text}"
    Hedef Dil: {target_lang}
    Görevin: Bu metni hedef dile kelime kelime değil, o kültürdeki en yakın karşılığıyla çevir. 
    Eğer bir atasözü veya deyimse, o kültürdeki tam karşılığını bul.
    Sadece çeviriyi ve varsa çok kısa açıklamasını yaz."""

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.2-3b-preview", # Aldığın key ile çalışan hızlı model
            temperature=0.3
        )
        ai_response = chat_completion.choices[0].message.content.strip()

        # 4. YENİ BİLGİYİ HAFIZAYA KAYDET (Öğrenen Yapı)
        try:
            insert_sql = sa_text("""
                INSERT INTO italky_cultural_memory (source_text, target_lang, cultural_output, embedding)
                VALUES (:txt, :lang, :out, :vec)
                ON CONFLICT (source_text, target_lang) DO NOTHING
            """)
            db.execute(insert_sql, {
                "txt": source_text, 
                "lang": target_lang, 
                "out": ai_response, 
                "vec": query_vector
            })
            db.commit()
        except:
            db.rollback()

        return {"result": ai_response, "source": "italky_ai_engine"}

    except Exception as e:
        return {"error": f"AI motoru hatası: {str(e)}"}# app/models/italky_translate.py
import os
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.db_sa import get_db
from groq import Groq
from sentence_transformers import SentenceTransformer
import httpx

router = APIRouter()

# Ayarlar
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# Not: Embedding modeli ilk açılışta 100-200MB indirebilir, Render'da cache'lenir.
embed_model = SentenceTransformer('all-MiniLM-L6-v2') 
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

@router.post("/italky/translate")
async def italky_cultural_translate(req: dict, db: Session = Depends(get_db)):
    source_text = req.get("text", "").strip()
    target_lang = req.get("target_lang", "en").strip()

    if not source_text:
        return {"error": "Metin boş olamaz evladım."}

    # 1. ANLAMSAL VEKTÖR OLUŞTUR
    query_vector = embed_model.encode(source_text).tolist()

    # 2. SUPABASE HAFIZA KONTROLÜ (Daha önce kurduğumuz SQL fonksiyonunu çağırıyoruz)
    # Not: Doğrudan SQL execute ile hafızaya bakıyoruz
    try:
        from sqlalchemy import text as sa_text
        # match_cultural_memory fonksiyonunu çağırıyoruz
        sql = sa_text("SELECT * FROM match_cultural_memory(:vec, 0.85, 1)")
        result = db.execute(sql, {"vec": query_vector}).fetchone()
        
        if result:
            return {
                "result": result.cultural_output,
                "source": "italky_memory",
                "explanation": result.meaning_description
            }
    except Exception as e:
        print(f"Hafıza arama hatası: {e}")

    # 3. HAFIZADA YOKSA GROQ (LLAMA 3.2) İLE KÜLTÜREL ÇEVİRİ
    if not groq_client:
        return {"error": "Groq API anahtarı ayarlanmamış."}

    prompt = f"""Sen bir kültürel çeviri uzmanısın. 
    Kaynak Metin: "{source_text}"
    Hedef Dil: {target_lang}
    Görevin: Bu metni hedef dile kelime kelime değil, o kültürdeki en yakın karşılığıyla çevir. 
    Eğer bir atasözü veya deyimse, o kültürdeki tam karşılığını bul.
    Sadece çeviriyi ve varsa çok kısa açıklamasını yaz."""

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.2-3b-preview", # Aldığın key ile çalışan hızlı model
            temperature=0.3
        )
        ai_response = chat_completion.choices[0].message.content.strip()

        # 4. YENİ BİLGİYİ HAFIZAYA KAYDET (Öğrenen Yapı)
        try:
            insert_sql = sa_text("""
                INSERT INTO italky_cultural_memory (source_text, target_lang, cultural_output, embedding)
                VALUES (:txt, :lang, :out, :vec)
                ON CONFLICT (source_text, target_lang) DO NOTHING
            """)
            db.execute(insert_sql, {
                "txt": source_text, 
                "lang": target_lang, 
                "out": ai_response, 
                "vec": query_vector
            })
            db.commit()
        except:
            db.rollback()

        return {"result": ai_response, "source": "italky_ai_engine"}

    except Exception as e:
        return {"error": f"AI motoru hatası: {str(e)}"}
