# FILE: italky-api/app/routers/lang_pool.py
from __future__ import annotations

import json
import os
import re
import random
from pathlib import Path
from typing import Dict, List, Any, Optional

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

# --- AYARLAR ---
# Render'da kalıcı disk yoksa bile (ephemeral) build sonrası aynı instance'ta çalışır.
# İstersen bunu Supabase Storage'a da taşırız.
STATIC_DIR = Path(os.getenv("LANGPOOL_STATIC_DIR", "static")).resolve()
LANG_DIR = STATIC_DIR / "lang"
LANG_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_SECRET = (os.getenv("ADMIN_SECRET", "") or "").strip()

LANGS = {
    "en": "İngilizce",
    "de": "Almanca",
    "fr": "Fransızca",
    "es": "İspanyolca",
    "it": "İtalyanca",
}

POS_ALLOWED = {"noun", "verb", "adj", "adv"}
LVL_ALLOWED = {"A1", "A2", "B1", "B2", "C1"}

router = APIRouter()

# ---- MODELLER ----
class BuildReq(BaseModel):
    lang: str
    target: int = 1000
    chunk: int = 200
    max_rounds: int = 25
    version: int = 1

class BuildResp(BaseModel):
    lang: str
    target: int
    total: int
    path: str

# ---- AUTH ----
def require_admin(x_admin_secret: Optional[str] = Header(default=None)):
    # header: X-ADMIN-SECRET: ...
    if not ADMIN_SECRET:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET not set on server")
    if (x_admin_secret or "").strip() != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ---- HELPERS ----
def norm(s: str) -> str:
    s = (s or "").strip().lower()
    try:
        import unicodedata
        s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
    except Exception:
        pass
    s = re.sub(r"[.,!?;:()\"']", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def extract_json_array(text: str) -> Any:
    t = re.sub(r"```json|```", "", str(text or ""), flags=re.I).strip()
    a = t.find("[")
    b = t.rfind("]")
    if a == -1 or b == -1 or b <= a:
        raise ValueError("JSON array not found")
    t = t[a:b+1]
    try:
        return json.loads(t)
    except Exception:
        # son çare
        return json.loads(t.replace("'", '"'))

def sanitize_items(arr: Any) -> List[Dict[str, str]]:
    if not isinstance(arr, list):
        return []
    out: List[Dict[str, str]] = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        w = str(it.get("w", "")).strip()
        tr = str(it.get("tr", "")).strip()
        pos = str(it.get("pos", "")).strip().lower()
        lvl = str(it.get("lvl", "")).strip().upper()

        if not w or not tr:
            continue

        if pos not in POS_ALLOWED:
            if pos.startswith("n"): pos = "noun"
            elif pos.startswith("v"): pos = "verb"
            elif pos.startswith("ad") and pos != "adj": pos = "adv"
            elif pos.startswith("a"): pos = "adj"
        if pos not in POS_ALLOWED:
            continue

        if lvl not in LVL_ALLOWED:
            lvl = "B1"

        out.append({"w": w, "tr": tr, "pos": pos, "lvl": lvl})
    return out

def build_prompt(lang_name: str, n: int, seed: int) -> str:
    return f"""
Bana {lang_name} dilinde {n} ADET FARKLI kelime üret.
- Tek kelime veya en fazla 2 kelimelik kalıp olabilir (örn: "take off").
- Küfür/argo yok.
- Her madde şu alanları içersin:
  w (kelime), tr (Türkçe), pos (noun|verb|adj|adv), lvl (A1|A2|B1|B2|C1)

Seviye dağılımı:
A1 %20, A2 %20, B1 %25, B2 %20, C1 %15

SADECE JSON ARRAY:
[
  {{ "w":"...", "tr":"...", "pos":"noun", "lvl":"A1" }}
]
Seed:{seed}
""".strip()

def load_existing(lang: str) -> List[Dict[str, str]]:
    p = LANG_DIR / f"{lang}.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items", [])
        if not isinstance(items, list):
            return []
        cleaned = []
        for it in items:
            if isinstance(it, dict) and it.get("w") and it.get("tr"):
                cleaned.append({
                    "w": str(it["w"]).strip(),
                    "tr": str(it["tr"]).strip(),
                    "pos": str(it.get("pos","noun")).strip().lower(),
                    "lvl": str(it.get("lvl","B1")).strip().upper(),
                })
        return cleaned
    except Exception:
        return []

def write_lang(lang: str, version: int, items: List[Dict[str, str]]):
    payload = {"lang": lang, "version": version, "items": items}
    (LANG_DIR / f"{lang}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ---- /api/chat çağrısı: chat router’ın fonksiyonunu direkt kullanıyoruz ----
# Bu import, mevcut projendeki chat router’a göre çalışır.
from app.routers import chat as chat_router

async def call_internal_chat(prompt: str) -> str:
    """
    chat_router içinde /api/chat endpoint’inin kullandığı fonksiyonu çağırır.
    Eğer chat router’ında farklı isim varsa bana o dosyayı at, 1 dakikada uyarlayayım.
    """
    # Muhtemel: chat_router.chat_endpoint(text,max_tokens) veya benzeri.
    # Biz en güvenlisi: router’ın endpoint fonksiyonunu yakalayalım:
    if hasattr(chat_router, "chat") and callable(getattr(chat_router, "chat")):
        # chat(text: str, max_tokens: int=...)
        r = await chat_router.chat(text=prompt, max_tokens=3200)  # type: ignore
        return getattr(r, "text", r.get("text") if isinstance(r, dict) else str(r))
    if hasattr(chat_router, "chat_endpoint") and callable(getattr(chat_router, "chat_endpoint")):
        r = await chat_router.chat_endpoint(text=prompt, max_tokens=3200)  # type: ignore
        return getattr(r, "text", r.get("text") if isinstance(r, dict) else str(r))

    # Fallback: chat_router içinde router endpoint fonksiyonu adını bilmiyoruz
    raise RuntimeError("Cannot find chat function in app.routers.chat. Send app/routers/chat.py")

@router.post("/admin/lang/build", response_model=BuildResp, dependencies=[Depends(require_admin)])
async def build_lang_pool(req: BuildReq):
    lang = (req.lang or "").strip().lower()
    if lang not in LANGS:
        raise HTTPException(status_code=400, detail=f"Unsupported lang: {lang}")

    target = max(50, min(10000, int(req.target)))
    chunk = max(50, min(400, int(req.chunk)))
    max_rounds = max(3, min(60, int(req.max_rounds)))
    version = int(req.version)

    # mevcut dosyadan devam
    items = load_existing(lang)
    seen = set(norm(it["w"]) for it in items if it.get("w"))

    rounds = 0
    while len(items) < target and rounds < max_rounds:
        rounds += 1
        need = target - len(items)
        ask = chunk if need > chunk else need
        seed = random.randint(1, 10**9)

        prompt = build_prompt(LANGS[lang], ask, seed)

        try:
            txt = await call_internal_chat(prompt)
            arr = extract_json_array(txt)
            cleaned = sanitize_items(arr)
        except Exception as e:
            # ara ara yine de kaydet
            write_lang(lang, version, items[:target])
            raise HTTPException(status_code=500, detail=f"chat/parse failed: {e}")

        added = 0
        for it in cleaned:
            k = norm(it["w"])
            if not k or k in seen:
                continue
            seen.add(k)
            items.append(it)
            added += 1

        # ara kaydet (Render restart atarsa bile bir kısmı kalır)
        write_lang(lang, version, items[:target])

        # verim çok düşükse döngü uzamasın
        if added == 0:
            break

    write_lang(lang, version, items[:target])
    return BuildResp(lang=lang, target=target, total=min(len(items), target), path=str(LANG_DIR / f"{lang}.json"))

@router.get("/assets/lang/{lang}.json")
def get_lang_asset(lang: str):
    lang = (lang or "").strip().lower()
    p = LANG_DIR / f"{lang}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    return json.loads(p.read_text(encoding="utf-8"))
