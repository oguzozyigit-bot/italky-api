from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Request

from app.routers.admin import _get_supabase, _safe_data

router = APIRouter(prefix="/api/trendyol", tags=["Trendyol"])
mp_router = APIRouter(prefix="/api/mp", tags=["Marketplace"])

SKIPPED_PACKAGE_STATUSES = {
    "cancelled",
    "returned",
    "unsupplied",
    "undelivered",
}
DEFAULT_DELIVERY_SUFFIX = "alternative-delivery"
DEFAULT_BASE_URL = "https://api.trendyol.com/sapigw"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def clean(value: object) -> str:
    return str(value or "").strip()


def env_bool(name: str) -> bool:
    return clean(os.getenv(name)).lower() == "true"


def seller_id() -> str:
    return clean(os.getenv("TRENDYOL_SELLER_ID"))


def delivery_suffix() -> str:
    return clean(os.getenv("TRENDYOL_DIGITAL_DELIVERY_SUFFIX")) or DEFAULT_DELIVERY_SUFFIX


def base_url() -> str:
    return (clean(os.getenv("TRENDYOL_BASE_URL")) or DEFAULT_BASE_URL).rstrip("/")


def require_internal_key(x_api_key: Optional[str]) -> None:
    expected = clean(os.getenv("TRENDYOL_WEBHOOK_API_KEY"))
    if not expected or clean(x_api_key) != expected:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def safe_error(exc: Exception) -> str:
    text = str(exc or "").strip()
    api_key = clean(os.getenv("TRENDYOL_API_KEY"))
    api_secret = clean(os.getenv("TRENDYOL_API_SECRET"))
    for secret in (api_key, api_secret):
        if secret:
            text = text.replace(secret, "***")
    return text[:1000]


def trendyolHeaders() -> dict[str, str]:
    api_key = clean(os.getenv("TRENDYOL_API_KEY"))
    api_secret = clean(os.getenv("TRENDYOL_API_SECRET"))
    sid = seller_id()
    if not sid or not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="TRENDYOL_ENV_MISSING")

    token = base64.b64encode(f"{api_key}:{api_secret}".encode("utf-8")).decode("ascii")
    ua_suffix = clean(os.getenv("TRENDYOL_USER_AGENT_SUFFIX")) or "SelfIntegration"
    return {
        "Authorization": f"Basic {token}",
        "User-Agent": f"{sid} - {ua_suffix}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def parse_json_response(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        text = clean(response.text)
        return {"raw": text[:2000]} if text else {}


def trendyolRequest(path: str, opts: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    opts = opts or {}
    method = clean(opts.get("method") or "GET").upper()
    body = opts.get("body")
    final_path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url()}{final_path}"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=trendyolHeaders(),
            json=body,
            timeout=30,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TRENDYOL_REQUEST_FAILED: {safe_error(exc)}")

    data = parse_json_response(response)
    if response.status_code < 200 or response.status_code >= 300:
        raise HTTPException(
            status_code=502,
            detail={
                "reason": "TRENDYOL_HTTP_ERROR",
                "status_code": response.status_code,
                "response": data,
            },
        )

    return {"status_code": response.status_code, "data": data}


def deliverTrendyolDigitalCode(payload: dict[str, Any]) -> dict[str, Any]:
    package_id = clean(payload.get("packageId"))
    digital_code = clean(payload.get("digitalCode"))
    if not package_id:
        raise HTTPException(status_code=400, detail="PACKAGE_ID_REQUIRED")
    if len(digital_code) < 6 or len(digital_code) > 120:
        raise HTTPException(status_code=400, detail="DIGITAL_CODE_LENGTH_INVALID")

    body = {
        "isPhoneNumber": True,
        "trackingInfo": clean(os.getenv("TRENDYOL_DELIVERY_PHONE")),
        "params": {"digitalCode": digital_code},
    }
    path = f"/integration/order/sellers/{seller_id()}/shipment-packages/{package_id}/{delivery_suffix()}"
    response = trendyolRequest(path, {"method": "PUT", "body": body})
    return {"payload": body, "response": response}


def get_value(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj.get(key) is not None:
            return obj.get(key)
    return None


def normalize_package(raw: dict[str, Any]) -> dict[str, Any]:
    pkg = raw.get("pkg") if isinstance(raw.get("pkg"), dict) else raw
    if isinstance(pkg.get("package"), dict):
        pkg = pkg["package"]
    if isinstance(pkg.get("shipmentPackage"), dict):
        pkg = pkg["shipmentPackage"]
    return pkg


def package_id_from(pkg: dict[str, Any]) -> Optional[int]:
    value = get_value(pkg, "id", "shipmentPackageId", "packageId")
    try:
        return int(value)
    except Exception:
        return None


def order_number_from(pkg: dict[str, Any]) -> str:
    return clean(get_value(pkg, "orderNumber", "order_number", "orderNo", "orderId"))


def package_status_from(pkg: dict[str, Any]) -> str:
    return clean(get_value(pkg, "status", "shipmentPackageStatus", "packageStatus"))


def line_quantity(line: dict[str, Any]) -> int:
    try:
        return max(1, int(get_value(line, "quantity", "amount") or 1))
    except Exception:
        return 1


def line_id_from(line: dict[str, Any]) -> Optional[int]:
    value = get_value(line, "lineId", "id", "orderLineId")
    try:
        return int(value)
    except Exception:
        return None


def get_mapping_for_line(sb: Any, line: dict[str, Any]) -> Optional[dict[str, Any]]:
    barcode = clean(line.get("barcode"))
    if barcode:
        res = (
            sb.table("marketplace_sku_mappings")
            .select("*")
            .eq("marketplace", "trendyol")
            .eq("active", True)
            .eq("barcode", barcode)
            .limit(1)
            .execute()
        )
        rows = _safe_data(res) or []
        if rows:
            return rows[0]

    stock_code = clean(get_value(line, "merchantSku", "stockCode", "stock_code"))
    if stock_code:
        res = (
            sb.table("marketplace_sku_mappings")
            .select("*")
            .eq("marketplace", "trendyol")
            .eq("active", True)
            .eq("stock_code", stock_code)
            .limit(1)
            .execute()
        )
        rows = _safe_data(res) or []
        if rows:
            return rows[0]

    return None


def reserve_code(sb: Any, mapping: dict[str, Any], pkg: dict[str, Any], line: dict[str, Any], quantity_index: int) -> str:
    package_id = package_id_from(pkg)
    order_number = order_number_from(pkg)
    line_id = line_id_from(line)
    args = {
        "p_campaign_id": mapping.get("campaign_id"),
        "p_delivery_type": clean(mapping.get("delivery_type") or "manual"),
        "p_order_number": order_number,
        "p_package_id": package_id,
        "p_line_id": line_id,
        "p_quantity_index": quantity_index,
        "p_barcode": clean(line.get("barcode") or mapping.get("barcode")),
    }
    res = sb.rpc("reserve_trendyol_promo_code", args).execute()
    data = _safe_data(res)
    code = clean(data[0] if isinstance(data, list) and data else data)
    if not code:
        raise HTTPException(status_code=500, detail="PROMO_CODE_RESERVATION_FAILED")
    return code


def get_reserved_rows(sb: Any, codes: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in codes:
        res = (
            sb.table("promo_codes")
            .select("id, code_value, delivery_attempt_count")
            .eq("code_value", code)
            .limit(1)
            .execute()
        )
        data = _safe_data(res) or []
        if data:
            rows.append(data[0])
    return rows


def update_reserved_rows(sb: Any, codes: list[str], payload: dict[str, Any], increment_attempts: bool) -> None:
    for row in get_reserved_rows(sb, codes):
        next_payload = dict(payload)
        if increment_attempts:
            try:
                next_payload["delivery_attempt_count"] = int(row.get("delivery_attempt_count") or 0) + 1
            except Exception:
                next_payload["delivery_attempt_count"] = 1
        sb.table("promo_codes").update(next_payload).eq("id", row["id"]).execute()


def processTrendyolPackage(payload: dict[str, Any]) -> dict[str, Any]:
    sb = _get_supabase()
    pkg = normalize_package(payload)
    package_id = package_id_from(pkg)
    order_number = order_number_from(pkg)
    if not package_id:
        raise HTTPException(status_code=400, detail="PACKAGE_ID_REQUIRED")
    if not order_number:
        raise HTTPException(status_code=400, detail="ORDER_NUMBER_REQUIRED")

    status = package_status_from(pkg)
    if status.lower() in SKIPPED_PACKAGE_STATUSES:
        return {"ok": True, "status": "skipped", "reason": "PACKAGE_STATUS_SKIPPED", "package_id": package_id}

    lines = pkg.get("lines") or pkg.get("items") or []
    if not isinstance(lines, list):
        lines = []

    reserved_codes: list[str] = []
    matched_lines = 0
    for line in lines:
        if not isinstance(line, dict):
            continue
        mapping = get_mapping_for_line(sb, line)
        if not mapping:
            continue

        business_unit = clean(line.get("businessUnit"))
        if business_unit and business_unit != "Digital Goods":
            raise HTTPException(status_code=400, detail="LINE_IS_NOT_DIGITAL_GOODS")

        matched_lines += 1
        for quantity_index in range(1, line_quantity(line) + 1):
            reserved_codes.append(reserve_code(sb, mapping, pkg, line, quantity_index))

    if not matched_lines:
        return {"ok": True, "status": "skipped", "reason": "NO_MATCHING_SKU", "package_id": package_id}

    digital_code = " | ".join(f"{idx}) {code}" for idx, code in enumerate(reserved_codes, start=1)) if len(reserved_codes) > 1 else reserved_codes[0]
    if len(digital_code) > 120:
        update_reserved_rows(
            sb,
            reserved_codes,
            {"delivery_status": "failed", "delivery_error": "DIGITAL_CODE_LENGTH_INVALID", "delivery_payload": {"digitalCode": digital_code}},
            increment_attempts=False,
        )
        raise HTTPException(status_code=400, detail="DIGITAL_CODE_LENGTH_INVALID")

    delivery = deliverTrendyolDigitalCode({"packageId": package_id, "digitalCode": digital_code})
    update_reserved_rows(
        sb,
        reserved_codes,
        {
            "delivery_status": "delivered",
            "delivered_at": iso_now(),
            "delivery_payload": delivery["payload"],
            "delivery_response": delivery["response"],
            "delivery_error": None,
        },
        increment_attempts=True,
    )
    return {
        "ok": True,
        "status": "delivered",
        "package_id": package_id,
        "order_number": order_number,
        "delivered_count": len(reserved_codes),
    }


def job_key_from_payload(payload: dict[str, Any]) -> tuple[str, Optional[int]]:
    pkg = normalize_package(payload)
    return order_number_from(pkg), package_id_from(pkg)


def upsert_delivery_job(sb: Any, payload: dict[str, Any], status: str = "pending") -> dict[str, Any]:
    order_number, package_id = job_key_from_payload(payload)
    if not order_number:
        raise HTTPException(status_code=400, detail="ORDER_NUMBER_REQUIRED")
    if not package_id:
        raise HTTPException(status_code=400, detail="PACKAGE_ID_REQUIRED")

    row = {
        "marketplace": "trendyol",
        "order_number": order_number,
        "package_id": package_id,
        "status": status,
        "payload": payload,
        "updated_at": iso_now(),
    }
    try:
        res = sb.table("marketplace_delivery_jobs").upsert(row, on_conflict="marketplace,order_number,package_id").execute()
        data = _safe_data(res)
        return data[0] if isinstance(data, list) and data else row
    except Exception:
        existing = (
            sb.table("marketplace_delivery_jobs")
            .select("id")
            .eq("marketplace", "trendyol")
            .eq("order_number", order_number)
            .eq("package_id", package_id)
            .limit(1)
            .execute()
        )
        rows = _safe_data(existing) or []
        if rows:
            sb.table("marketplace_delivery_jobs").update(row).eq("id", rows[0]["id"]).execute()
            return {**row, "id": rows[0]["id"]}
        res = sb.table("marketplace_delivery_jobs").insert(row).execute()
        data = _safe_data(res)
        return data[0] if isinstance(data, list) and data else row


def fetch_pending_jobs(limit: int = 20) -> list[dict[str, Any]]:
    sb = _get_supabase()
    res = (
        sb.table("marketplace_delivery_jobs")
        .select("*")
        .eq("marketplace", "trendyol")
        .in_("status", ["pending", "failed"])
        .lt("attempts", 5)
        .order("updated_at")
        .limit(limit)
        .execute()
    )
    return _safe_data(res) or []


def update_job(job: dict[str, Any], payload: dict[str, Any], increment_attempts: bool = False) -> None:
    sb = _get_supabase()
    update_payload = dict(payload)
    update_payload["updated_at"] = iso_now()
    if increment_attempts:
        try:
            update_payload["attempts"] = int(job.get("attempts") or 0) + 1
        except Exception:
            update_payload["attempts"] = 1
    sb.table("marketplace_delivery_jobs").update(update_payload).eq("id", job["id"]).execute()


def processPendingTrendyolJobs(limit: int = 20) -> dict[str, Any]:
    jobs = fetch_pending_jobs(limit)
    results: list[dict[str, Any]] = []
    for job in jobs:
        try:
            update_job(job, {"status": "processing"})
            result = processTrendyolPackage({"pkg": job.get("payload") or {}})
            status = "delivered" if result.get("status") == "delivered" else "skipped"
            update_job(job, {"status": status, "last_error": None})
            results.append({"id": job.get("id"), "ok": True, "status": status, "result": result})
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            update_job(job, {"status": "failed", "last_error": detail[:1000]}, increment_attempts=True)
            results.append({"id": job.get("id"), "ok": False, "error": detail[:1000]})
        except Exception as exc:
            err = safe_error(exc)
            update_job(job, {"status": "failed", "last_error": err}, increment_attempts=True)
            results.append({"id": job.get("id"), "ok": False, "error": err})
    return {"ok": True, "processed": len(results), "results": results}


async def enqueue_order_hook(request: Request) -> dict[str, bool]:
    body = await request.json()
    sb = _get_supabase()
    upsert_delivery_job(sb, body, status="pending")
    return {"ok": True}


@router.post("/webhook")
async def trendyol_webhook(request: Request, x_api_key: Optional[str] = Header(default=None)):
    require_internal_key(x_api_key)
    return await enqueue_order_hook(request)


@mp_router.post("/order-hook")
async def marketplace_order_hook(request: Request, x_api_key: Optional[str] = Header(default=None)):
    require_internal_key(x_api_key)
    return await enqueue_order_hook(request)


@router.post("/process-jobs")
def process_jobs(x_api_key: Optional[str] = Header(default=None)):
    require_internal_key(x_api_key)
    if not env_bool("TRENDYOL_ENABLED"):
        return {"ok": True, "skipped": "TRENDYOL_ENABLED=false"}
    return processPendingTrendyolJobs(limit=20)


@router.post("/poll")
def poll(x_api_key: Optional[str] = Header(default=None)):
    require_internal_key(x_api_key)
    if not env_bool("TRENDYOL_ENABLED"):
        return {"ok": True, "skipped": "TRENDYOL_ENABLED=false"}

    statuses = ["Created", "Picking", "Invoiced", "CREATED", "PICKING", "INVOICED"]
    fetched = 0
    sb = _get_supabase()
    for status in statuses:
        path = f"/integration/order/sellers/{seller_id()}/orders?status={status}"
        response = trendyolRequest(path, {"method": "GET"})
        data = response.get("data") or {}
        packages = data.get("content") if isinstance(data, dict) else None
        if packages is None and isinstance(data, dict):
            packages = data.get("shipmentPackages") or data.get("orders")
        if not isinstance(packages, list):
            packages = []
        for pkg in packages:
            if isinstance(pkg, dict):
                upsert_delivery_job(sb, pkg, status="pending")
                fetched += 1

    processed = processPendingTrendyolJobs(limit=20)
    return {"ok": True, "fetched": fetched, "processed": processed}


@router.get("/env-check")
def env_check(x_api_key: Optional[str] = Header(default=None)):
    require_internal_key(x_api_key)
    return {
        "ok": True,
        "hasSellerId": bool(seller_id()),
        "hasApiKey": bool(clean(os.getenv("TRENDYOL_API_KEY"))),
        "hasApiSecret": bool(clean(os.getenv("TRENDYOL_API_SECRET"))),
        "enabled": env_bool("TRENDYOL_ENABLED"),
        "baseUrl": base_url(),
        "suffix": delivery_suffix(),
    }
