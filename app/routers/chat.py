# FILE: italky-api/app/routers/chat.py
from __future__ import annotations

import os
import re
import asyncio
import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("uvicorn.error")
router = APIRouter()

try:
    import requests  # type: ignore
except Exception:
    requests = None  # type: ignore


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ChatRequest(FlexibleModel):
    text: Optional[str] = None
    message: Optional[str] = None
    user_id: Optional[str] = None
    history: Optional[List[Dict[str, str]]] = None  # [{role, content}]
    max_tokens: Optional[int] = 520


class ChatResponse(FlexibleModel):
    text: str


GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY", "") or "").strip()
PREFERRED_MODELS = [
    (os.getenv("GEMINI_MODEL_CHAT", "") or "").strip(),
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-pro-latest",
]
PREFERRED_MODELS = [m for m in PREFERRED_MODELS if m]
_selected_model_cache: Dict[str, str] = {"name": ""}


def list_gemini_models() -> List[Dict[str, Any]]:
    if not GEMINI_API_KEY or requests is None:
        return []
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    r = requests.get(url, params={"key": GEMINI_API_KEY}, timeout=20)
    r.raise_for_status()
    return (r.json().get("models") or [])


def pick_best_model(models: List[Dict[str, Any]]) -> str:
    if not models:
        return ""
    by_name: Dict[str, Dict[str, Any]] = {}
    for m in models:
        nm = (m.get("name") or "").strip()
        if nm:
            by_name[nm.replace("models/", "")] = m

    for want in PREFERRED_MODELS:
        if want in by_name:
            meth = by_name[want].get("supportedGenerationMethods") or []
            if not meth or ("generateContent" in meth):
                return want

    for nm, mm in by_name.items():
        meth = mm.get("supportedGenerationMethods") or []
        if (not meth) or ("generateContent" in meth):
            return nm

    return next(iter(by_name.keys()), "")


def _gemini_build(messages: List[Dict[str, Any]], max_tokens: int = 520) -> Dict[str, Any]:
    system_text = ""
    contents: List[Dict[str, Any]] = []

    for m in messages:
        role = (m.get("role") or "").strip().lower()
        text_ = (m.get("content") or "").strip()
        if not text_:
            continue

        if role == "system":
            system_text += (text_ + "\n")
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text_}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text_}]})

    body: Dict[str, Any] = {
        "contents": contents or [{"role": "user", "parts": [{"text": "Hi"}]}],
        "generationConfig": {
            "temperature": 0.25,
            "topP": 0.9,
            "maxOutputTokens": int(max_tokens or 520),
        },
    }
    if system_text.strip():
        body["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
    return body


async def call_gemini(messages: List[Dict[str, Any]], max_tokens: int = 520) -> str:
    if not GEMINI_API_KEY:
        return "Gemini anahtarı yok. (GEMINI_API_KEY eksik)"
    if requests is None:
        return "Sunucuda requests yok. (pip install requests)"

    if not _selected_model_cache.get("name"):
        try:
            models = await asyncio.to_thread(list_gemini_models)
            picked = pick_best_model(models)
            _selected_model_cache["name"] = picked or "gemini-1.5-flash"
            logger.warning("[GEMINI_MODEL] picked=%s", _selected_model_cache["name"])
        except Exception:
            _selected_model_cache["name"] = (PREFERRED_MODELS[0] if PREFERRED_MODELS else "gemini-1.5-flash")

    model_name = _selected_model_cache.get("name") or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    body = _gemini_build(messages, max_tokens=max_tokens)

    def _sync():
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=35)
        if r.status_code == 404:
            return "__MODEL_NOT_FOUND__"
        r.raise_for_status()
        dd = r.json()
        try:
            c0 = (dd.get("candidates") or [])[0]
            content = c0.get("content") or {}
            parts = content.get("parts") or []
            txt = (parts[0].get("text") if parts else "") or ""
            return str(txt).strip()
        except Exception:
            return ""

    out = await asyncio.to_thread(_sync)
    if out == "__MODEL_NOT_FOUND__":
        _selected_model_cache["name"] = ""
        return ""
    return out.strip() if out else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _dedupe_lines(text: str) -> str:
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    out = []
    prev = ""
    for ln in lines:
        n = _norm(ln)
        if not n:
            if out and out[-1] == "":
                continue
            out.append("")
            prev = ""
            continue
        if n == prev:
            continue
        out.append(ln)
        prev = n
    return "\n".join(out).strip()

def sanitize_reply(text: str, max_chars: int = 900) -> str:
    t = (text or "").strip()
    if not t:
        return t
    t = _dedupe_lines(t)
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + "…"
    return t


@router.post("/chat", response_model=ChatResponse)
async def api_chat(req: ChatRequest):
    msg = (req.text or req.message or "").strip()
    if not msg:
        raise HTTPException(400, "empty text")

    # ✅ KİLİT KURAL: "Google/OpenAI/Gemini" geçmez.
    system = (
        "Sen Italky Chat AI'sın.\n"
        "Her zaman Türkçe yanıt ver (kullanıcı özellikle başka dil istemedikçe).\n"
        "Seni geliştiren ve üreten firma: Italky AI.\n"
        "Kesin kural: Google, OpenAI, Gemini, Alphabet, DeepMind gibi isimleri ASLA anma.\n"
        "Kullanıcı 'seni kim geliştirdi/üretti/yarattı' derse tek cevap: "
        "'Ben Italky AI tarafından geliştirilen bir dil yazılımıyım.'\n"
        "Kısa, net, yardımcı ol. Uydurma yapma.\n"
    )

    hist = []
    try:
        for h in (req.history or [])[-20:]:
            r = str(h.get("role", "")).strip().lower()
            c = str(h.get("content", "")).strip()
            if r in ("user", "assistant") and c:
                hist.append({"role": r, "content": c})
    except Exception:
        hist = []

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
    for h in hist:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg})

    out = await call_gemini(messages, max_tokens=int(req.max_tokens or 520))
    out = sanitize_reply(out or "Bir aksilik oldu.")
    return ChatResponse(text=out)
