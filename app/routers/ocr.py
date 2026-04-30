from fastapi import APIRouter, UploadFile, File, HTTPException
import easyocr
from deep_translator import GoogleTranslator

# main.py zaten '/api' prefix'i ile eklediği için burada sadece '/ocr' bırakıyoruz.
router = APIRouter(
    prefix="/ocr",
    tags=["OCR Bridge"]
)

# OCR Motorunu başlatıyoruz. 
# Render üzerinde GPU olmadığı için gpu=False olarak ayarladık.
reader = easyocr.Reader(['en', 'tr'], gpu=False)

@router.post("/process")
async def process_screen_ocr(image_file: UploadFile = File(...)):
    """
    italkyAI OCR: Ekran görüntüsündeki metinleri ayıklar ve Türkçeye çevirir.
    URL: /api/ocr/process
    """
    try:
        # 1. Gelen görseli oku
        image_bytes = await image_file.read()
        
        # 2. OCR İşlemi (Metin çıkarma)
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

        # 4. Çeviri İşlemi (deep-translator ile - modern ve uyumlu)
        # Kaynak dili otomatik algılar (auto), hedefi Türkçe (tr) yapar.
        translated_text = GoogleTranslator(source='auto', target='tr').translate(original_text)

        return {
            "status": "success",
            "detected_text": original_text,
            "translated_text": translated_text
        }

    except Exception as e:
        print(f"italkyAI OCR Hatası: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
