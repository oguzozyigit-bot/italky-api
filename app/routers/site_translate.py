from __future__ import annotations

import os
from typing import List, Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["site-translate"])

GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()

SUPPORTED_SITE_LANGS = [
    {"code": "tr", "label": "Türkçe", "dir": "ltr"},
    {"code": "en", "label": "English", "dir": "ltr"},
    {"code": "de", "label": "Deutsch", "dir": "ltr"},
    {"code": "fr", "label": "Français", "dir": "ltr"},
    {"code": "it", "label": "Italiano", "dir": "ltr"},
    {"code": "es", "label": "Español", "dir": "ltr"},
    {"code": "ar", "label": "العربية", "dir": "rtl"},
    {"code": "ru", "label": "Русский", "dir": "ltr"},
    {"code": "bg", "label": "Български", "dir": "ltr"},
    {"code": "bn", "label": "বাংলা", "dir": "ltr"},
    {"code": "ca", "label": "Català", "dir": "ltr"},
    {"code": "cs", "label": "Čeština", "dir": "ltr"},
    {"code": "da", "label": "Dansk", "dir": "ltr"},
    {"code": "el", "label": "Ελληνικά", "dir": "ltr"},
    {"code": "et", "label": "Eesti", "dir": "ltr"},
    {"code": "eu", "label": "Euskara", "dir": "ltr"},
    {"code": "fi", "label": "Suomi", "dir": "ltr"},
    {"code": "gl", "label": "Galego", "dir": "ltr"},
    {"code": "hu", "label": "Magyar", "dir": "ltr"},
    {"code": "id", "label": "Bahasa Indonesia", "dir": "ltr"},
    {"code": "lt", "label": "Lietuvių", "dir": "ltr"},
    {"code": "lv", "label": "Latviešu", "dir": "ltr"},
    {"code": "ms", "label": "Bahasa Melayu", "dir": "ltr"},
    {"code": "nl", "label": "Nederlands", "dir": "ltr"},
    {"code": "pl", "label": "Polski", "dir": "ltr"},
    {"code": "ro", "label": "Română", "dir": "ltr"},
    {"code": "sk", "label": "Slovenčina", "dir": "ltr"},
    {"code": "sl", "label": "Slovenščina", "dir": "ltr"},
    {"code": "sq", "label": "Shqip", "dir": "ltr"},
    {"code": "th", "label": "ไทย", "dir": "ltr"},
    {"code": "ur", "label": "اردو", "dir": "rtl"},
    {"code": "vi", "label": "Tiếng Việt", "dir": "ltr"},
    {"code": "zh", "label": "中文", "dir": "ltr"},
    {"code": "pt", "label": "Português", "dir": "ltr"},
    {"code": "hi", "label": "हिन्दी", "dir": "ltr"},
    {"code": "ja", "label": "日本語", "dir": "ltr"},
    {"code": "ko", "label": "한국어", "dir": "ltr"},
    {"code": "sv", "label": "Svenska", "dir": "ltr"},
    {"code": "no", "label": "Norsk", "dir": "ltr"},
    {"code": "uk", "label": "Українська", "dir": "ltr"},
    {"code": "fa", "label": "فارسی", "dir": "rtl"},
]

SUPPORTED_CODES = {x["code"] for x in SUPPORTED_SITE_LANGS}

COUNTRY_TO_LANG = {
    "TR": "tr",
    "GB": "en", "US": "en", "CA": "en", "AU": "en", "NZ": "en", "IE": "en",
    "DE": "de", "AT": "de", "CH": "de",
    "FR": "fr", "BE": "fr", "LU": "fr",
    "IT": "it",
    "ES": "es", "MX": "es", "AR": "es", "CO": "es", "CL": "es", "PE": "es", "VE": "es",
    "SA": "ar", "AE": "ar", "EG": "ar", "IQ": "ar", "JO": "ar", "KW": "ar", "LB": "ar",
    "LY": "ar", "MA": "ar", "OM": "ar", "QA": "ar", "SY": "ar", "TN": "ar", "YE": "ar",
    "RU": "ru",
    "BG": "bg",
    "BD": "bn",
    "CZ": "cs",
    "DK": "da",
    "GR": "el", "CY": "el",
    "EE": "et",
    "FI": "fi",
    "HU": "hu",
    "ID": "id",
    "LT": "lt",
    "LV": "lv",
    "MY": "ms",
    "NL": "nl",
    "PL": "pl",
    "RO": "ro", "MD": "ro",
    "SK": "sk",
    "SI": "sl",
    "AL": "sq", "XK": "sq",
    "TH": "th",
    "PK": "ur",
    "VN": "vi",
    "CN": "zh", "TW": "zh", "HK": "zh", "SG": "zh",
    "PT": "pt", "BR": "pt",
    "IN": "hi",
    "JP": "ja",
    "KR": "ko",
    "SE": "sv",
    "NO": "no",
    "UA": "uk",
    "IR": "fa",
}


class SiteTranslateBody(BaseModel):
    texts: List[str]
    target_lang: str
    source_lang: Optional[str] = "tr"
    format: Optional[str] = "text"


def normalize_lang(code: Optional[str]) -> str:
    value = str(code or "tr").strip().lower().replace("_", "-")
    base = value.split("-")[0]
    if base in SUPPORTED_CODES:
        return base
    return "tr"


def require_google_key() -> str:
    if not GOOGLE_TRANSLATE_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_TRANSLATE_API_KEY missing")
    return GOOGLE_TRANSLATE_API_KEY


def translate_batch_google(
    texts: List[str],
    source_lang: str,
    target_lang: str,
    fmt: str = "text",
) -> List[str]:
    api_key = require_google_key()
    cleaned = [str(x or "") for x in texts]
    if not cleaned:
        return []

    url = "https://translation.googleapis.com/language/translate/v2"
    payload = {
        "q": cleaned,
        "source": source_lang,
        "target": target_lang,
        "format": "html" if fmt == "html" else "text",
        "key": api_key,
    }

    r = requests.post(url, data=payload, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"google_translate_failed: {r.text[:300]}")

    data = r.json() or {}
    items = data.get("data", {}).get("translations", []) or []
    out: List[str] = [str(item.get("translatedText") or "") for item in items]

    if len(out) != len(cleaned):
        raise HTTPException(status_code=500, detail="google_translate_count_mismatch")

    return out


def detect_country_from_headers(request: Request) -> str:
    candidates = [
        request.headers.get("cf-ipcountry"),
        request.headers.get("x-vercel-ip-country"),
        request.headers.get("x-country-code"),
        request.headers.get("cloudfront-viewer-country"),
    ]
    for value in candidates:
        code = str(value or "").strip().upper()
        if len(code) == 2 and code.isalpha():
            return code
    return ""


def detect_country_from_ip_service() -> str:
    try:
        r = requests.get("https://ipapi.co/json/", timeout=5)
        if r.status_code == 200:
            data = r.json() or {}
            code = str(data.get("country_code") or "").strip().upper()
            if len(code) == 2 and code.isalpha():
                return code
    except Exception:
        pass
    return ""


@router.get("/api/site-languages")
def site_languages():
    return {
        "ok": True,
        "languages": SUPPORTED_SITE_LANGS,
        "default_lang": "tr",
    }


@router.get("/api/site-country")
def site_country(request: Request):
    country = detect_country_from_headers(request)
    source = "header"

    if not country:
        country = detect_country_from_ip_service()
        source = "ip"

    lang = COUNTRY_TO_LANG.get(country, "tr") if country else "tr"

    return {
        "ok": True,
        "country": country or "",
        "suggested_lang": lang,
        "source": source if country else "default",
    }


@router.post("/api/site-translate")
def site_translate(body: SiteTranslateBody, authorization: Optional[str] = Header(default=None)):
    target_lang = normalize_lang(body.target_lang)
    source_lang = normalize_lang(body.source_lang)
    fmt = "html" if str(body.format or "text").strip().lower() == "html" else "text"

    if target_lang == source_lang:
        return {
            "ok": True,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "translations": body.texts,
        }

    texts = [str(x or "") for x in body.texts]
    if not texts:
        return {
            "ok": True,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "translations": [],
        }

    translations = translate_batch_google(
        texts=texts,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt=fmt,
    )

    return {
        "ok": True,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "translations": translations,
    }
