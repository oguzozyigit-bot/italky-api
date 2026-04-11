from __future__ import annotations

import os
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["italkyai-chat"])

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatBody(BaseModel):
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    text: str
    history: List[ChatTurn] = []
    voice_mode: Optional[str] = "tts"


def build_messages(history: List[ChatTurn], user_text: str) -> List[dict]:
    system_prompt = (
        "Sen italkyAI Sohbet AI'sin. "
        "Doğal, sıcak, akıcı ve gerçekten sohbet eden bir asistansın. "
        "Kısa ama ruhsuz cevap verme. "
        "Kullanıcının son söyledikleriyle bağ kur. "
        "Gereksiz resmi dil kullanma. "
        "Türkçe konuş. "
        "Sohbet devamlılığını koru."
    )

    messages: List[dict] = [{"role": "system", "content": system_prompt}]

    for item in history[-10:]:
        content = (item.content or "").strip()
        if not content:
            continue
        role = "assistant" if item.role == "assistant" else "user"
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_text.strip()})
    return messages


def call_gemini(messages: List[dict]) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        prompt = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages
        )

        result = model.generate_content(prompt)
        text = (getattr(result, "text", "") or "").strip()
        return text or None
    except Exception as e:
        print("Gemini chat error:", e)
        return None


def call_openai(messages: List[dict]) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=OPENAI_API_KEY)

        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,
        )

        text = (completion.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print("OpenAI chat error:", e)
        return None


@router.post("/api/italkyai/chat")
async def italkyai_chat(body: ChatBody):
    user_text = (body.text or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="empty_text")

    messages = build_messages(body.history, user_text)

    reply = call_gemini(messages)
    model_used = "gemini"

    if not reply:
        reply = call_openai(messages)
        model_used = "openai"

    if not reply:
        return {
            "ok": False,
            "reply": "Şu an cevap üretirken bir sorun oluştu. Bir daha dener misin?"
        }

    return {
        "ok": True,
        "reply": reply,
        "model": model_used
    }
