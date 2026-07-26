from fastapi import APIRouter, UploadFile, File, Form, HTTPException
import os
import shutil
import uuid
import speech_recognition as sr
from deep_translator import GoogleTranslator  # googletrans yerine bu geldi
from pydub import AudioSegment

try:
    import static_ffmpeg  # type: ignore
    static_ffmpeg.add_paths()
    FFMPEG_READY = True
except Exception:
    # Vercel paket boyutu nedeniyle static-ffmpeg kullanılmıyor.
    # Ana API açılmalı; bu endpoint çağrılırsa kontrollü hata döner.
    FFMPEG_READY = False

router = APIRouter(
    prefix="/api/whatsapp",
    tags=["WhatsApp Bridge"]
)

def convert_ogg_to_wav(ogg_path, wav_path):
    """
    Android'den gelen OGG dosyasını STT için WAV formatına çevirir.
    Vercel ortamında ffmpeg yoksa ana API çökmez, sadece bu işlem devre dışı kalır.
    """
    if not FFMPEG_READY:
        raise HTTPException(
            status_code=503,
            detail="WhatsApp ses dönüştürme bu ortamda geçici olarak devre dışı: ffmpeg yok."
        )
    audio = AudioSegment.from_file(ogg_path, format="ogg")
    audio.export(wav_path, format="wav")

@router.post("/process")
async def process_whatsapp_voice(
    audio_file: UploadFile = File(...),
    source_lang: str = Form("tr-TR"),
    target_lang: str = Form("en-US")
):
    """
    italkyAI Bridge: Sesi alır, metne çevirir, tercüme eder ve sonucu döner.
    """
    session_id = str(uuid.uuid4())
    temp_ogg = f"temp_{session_id}.ogg"
    temp_wav = f"temp_{session_id}.wav"

    try:
        # 1. Gelen sesi geçici olarak kaydet
        with open(temp_ogg, "wb") as buffer:
            shutil.copyfileobj(audio_file.file, buffer)

        # 2. Format Dönüştürme (OGG -> WAV)
        convert_ogg_to_wav(temp_ogg, temp_wav)

        # 3. STT - Speech to Text (Sesi Yazıya Dökme)
        recognizer = sr.Recognizer()
        with sr.AudioFile(temp_wav) as source:
            audio_data = recognizer.record(source)
            # Google STT kullanarak metni çözüyoruz
            original_text = recognizer.recognize_google(audio_data, language=source_lang)

        # 4. Çeviri İşlemi (Deep Translator ile)
        # source_lang: tr-TR -> tr , target_lang: en-US -> en formatına çeviriyoruz
        dest_code = target_lang.split("-")[0]
        
        translated_text = GoogleTranslator(source='auto', target=dest_code).translate(original_text)

        # 5. Başarılı Sonuç
        return {
            "status": "success",
            "original_text": original_text,
            "translated_text": translated_text,
            "audio_url": "" 
        }

    except sr.UnknownValueError:
        return {"status": "error", "message": "Ses anlaşılamadı, lütfen tekrar deneyin."}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Bridge Hatası: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        # Geçici dosyaları temizle
        for f in [temp_ogg, temp_wav]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
