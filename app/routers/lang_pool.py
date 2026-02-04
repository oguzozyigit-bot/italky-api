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

# ✅ chat.py içinden direkt gemini çağrısını kullanacağız (API /chat değil!)
from app.routers.chat import call_gemini

router = APIRouter()

# ====== AYARLAR ======
ADMIN_SECRET = (os.getenv("ADMIN_SECRET", "") or "").strip()

STATIC_DIR = Path(os.getenv("LANGPOOL_STATIC_DIR", "static")).resolve()
LANG_DIR = STATIC_DIR / "lang"
LANG_DIR.mkdir(parents=True, exist_ok=True)

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

# ====== MODELLER ======
class BuildReq(BaseModel):
    lang: str
    target: int = 1000
    chunk: int = 120
    max_rounds: int = 60
    version: int = 1
    mode: str = "fill"   # ✅ "fill" veya "add"

class BuildResp(BaseModel):
    lang: str
    target: int
    total: int
    path: str

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
    return f"{lang_name} dilinde {n} farklı kelime üret. Seed:{seed}"

def build_system_instruction() -> str:
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
        "Target level distribution: A1 20%, A2 20%, B1 25%, B2 20%, C1 15%\n"
    )

def load_existing(lang: str) -> List[Dict[str, str]]:
    p = LANG_DIR / f"{lang}.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items", [])
        if not isinstance(items, list):
            return []
        cleaned: List[Dict[str, str]] = []
        for it in items:
            if isinstance(it, dict) and it.get("w") and it.get("tr"):
                cleaned.append({
                    "w": str(it["w"]).strip(),
                    "tr": str(it["tr"]).strip(),
                    "pos": str(it.get("pos", "noun")).strip().lower(),
                    "lvl": str(it.get("lvl", "B1")).strip().upper(),
                })
        return cleaned
    except Exception:
        return []

def write_lang(lang: str, version: int, items: List[Dict[str, str]]):
    try:
        LANG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"lang": lang, "version": version, "items": items}
        (LANG_DIR / f"{lang}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"write_lang failed: {e}")

async def gemini_generate_json(prompt_user: str, system_instruction: str, max_tokens: int) -> str:
    messages = [{"role": "user", "content": prompt_user}]
    return await call_gemini(messages, system_instruction=system_instruction, max_tokens=max_tokens)

# ====== ENDPOINTS ======
@router.post("/admin/lang/build", response_model=BuildResp, dependencies=[Depends(require_admin)])
async def build_lang(req: BuildReq):
    lang = (req.lang or "").strip().lower()
    if lang not in LANGS:
        raise HTTPException(status_code=400, detail=f"Unsupported lang: {lang}")

    target = max(50, min(20000, int(req.target)))
    chunk = max(50, min(200, int(req.chunk)))
    max_rounds = max(3, min(200, int(req.max_rounds)))
    version = int(req.version)

    items = load_existing(lang)
    # mode="add" ise: bu çağrıda sadece chunk kadar ekle, hızlı dön
    if req.mode == "add":
    target = min(len(items) + chunk, 20000)
    seen = set(norm(it["w"]) for it in items if it.get("w"))

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

        # ✅ 3 kez retry
        cleaned: List[Dict[str, str]] = []
        for _ in range(3):
            try:
                arr = extract_json_array(raw_text)
                cleaned = sanitize_items(arr)
                if cleaned:
                    break
            except Exception:
                cleaned = []

            seed2 = random.randint(1, 10**9)
            raw_text = await gemini_generate_json(
                f"{LANGS[lang]} dilinde {ask} farklı kelime üret. Seed:{seed2}. SADECE JSON ARRAY!",
                system_instruction,
                max_tokens=3200
            )

        # ✅ hiç üretemediyse no_progress say
        if not cleaned:
            no_progress += 1
            if no_progress >= 5:
                break
            continue

        added = 0
        for it in cleaned:
            k = norm(it["w"])
            if not k or k in seen:
                continue
            seen.add(k)
            items.append(it)
            added += 1

        write_lang(lang, version, items[:target])

        if added == 0:
            no_progress += 1
            if no_progress >= 5:
                break
        else:
            no_progress = 0

    if len(items) == 0:
        raise HTTPException(status_code=500, detail="No items generated (model did not return parseable JSON).")

    write_lang(lang, version, items[:target])
    return BuildResp(lang=lang, target=target, total=min(len(items), target), path=str(LANG_DIR / f"{lang}.json"))

@router.get("/assets/lang/{lang}.json")
def get_lang(lang: str):
    lang = (lang or "").strip().lower()
    p = LANG_DIR / f"{lang}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    return json.loads(p.read_text(encoding="utf-8"))
