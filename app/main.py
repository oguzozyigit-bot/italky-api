from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import os
from app.routers.auth import router as auth_router
from app.routers.content import router as content_router
from app.routers.progress import router as progress_router
from app.routers.ai import router as ai_router
from app.routers.stt import router as stt_router
from app.routers.tts import router as tts_router
from app.routers.admin import router as admin_router
from app.routers.account import router as account_router
from app.routers.session import router as session_router
from app.routers.translator import router as translator_router
from app.routers.support import router as support_router
from app.routers.conversation import router as conversation_router
from app.routers.paytr import router as paytr_router
from app.routers.promo import router as promo_router
from app.routers.billing_google import router as billing_google_router
from app.routers.google_play_entitlement import router as google_play_entitlement_router
from app.routers.trendyol import router as trendyol_router
from app.routers.adapty import router as adapty_router

app = FastAPI(title="italky AI API", version="0.1.0")

# Session middleware for OAuth state
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-session-secret-change-in-prod"),
    same_site="lax",
    https_only=False,  # Render handles HTTPS termination
    max_age=3600,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router)
app.include_router(content_router)
app.include_router(progress_router)
app.include_router(ai_router)
app.include_router(stt_router)
app.include_router(tts_router)
app.include_router(admin_router)
app.include_router(account_router)
app.include_router(session_router)
app.include_router(translator_router)
app.include_router(support_router)
app.include_router(conversation_router)
app.include_router(paytr_router)
app.include_router(promo_router)
app.include_router(google_play_entitlement_router)
app.include_router(billing_google_router)
app.include_router(trendyol_router)
app.include_router(adapty_router)


@app.get("/")
async def root():
    return {"message": "italky AI API", "version": "0.1.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
