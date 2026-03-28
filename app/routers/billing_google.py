from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import Client, create_client

router = APIRouter(tags=["billing-google"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Jeton ürünleri
PRODUCT_TOKENS = {
    "jeton_10": 10,
    "jeton_20": 20,
    "jeton_50": 50,
    "jeton_100": 100,
    "jeton_250": 250,
    "jeton_500": 500,
}

# Paket fiyatları ve varsayılan bilgiler
# Not:
# - Upgrade sırasında yeni paket bonus jetonu VERİLMEZ
# - Kalan gün değeri jetona çevrilir
PLAYSTORE_PACKAGES: Dict[str, Dict[str, Any]] = {
    "edu_699": {
        "code": "edu_699",
        "name": "Online Dil Eğitim Asistanı",
        "price_tl": 699.0,
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
        "price_tl": 699.0,
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
        "price_tl": 999.0,
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

# 1 jeton kaç TL
TOKEN_UNIT_TL = 2.9


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


def _insert_wallet_tx(user_id: str, tx_type: str, amount: int, balance_after: int, note: str):
    try:
        supabase.table("wallet_tx").insert(
            {
                "user_id": user_id,
                "type": tx_type,
                "amount": amount,
                "balance_after": balance_after,
                "note": note,
            }
        ).execute()
    except Exception:
        pass


def _profile_or_404(user_id: str) -> Dict[str, Any]:
    prof = (
        supabase.table("profiles")
        .select(
            "id,tokens,package_active,package_started_at,package_ends_at,"
            "selected_package_code"
        )
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(prof) or []
    if not rows:
        raise HTTPException(status_code=404, detail="profile not found")
    return rows[0] or {}


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


def _has_active_package(profile_row: Dict[str, Any]) -> bool:
    if not bool(profile_row.get("package_active")):
        return False

    end_dt = _parse_dt(profile_row.get("package_ends_at"))
    if not end_dt:
        return True

    return end_dt > _now()


def _get_remaining_days(profile_row: Dict[str, Any]) -> int:
    end_dt = _parse_dt(profile_row.get("package_ends_at"))
    if not end_dt:
      return 0

    diff = end_dt - _now()
    days = diff.days
    return max(0, days)


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

        # DB'de price_tl yoksa fallback'ten al
        fallback = PLAYSTORE_PACKAGES.get(product_id, {})
        row["price_tl"] = float(row.get("price_tl") or fallback.get("price_tl") or 0)
        row["source_type"] = "playstore"
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


def _expire_all_active_entitlements(user_id: str):
    try:
        supabase.table("nfc_entitlements").update(
            {
                "status": "expired",
                "updated_at": _iso(_now()),
            }
        ).eq("user_id", user_id).eq("status", "active").execute()
    except Exception:
        pass


def _create_playstore_entitlement(
    user_id: str,
    package_row: Dict[str, Any],
    purchase_token: str,
    remaining_jeton_for_new_entitlement: int
):
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
        "remaining_jeton": int(remaining_jeton_for_new_entitlement),
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

    ins = supabase.table("nfc_entitlements").insert(entitlement).execute()
    return entitlement, _safe_data(ins)


def _apply_package_to_profile(
    user_id: str,
    package_row: Dict[str, Any],
    final_tokens: int
):
    start_dt = _now()
    end_dt = start_dt + timedelta(days=int(package_row.get("duration_days") or 365))

    update_payload = {
        "selected_package_code": str(package_row.get("code")),
        "package_active": True,
        "package_started_at": _iso(start_dt),
        "package_ends_at": _iso(end_dt),
        "tokens": final_tokens,
    }

    supabase.table("profiles").update(update_payload).eq("id", user_id).execute()
    return _iso(start_dt), _iso(end_dt)


def _calculate_upgrade_credit_tokens(
    old_package_code: str,
    remaining_days: int,
) -> int:
    old_pkg = _load_package_from_db_or_defaults(old_package_code)

    old_price = float(old_pkg.get("price_tl") or 0)
    old_duration = int(old_pkg.get("duration_days") or 365)

    if old_price <= 0 or old_duration <= 0 or remaining_days <= 0:
        return 0

    daily_value = old_price / old_duration
    remaining_value = daily_value * remaining_days
    credit_tokens = int(-(-remaining_value // TOKEN_UNIT_TL))  # ceil için
    return max(0, credit_tokens)


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
    _insert_wallet_tx(user_id, "purchase", amount, next_tokens, f"Jeton satın alma: {product_id}")

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
    profile = _profile_or_404(user_id)

    current_tokens = int(profile.get("tokens") or 0)
    final_tokens = current_tokens
    upgrade_credit_tokens = 0
    is_upgrade = False

    old_package_code = str(profile.get("selected_package_code") or "").strip()

    # Kullanıcının aktif paketi varsa upgrade say
    if _has_active_package(profile) and old_package_code and old_package_code != product_id:
        remaining_days = _get_remaining_days(profile)
        upgrade_credit_tokens = _calculate_upgrade_credit_tokens(
            old_package_code=old_package_code,
            remaining_days=remaining_days,
        )
        final_tokens = current_tokens + upgrade_credit_tokens
        is_upgrade = True

        if upgrade_credit_tokens > 0:
            _insert_wallet_tx(
                user_id=user_id,
                tx_type="upgrade_credit",
                amount=upgrade_credit_tokens,
                balance_after=final_tokens,
                note=f"Upgrade kredisi: {old_package_code} -> {product_id}",
            )

    # Aynı paketi yeniden alıyorsa renewal gibi davranıp bonus ekleme
    elif _has_active_package(profile) and old_package_code == product_id:
        is_upgrade = True
        # renewal gibi ama senin kurala göre yine bonus yok
        final_tokens = current_tokens

    else:
        # İlk kez paket alıyorsa bonus ver
        package_bonus = int(package_row.get("jeton_amount") or 0)
        final_tokens = current_tokens + package_bonus

        if package_bonus > 0:
            _insert_wallet_tx(
                user_id=user_id,
                tx_type="package_bonus",
                amount=package_bonus,
                balance_after=final_tokens,
                note=f"Paket bonusu: {product_id}",
            )

    # Eski entitlement kapat
    _expire_all_active_entitlements(user_id)

    # Profili güncelle
    started_at, expires_at = _apply_package_to_profile(
        user_id=user_id,
        package_row=package_row,
        final_tokens=final_tokens
    )

    # Yeni entitlement aç
    entitlement_payload, _ = _create_playstore_entitlement(
        user_id=user_id,
        package_row=package_row,
        purchase_token=purchase_token,
        remaining_jeton_for_new_entitlement=final_tokens
    )

    # Satın alma log
    _insert_purchase_log(
        user_id=user_id,
        product_id=product_id,
        amount=0 if is_upgrade else int(package_row.get("jeton_amount") or 0),
        purchase_token=purchase_token
    )

    return {
        "ok": True,
        "package_code": package_row.get("code"),
        "tokens": final_tokens,
        "package_active": True,
        "started_at": started_at,
        "expires_at": expires_at,
        "upgrade_credit_tokens": upgrade_credit_tokens,
        "is_upgrade": is_upgrade,
        "entitlement": entitlement_payload,
    }


@router.post("/api/billing/google/premium")
async def billing_google_premium(req: GoogleBillingConfirmReq):
    product_id = (req.product_id or "").strip()
    if product_id not in PLAYSTORE_PACKAGES:
        req.product_id = "premium_999"

    return await billing_google_package(req)
