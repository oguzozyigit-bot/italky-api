import json, os, re, time, random
from pathlib import Path
from typing import Dict, List, Any
import requests

# === AYARLAR ===
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")  # Render ise domain
OUT_DIR = Path("assets/lang")
OUT_DIR.mkdir(parents=True, exist_ok=True)

LANGS = {
  "en": {"name":"İngilizce"},
  "de": {"name":"Almanca"},
  "fr": {"name":"Fransızca"},
  "es": {"name":"İspanyolca"},
  "it": {"name":"İtalyanca"},
}

TARGET_PER_LANG = int(os.getenv("TARGET_PER_LANG", "1000"))   # 1000 / 2000 vb
CHUNK = int(os.getenv("CHUNK", "250"))                        # 200-300 ideal
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "30"))               # takılmasın diye üst sınır
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.3"))              # rate limit yumuşatma

POS_ALLOWED = {"noun","verb","adj","adv"}
LVL_ALLOWED = {"A1","A2","B1","B2","C1"}

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

def call_chat(prompt: str, max_tokens: int = 3000) -> str:
  url = f"{BASE_URL}/api/chat"
  r = requests.post(url, json={"text": prompt, "max_tokens": max_tokens}, timeout=180)
  r.raise_for_status()
  j = r.json()
  return str(j.get("text",""))

def extract_json_array(text: str) -> Any:
  t = re.sub(r"```json|```", "", text, flags=re.I).strip()
  a = t.find("[")
  b = t.rfind("]")
  if a == -1 or b == -1 or b <= a:
    raise ValueError("JSON array not found in response")
  t = t[a:b+1]
  try:
    return json.loads(t)
  except Exception:
    # son çare: tek tırnak düzeltme
    t2 = t.replace("'", '"')
    return json.loads(t2)

def sanitize_items(arr: Any) -> List[Dict[str,str]]:
  if not isinstance(arr, list):
    return []
  out: List[Dict[str,str]] = []
  for it in arr:
    if not isinstance(it, dict):
      continue
    w = str(it.get("w","")).strip()
    tr = str(it.get("tr","")).strip()
    pos = str(it.get("pos","")).strip().lower()
    lvl = str(it.get("lvl","")).strip().upper()

    if not w or not tr:
      continue

    if pos not in POS_ALLOWED:
      # normalize tahmini
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
  # dağılımı dengeli iste
  return f"""
Bana {lang_name} dilinde {n} ADET FARKLI kelime üret.
- Tek kelime veya en fazla 2 kelimelik kalıp (örn: "take off") olabilir.
- Küfür/argo yok.
- Her madde şu alanları içersin:
  w (kelime), tr (Türkçe), pos (noun|verb|adj|adv), lvl (A1|A2|B1|B2|C1)

Seviye dağılımı:
A1 %20, A2 %20, B1 %25, B2 %20, C1 %15

SADECE JSON ARRAY döndür:
[
  {{ "w":"...", "tr":"...", "pos":"noun", "lvl":"A1" }},
  ...
]

Kurallar:
- Hepsi birbirinden farklı olsun
- tr kısa ve net olsun
Seed:{seed}
""".strip()

def read_existing(lang_code: str) -> List[Dict[str,str]]:
  p = OUT_DIR / f"{lang_code}.json"
  if not p.exists():
    return []
  try:
    data = json.loads(p.read_text(encoding="utf-8"))
    items = data.get("items", [])
    if isinstance(items, list):
      # hafif normalize
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
  return []

def write_lang(lang_code: str, items: List[Dict[str,str]]):
  payload = {"lang": lang_code, "version": 1, "items": items}
  (OUT_DIR / f"{lang_code}.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8"
  )

def build_lang(lang_code: str):
  lang_name = LANGS[lang_code]["name"]
  print(f"\n==> {lang_code} ({lang_name})")

  items = read_existing(lang_code)
  seen = set(norm(it["w"]) for it in items if it.get("w"))
  print(f"  existing: {len(items)}")

  rounds = 0
  while len(items) < TARGET_PER_LANG and rounds < MAX_ROUNDS:
    rounds += 1
    need = TARGET_PER_LANG - len(items)
    ask = min(CHUNK, need)
    seed = random.randint(1, 10**9)
    prompt = build_prompt(lang_name, ask, seed)

    print(f"  round {rounds}: ask {ask} | have {len(items)}/{TARGET_PER_LANG}")
    try:
      txt = call_chat(prompt, max_tokens=3200)
      arr = extract_json_array(txt)
      cleaned = sanitize_items(arr)
    except Exception as e:
      print(f"    !! error: {e}")
      time.sleep(SLEEP_SEC)
      continue

    added = 0
    for it in cleaned:
      k = norm(it["w"])
      if not k or k in seen:
        continue
      seen.add(k)
      items.append(it)
      added += 1

    print(f"    parsed={len(cleaned)} added={added} total={len(items)}")
    write_lang(lang_code, items[:TARGET_PER_LANG])  # ara kaydet
    time.sleep(SLEEP_SEC)

    # verim düşükse chunk küçültme ipucu
    if added < max(10, int(ask*0.3)):
      print("    (verim düşük -> CHUNK=200 yapmayı düşünebilirsin)")

  if len(items) < TARGET_PER_LANG:
    print(f"  !! target olmadı: {len(items)}/{TARGET_PER_LANG} (MAX_ROUNDS bitti)")
  else:
    print(f"  ✅ done: {len(items)}/{TARGET_PER_LANG}")

  write_lang(lang_code, items[:TARGET_PER_LANG])

def main():
  print(f"BASE_URL = {BASE_URL}")
  # /api/chat kontrolü (fail olursa yine deneyecek)
  for lc in LANGS.keys():
    build_lang(lc)

  print("\n✅ bitti. Dosyalar:")
  for lc in LANGS.keys():
    print(f"  - assets/lang/{lc}.json")

if __name__ == "__main__":
  main()
