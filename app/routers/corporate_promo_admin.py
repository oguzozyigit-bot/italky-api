from __future__ import annotations

import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.routers.admin import _get_supabase, _require_admin, _safe_data

router = APIRouter(prefix="/api/admin/corporate-promos", tags=["Corporate Promos"])

CORPORATE_PROMO_TABLE = "corporate_promo_codes"
FORBIDDEN_LETTER_PAIRS = {"AK", "FG", "FB", "GS"}
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"
DURATION_DAYS = {1: 30, 3: 90, 6: 180, 12: 365}


class CorporatePromoGenerateIn(BaseModel):
    company_name: str = Field(..., min_length=2)
    campaign_name: Optional[str] = None
    quantity: int = Field(..., ge=1, le=50000)
    duration_months: int = Field(..., ge=1)
    valid_until: str
    note: Optional[str] = None


class CorporatePromoStatusIn(BaseModel):
    code: str
    status: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="VALID_UNTIL_REQUIRED")
    if len(raw) == 10:
        raw = f"{raw}T23:59:59+00:00"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail="INVALID_VALID_UNTIL")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _duration_days(months: int) -> int:
    if months not in DURATION_DAYS:
        raise HTTPException(status_code=400, detail="INVALID_DURATION_MONTHS")
    return DURATION_DAYS[months]


def _is_valid_code(code: str) -> bool:
    value = str(code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{8}", value):
        return False
    letters = "".join(ch for ch in value if ch.isalpha())
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(letters) != 2 or len(digits) != 6:
        return False
    return letters not in FORBIDDEN_LETTER_PAIRS


def _generate_code() -> str:
    while True:
        letter_positions = sorted(random.sample(range(8), 2))
        chosen_letters = random.sample(LETTERS, 2)
        if "".join(chosen_letters) in FORBIDDEN_LETTER_PAIRS:
            continue

        out = []
        letter_idx = 0
        for i in range(8):
            if i in letter_positions:
                out.append(chosen_letters[letter_idx])
                letter_idx += 1
            else:
                out.append(random.choice(DIGITS))

        code = "".join(out)
        if _is_valid_code(code):
            return code


def _normalize_status(value: str) -> str:
    status = str(value or "").strip().lower()
    if status not in {"active", "activated", "expired", "cancelled"}:
        raise HTTPException(status_code=400, detail="INVALID_STATUS")
    return status


@router.post("/generate")
async def generate_corporate_promos(payload: CorporatePromoGenerateIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    duration_days = _duration_days(payload.duration_months)
    valid_until = _parse_dt(payload.valid_until)
    company_name = payload.company_name.strip()
    campaign_name = (payload.campaign_name or company_name).strip() or company_name

    rows = []
    seen = set()
    while len(rows) < payload.quantity:
        code = _generate_code()
        if code in seen:
            continue
        seen.add(code)
        rows.append({
            "code": code,
            "company_name": company_name,
            "campaign_name": campaign_name,
            "duration_months": payload.duration_months,
            "duration_days": duration_days,
            "valid_until": valid_until.isoformat(),
            "status": "active",
            "created_by": ctx.get("user_id"),
            "note": payload.note,
        })

    try:
        inserted = 0
        samples = []
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            res = sb.table(CORPORATE_PROMO_TABLE).insert(chunk).execute()
            data = _safe_data(res) or []
            inserted += len(data) if isinstance(data, list) else len(chunk)
            if len(samples) < 20:
                samples.extend((data if isinstance(data, list) else chunk)[:20 - len(samples)])
        return {"ok": True, "inserted": inserted, "sample": samples}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CORPORATE_PROMO_GENERATE_FAILED: {e}")


@router.get("/report")
async def corporate_promo_report(
    company: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    from_date: Optional[str] = Query(default=None),
    to_date: Optional[str] = Query(default=None),
    ctx: Dict[str, Any] = Depends(_require_admin),
):
    sb = _get_supabase()
    try:
        q = sb.table(CORPORATE_PROMO_TABLE).select("*").order("created_at", desc=True).limit(5000)
        if company:
            q = q.ilike("company_name", f"%{company.strip()}%")
        if status:
            q = q.eq("status", _normalize_status(status))
        if from_date:
            q = q.gte("created_at", _parse_dt(from_date).isoformat())
        if to_date:
            q = q.lte("created_at", _parse_dt(to_date).isoformat())

        res = q.execute()
        rows = _safe_data(res) or []
        now = _utcnow()
        this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        def activated_at(row: dict) -> Optional[datetime]:
            raw = row.get("activated_at")
            if not raw:
                return None
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                return None

        stats = {
            "total": len(rows),
            "activated": sum(1 for r in rows if r.get("status") == "activated"),
            "unused": sum(1 for r in rows if r.get("status") == "active" and not r.get("activated_at")),
            "expired": sum(1 for r in rows if r.get("status") == "expired"),
            "cancelled": sum(1 for r in rows if r.get("status") == "cancelled"),
            "activated_this_month": sum(1 for r in rows if (activated_at(r) or datetime.min.replace(tzinfo=timezone.utc)) >= this_month),
        }
        stats["billable_count"] = stats["activated"]
        return {"ok": True, "stats": stats, "items": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CORPORATE_PROMO_REPORT_FAILED: {e}")


@router.post("/status")
async def update_corporate_promo_status(payload: CorporatePromoStatusIn, ctx: Dict[str, Any] = Depends(_require_admin)):
    sb = _get_supabase()
    code = str(payload.code or "").strip().upper()
    if not _is_valid_code(code):
        raise HTTPException(status_code=400, detail="INVALID_CODE")
    status = _normalize_status(payload.status)
    try:
        res = sb.table(CORPORATE_PROMO_TABLE).update({"status": status}).eq("code", code).execute()
        return {"ok": True, "result": _safe_data(res)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CORPORATE_PROMO_STATUS_FAILED: {e}")
