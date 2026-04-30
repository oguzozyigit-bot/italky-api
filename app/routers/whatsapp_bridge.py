from fastapi import APIRouter, UploadFile, File, Form, HTTPException
import os
import shutil
import uuid
import speech_recognition as sr
from deep_translator import GoogleTranslator
from pydub import AudioSegment
import static_ffmpeg

# Render üzerinde ffmpeg yetki sorununu çözmek için yolları ekliyoruz
static_ffmpeg.add_paths()

router = APIRouter(
    prefix="/api/whatsapp",
    tags=["WhatsApp Bridge"]
)

def convert_ogg_to_wav(ogg_path, wav_path):
    """
    Android'den gelen OGG dosyasını STT için WAV formatına çevirir.
    pydub, static-ffmpeg sayesinde arka planda ffmpeg motorunu kullanır.
    """
    audio = AudioSegment.from_file(ogg_path, format="ogg")
    audio.export(wav_path, format="wav")

@router.post("/process")
async def process_whatsapp_voice(
    audio_file: UploadFile = File(...),
    source_lang: str = Form("tr-TR"),
    target_lang: str = Form("en-US")
):
    """
    italkyAI Bridge: Ses kaydını alır, metne çevirir ve tercüme eder.
    """
    session_id = str(uuid.uuid4())
    temp_ogg = f"temp_{session_id}.ogg"
    temp_wav = f"temp_{session_id}.wav"

    try:
        # 1. Gelen sesi geçici olarak diske kaydet
        with open(temp_ogg, "wb") as buffer:
            shutil.copyfileobj(audio_file.file, buffer)

        # 2. Format Dönüştürme (OGG -> WAV)
        convert_ogg_to_wav(temp_ogg, temp_wav)

        # 3. STT - Speech to Text (Ses Çözümleme)
        recognizer = sr.Recognizer()
        with sr.AudioFile(temp_wav) as source:
            audio_data = recognizer.record(source)
            # Google STT motorunu kullanarak sesi yazıya döküyoruz
            original_text = recognizer.recognize_google(audio_data, language=source_lang)

        # 4. Çeviri İşlemi (Deep Translator ile)
        # source_lang: tr-TR -> target_lang: en-US dönüşümü
        # auto algılama ile hata payını düşürüyoruz
        dest_code = target_lang.split("-")[0] # 'en-US' -> 'en'
        
        translated_text = GoogleTranslator(source='auto', target=dest_code).translate(original_text)

        # 5. Başarılı Sonuç Dönüşü
        return {
            "status": "success",
            "original_text": original_text,
            "translated_text": translated_text,
            "audio_url": "" # Android tarafında panoya kopyalanacak
        }

    except sr.UnknownValueError:
        return {"status": "error", "message": "Ses anlaşılamadı veya çok kısa."}
    except Exception as e:
        print(f"italkyAI Bridge Hatası: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        # Sunucu güvenliği ve temizlik için geçici dosyaları sil
        for f in [temp_ogg, temp_wav]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass
