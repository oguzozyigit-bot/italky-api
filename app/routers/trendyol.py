from __future__ import annotations

import base64
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests
from fastapi import APIRouter, Header, HTTPException, Request

from app.routers.admin import _get_supabase, _safe_data

router = APIRouter(prefix="/api/trendyol", tags=["Trendyol"])
mp_router = APIRouter(prefix="/api/mp", tags=["Marketplace"])
logger = logging.getLogger(__name__)

SKIPPED_PACKAGE_STATUSES = {
    "cancelled",
    "returned",
    "unsupplied",
    "undelivered",
}
DEFAULT_DELIVERY_SUFFIX = "alternative-delivery"
DEFAULT_BASE_URL = "https://api.trendyol.com/sapigw"
DEFAULT_ANDROID_DOWNLOAD_URL = "https://italky.ai/indir"
DEFAULT_SUPPORT_URL = "https://italky.ai/destek"
CODE_LETTERS = "ABCDEFGHJKLMNPRSTUVYZ"
CODE_DIGITS = "23456789"
FORBIDDEN_CODE_PREFIXES = {"GS", "FB", "FG"}
FORBIDDEN_CODE_NUMBERS = {"1903", "1905", "1907"}


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


def android_download_url() -> str:
    return clean(os.getenv("ANDROID_DOWNLOAD_URL")) or DEFAULT_ANDROID_DOWNLOAD_URL


def support_url() -> str:
    value = clean(os.getenv("TRENDYOL_DELIVERY_TRACKING_INFO")) or clean(os.getenv("TRENDYOL_SUPPORT_URL")) or DEFAULT_SUPPORT_URL
    if value and not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value


def alternative_delivery_contact_mode() -> str:
    mode = clean(os.getenv("TRENDYOL_DELIVERY_CONTACT_MODE")).lower() or "phone"
    if mode not in {"link", "phone"}:
        raise HTTPException(status_code=400, detail="TRENDYOL_DELIVERY_CONTACT_MODE_INVALID")
    return mode


def base_url() -> str:
    return (clean(os.getenv("TRENDYOL_BASE_URL")) or DEFAULT_BASE_URL).rstrip("/")


def require_internal_key(x_api_key: Optional[str]) -> None:
    expected = clean(os.getenv("TRENDYOL_WEBHOOK_API_KEY"))
    if not expected or clean(x_api_key) != expected:
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def require_debug_key(key: Optional[str], x_internal_key: Optional[str]) -> None:
    expected = clean(os.getenv("TRENDYOL_DEBUG_KEY")) or clean(os.getenv("TRENDYOL_WEBHOOK_API_KEY"))
    provided = clean(x_internal_key) or clean(key)
    if not expected or provided != expected:
        raise HTTPException(status_code=403, detail="FORBIDDEN")


def safe_error(exc: Exception) -> str:
    text = str(exc or "").strip()
    for secret in (clean(os.getenv("TRENDYOL_API_KEY")), clean(os.getenv("TRENDYOL_API_SECRET"))):
        if secret:
            text = text.replace(secret, "***")
    return text[:1000]


def log_credential_presence() -> None:
    logger.info(
        "trendyol credentials sellerId=%s apiKey=%s apiSecret=%s enabled=%s",
        bool(seller_id()),
        bool(clean(os.getenv("TRENDYOL_API_KEY"))),
        bool(clean(os.getenv("TRENDYOL_API_SECRET"))),
        env_bool("TRENDYOL_ENABLED"),
    )


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
            detail={"reason": "TRENDYOL_HTTP_ERROR", "status_code": response.status_code, "response": data},
        )
    return {"status_code": response.status_code, "data": data}


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


def line_id_from(line: dict[str, Any]) -> int:
    value = get_value(line, "lineId", "id", "orderLineId")
    try:
        return int(value)
    except Exception:
        return 0


def package_lines(pkg: dict[str, Any]) -> list[dict[str, Any]]:
    lines = pkg.get("lines") or pkg.get("items") or []
    return [line for line in lines if isinstance(line, dict)] if isinstance(lines, list) else []


def extract_packages(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("content", "shipmentPackages", "orders"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if any(key in data for key in ("orderNumber", "id", "shipmentPackageId", "packageId")):
        return [data]
    return []


def first_package(data: Any) -> dict[str, Any]:
    packages = extract_packages(data)
    return packages[0] if packages else {}


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def collect_line_values(lines: list[Any], *keys: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not isinstance(line, dict):
            continue
        value = clean(get_value(line, *keys))
        if value and value not in seen:
            values.append(value)
            seen.add(value)
    return values


def summarize_order_response(data: Any) -> dict[str, Any]:
    pkg = first_package(data)
    shipment_address = pkg.get("shipmentAddress") if isinstance(pkg.get("shipmentAddress"), dict) else {}
    invoice_address = pkg.get("invoiceAddress") if isinstance(pkg.get("invoiceAddress"), dict) else {}
    lines = pkg.get("lines") if isinstance(pkg.get("lines"), list) else []
    return {
        "customerEmail_exists": has_value(pkg.get("customerEmail")),
        "customerFirstName_exists": has_value(pkg.get("customerFirstName")),
        "customerLastName_exists": has_value(pkg.get("customerLastName")),
        "shipmentAddress_exists": bool(shipment_address),
        "shipmentAddress_phone_exists": has_value(shipment_address.get("phone")),
        "invoiceAddress_exists": bool(invoice_address),
        "invoiceAddress_phone_exists": has_value(invoice_address.get("phone")),
        "line_count": len(lines),
        "stock_codes": collect_line_values(lines, "stockCode", "stock_code", "merchantSku"),
        "barcodes": collect_line_values(lines, "barcode"),
        "business_units": collect_line_values(lines, "businessUnit"),
        "shipmentPackageStatus": clean(get_value(pkg, "shipmentPackageStatus", "status", "packageStatus")),
        "cargoProviderName": clean(pkg.get("cargoProviderName")),
        "cargoTrackingNumber": clean(pkg.get("cargoTrackingNumber")),
        "deliveryType": clean(pkg.get("deliveryType")),
        "packageHistories_exists": isinstance(pkg.get("packageHistories"), list) and bool(pkg.get("packageHistories")),
    }


def debug_error_response(error: str, detail: Any, stage: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "detail": detail if isinstance(detail, str) else str(detail), "stage": stage}


def response_preview(data: Any) -> dict[str, Any]:
    packages = extract_packages(data)
    first = packages[0] if packages else {}
    return {
        "top_level_type": type(data).__name__,
        "top_level_keys": list(data.keys())[:30] if isinstance(data, dict) else [],
        "package_count": len(packages),
        "first_package_keys": list(first.keys())[:30] if isinstance(first, dict) else [],
        "first_orderNumber_exists": bool(clean(first.get("orderNumber"))) if isinstance(first, dict) else False,
        "first_package_id_exists": bool(package_id_from(first)) if isinstance(first, dict) else False,
    }


def debug_trendyol_order_request(params: dict[str, str]) -> dict[str, Any]:
    query = "&".join(f"{name}={quote(value)}" for name, value in params.items() if clean(value))
    path = f"/integration/order/sellers/{seller_id()}/orders"
    if query:
        path = f"{path}?{query}"

    logger.info("trendyol request path=%s", path.split("?")[0])
    response = trendyolRequest(path, {"method": "GET"})
    logger.info("trendyol response code=%s", response.get("status_code"))
    logger.info("trendyol response preview=%s", response_preview(response.get("data")))
    return response


def format_trendyol_digital_code(codes: list[str]) -> str:
    code_text = " | ".join(f"{idx}) {code}" for idx, code in enumerate(codes, start=1)) if len(codes) > 1 else codes[0]
    return f"Uygulamayi Indir: {android_download_url()}  Kodu Gir: {code_text}"


def resolve_days_from_stock_code(stock_code: object = "", barcode: object = "") -> Optional[int]:
    stock = clean(stock_code).lower()
    code = clean(barcode).lower()
    pairs = (
        (365, "prm365", "itkai365"),
        (180, "prm180", "itkai180"),
        (90, "prm90", "itkai90"),
        (30, "prm30dg", "itkai30dg"),
        (7, "prm7", "itkai7"),
    )
    for days, stock_prefix, barcode_prefix in pairs:
        if stock.startswith(stock_prefix) or code.startswith(barcode_prefix):
            return days
    return None


def customer_contact_from(pkg: dict[str, Any]) -> dict[str, Any]:
    shipment_address = pkg.get("shipmentAddress") if isinstance(pkg.get("shipmentAddress"), dict) else {}
    invoice_address = pkg.get("invoiceAddress") if isinstance(pkg.get("invoiceAddress"), dict) else {}
    email = clean(pkg.get("customerEmail"))
    shipment_phone = clean(shipment_address.get("phone"))
    invoice_phone = clean(invoice_address.get("phone"))
    phone = shipment_phone or invoice_phone
    return {
        "email": email,
        "phone": phone,
        "shipment_phone": shipment_phone,
        "invoice_phone": invoice_phone,
        "customerEmail_exists": bool(email),
        "phone_exists": bool(phone),
    }


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


def validate_campaign(sb: Any, campaign_id: object) -> None:
    if not clean(campaign_id):
        raise HTTPException(status_code=400, detail="CAMPAIGN_NOT_FOUND")
    res = sb.table("promo_campaigns").select("id").eq("id", campaign_id).limit(1).execute()
    rows = _safe_data(res) or []
    if not rows:
        raise HTTPException(status_code=400, detail="CAMPAIGN_NOT_FOUND")


def is_valid_generated_code(code: str) -> bool:
    value = clean(code).upper()
    if len(value) != 6:
        return False
    prefix = value[:2]
    number = value[2:]
    if prefix in FORBIDDEN_CODE_PREFIXES or number in FORBIDDEN_CODE_NUMBERS:
        return False
    return prefix.isalpha() and number.isdigit()


def generate_trendyol_activation_code() -> str:
    for _ in range(500):
        prefix = f"{random.choice(CODE_LETTERS)}{random.choice(CODE_LETTERS)}"
        number = "".join(random.choice(CODE_DIGITS) for _ in range(4))
        code = f"{prefix}{number}"
        if is_valid_generated_code(code):
            return code
    raise HTTPException(status_code=500, detail="CODE_GENERATION_FAILED")


def code_exists(sb: Any, code_value: str) -> bool:
    res = sb.table("promo_codes").select("id").eq("code_value", code_value).limit(1).execute()
    return bool(_safe_data(res) or [])


def existing_trendyol_code(
    sb: Any,
    order_number: str,
    package_id: int,
    line_id: int,
    quantity_index: int,
) -> Optional[dict[str, Any]]:
    res = (
        sb.table("promo_codes")
        .select("*")
        .eq("marketplace", "trendyol")
        .eq("marketplace_order_number", order_number)
        .eq("marketplace_package_id", package_id)
        .eq("marketplace_line_id", line_id)
        .eq("marketplace_quantity_index", quantity_index)
        .limit(1)
        .execute()
    )
    rows = _safe_data(res) or []
    return rows[0] if rows else None


def create_or_get_trendyol_activation_code(
    sb: Any,
    mapping: dict[str, Any],
    pkg: dict[str, Any],
    line: dict[str, Any],
    quantity_index: int,
) -> str:
    order_number = order_number_from(pkg)
    package_id = package_id_from(pkg)
    line_id = line_id_from(line)
    if not order_number or not package_id:
        raise HTTPException(status_code=400, detail="ORDER_PACKAGE_REQUIRED")

    existing = existing_trendyol_code(sb, order_number, package_id, line_id, quantity_index)
    if existing and clean(existing.get("code_value")):
        return clean(existing.get("code_value")).upper()

    campaign_id = mapping.get("campaign_id")
    validate_campaign(sb, campaign_id)
    barcode = clean(line.get("barcode") or mapping.get("barcode"))

    for _ in range(50):
        code_value = generate_trendyol_activation_code()
        if code_exists(sb, code_value):
            continue
        body = {
            "campaign_id": campaign_id,
            "code_value": code_value,
            "delivery_type": "manual",
            "is_active": True,
            "is_used": False,
            "marketplace": "trendyol",
            "delivery_status": "reserved",
            "reserved_at": iso_now(),
            "marketplace_order_number": order_number,
            "marketplace_package_id": package_id,
            "marketplace_line_id": line_id,
            "marketplace_quantity_index": quantity_index,
            "marketplace_barcode": barcode,
        }
        try:
            sb.table("promo_codes").insert(body).execute()
            return code_value
        except Exception as exc:
            again = existing_trendyol_code(sb, order_number, package_id, line_id, quantity_index)
            if again and clean(again.get("code_value")):
                return clean(again.get("code_value")).upper()
            if code_exists(sb, code_value):
                continue
            raise HTTPException(status_code=500, detail=f"CODE_GENERATION_FAILED: {safe_error(exc)}")

    raise HTTPException(status_code=500, detail="CODE_GENERATION_FAILED")


def delivery_job_for(sb: Any, order_number: str, package_id: int) -> Optional[dict[str, Any]]:
    res = (
        sb.table("marketplace_delivery_jobs")
        .select("*")
        .eq("marketplace", "trendyol")
        .eq("order_number", order_number)
        .eq("package_id", package_id)
        .limit(1)
        .execute()
    )
    rows = _safe_data(res) or []
    return rows[0] if rows else None


def automation_from_job(job: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not job:
        return {}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    automation = payload.get("automation") if isinstance(payload.get("automation"), dict) else {}
    return automation


def send_trendyol_digital_code_email(email: str, digital_code: str, dry_run: bool) -> dict[str, Any]:
    return {"to": email, "sent": False, "enabled": False, "dry_run": dry_run, "reason": "EXTERNAL_EMAIL_DISABLED"}


def send_trendyol_digital_code_sms(phone: str, digital_code: str, dry_run: bool) -> dict[str, Any]:
    return {"to": phone, "sent": False, "enabled": False, "dry_run": dry_run, "reason": "TRENDYOL_ADEL_SMS_USED"}


def build_alternative_delivery_payload(digital_code: str) -> dict[str, Any]:
    mode = alternative_delivery_contact_mode()
    if mode == "phone":
        phone = clean(os.getenv("TRENDYOL_DELIVERY_PHONE"))
        if not phone:
            raise HTTPException(status_code=400, detail="TRENDYOL_DELIVERY_PHONE_REQUIRED")
        return {"isPhoneNumber": True, "trackingInfo": phone, "params": {"digitalCode": digital_code}}

    tracking_info = support_url()
    if not tracking_info:
        raise HTTPException(status_code=400, detail="TRENDYOL_DELIVERY_TRACKING_INFO_REQUIRED")
    return {"isPhoneNumber": False, "trackingInfo": tracking_info, "params": {"digitalCode": digital_code}}


def send_alternative_delivery(package_id: int, digital_code: str, dry_run: bool) -> dict[str, Any]:
    try:
        planned_payload = build_alternative_delivery_payload(digital_code)
    except HTTPException as exc:
        if dry_run:
            return {"sent": False, "dry_run": True, "reason": exc.detail}
        raise

    if dry_run:
        return {"sent": False, "dry_run": True, "reason": "DRY_RUN", "payload": planned_payload}
    try:
        delivery = deliverTrendyolDigitalCode({"packageId": package_id, "digitalCode": digital_code})
        return {"sent": True, "dry_run": False, "payload": delivery.get("payload"), "response": delivery.get("response")}
    except HTTPException as exc:
        return {"sent": False, "dry_run": False, "reason": "TRENDYOL_ADEL_FAILED", "detail": exc.detail, "payload": planned_payload}
    except Exception as exc:
        return {"sent": False, "dry_run": False, "reason": "TRENDYOL_ADEL_FAILED", "detail": safe_error(exc), "payload": planned_payload}


def manual_deliver_package(package_id: int, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"attempted": False, "dry_run": True, "reason": "DRY_RUN"}
    path = f"/integration/order/sellers/{seller_id()}/shipment-packages/{package_id}/manual-deliver"
    try:
        response = trendyolRequest(path, {"method": "PUT", "body": {}})
        return {"attempted": True, "success": True, "response": response}
    except HTTPException as exc:
        return {"attempted": True, "success": False, "detail": exc.detail}
    except Exception as exc:
        return {"attempted": True, "success": False, "detail": safe_error(exc)}


def log_automation_step(summary: dict[str, Any]) -> None:
    logger.info(
        "trendyol automation orderNumber=%s shipmentPackageId=%s stockCode=%s barcode=%s resolved_days=%s "
        "generated_code=%s customerEmail_exists=%s phone_exists=%s email_sent=%s sms_sent=%s "
        "alternative_delivery_sent=%s manual_deliver_status=%s",
        summary.get("orderNumber"),
        summary.get("shipmentPackageId"),
        summary.get("stockCode"),
        summary.get("barcode"),
        summary.get("resolved_days"),
        summary.get("generated_code"),
        summary.get("customerEmail_exists"),
        summary.get("phone_exists"),
        summary.get("email_sent"),
        summary.get("sms_sent"),
        summary.get("alternative_delivery_sent"),
        summary.get("manual_deliver_status"),
    )


def select_automatable_line(sb: Any, pkg: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], Optional[dict[str, Any]], int]:
    for line in package_lines(pkg):
        stock_code = clean(get_value(line, "merchantSku", "stockCode", "stock_code"))
        barcode = clean(line.get("barcode"))
        days = resolve_days_from_stock_code(stock_code, barcode)
        if not days:
            continue
        mapping = get_mapping_for_line(sb, line) if not dry_run else {"dry_run": True}
        return line, mapping, days
    raise HTTPException(status_code=400, detail="NO_SUPPORTED_TRENDYOL_SKU")


def already_processed_reason(reserved_rows: list[dict[str, Any]], existing_automation: dict[str, Any]) -> Optional[str]:
    for row in reserved_rows:
        if row.get("is_used") is True:
            return "CODE_ALREADY_USED"

    for row in reserved_rows:
        status = clean(row.get("delivery_status")).lower()
        if status in {"delivered", "sent"} or row.get("delivered_at"):
            return "ADEL_ALREADY_SENT"

    alternative_delivery = (
        existing_automation.get("alternative_delivery")
        if isinstance(existing_automation.get("alternative_delivery"), dict)
        else {}
    )
    if alternative_delivery.get("sent") is True:
        return "ADEL_ALREADY_SENT"
    return None


def automate_trendyol_package(pkg: dict[str, Any], dry_run: bool, attempt_manual_deliver: bool = False) -> dict[str, Any]:
    sb = _get_supabase()
    package_id = package_id_from(pkg)
    order_number = order_number_from(pkg)
    if not package_id:
        raise HTTPException(status_code=400, detail="PACKAGE_ID_REQUIRED")
    if not order_number:
        raise HTTPException(status_code=400, detail="ORDER_NUMBER_REQUIRED")

    line, mapping, resolved_days = select_automatable_line(sb, pkg, dry_run)
    stock_code = clean(get_value(line, "merchantSku", "stockCode", "stock_code"))
    barcode = clean(line.get("barcode"))
    business_unit = clean(line.get("businessUnit"))
    if business_unit and business_unit != "Digital Goods":
        raise HTTPException(status_code=400, detail="LINE_IS_NOT_DIGITAL_GOODS")
    if not dry_run and not mapping:
        raise HTTPException(status_code=400, detail="SKU_MAPPING_NOT_FOUND")

    existing_job = delivery_job_for(sb, order_number, package_id) if not dry_run else None
    existing_automation = automation_from_job(existing_job)
    reserved_codes = existing_automation.get("reserved_codes") if isinstance(existing_automation.get("reserved_codes"), list) else []
    reserved_codes = [clean(code).upper() for code in reserved_codes if clean(code)]

    if not dry_run and not reserved_codes:
        for quantity_index in range(1, line_quantity(line) + 1):
            reserved_codes.append(create_or_get_trendyol_activation_code(sb, mapping or {}, pkg, line, quantity_index))

    digital_code = format_trendyol_digital_code(reserved_codes) if reserved_codes else "DRY_RUN_CODE_PLACEHOLDER"
    contact = customer_contact_from(pkg)
    email_result = send_trendyol_digital_code_email(contact["email"], digital_code, dry_run)
    sms_result = send_trendyol_digital_code_sms(contact["phone"], digital_code, dry_run)
    reserved_rows = get_reserved_rows(sb, reserved_codes) if not dry_run and reserved_codes else []
    processed_reason = already_processed_reason(reserved_rows, existing_automation) if not dry_run else None
    if processed_reason:
        adel_result = {
            "sent": False,
            "skipped": True,
            "dry_run": False,
            "reason": processed_reason,
            "message": "Existing Trendyol code/order is already processed; ADEL resend skipped.",
        }
    else:
        adel_result = send_alternative_delivery(package_id, digital_code, dry_run)
    manual_deliver = manual_deliver_package(package_id, dry_run) if attempt_manual_deliver and not processed_reason else {
        "attempted": False,
        "status": "already_processed" if processed_reason else "not_required_now",
        "reason": processed_reason or "ADEL_SMS_FLOW_ADVANCES_WITHOUT_MANUAL_DELIVER_IN_TEST",
    }

    automation = {
        "dry_run": dry_run,
        "orderNumber": order_number,
        "shipmentPackageId": package_id,
        "stockCode": stock_code,
        "barcode": barcode,
        "businessUnit": business_unit,
        "resolved_days": resolved_days,
        "reserved_codes": reserved_codes,
        "digital_code": digital_code if reserved_codes else None,
        "customer_contact": contact,
        "already_processed": bool(processed_reason),
        "already_processed_reason": processed_reason,
        "email": email_result,
        "sms": sms_result,
        "alternative_delivery": adel_result,
        "manual_deliver": manual_deliver,
        "shipmentPackageStatus": package_status_from(pkg),
        "cargoProviderName": clean(pkg.get("cargoProviderName")),
        "updated_at": iso_now(),
    }

    if not dry_run:
        job_payload = dict(pkg)
        job_payload["automation"] = automation
        job_status = "delivered" if adel_result.get("sent") or processed_reason else "failed"
        upsert_delivery_job(sb, job_payload, status=job_status)
        if reserved_codes and not processed_reason:
            update_reserved_rows(
                sb,
                reserved_codes,
                {
                    "delivery_status": "delivered" if adel_result.get("sent") else "failed",
                    "delivered_at": iso_now() if adel_result.get("sent") else None,
                    "delivery_payload": {"alternative_delivery": adel_result.get("payload"), "manual_deliver": manual_deliver},
                    "delivery_response": adel_result.get("response"),
                    "delivery_error": None if adel_result.get("sent") else clean(adel_result.get("reason") or adel_result.get("detail")),
                },
                increment_attempts=True,
            )

    log_automation_step({
        "orderNumber": order_number,
        "shipmentPackageId": package_id,
        "stockCode": stock_code,
        "barcode": barcode,
        "resolved_days": resolved_days,
        "generated_code": bool(reserved_codes),
        "customerEmail_exists": contact["customerEmail_exists"],
        "phone_exists": contact["phone_exists"],
        "email_sent": email_result.get("sent"),
        "sms_sent": sms_result.get("sent"),
        "alternative_delivery_sent": adel_result.get("sent"),
        "manual_deliver_status": manual_deliver.get("status") or manual_deliver.get("success") or manual_deliver.get("reason"),
    })
    return {"ok": True, "automation": automation}


def deliverTrendyolDigitalCode(payload: dict[str, Any]) -> dict[str, Any]:
    package_id = clean(payload.get("packageId"))
    digital_code = clean(payload.get("digitalCode"))
    if not package_id:
        raise HTTPException(status_code=400, detail="PACKAGE_ID_REQUIRED")
    if len(digital_code) < 6 or len(digital_code) > 120:
        raise HTTPException(status_code=400, detail="DIGITAL_CODE_LENGTH_INVALID")

    body = build_alternative_delivery_payload(digital_code)
    path = f"/integration/order/sellers/{seller_id()}/shipment-packages/{package_id}/{delivery_suffix()}"
    response = trendyolRequest(path, {"method": "PUT", "body": body})
    return {"payload": body, "response": response}


def get_reserved_rows(sb: Any, codes: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in codes:
        res = (
            sb.table("promo_codes")
            .select(
                "id, code_value, is_used, delivery_status, delivered_at, "
                "delivery_attempt_count, delivery_error"
            )
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
    pkg = normalize_package(payload)
    status = package_status_from(pkg)
    if status.lower() in SKIPPED_PACKAGE_STATUSES:
        return {"ok": True, "status": "skipped", "reason": "PACKAGE_STATUS_SKIPPED", "package_id": package_id_from(pkg)}
    return automate_trendyol_package(pkg, dry_run=False)


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
            status = "delivered" if result.get("ok") else "failed"
            update_job(job, {"status": status, "last_error": None if result.get("ok") else str(result)[:1000]})
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


@router.get("/automation/package")
def automation_package(
    shipmentPackageId: Optional[str] = None,
    orderNumber: Optional[str] = None,
    dry_run: bool = True,
    attempt_manual_deliver: bool = False,
    key: Optional[str] = None,
    x_internal_key: Optional[str] = Header(default=None, alias="X-Internal-Key"),
):
    require_debug_key(key, x_internal_key)
    package_id = clean(shipmentPackageId)
    order_number = clean(orderNumber)
    logger.info(
        "trendyol automation/package called orderNumber_exists=%s shipmentPackageId_exists=%s dry_run=%s",
        bool(order_number),
        bool(package_id),
        dry_run,
    )
    log_credential_presence()
    if not package_id and not order_number:
        return debug_error_response("ORDER_OR_PACKAGE_REQUIRED", "shipmentPackageId or orderNumber is required", "validate")

    params: dict[str, str] = {}
    if package_id:
        params["shipmentPackageIds"] = package_id
    if order_number:
        params["orderNumber"] = order_number

    try:
        response = debug_trendyol_order_request(params)
        raw = response.get("data") or {}
        pkg = first_package(raw)
        if not pkg:
            return debug_error_response("PACKAGE_NOT_FOUND", "Trendyol response did not include a package", "trendyol_response")
        result = automate_trendyol_package(pkg, dry_run=dry_run, attempt_manual_deliver=attempt_manual_deliver)
        return {
            "ok": bool(result.get("ok")),
            "dry_run": dry_run,
            "query": {"orderNumber": order_number, "shipmentPackageId": package_id},
            "summary": summarize_order_response(raw),
            "result": result,
            "raw": raw,
        }
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return debug_error_response("TRENDYOL_AUTOMATION_FAILED", exc.detail, "automation")
    except Exception as exc:
        return debug_error_response("TRENDYOL_AUTOMATION_FAILED", safe_error(exc), "unknown")


@router.get("/debug/order")
def debug_order(
    orderNumber: Optional[str] = None,
    key: Optional[str] = None,
    x_internal_key: Optional[str] = Header(default=None, alias="X-Internal-Key"),
):
    require_debug_key(key, x_internal_key)
    order_number = clean(orderNumber)
    logger.info("trendyol debug/order called orderNumber_exists=%s shipmentPackageId_exists=%s", bool(order_number), False)
    log_credential_presence()
    if not order_number:
        return debug_error_response("ORDER_NUMBER_REQUIRED", "orderNumber query parameter is required", "validate")

    try:
        response = debug_trendyol_order_request({"orderNumber": order_number})
        raw = response.get("data") or {}
        return {"ok": True, "query": {"orderNumber": order_number, "shipmentPackageId": ""}, "summary": summarize_order_response(raw), "raw": raw}
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return debug_error_response("TRENDYOL_DEBUG_ORDER_FAILED", exc.detail, "trendyol_request")
    except Exception as exc:
        return debug_error_response("TRENDYOL_DEBUG_ORDER_FAILED", safe_error(exc), "unknown")


@router.get("/debug/package")
def debug_package(
    shipmentPackageId: Optional[str] = None,
    key: Optional[str] = None,
    x_internal_key: Optional[str] = Header(default=None, alias="X-Internal-Key"),
):
    require_debug_key(key, x_internal_key)
    package_id = clean(shipmentPackageId)
    logger.info("trendyol debug/package called orderNumber_exists=%s shipmentPackageId_exists=%s", False, bool(package_id))
    log_credential_presence()
    if not package_id:
        return debug_error_response("SHIPMENT_PACKAGE_ID_REQUIRED", "shipmentPackageId query parameter is required", "validate")

    try:
        response = debug_trendyol_order_request({"shipmentPackageIds": package_id})
        raw = response.get("data") or {}
        return {"ok": True, "query": {"orderNumber": "", "shipmentPackageId": package_id}, "summary": summarize_order_response(raw), "raw": raw}
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return debug_error_response("TRENDYOL_DEBUG_PACKAGE_FAILED", exc.detail, "trendyol_request")
    except Exception as exc:
        return debug_error_response("TRENDYOL_DEBUG_PACKAGE_FAILED", safe_error(exc), "unknown")


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
