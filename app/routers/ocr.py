from fastapi import APIRouter, UploadFile, File, HTTPException
import easyocr
import io
from PIL import Image
from deep_translator import GoogleTranslator

router = APIRouter(
    prefix="/api/ocr",
    tags=["OCR Bridge"]
)

# OCR Motorunu başlatıyoruz. 
# gpu=False: Render ücretsiz/standart planlarda GPU olmadığı için CPU modunda çalıştırır.
reader = easyocr.Reader(['en', 'tr'], gpu=False)

@router.post("/process")
async def process_screen_ocr(image_file: UploadFile = File(...)):
    """
    italkyAI OCR: Ekran görüntüsündeki metinleri ayıklar ve Türkçeye çevirir.
    """
    try:
        # 1. Gelen görseli belleğe oku
        image_bytes = await image_file.read()
        
        # 2. OCR İşlemi (Görseldeki metni yazıya dökme)
        # detail=0: Koordinatlar olmadan sadece düz metin listesi döner.
        results = reader.readtext(image_bytes, detail=0)
        
        # 3. Yakalanan metin parçalarını tek bir metin haline getir
        original_text = " ".join(results).strip()
        
        if not original_text:
            return {
                "status": "success",
                "detected_text": "",
                "translated_text": "Ekranda okunabilir bir metin bulunamadı.",
                "message": "Empty"
            }

        # 4. Çeviri İşlemi (Deep Translator ile)
        # Kaynak dili otomatik algılayıp (auto), hedef dili Türkçe (tr) yaparız.
        translated_text = GoogleTranslator(source='auto', target='tr').translate(original_text)

        # 5. Sonuç Dönüşü
        return {
            "status": "success",
            "detected_text": original_text,
            "translated_text": translated_text
        }

    except Exception as e:
        print(f"italkyAI OCR Hatası: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
