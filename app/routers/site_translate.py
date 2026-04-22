from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Header, HTTPException
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
]

SUPPORTED_CODES = {x["code"] for x in SUPPORTED_SITE_LANGS}


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
    out: List[str] = []

    for item in items:
        out.append(str(item.get("translatedText") or ""))

    if len(out) != len(cleaned):
        raise HTTPException(status_code=500, detail="google_translate_count_mismatch")

    return out


@router.get("/api/site-languages")
def site_languages():
    return {
        "ok": True,
        "languages": SUPPORTED_SITE_LANGS,
        "default_lang": "tr",
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
