from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["billing-google"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

PRODUCT_TOKENS = {
    "jeton_10": 10,
    "jeton_20": 20,
    "jeton_50": 50,
    "jeton_100": 100,
    "jeton_250": 250,
    "jeton_500": 500,
}

PLAYSTORE_PACKAGES: Dict[str, Dict[str, Any]] = {
    "edu_699": {
        "code": "edu_699",
        "name": "Online Dil Eğitim Asistanı",
        "duration_days": 365,
        "language_limit": 0,
        "jeton_amount": 100,
        "can_use_text_to_text": True,
        "can_use_face_to_face": False,
        "can_use_side_to_side": False,
        "can_use_offline": False,
        "can_use_clone_voice": False,
        "source_type": "playstore",
    },
    "translate_699": {
        "code": "translate_699",
        "name": "Cebinizdeki Tercüman",
        "duration_days": 365,
        "language_limit": 0,
        "jeton_amount": 100,
        "can_use_text_to_text": True,
        "can_use_face_to_face": True,
        "can_use_side_to_side": True,
        "can_use_offline": True,
        "can_use_clone_voice": False,
        "source_type": "playstore",
    },
    "premium_999": {
        "code": "premium_999",
        "name": "Premium Üyelik",
        "duration_days": 365,
        "language_limit": 0,
        "jeton_amount": 100,
        "can_use_text_to_text": True,
        "can_use_face_to_face": True,
        "can_use_side_to_side": True,
        "can_use_offline": True,
        "can_use_clone_voice": True,
        "source_type": "playstore",
    },
}


class GoogleBillingConfirmReq(BaseModel):
    user_id: str
    product_id: str
    purchase_token: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _safe_data(res: Any):
    return getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)


def _profile_or_404(user_id: str) -> Dict[str, Any]:
    prof = (
        supabase.table("profiles")
        .select("id,tokens,package_active,package_ends_at,selected_package_code")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(prof) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")
    return rows[0] or {}


def _has_active_package(profile_row: Dict[str, Any]) -> bool:
    if not bool(profile_row.get("package_active")):
        return False

    raw_end = profile_row.get("package_ends_at")
    if not raw_end:
        return True

    try:
        end_dt = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
        return end_dt > _now()
    except Exception:
        return False


def _purchase_exists(purchase_token: str) -> bool:
    existing = (
        supabase.table("billing_purchases")
        .select("id")
        .eq("purchase_token", purchase_token)
        .limit(1)
        .execute()
    )
    return bool(_safe_data(existing))


def _insert_purchase_log(user_id: str, product_id: str, amount: int, purchase_token: str):
    supabase.table("billing_purchases").insert(
        {
            "user_id": user_id,
            "product_id": product_id,
            "amount": amount,
            "purchase_token": purchase_token,
            "provider": "google_play",
        }
    ).execute()


def _load_package_from_db_or_defaults(product_id: str) -> Dict[str, Any]:
    db_res = (
        supabase.table("nfc_packages")
        .select("*")
        .eq("code", product_id)
        .limit(1)
        .execute()
    )
    db_rows = _safe_data(db_res) or []
    if db_rows:
        row = db_rows[0] or {}
        if not bool(row.get("is_active", True)):
            raise HTTPException(status_code=400, detail="package not active")
        return row

    fallback = PLAYSTORE_PACKAGES.get(product_id)
    if not fallback:
        raise HTTPException(status_code=400, detail="invalid package product_id")
    return fallback


def _expire_old_playstore_entitlements(user_id: str):
    try:
        supabase.table("nfc_entitlements").update(
            {
                "status": "expired",
                "updated_at": _iso(_now()),
            }
        ).eq("user_id", user_id).eq("source_type", "playstore").eq("status", "active").execute()
    except Exception:
        pass


def _create_playstore_entitlement(user_id: str, package_row: Dict[str, Any], purchase_token: str):
    start_dt = _now()
    duration_days = int(package_row.get("duration_days") or 365)
    end_dt = start_dt + timedelta(days=duration_days)

    entitlement = {
        "user_id": user_id,
        "card_uid": None,
        "package_code": str(package_row.get("code")),
        "started_at": _iso(start_dt),
        "expires_at": _iso(end_dt),
        "remaining_languages": int(package_row.get("language_limit") or 0),
        "remaining_jeton": int(package_row.get("jeton_amount") or 0),
        "can_use_text_to_text": bool(package_row.get("can_use_text_to_text", True)),
        "can_use_face_to_face": bool(package_row.get("can_use_face_to_face", False)),
        "can_use_side_to_side": bool(package_row.get("can_use_side_to_side", False)),
        "can_use_offline": bool(package_row.get("can_use_offline", False)),
        "can_use_clone_voice": bool(package_row.get("can_use_clone_voice", False)),
        "status": "active",
        "source_type": "playstore",
        "purchase_token": purchase_token,
        "granted_by": "system_playstore",
        "note": f"playstore:{package_row.get('code')}",
    }

    _expire_old_playstore_entitlements(user_id)

    ins = supabase.table("nfc_entitlements").insert(entitlement).execute()
    return entitlement, _safe_data(ins)


def _apply_package_to_profile(user_id: str, package_row: Dict[str, Any]):
    prof = _profile_or_404(user_id)
    current_tokens = int(prof.get("tokens") or 0)
    bonus = int(package_row.get("jeton_amount") or 0)

    start_dt = _now()
    end_dt = start_dt + timedelta(days=int(package_row.get("duration_days") or 365))

    update_payload = {
        "selected_package_code": str(package_row.get("code")),
        "package_active": True,
        "package_started_at": _iso(start_dt),
        "package_ends_at": _iso(end_dt),
        "tokens": current_tokens + bonus,
    }

    supabase.table("profiles").update(update_payload).eq("id", user_id).execute()
    return current_tokens + bonus, _iso(start_dt), _iso(end_dt)


@router.post("/api/billing/google/confirm")
async def billing_google_confirm(req: GoogleBillingConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")

    amount = PRODUCT_TOKENS.get(product_id)
    if not amount:
        raise HTTPException(status_code=400, detail="invalid product_id")

    if _purchase_exists(purchase_token):
        prof = _profile_or_404(user_id)
        return {
            "ok": True,
            "already_processed": True,
            "tokens": int(prof.get("tokens") or 0),
        }

    prof = _profile_or_404(user_id)

    if not _has_active_package(prof):
        raise HTTPException(status_code=403, detail="active package required before token purchase")

    current_tokens = int(prof.get("tokens") or 0)
    next_tokens = current_tokens + amount

    supabase.table("profiles").update({"tokens": next_tokens}).eq("id", user_id).execute()
    _insert_purchase_log(user_id, product_id, amount, purchase_token)

    return {"ok": True, "tokens": next_tokens}


@router.post("/api/billing/google/package")
async def billing_google_package(req: GoogleBillingConfirmReq):
    user_id = (req.user_id or "").strip()
    product_id = (req.product_id or "").strip()
    purchase_token = (req.purchase_token or "").strip()

    if not user_id:
        raise HTTPException(status_code=422, detail="user_id required")
    if not product_id:
        raise HTTPException(status_code=422, detail="product_id required")
    if not purchase_token:
        raise HTTPException(status_code=422, detail="purchase_token required")

    if _purchase_exists(purchase_token):
        prof = _profile_or_404(user_id)
        return {
            "ok": True,
            "already_processed": True,
            "package_code": prof.get("selected_package_code"),
            "tokens": int(prof.get("tokens") or 0),
            "package_active": bool(prof.get("package_active")),
        }

    package_row = _load_package_from_db_or_defaults(product_id)
    tokens_after, started_at, expires_at = _apply_package_to_profile(user_id, package_row)
    entitlement_payload, _ = _create_playstore_entitlement(user_id, package_row, purchase_token)
    _insert_purchase_log(user_id, product_id, int(package_row.get("jeton_amount") or 0), purchase_token)

    return {
        "ok": True,
        "package_code": package_row.get("code"),
        "tokens": tokens_after,
        "package_active": True,
        "started_at": started_at,
        "expires_at": expires_at,
        "entitlement": entitlement_payload,
    }


@router.post("/api/billing/google/premium")
async def billing_google_premium(req: GoogleBillingConfirmReq):
    product_id = (req.product_id or "").strip()

    if product_id not in PLAYSTORE_PACKAGES:
        req.product_id = "premium_999"

    return await billing_google_package(req)
