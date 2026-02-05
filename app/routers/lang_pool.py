from __future__ import annotations

import json
import os
import re
import random
from typing import Dict, List, Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

# ✅ Gemini çağrısı (chat.py içindeki async fonksiyon)
from app.routers.chat import call_gemini

router = APIRouter()

# ====== ENV ======
ADMIN_SECRET = (os.getenv("ADMIN_SECRET", "") or "").strip()

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()
LANGPOOL_BUCKET = (os.getenv("LANGPOOL_BUCKET", "lang") or "lang").strip()

LANGS = {
    "en": "İngilizce",
    "de": "Almanca",
    "fr": "Fransızca",
    "es": "İspanyolca",
    "it": "İtalyanca",
}

POS_ALLOWED = {"noun", "verb", "adj", "adv"}
LVL_ALLOWED = {"A1", "A2", "B1", "B2", "C1"}

# ====== AUTH ======
def require_admin(x_admin_secret: Optional[str] = Header(default=None)):
    if not ADMIN_SECRET:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET not set")
    if (x_admin_secret or "").strip() != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ====== MODELS ======
class BuildReq(BaseModel):
    lang: str
    target: int = 1000
    chunk: int = 120
    max_rounds: int = 60
    version: int = 1
    mode: str = "fill"  # "fill" | "add"

class BuildResp(BaseModel):
    lang: str
    target: int
    total: int
    public_url: str

# ====== HELPERS ======
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
            if pos.startswith("n"):
                pos = "noun"
            elif pos.startswith("v"):
                pos = "verb"
            elif pos.startswith("ad") and pos != "adj":
                pos = "adv"
            elif pos.startswith("a"):
                pos = "adj"
        if pos not in POS_ALLOWED:
            continue

        if lvl not in LVL_ALLOWED:
            lvl = "B1"

        out.append({"w": w, "tr": tr, "pos": pos, "lvl": lvl})
    return out

def build_prompt(lang_name: str, n: int, seed: int) -> str:
    # ASCII güvenli (tr alanı da ascii)
    return f"{lang_name} dilinde {n} farkli kelime uret. Seed:{seed}"

def build_system_instruction() -> str:
    # ASCII-safe tr: Türkçe karakter istemiyoruz (encoding riskini azaltır)
    return (
        "YOU ARE A DATA GENERATOR. OUTPUT ONLY VALID JSON.\n"
        "Return ONLY a JSON ARRAY. No markdown. No extra text.\n"
        "Schema:\n"
        "[\n"
        '  {"w":"word","tr":"turkish_ascii","pos":"noun|verb|adj|adv","lvl":"A1|A2|B1|B2|C1"}\n'
        "]\n"
        "Rules:\n"
        "- All items must be unique\n"
        "- No profanity\n"
        "- w is 1 word or max 2-word phrase\n"
        "- tr MUST be Turkish meaning but written in ASCII only (no ğüşöçıİ).\n"
        "  Example: 'ozgurluk', 'guzel', 'cocuk', 'soguk'\n"
        "- pos must be noun|verb|adj|adv\n"
        "- lvl must be A1|A2|B1|B2|C1\n"
        "Target distribution: A1 20%, A2 20%, B1 25%, B2 20%, C1 15%\n"
    )

async def supabase_upload(lang: str, payload: dict):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase env missing (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

    path = f"{lang}.json"
    url = f"{SUPABASE_URL}/storage/v1/object/{LANGPOOL_BUCKET}/{path}"

    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": "application/json",
        "x-upsert": "true",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(url, headers=headers, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Supabase upload failed: {r.status_code} {r.text}")

async def supabase_download(lang: str) -> dict:
    # Public bucket ise public URL ile çekiyoruz
    if not SUPABASE_URL:
        raise HTTPException(status_code=500, detail="SUPABASE_URL missing")
    url = f"{SUPABASE_URL}/storage/v1/object/public/{LANGPOOL_BUCKET}/{lang}.json"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
    if r.status_code != 200:
        return {"lang": lang, "version": 1, "items": []}
    return r.json()

def public_url(lang: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{LANGPOOL_BUCKET}/{lang}.json"

async def gemini_generate_json(user_prompt: str, system_instruction: str, max_tokens: int) -> str:
    messages = [{"role": "user", "content": user_prompt}]
    return await call_gemini(messages, system_instruction=system_instruction, max_tokens=max_tokens)

# ====== ENDPOINTS ======
@router.post("/admin/lang/build", response_model=BuildResp, dependencies=[Depends(require_admin)])
async def build_lang(req: BuildReq):
    lang = (req.lang or "").strip().lower()
    if lang not in LANGS:
        raise HTTPException(status_code=400, detail=f"Unsupported lang: {lang}")

    target = max(50, min(20000, int(req.target)))
    chunk = max(20, min(200, int(req.chunk)))
    max_rounds = max(1, min(200, int(req.max_rounds)))
    version = int(req.version)
    mode = (req.mode or "fill").strip().lower()

    existing = await supabase_download(lang)
    items = existing.get("items", [])
    if not isinstance(items, list):
        items = []

    # normalize existing
    seen = set()
    cleaned_existing: List[Dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        w = str(it.get("w","")).strip()
        tr = str(it.get("tr","")).strip()
        if not w or not tr:
            continue
        k = norm(w)
        if not k or k in seen:
            continue
        seen.add(k)
        cleaned_existing.append({
            "w": w,
            "tr": tr,
            "pos": str(it.get("pos","noun")).strip().lower() or "noun",
            "lvl": str(it.get("lvl","B1")).strip().upper() or "B1",
        })

    items = cleaned_existing

    # mode add: sadece chunk kadar ekle
    if mode == "add":
        target = min(len(items) + chunk, 20000)

    system_instruction = build_system_instruction()

    rounds = 0
    no_progress = 0

    while len(items) < target and rounds < max_rounds:
        rounds += 1
        need = target - len(items)
        ask = chunk if need > chunk else need

        seed = random.randint(1, 10**9)
        user_prompt = build_prompt(LANGS[lang], ask, seed)

        raw_text = await gemini_generate_json(user_prompt, system_instruction, max_tokens=3200)

        # retry parse
        new_batch: List[Dict[str, str]] = []
        for _ in range(3):
            try:
                arr = extract_json_array(raw_text)
                new_batch = sanitize_items(arr)
                if new_batch:
                    break
            except Exception:
                new_batch = []

            seed2 = random.randint(1, 10**9)
            raw_text = await gemini_generate_json(
                f"{LANGS[lang]} dilinde {ask} farkli kelime uret. Seed:{seed2}. ONLY JSON ARRAY!",
                system_instruction,
                max_tokens=3200
            )

        if not new_batch:
            no_progress += 1
            if no_progress >= 5:
                break
            continue

        added = 0
        for it in new_batch:
            k = norm(it["w"])
            if not k or k in seen:
                continue
            seen.add(k)
            items.append(it)
            added += 1

        if added == 0:
            no_progress += 1
            if no_progress >= 5:
                break
        else:
            no_progress = 0

        # her turda upload (kaldığı yerden devam)
        payload = {"lang": lang, "version": version, "items": items[:target]}
        await supabase_upload(lang, payload)

    if len(items) == 0:
        raise HTTPException(status_code=500, detail="No items generated (model did not return parseable JSON).")

    payload = {"lang": lang, "version": version, "items": items[:target]}
    await supabase_upload(lang, payload)

    return BuildResp(lang=lang, target=target, total=min(len(items), target), public_url=public_url(lang))

@router.get("/assets/lang/{lang}.json")
async def get_lang(lang: str):
    lang = (lang or "").strip().lower()
    if lang not in LANGS:
        raise HTTPException(status_code=400, detail="Unsupported lang")
    data = await supabase_download(lang)
    # eğer bucket boşsa 404 yerine boş havuz dönelim
    return data
