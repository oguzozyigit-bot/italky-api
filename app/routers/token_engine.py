from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException

CHARS_PER_JETON = 1000
ICANY_PERSONAL_SPEND_URL = "https://icany.ai/api/bridge/personal-spend"

VALID_USAGE_TYPES = {
    "ai_text",
    "voice_tts",
    "general",
}


def calc_tokens_for_chars(used_chars: int) -> int:
    used_chars = int(used_chars or 0)
    if used_chars <= 0:
        return 0
    return math.ceil(used_chars / CHARS_PER_JETON)


def _shared_wallet_spend(
    jwt_token: str,
    amount: int,
    module: str,
    request_id: str,
    note: str = "",
) -> Dict[str, Any]:
    payload = json.dumps(
        {
            "amount": int(amount),
            "module": str(module or "usage"),
            "requestId": str(request_id or ""),
            "note": str(note or ""),
        }
    ).encode("utf-8")
    request = Request(
        ICANY_PERSONAL_SPEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            data = {}
        if exc.code == 402 or data.get("code") == "INSUFFICIENT_TOKENS":
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "INSUFFICIENT_TOKENS",
                    "message": data.get("error") or "Jeton yetersiz.",
                    "required_tokens": amount,
                    "tokens_after": data.get("tokenBalance", 0),
                },
            )
        raise HTTPException(status_code=502, detail=data.get("error") or "Ortak cüzdan yanıt vermedi")
    except (URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Ortak cüzdana ulaşılamadı: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ortak cüzdan hatası: {exc}")

    if not data.get("ok"):
        raise HTTPException(status_code=502, detail=data.get("error") or "Ortak cüzdan işlemi başarısız")
    return data


def spend_chars(
    user_id: str,
    used_chars: int,
    usage_type: str = "general",
    extra_meta: Optional[Dict[str, Any]] = None,
    jwt_token: str = "",
    request_id: str = "",
) -> Dict[str, Any]:
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not jwt_token:
        raise HTTPException(status_code=401, detail="Authorization token required")
    if usage_type not in VALID_USAGE_TYPES:
        raise HTTPException(status_code=400, detail="invalid usage_type")

    used_chars = int(used_chars or 0)
    if used_chars <= 0:
        return {
            "ok": True,
            "charged_tokens": 0,
            "module": usage_type,
            "tokens_before": None,
            "tokens_after": None,
            "chars_per_jeton": CHARS_PER_JETON,
        }

    charged_tokens = calc_tokens_for_chars(used_chars)
    meta = extra_meta or {}
    shared = _shared_wallet_spend(
        jwt_token=jwt_token,
        amount=charged_tokens,
        module=str(meta.get("original_module") or usage_type),
        request_id=request_id,
        note=str(meta.get("note") or ""),
    )

    return {
        "ok": True,
        "charged_tokens": int(shared.get("chargedTokens") or charged_tokens),
        "module": usage_type,
        "tokens_before": shared.get("tokensBefore"),
        "tokens_after": shared.get("tokensAfter", shared.get("tokenBalance")),
        "chars_per_jeton": CHARS_PER_JETON,
        "wallet": "icany_personal",
        "idempotent_replay": bool(shared.get("idempotentReplay")),
    }
