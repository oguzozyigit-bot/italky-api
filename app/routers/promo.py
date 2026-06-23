from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from supabase import Client, create_client

PromoTableFound = Literal["promo_codes", "web_promo_codes"]
PromoLookupKind = Literal["campaign", "simple", "web"]

router = APIRouter(prefix="/api/promo", tags=["promo"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
API_PUBLIC_BASE = os.getenv("API_PUBLIC_BASE", "https://italky-api.onrender.com").rstrip("/")
IOS_APP_STORE_URL = os.getenv(
    "PROMO_IOS_APP_STORE_URL",
    "https://apps.apple.com/app/italkyai/id6768123713",
).strip()
ANDROID_PLAY_STORE_URL = os.getenv(
    "PROMO_ANDROID_PLAY_STORE_URL",
    "https://play.google.com/store/apps/details?id=com.ozyigits.italkyai",
).strip()
PROMO_DEEP_LINK_FALLBACK_MS = int(os.getenv("PROMO_DEEP_LINK_FALLBACK_MS", "1500") or 1500)

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class PromoRedeemRequest(BaseModel):
    source: Literal["manual", "nfc"] = "manual"
    user_id: Optional[str] = None
    code: Optional[str] = None
    uid: Optional[str] = None


class PromoRedeemResponse(BaseModel):
    ok: bool
    reason: Optional[str] = None
    grant_type: Optional[str] = None
    membership_months: int = 0
    membership_days: int = 0
    package_code: Optional[str] = None
    membership_started_at: Optional[str] = None
    membership_ends_at: Optional[str] = None
    tokens_loaded: int = 0
    tokens_after: Optional[int] = None
    message: Optional[str] = None
    table_found: Optional[PromoTableFound] = None
    app_deep_link: Optional[str] = None
    fallback_store_url: Optional[str] = None
    redirect_url: Optional[str] = None


@dataclass
class PromoLookupResult:
    table_found: PromoTableFound
    kind: PromoLookupKind
    normalized_code: str
    campaign_record: Optional[dict] = None
    simple_record: Optional[dict] = None
    web_record: Optional[dict] = None


def promo_log(label: str, data: dict | None = None) -> None:
    try:
        print(f"[Promo] {label} {data or {}}")
    except Exception:
        pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def clean(value: object) -> str:
    return str(value or "").strip()


def normalize_promo_code(code: Optional[str]) -> str:
    return str(code or "").strip().upper()


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def add_months_safe(dt: datetime, months: int) -> datetime:
    if months <= 0:
        return dt
    year = dt.year + ((dt.month - 1 + months) // 12)
    month = ((dt.month - 1 + months) % 12) + 1
    month_days = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(dt.day, month_days[month - 1])
    return dt.replace(year=year, month=month, day=day)


def require_auth_user_id(auth_header: Optional[str]) -> str:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="MISSING_AUTH")
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="MISSING_AUTH")
    try:
        user_res = supabase.auth.get_user(token)
        user = getattr(user_res, "user", None)
        user_id = clean(getattr(user, "id", "")) if user else ""
    except Exception as exc:
        promo_log("auth invalid", {"message": str(exc)})
        raise HTTPException(status_code=401, detail="INVALID_AUTH")
    if not user_id:
        raise HTTPException(status_code=401, detail="INVALID_AUTH")
    return user_id


def resolve_redeem_user_id(payload_user_id: Optional[str], auth_header: Optional[str]) -> str:
    auth_user_id = require_auth_user_id(auth_header)
    requested_user_id = clean(payload_user_id)
    if requested_user_id and requested_user_id != auth_user_id:
        promo_log("user id mismatch", {"auth_user_id": auth_user_id, "payload_user_id": requested_user_id})
        raise HTTPException(status_code=403, detail="user_id_mismatch")
    return auth_user_id


def get_profile(user_id: str) -> dict:
    res = supabase.table("profiles").select(
        "id, email, tokens, selected_package_code, package_started_at, package_ends_at, "
        "promo_used_at, promo_code_used, has_ever_paid, "
        "membership_status, membership_source, membership_product_id, membership_started_at, "
        "membership_ends_at, membership_last_checked_at, trial_started_at, trial_ends_at, "
        "trial_used, plan, app_access_mode"
    ).eq("id", user_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="PROFILE_NOT_FOUND")
    return rows[0]


def promo_row_code_value(code_rec: dict) -> str:
    return normalize_promo_code(code_rec.get("code_value") or code_rec.get("code"))


def is_missing_column_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "42703" in text
        or "does not exist" in text
        or ("column" in text and ("code_value" in text or ".code" in text or " code " in text))
    )


def lookup_code_in_table(
    table: PromoTableFound,
    code: Optional[str],
    *,
    delivery_type: Optional[str] = None,
    kind: str = "any",
) -> Optional[dict]:
    final_code = normalize_promo_code(code)
    if not final_code:
        return None

    columns = ("code_value", "code")
    for column in columns:
        promo_log("query table", {
            "table": table,
            "column": column,
            "value": final_code,
            "delivery_type": delivery_type,
            "kind": kind,
        })

        try:
            query = supabase.table(table).select("*").eq(column, final_code)
            if delivery_type:
                query = query.eq("delivery_type", delivery_type)
            res = query.limit(1).execute()
            rows = res.data or []
            if rows:
                promo_log("lookup hit", {
                    "table_found": table,
                    "kind": kind,
                    "column": column,
                    "value": final_code,
                    "response": "hit",
                    "row_id": rows[0].get("id"),
                })
                return rows[0]

            promo_log("lookup miss", {
                "table": table,
                "kind": kind,
                "column": column,
                "value": final_code,
                "response": "miss",
            })
        except Exception as exc:
            if column == "code_value" and is_missing_column_error(exc):
                promo_log("code_value column unavailable; falling back to code", {
                    "table": table,
                    "kind": kind,
                    "message": str(exc),
                })
                continue
            promo_log("lookup error", {
                "table": table,
                "kind": kind,
                "column": column,
                "value": final_code,
                "response": "error",
                "message": str(exc),
            })
            if column == "code":
                break

    promo_log("lookup exhausted", {
        "table": table,
        "kind": kind,
        "value": final_code,
        "response": "miss",
        "columns_tried": list(columns),
    })
    return None


def is_simple_promo_row(row: dict) -> bool:
    if clean(row.get("campaign_id")):
        return False
    if safe_int(row.get("duration_days") or row.get("days"), 0) > 0:
        return True
    status = str(row.get("status") or "").strip().lower()
    return status in {"active", "aktif", "used"}


def get_code_record(source: str, code: Optional[str], uid: Optional[str]) -> Optional[dict]:
    if source == "manual":
        final_code = normalize_promo_code(code)
        if not final_code:
            raise HTTPException(status_code=400, detail="CODE_REQUIRED")

        web_rec = lookup_code_in_table("web_promo_codes", final_code, kind="campaign")
        if web_rec:
            campaign_like = web_promo_as_campaign_record(web_rec)
            if campaign_like:
                return campaign_like

        return lookup_code_in_table("promo_codes", final_code, delivery_type="manual", kind="campaign")

    if source == "nfc":
        final_uid = str(uid or "").strip()
        if not final_uid:
            raise HTTPException(status_code=400, detail="UID_REQUIRED")

        promo_log("query table", {"table": "promo_codes", "nfc_uid": final_uid, "delivery_type": "nfc"})
        res = supabase.table("promo_codes").select(
            "id, campaign_id, code_value, delivery_type, nfc_uid, is_active, is_used, used_by, used_at, bound_user_id, marketplace"
        ).eq("delivery_type", "nfc").eq("nfc_uid", final_uid).limit(1).execute()
        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
        return rows[0]

    raise HTTPException(status_code=400, detail="INVALID_SOURCE")


def lookup_simple_code_record(code: Optional[str]) -> Optional[dict]:
    final_code = normalize_promo_code(code)
    if not final_code:
        return None

    web_rec = lookup_code_in_table("web_promo_codes", final_code, kind="simple")
    if web_rec and is_simple_promo_row(web_rec):
        return web_rec

    promo_rec = lookup_code_in_table("promo_codes", final_code, kind="simple")
    if promo_rec and is_simple_promo_row(promo_rec):
        return promo_rec

    return None


def web_promo_as_campaign_record(web_rec: dict) -> Optional[dict]:
    campaign_id = clean(web_rec.get("campaign_id"))
    if not campaign_id:
        return None

    code_value = web_promo_code_value(web_rec)
    if not code_value:
        return None

    status = str(web_rec.get("status") or "").strip().lower()
    is_active_raw = web_rec.get("is_active")
    if is_active_raw is None:
        is_active = status in {"active", "aktif"}
    else:
        is_active = bool(is_active_raw)

    return {
        "id": web_rec.get("id"),
        "campaign_id": campaign_id,
        "code_value": code_value,
        "delivery_type": str(web_rec.get("delivery_type") or "manual").strip() or "manual",
        "nfc_uid": web_rec.get("nfc_uid"),
        "is_active": is_active,
        "is_used": bool(web_rec.get("is_used", False)),
        "used_by": web_rec.get("used_by"),
        "used_at": web_rec.get("used_at"),
        "bound_user_id": web_rec.get("bound_user_id"),
        "marketplace": web_rec.get("marketplace"),
        "_web_promo_source": True,
    }


def resolve_manual_promo_lookup(code: Optional[str]) -> PromoLookupResult:
    final_code = normalize_promo_code(code)
    if not final_code:
        raise HTTPException(status_code=400, detail="CODE_REQUIRED")

    # 1) web_promo_codes first — code_value
    web_rec = lookup_code_in_table("web_promo_codes", final_code, kind="any")
    if web_rec:
        campaign_like = web_promo_as_campaign_record(web_rec)
        if campaign_like:
            return PromoLookupResult(
                table_found="web_promo_codes",
                kind="campaign",
                normalized_code=final_code,
                campaign_record=campaign_like,
            )
        if is_simple_promo_row(web_rec):
            return PromoLookupResult(
                table_found="web_promo_codes",
                kind="web",
                normalized_code=final_code,
                web_record=web_rec,
            )

    # 2) promo_codes campaign — code_value + delivery_type=manual
    campaign_rec = lookup_code_in_table("promo_codes", final_code, delivery_type="manual", kind="campaign")
    if campaign_rec:
        return PromoLookupResult(
            table_found="promo_codes",
            kind="campaign",
            normalized_code=final_code,
            campaign_record=campaign_rec,
        )

    # 3) promo_codes simple — code_value
    simple_rec = lookup_code_in_table("promo_codes", final_code, kind="simple")
    if simple_rec and is_simple_promo_row(simple_rec):
        return PromoLookupResult(
            table_found="promo_codes",
            kind="simple",
            normalized_code=final_code,
            simple_record=simple_rec,
        )

    promo_log("lookup miss", {
        "code_value": final_code,
        "column": "code_value",
        "tables_queried": ["web_promo_codes", "promo_codes"],
        "reason": "PROMO_NOT_FOUND",
    })
    raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")


def get_simple_code_record(source: str, code: Optional[str]) -> dict:
    if source != "manual":
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")

    final_code = normalize_promo_code(code)
    if not final_code:
        raise HTTPException(status_code=400, detail="CODE_REQUIRED")

    simple_rec = lookup_simple_code_record(final_code)
    if not simple_rec:
        raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
    return simple_rec


def get_campaign(campaign_id: str) -> dict:
    res = supabase.table("promo_campaigns").select(
        "id, code, name, description, is_active, starts_at, ends_at, grant_type, "
        "membership_months, membership_days, token_amount, package_code, stack_mode, per_user_limit, max_total_redemptions"
    ).eq("id", campaign_id).limit(1).execute()
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="CAMPAIGN_NOT_FOUND")
    return rows[0]


def count_user_redemptions(campaign_id: str, user_id: str) -> int:
    res = supabase.table("promo_redemptions").select("id", count="exact").eq("campaign_id", campaign_id).eq("user_id", user_id).execute()
    return int(res.count or 0)


def count_total_redemptions(campaign_id: str) -> int:
    res = supabase.table("promo_redemptions").select("id", count="exact").eq("campaign_id", campaign_id).execute()
    return int(res.count or 0)


def active_base_date(profile: dict) -> datetime:
    current = now_utc()
    dates = [
        parse_dt(profile.get("membership_ends_at")),
        parse_dt(profile.get("package_ends_at")),
        parse_dt(profile.get("trial_ends_at")),
        current,
    ]
    return max(dt for dt in dates if dt)


def coalesce_started_at(profile: dict, key: str, current: datetime) -> datetime:
    return parse_dt(profile.get(key)) or current


def current_membership_start(profile: dict) -> datetime:
    current = now_utc()
    starts = [
        parse_dt(profile.get("membership_started_at")),
        parse_dt(profile.get("package_started_at")),
        parse_dt(profile.get("trial_started_at")),
    ]
    valid = [dt for dt in starts if dt]
    return min(valid) if valid else current


def validate_user_eligibility(profile: dict) -> None:
    # New access model: paid users and active members can still extend time with a valid promo.
    return None


def validate_code_unused(code_rec: dict) -> None:
    if bool(code_rec.get("is_used", False)):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")


def validate_code_and_campaign(code_rec: dict, campaign: dict, user_id: str) -> None:
    if not bool(code_rec.get("is_active", False)):
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")
    validate_code_unused(code_rec)
    bound_user_id = str(code_rec.get("bound_user_id") or "").strip()
    if bound_user_id and bound_user_id != user_id:
        raise HTTPException(status_code=400, detail="PROMO_ACCOUNT_MISMATCH")
    if not bool(campaign.get("is_active", False)):
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    current = now_utc()
    starts_at = parse_dt(campaign.get("starts_at"))
    ends_at = parse_dt(campaign.get("ends_at"))
    if starts_at and current < starts_at:
        raise HTTPException(status_code=400, detail="PROMO_NOT_STARTED")
    if ends_at and current > ends_at:
        raise HTTPException(status_code=400, detail="PROMO_EXPIRED")

    per_user_limit = int(campaign.get("per_user_limit") or 1)
    if count_user_redemptions(campaign["id"], user_id) >= per_user_limit:
        raise HTTPException(status_code=400, detail="PROMO_USER_LIMIT_REACHED")
    max_total = campaign.get("max_total_redemptions")
    if max_total is not None and count_total_redemptions(campaign["id"]) >= int(max_total):
        raise HTTPException(status_code=400, detail="PROMO_GLOBAL_LIMIT_REACHED")


def validate_simple_code(code_rec: dict, user_id: str) -> int:
    if code_rec.get("_web_promo_source") or safe_int(code_rec.get("days"), 0) > 0:
        return validate_web_promo_code(code_rec, user_id)

    status = str(code_rec.get("status") or "").strip().lower()
    used_by = str(code_rec.get("used_by") or "").strip()

    if status == "used" or used_by or code_rec.get("used_at"):
        if used_by and used_by == user_id:
            raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED_BY_USER")
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")

    if status not in {"active", "aktif"}:
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    expires_at = parse_dt(code_rec.get("expires_at"))
    if expires_at and expires_at <= now_utc():
        raise HTTPException(status_code=400, detail="PROMO_EXPIRED")

    duration_days = safe_int(code_rec.get("duration_days") or code_rec.get("days"), 0)
    if duration_days <= 0:
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    return duration_days


def web_promo_code_value(code_rec: dict) -> str:
    return promo_row_code_value(code_rec)


def validate_web_promo_code(code_rec: dict, user_id: str) -> int:
    status = str(code_rec.get("status") or "").strip().lower()
    if status == "used":
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")
    if status not in {"active", "aktif"}:
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    expires_at = parse_dt(code_rec.get("expires_at"))
    if expires_at and expires_at <= now_utc():
        raise HTTPException(status_code=400, detail="PROMO_EXPIRED")

    used_count = safe_int(code_rec.get("used_count"), 0)
    max_uses = safe_int(code_rec.get("max_uses"), 0)
    if max_uses > 0 and used_count >= max_uses:
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")

    duration_days = safe_int(code_rec.get("days") or code_rec.get("duration_days"), 0)
    if duration_days <= 0:
        raise HTTPException(status_code=400, detail="PROMO_NOT_ACTIVE")

    return duration_days


def assert_mutation_ok(result: object, detail: str) -> None:
    if getattr(result, "error", None):
        raise HTTPException(status_code=500, detail=detail)
    data = getattr(result, "data", None)
    if data is None:
        raise HTTPException(status_code=500, detail=detail)


def write_access_duration_event(
    *,
    user_id: str,
    email: Optional[str],
    source_ref: str,
    product_id: str,
    days_added: int,
    previous_ends_at: datetime,
    new_ends_at: datetime,
    metadata: dict,
) -> None:
    payload = {
        "user_id": user_id,
        "email": email,
        "source": "promo_code",
        "source_ref": source_ref,
        "product_id": product_id,
        "days_added": days_added,
        "previous_ends_at": previous_ends_at.isoformat(),
        "new_ends_at": new_ends_at.isoformat(),
        "metadata": metadata,
    }
    try:
        ins = supabase.table("access_duration_events").insert(payload).execute()
        assert_mutation_ok(ins, "ACCESS_DURATION_EVENT_FAILED")
    except HTTPException:
        raise
    except Exception as exc:
        promo_log("access duration event failed", {"message": str(exc)})
        raise HTTPException(status_code=500, detail="ACCESS_DURATION_EVENT_FAILED")


def apply_membership(profile: dict, campaign: dict, promo_code: str) -> tuple[Optional[str], Optional[str], Optional[str], int, int, Optional[datetime], Optional[datetime]]:
    membership_days = safe_int(campaign.get("membership_days"), 0)
    months = safe_int(campaign.get("membership_months"), 0)
    package_code = str(campaign.get("package_code") or "member").strip() or "member"
    if membership_days <= 0 and months <= 0:
        return profile.get("selected_package_code"), profile.get("package_started_at"), profile.get("package_ends_at"), 0, 0, None, None

    current = now_utc()
    base = active_base_date(profile)
    if membership_days > 0:
        new_end = base + timedelta(days=membership_days)
        days_added = membership_days
    else:
        new_end = add_months_safe(base, months)
        days_added = max(1, (new_end - base).days)

    package_started_at = coalesce_started_at(profile, "package_started_at", current)
    membership_started_at = coalesce_started_at(profile, "membership_started_at", current)
    current_iso = current.isoformat()
    payload = {
        "package_active": True,
        "selected_package_code": package_code,
        "package_started_at": package_started_at.isoformat(),
        "package_ends_at": new_end.isoformat(),
        "promo_used_at": current_iso,
        "promo_code_used": promo_code,
        "membership_status": "active",
        "membership_source": "promo_code",
        "membership_product_id": promo_code,
        "membership_started_at": membership_started_at.isoformat(),
        "membership_ends_at": new_end.isoformat(),
        "membership_last_checked_at": current_iso,
        "plan": "member",
        "app_access_mode": "member",
    }
    upd = supabase.table("profiles").update(payload).eq("id", profile["id"]).execute()
    assert_mutation_ok(upd, "PROFILE_UPDATE_FAILED")

    write_access_duration_event(
        user_id=profile["id"],
        email=profile.get("email"),
        source_ref=promo_code,
        product_id=package_code,
        days_added=days_added,
        previous_ends_at=base,
        new_ends_at=new_end,
        metadata={
            "campaign_id": campaign.get("id"),
            "code_value": promo_code,
            "membership_days": membership_days,
            "membership_months": months,
        },
    )
    return package_code, membership_started_at.isoformat(), new_end.isoformat(), membership_days, months, base, new_end


def apply_simple_membership(profile: dict, promo_code: str, duration_days: int) -> tuple[str, str, str]:
    current = now_utc()
    base = active_base_date(profile)
    new_end = base + timedelta(days=duration_days)
    package_code = "promo_code"
    package_started_at = coalesce_started_at(profile, "package_started_at", current)
    membership_started_at = coalesce_started_at(profile, "membership_started_at", current)
    current_iso = current.isoformat()
    payload = {
        "package_active": True,
        "selected_package_code": package_code,
        "package_started_at": package_started_at.isoformat(),
        "package_ends_at": new_end.isoformat(),
        "promo_used_at": current_iso,
        "promo_code_used": promo_code,
        "membership_status": "active",
        "membership_source": "promo_code",
        "membership_product_id": promo_code,
        "membership_started_at": membership_started_at.isoformat(),
        "membership_ends_at": new_end.isoformat(),
        "membership_last_checked_at": current_iso,
        "plan": "member",
        "app_access_mode": "member",
    }
    upd = supabase.table("profiles").update(payload).eq("id", profile["id"]).execute()
    assert_mutation_ok(upd, "PROFILE_UPDATE_FAILED")
    write_access_duration_event(
        user_id=profile["id"],
        email=profile.get("email"),
        source_ref=promo_code,
        product_id=package_code,
        days_added=duration_days,
        previous_ends_at=base,
        new_ends_at=new_end,
        metadata={"campaign_id": None, "code_value": promo_code, "membership_days": duration_days},
    )
    return package_code, membership_started_at.isoformat(), new_end.isoformat()


def apply_tokens(profile: dict, campaign: dict) -> tuple[int, int]:
    token_amount = int(campaign.get("token_amount") or 0)
    current_tokens = int(profile.get("tokens") or 0)
    next_tokens = current_tokens + token_amount
    if token_amount > 0:
        upd = supabase.table("profiles").update({"tokens": next_tokens}).eq("id", profile["id"]).execute()
        assert_mutation_ok(upd, "PROFILE_TOKENS_UPDATE_FAILED")
    return token_amount, next_tokens


def code_used_payload(code_rec: dict, user_id: str) -> dict:
    payload = {
        "is_used": True,
        "used_by": user_id,
        "used_at": iso_now(),
        "activated_at": iso_now(),
        "bound_user_id": user_id,
        "activated_by": user_id,
    }
    if str(code_rec.get("marketplace") or "").strip().lower() == "trendyol":
        payload["invoice_status"] = "handled_by_trendyol"
    return payload


def mark_activation_link_used(code_value: str) -> None:
    final_code = clean(code_value).upper()
    if not final_code:
        return
    try:
        supabase.table("activation_links").update({"used_at": iso_now()}).eq("code_value", final_code).is_("used_at", "null").execute()
    except Exception as exc:
        promo_log("activation link used_at update skipped", {"message": str(exc), "code_value": final_code})


def mark_code_used(code_rec: dict, user_id: str) -> None:
    if code_rec.get("_web_promo_source"):
        payload = {
            "is_used": True,
            "status": "used",
            "used_by": user_id,
            "used_at": iso_now(),
            "bound_user_id": user_id,
        }
        promo_log("update table", {"table": "web_promo_codes", "action": "mark_used", "id": code_rec.get("id")})
        upd = (
            supabase.table("web_promo_codes")
            .update(payload)
            .eq("id", code_rec["id"])
            .eq("is_used", False)
            .execute()
        )
        assert_mutation_ok(upd, "PROMO_MARK_USED_FAILED")
        if not (getattr(upd, "data", None) or []):
            raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")
        mark_activation_link_used(str(code_rec.get("code_value") or ""))
        return

    payload = code_used_payload(code_rec, user_id)
    promo_log("update table", {"table": "promo_codes", "action": "mark_used", "id": code_rec.get("id")})
    try:
        upd = supabase.table("promo_codes").update(payload).eq("id", code_rec["id"]).eq("is_used", False).execute()
    except Exception as exc:
        promo_log("activated_by update retry without optional column", {"message": str(exc), "promo_code_id": code_rec.get("id")})
        payload.pop("activated_by", None)
        upd = supabase.table("promo_codes").update(payload).eq("id", code_rec["id"]).eq("is_used", False).execute()

    assert_mutation_ok(upd, "PROMO_MARK_USED_FAILED")
    if not (getattr(upd, "data", None) or []):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")
    mark_activation_link_used(str(code_rec.get("code_value") or ""))


def mark_simple_code_used(code_rec: dict, user_id: str) -> None:
    if code_rec.get("_web_promo_source") or safe_int(code_rec.get("days"), 0) > 0:
        mark_web_promo_code_used(code_rec, user_id)
        return

    payload = {
        "status": "used",
        "used_by": user_id,
        "used_at": iso_now(),
        "activated_at": iso_now(),
    }
    if str(code_rec.get("marketplace") or "").strip().lower() == "trendyol":
        payload["invoice_status"] = "handled_by_trendyol"
    promo_log("update table", {"table": "promo_codes", "action": "mark_simple_used", "id": code_rec.get("id")})
    upd = supabase.table("promo_codes").update(payload).eq("id", code_rec["id"]).eq("status", str(code_rec.get("status") or "")).execute()
    assert_mutation_ok(upd, "PROMO_MARK_USED_FAILED")
    if not (getattr(upd, "data", None) or []):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")


def mark_web_promo_code_used(code_rec: dict, user_id: str) -> None:
    used_count = safe_int(code_rec.get("used_count"), 0)
    max_uses = safe_int(code_rec.get("max_uses"), 0)
    next_count = used_count + 1
    exhausted = (max_uses > 0 and next_count >= max_uses) or max_uses == 1
    payload: dict = {"used_count": next_count}
    if exhausted:
        payload["status"] = "used"

    upd = (
        supabase.table("web_promo_codes")
        .update(payload)
        .eq("id", code_rec["id"])
        .eq("used_count", used_count)
        .execute()
    )
    assert_mutation_ok(upd, "PROMO_MARK_USED_FAILED")
    if not (getattr(upd, "data", None) or []):
        raise HTTPException(status_code=400, detail="PROMO_ALREADY_USED")


def get_profile_after(user_id: str) -> dict:
    return get_profile(user_id)


def log_redemption(code_rec: dict, campaign: dict, profile_before: dict, profile_after: dict, user_id: str, source: str, membership_months: int, granted_tokens: int, package_code: Optional[str]) -> None:
    ins = supabase.table("promo_redemptions").insert({
        "promo_code_id": code_rec["id"],
        "campaign_id": campaign["id"],
        "user_id": user_id,
        "source": source,
        "grant_type": campaign["grant_type"],
        "granted_membership_months": membership_months,
        "granted_tokens": granted_tokens,
        "granted_package_code": package_code,
        "before_package_code": profile_before.get("selected_package_code"),
        "after_package_code": profile_after.get("selected_package_code"),
        "before_membership_end": profile_before.get("package_ends_at"),
        "after_membership_end": profile_after.get("package_ends_at"),
        "before_tokens": int(profile_before.get("tokens") or 0),
        "after_tokens": int(profile_after.get("tokens") or 0),
        "created_at": iso_now(),
    }).execute()
    assert_mutation_ok(ins, "PROMO_LOG_FAILED")


def build_success_message(grant_type: str, membership_months: int, membership_days: int, tokens_loaded: int) -> str:
    parts: list[str] = []
    if grant_type in ("membership", "bundle") and (membership_days > 0 or membership_months > 0):
        parts.append("kullanım süreniz uzatıldı")
    if grant_type in ("tokens", "bundle") and tokens_loaded > 0:
        parts.append(f"{tokens_loaded} jeton yüklendi")
    return " ve ".join(parts) if parts else "Promosyon başarıyla uygulandı."


def store_fallback_url(user_agent: str) -> str:
    ua = str(user_agent or "").lower()
    if "android" in ua:
        return ANDROID_PLAY_STORE_URL
    return IOS_APP_STORE_URL


def build_promo_deep_link(code_value: str) -> str:
    final_code = normalize_promo_code(code_value)
    query = urlencode({"code": final_code, "ok": "1"})
    return f"italky://promo/redeemed?{query}"


def build_redeem_open_url(code_value: str) -> str:
    final_code = normalize_promo_code(code_value)
    query = urlencode({"code": final_code, "ok": "1"})
    return f"{API_PUBLIC_BASE}/api/promo/redeem/open?{query}"


def with_redirect_metadata(response: PromoRedeemResponse, code_value: str) -> PromoRedeemResponse:
    if not response.ok:
        return response
    final_code = normalize_promo_code(code_value)
    if not final_code:
        return response
    deep_link = build_promo_deep_link(final_code)
    open_url = build_redeem_open_url(final_code)
    promo_log("post redeem redirect targets", {
        "app_deep_link": deep_link,
        "fallback_store_url": IOS_APP_STORE_URL,
        "redirect_url": open_url,
    })
    return response.model_copy(update={
        "app_deep_link": deep_link,
        "fallback_store_url": IOS_APP_STORE_URL,
        "redirect_url": open_url,
    })


def render_redeem_open_html(*, deep_link: str, store_url: str, code_value: str) -> str:
    safe_code = escape_html(normalize_promo_code(code_value))
    fallback_ms = max(800, PROMO_DEEP_LINK_FALLBACK_MS)
    deep_link_js = json.dumps(deep_link)
    store_url_js = json.dumps(store_url)
    return f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>italkyAI</title>
  <style>
    body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#05070f;color:#fff;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;text-align:center;padding:24px}}
    .card{{max-width:420px}}
    h1{{margin:0 0 10px;font-size:24px}}
    p{{margin:0;color:rgba(255,255,255,.72);line-height:1.6}}
    a{{display:inline-block;margin-top:18px;padding:14px 20px;border-radius:14px;background:#fff;color:#101421;text-decoration:none;font-weight:800}}
  </style>
</head>
<body>
  <div class="card">
    <h1>Promosyon uygulandı</h1>
    <p>Kod: <strong>{safe_code}</strong></p>
    <p>italkyAI uygulaması açılıyor. Otomatik yönlendirme olmazsa aşağıdaki bağlantıyı kullanın.</p>
    <a id="storeLink" href="{escape_html(store_url)}">Uygulamayı indir</a>
  </div>
  <script>
    (function () {{
      var deepLink = {deep_link_js};
      var storeUrl = {store_url_js};
      var fallbackMs = {fallback_ms};
      try {{ window.location.href = deepLink; }} catch (e) {{}}
      setTimeout(function () {{
        try {{ window.location.href = storeUrl; }} catch (e) {{}}
      }}, fallbackMs);
    }})();
  </script>
</body>
</html>"""


def escape_html(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@router.get("/redeem/open")
def redeem_open_after_success(
    request: Request,
    code: Optional[str] = Query(None),
    ok: Optional[str] = Query(None),
):
    final_code = normalize_promo_code(code)
    if not final_code:
        raise HTTPException(status_code=400, detail="CODE_REQUIRED")

    deep_link = build_promo_deep_link(final_code)
    store_url = store_fallback_url(request.headers.get("user-agent", ""))
    promo_log("redeem open redirect page", {
        "code_value": final_code,
        "app_deep_link": deep_link,
        "fallback_store_url": store_url,
        "ok": ok,
    })
    return HTMLResponse(
        content=render_redeem_open_html(
            deep_link=deep_link,
            store_url=store_url,
            code_value=final_code,
        )
    )


def maybe_redirect_after_redeem(response: PromoRedeemResponse, code_value: str, follow_redirect: bool):
    enriched = with_redirect_metadata(response, code_value)
    if not follow_redirect or not enriched.ok:
        return enriched
    redirect_target = enriched.redirect_url or build_redeem_open_url(code_value)
    promo_log("redirect after redeem", {
        "redirect_url": redirect_target,
        "app_deep_link": enriched.app_deep_link,
        "fallback_store_url": enriched.fallback_store_url,
    })
    return RedirectResponse(url=redirect_target, status_code=303)


def redeem_simple_promo(
    payload: PromoRedeemRequest,
    redeem_user_id: str,
    profile_before: dict,
    simple_code: Optional[dict] = None,
    table_found: PromoTableFound = "promo_codes",
) -> PromoRedeemResponse:
    simple_code = simple_code or get_simple_code_record(payload.source, payload.code)
    duration_days = validate_simple_code(simple_code, redeem_user_id)
    code_value = promo_row_code_value(simple_code) or normalize_promo_code(payload.code)
    package_code, membership_started_at, membership_ends_at = apply_simple_membership(
        profile_before,
        code_value,
        duration_days,
    )
    mark_simple_code_used(simple_code, redeem_user_id)
    profile_after = get_profile_after(redeem_user_id)
    promo_log("redeem success", {"table_found": table_found, "kind": "simple", "code_value": code_value})

    return PromoRedeemResponse(
        ok=True,
        grant_type="membership",
        membership_months=0,
        membership_days=duration_days,
        package_code=package_code,
        membership_started_at=membership_started_at,
        membership_ends_at=membership_ends_at,
        tokens_loaded=0,
        tokens_after=int(profile_after.get("tokens") or 0),
        message=f"{duration_days} günlük kullanım süresi eklendi",
        table_found=table_found,
    )


def redeem_web_promo(
    web_rec: dict,
    redeem_user_id: str,
    profile_before: dict,
    normalized_code: str,
) -> PromoRedeemResponse:
    validate_user_eligibility(profile_before)
    duration_days = validate_web_promo_code(web_rec, redeem_user_id)
    code_value = web_promo_code_value(web_rec) or normalized_code
    package_code, membership_started_at, membership_ends_at = apply_simple_membership(
        profile_before,
        code_value,
        duration_days,
    )
    mark_web_promo_code_used(web_rec, redeem_user_id)
    profile_after = get_profile_after(redeem_user_id)
    promo_log("redeem success", {"table_found": "web_promo_codes", "kind": "web", "code_value": code_value})

    return PromoRedeemResponse(
        ok=True,
        grant_type="membership",
        membership_months=0,
        membership_days=duration_days,
        package_code=package_code,
        membership_started_at=membership_started_at,
        membership_ends_at=membership_ends_at,
        tokens_loaded=0,
        tokens_after=int(profile_after.get("tokens") or 0),
        message=f"{duration_days} günlük kullanım süresi eklendi",
        table_found="web_promo_codes",
    )


def redeem_campaign_promo(
    code_rec: dict,
    payload: PromoRedeemRequest,
    redeem_user_id: str,
    profile_before: dict,
) -> PromoRedeemResponse:
    validate_code_unused(code_rec)
    validate_user_eligibility(profile_before)

    campaign = get_campaign(code_rec["campaign_id"])
    validate_code_and_campaign(code_rec, campaign, redeem_user_id)

    membership_months = safe_int(campaign.get("membership_months"), 0)
    membership_days = safe_int(campaign.get("membership_days"), 0)
    package_code = str(campaign.get("package_code") or "member").strip() or "member"
    grant_type = str(campaign.get("grant_type") or "").strip()
    code_value = promo_row_code_value(code_rec) or normalize_promo_code(payload.code)

    membership_started_at = None
    membership_ends_at = None
    tokens_loaded = 0

    if grant_type in ("membership", "bundle"):
        package_code, membership_started_at, membership_ends_at, membership_days, membership_months, _, _ = apply_membership(
            profile_before,
            campaign,
            code_value,
        )

    profile_mid = get_profile(redeem_user_id)
    if grant_type in ("tokens", "bundle"):
        tokens_loaded, _ = apply_tokens(profile_mid, campaign)

    mark_code_used(code_rec, redeem_user_id)
    profile_after = get_profile_after(redeem_user_id)

    log_redemption(
        code_rec=code_rec,
        campaign=campaign,
        profile_before=profile_before,
        profile_after=profile_after,
        user_id=redeem_user_id,
        source=payload.source,
        membership_months=membership_months,
        granted_tokens=tokens_loaded,
        package_code=package_code,
    )
    promo_log("redeem success", {
        "table_found": "web_promo_codes" if code_rec.get("_web_promo_source") else "promo_codes",
        "kind": "campaign",
        "code_value": code_value,
    })

    return PromoRedeemResponse(
        ok=True,
        grant_type=grant_type,
        membership_months=membership_months,
        membership_days=membership_days,
        package_code=package_code,
        membership_started_at=membership_started_at,
        membership_ends_at=membership_ends_at,
        tokens_loaded=tokens_loaded,
        tokens_after=int(profile_after.get("tokens") or 0),
        message=build_success_message(grant_type, membership_months, membership_days, tokens_loaded),
        table_found="web_promo_codes" if code_rec.get("_web_promo_source") else "promo_codes",
    )


@router.post("/redeem", response_model=None)
def redeem_promo(
    payload: PromoRedeemRequest,
    authorization: Optional[str] = Header(None),
    follow_redirect: bool = Query(False),
):
    redeem_user_id = resolve_redeem_user_id(payload.user_id, authorization)
    profile_before = get_profile(redeem_user_id)
    normalized_code = normalize_promo_code(payload.code)

    promo_log("redeem request", {
        "route": "POST /api/promo/redeem",
        "source": payload.source,
        "code_value": normalized_code,
        "user_id": redeem_user_id,
        "follow_redirect": follow_redirect,
        "lookup_columns": ["code_value", "code"],
        "lookup_order": ["web_promo_codes", "promo_codes:campaign", "promo_codes:simple"],
    })

    if payload.source == "nfc":
        code_rec = get_code_record(payload.source, payload.code, payload.uid)
        result = redeem_campaign_promo(code_rec, payload, redeem_user_id, profile_before)
        return maybe_redirect_after_redeem(result, normalized_code, follow_redirect)

    lookup = resolve_manual_promo_lookup(payload.code)
    redirect_code = lookup.normalized_code or normalized_code

    if lookup.kind == "campaign" and lookup.campaign_record:
        result = redeem_campaign_promo(lookup.campaign_record, payload, redeem_user_id, profile_before)
        return maybe_redirect_after_redeem(result, redirect_code, follow_redirect)

    if lookup.kind == "simple" and lookup.simple_record:
        validate_user_eligibility(profile_before)
        result = redeem_simple_promo(
            payload,
            redeem_user_id,
            profile_before,
            simple_code=lookup.simple_record,
            table_found=lookup.table_found,
        )
        return maybe_redirect_after_redeem(result, redirect_code, follow_redirect)

    if lookup.kind == "web" and lookup.web_record:
        result = redeem_web_promo(lookup.web_record, redeem_user_id, profile_before, lookup.normalized_code)
        return maybe_redirect_after_redeem(result, redirect_code, follow_redirect)

    raise HTTPException(status_code=404, detail="PROMO_NOT_FOUND")
