import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Any, Tuple

import requests

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")  # Render/Vercel ise değiştir
OUT_DIR = Path("assets/lang")
OUT_DIR.mkdir(parents=True, exist_ok=True)

LANGS = {
    "en": {"name": "İngilizce"},
    "de": {"name": "Almanca"},
    "fr": {"name": "Fransızca"},
    "es": {"name": "İspanyolca"},
    "it": {"name": "İtalyanca"},
}

TARGET = 1000
CHUNK = 250  # 1000 için 4 tur. İstersen 200 yapabilirsin.
MAX_ROUNDS = 12  # çok tekrara düşerse

POS_ALLOWED = {"noun", "verb", "adj", "adv"}
LVL_ALLOWED = {"A1", "A2", "B1", "B2", "C1"}

def norm(s: str) -> str:
    s = (s or "").strip().lower()
    # diacritics strip
    try:
        import unicodedata
        s = "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")
    except Exception:
        pass
    s = re.sub(r"[.,!?;:()\"']", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def call_api(prompt: str, max_tokens: int = 2600) -> str:
    url = f"{BASE_URL}/api/chat"
    r = requests.post(url, json={"text": prompt, "max_tokens": max_tokens}, timeout=120)
    r.raise_for_status()
    data = r.json()
    return str(data.get("text", ""))

def extract_json_array(text: str) -> Any:
    # remove fences
    t = re.sub(r"```json|```", "", text, flags=re.I).strip()
    # take first [ ... last ]
    a = t.find("[")
    b = t.rfind("]")
    if a == -1 or b == -1 or b <= a:
        raise ValueError("JSON array not found")
    t = t[a:b+1]
    # try parse
    try:
        return json.loads(t)
    except Exception:
        # try single-quote fix (last resort)
        t2 = t.replace("'", '"')
        return json.loads(t2)

def clean_items(raw_items: List[Any]) -> List[Dict[str, str]]:
    out = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        w = str(it.get("w", "")).strip()
        tr = str(it.get("tr", "")).strip()
        pos = str(it.get("pos", "")).strip().lower()
        lvl = str(it.get("lvl", "")).strip().upper()

        if not w or not tr:
            continue
        if pos not in POS_ALLOWED:
            # basit normalize
            if pos.startswith("n"): pos = "noun"
            elif pos.startswith("v"): pos = "verb"
            elif pos.startswith("a") and pos != "adv": pos = "adj"
            elif pos.startswith("ad"): pos = "adv"
        if pos not in POS_ALLOWED:
            continue

        if lvl not in LVL_ALLOWED:
            # boşsa B1'e çek
            lvl = "B1"

        out.append({"w": w, "tr": tr, "pos": pos, "lvl": lvl})
    return out

def make_prompt(lang_name: str, n: int, seed: int) -> str:
    # seviyeleri dengeli dağıt
    return f"""
Bana {lang_name} dilinde {n} ADET FARKLI kelime üret.
Sadece tek kelime veya en fazla 2 kelimelik kalıp (ör: 'take off') olsun.
Her madde için:
- w: hedef dilde kelime/kalıp (string)
- tr: Türkçe karşılığı (string)
- pos: noun|verb|adj|adv
- lvl: A1|A2|B1|B2|C1

Zorluk dağılımı:
A1:%20, A2:%20, B1:%25, B2:%20, C1:%15

SADECE JSON ARRAY döndür:
[
  {{ "w":"...", "tr":"...", "pos":"noun", "lvl":"A1" }},
  ...
]

Kurallar:
- Tamamı birbirinden farklı olacak
- Küfür/argo yok
- tr karşılığı kısa ve net
Seed:{seed}
""".strip()

def build_lang(lang_code: str):
    lang_name = LANGS[lang_code]["name"]
    print(f"\n==> {lang_code} ({lang_name})")

    items: List[Dict[str, str]] = []
    seen = set()

    # varsa mevcut dosyadan devam et (opsiyonel)
    out_path = OUT_DIR / f"{lang_code}.json"
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            for it in existing.get("items", []):
                k = norm(it.get("w", ""))
                if k and k not in seen:
                    seen.add(k)
                    items.append({
                        "w": it.get("w",""),
                        "tr": it.get("tr",""),
                        "pos": it.get("pos","noun"),
                        "lvl": it.get("lvl","B1"),
                    })
            print(f"  loaded existing: {len(items)} items")
        except Exception:
            pass

    round_no = 0
    while len(items) < TARGET and round_no < MAX_ROUNDS:
        round_no += 1
        need = TARGET - len(items)
        ask = CHUNK if need > CHUNK else need
        seed = int.from_bytes(os.urandom(4), "big")
        prompt = make_prompt(lang_name, ask, seed)

        print(f"  round {round_no}: requesting {ask} (have {len(items)})")
        txt = call_api(prompt, max_tokens=3000)
        raw = extract_json_array(txt)
        cleaned = clean_items(raw if isinstance(raw, list) else [])

        added = 0
        for it in cleaned:
            k = norm(it["w"])
            if not k or k in seen:
                continue
            seen.add(k)
            items.append(it)
            added += 1

        print(f"    parsed {len(cleaned)} valid, added {added}, total {len(items)}")

        # çok az ekleniyorsa chunk düşür
        if added < max(10, ask * 0.25) and CHUNK > 150:
            print("    low yield -> consider lowering CHUNK in script (e.g., 200/150)")

    if len(items) < TARGET:
        print(f"!! WARNING: {lang_code} ended with {len(items)} items (target {TARGET})")

    payload = {"lang": lang_code, "version": 1, "items": items[:TARGET]}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  wrote: {out_path} ({len(payload['items'])})")

def main():
    # hızlı check
    try:
        r = requests.get(BASE_URL, timeout=10)
        print(f"BASE_URL reachable? {BASE_URL} -> {r.status_code}")
    except Exception as e:
        print(f"BASE_URL check failed: {BASE_URL} ({e})")
        print("Devam ediyorum; /api/chat çalışıyorsa sorun yok.")

    for lc in LANGS.keys():
        build_lang(lc)

if __name__ == "__main__":
    main()
