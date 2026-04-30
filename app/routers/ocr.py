from fastapi import APIRouter, UploadFile, File, HTTPException
import easyocr
import io
from PIL import Image
from googletrans import Translator

router = APIRouter(
    prefix="/api/ocr",
    tags=["OCR Bridge"]
)

# OCR Motorunu (İngilizce ve Türkçe desteğiyle) ve Tercümanı başlatıyoruz
# Not: EasyOCR ilk çalıştırmada modelleri indirir, Render'da birkaç saniye sürebilir.
reader = easyocr.Reader(['en', 'tr'])
translator = Translator()

@router.post("/process")
async def process_screen_ocr(image_file: UploadFile = File(...)):
    """
    Android'den gelen ekran görüntüsündeki metinleri okur ve Türkçeye çevirir.
    """
    try:
        # 1. Gelen görseli belleğe oku
        image_bytes = await image_file.read()
        
        # 2. Görseli OCR motoruna ver (Direct bytes okuma)
        # detail=0 sadece metni döner, detail=1 koordinatları da verir
        results = reader.readtext(image_bytes, detail=0)
        
        # 3. Metinleri birleştir
        original_text = " ".join(results).strip()
        
        if not original_text:
            return {
                "status": "success",
                "detected_text": "",
                "translated_text": "Ekranda okunabilir bir metin bulunamadı.",
                "message": "Empty"
            }

        # 4. Metni Türkçeye Tercüme Et
        # Ekranda karışık diller olabilir, biz hedefi Türkçe (tr) yapıyoruz
        translation = translator.translate(original_text, dest='tr')

        return {
            "status": "success",
            "detected_text": original_text,
            "translated_text": translation.text
        }

    except Exception as e:
        print(f"OCR Hatası: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
