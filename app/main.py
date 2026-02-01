from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Italky API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "service": "italky-api"}

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/api/translate")
async def translate(payload: dict):
    """
    Şimdilik dummy.
    Sonra Google Translate API bağlayacağız.
    """
    text = (payload.get("text") or "").strip()
    target = (payload.get("target") or "tr").strip()
    return {"ok": True, "translated": f"[{target}] {text}"}
