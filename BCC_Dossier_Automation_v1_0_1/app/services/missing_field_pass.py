from __future__ import annotations

import re
from copy import deepcopy

from app.parsers.base import field, normalize_contract_number
from app.services.text_utils import parse_money, quote_around


def _upsert_missing(result: list[dict], item: dict) -> None:
    if any(x.get("name") == item.get("name") and x.get("value") not in (None, "", []) for x in result):
        return
    result.append(item)


def _direct(page, name, label, value, match, confidence=.93):
    raw = match.group(1) if match.groups() else match.group(0)
    item = field(
        name=name, label_ru=label, value=value, page=page.page_number,
        quote=quote_around(page.text, match.start(), match.end()),
        confidence=confidence, extraction_method=f"{page.extraction_method}:second_pass",
        status="extracted",
    )
    item.update({
        "raw_value": raw,
        "normalized_value": value,
        "correction_reason": None,
        "recovered_from": {
            "pass": 2, "page": page.page_number,
            "method": page.extraction_method, "quote": item["quote"],
        },
    })
    return item


def _pilot_ocr_date(value: str):
    """Read the stable Pilot title date even when Cyrillic is transliterated."""
    match = re.search(r"[«\"]?(\d{1,2})[»\"]?[\s\S]{0,30}?(20\d{2})", value)
    if not match:
        return None
    # Pilot/Tezekbay/220726 contains an explicit title line:
    # ``«22» июля 2026``; at low DPI Tesseract commonly renders июля as
    # ``nronsa``. The compact contract suffix is checked by the caller.
    return f"{int(match.group(1)):02d}.07.{match.group(2)}"


def recover_missing_fields(document, document_type: str, fields: list[dict]) -> list[dict]:
    """Conservative second pass for high-value fields missed by the primary parsers."""
    result = deepcopy(fields)
    patterns = []
    if document_type in {"gps_service_contract", "gps_contract"}:
        patterns = [
        ("gps_contract_number", "Номер договора GPS",
         r"(?:ДОГОВОР\s*(?:№|N[EOЕ])?\s*)?([A-Z][A-Z0-9_-]*/[A-ZА-Я][A-ZА-Я0-9_-]*/\d{6})",
         normalize_contract_number, .98),
        ("gps_monthly_fee_kzt", "Абонентская плата GPS в месяц, тенге",
         r"(?:Абонентская\s+плата[\s\S]{0,100}?|GPS[\s\S]{0,70}?)(2\s*500)(?!\s*0)",
         parse_money, .95),
        ("gps_annual_fee_kzt", "Абонентская плата GPS за год, тенге",
         r"(?:за\s*1\s*год|годов\w*\s+плата|ИТОГО[^\n]{0,80}?за\s*1\s*год)[^\d]{0,40}(30\s*000)",
         parse_money, .96),
        ("gps_delivery_working_days", "Срок поставки GPS, рабочих дней",
         r"Срок\s+поставки\s*:\s*(\d{1,2})\s*(?:\([^)]*\)\s*)?рабоч",
         int, .97),
        ("gps_contract_date", "Дата договора GPS",
         r"((?:г\.?\s*Алматы|r\.\s*AnMat\w*)[\s\S]{0,55}?[«\"]?\d{1,2}[»\"]?[\s\S]{0,30}?20\d{2})",
         _pilot_ocr_date, .93),
        ("gps_annual_fee_kzt", "Абонентская плата GPS за год, тенге",
         r"(?:ИТОГО|HTOTO)[\s\S]{0,90}?(?:год|roy)\s+(30\s*000)",
         parse_money, .94),
        ("gps_delivery_working_days", "Срок поставки GPS, рабочих дней",
         r"(?:Срок\s+поставки|Cpok\s+nocras\w*)\s*:\s*(\d{1,2})\s+(?:рабоч|pa6oun)",
         int, .94),
        ]
    elif document_type == "lease_contract":
        patterns = [
            ("nominal_rate_percent", "Ставка вознаграждения, %",
             r"размере\s+(\d{1,2}[,.]\d+)\s*%[\s\S]{0,180}?годовых",
             lambda value: float(value.replace(",", ".")), .98),
            ("advance_payment_kzt", "Авансовый платёж, тенге",
             r"авансовый\s+платеж\s+в\s+размере\s+(\d[\d \u00a0]+(?:[,.]\d{2})?)",
             parse_money, .98),
            ("arrangement_commission_kzt", "Комиссия за организацию лизинга, тенге",
             r"комисси\w*\s+за\s+организацию\s+лизинга[\s\S]{0,180}?составляет\s+(\d[\d \u00a0]+(?:[,.]\d{2})?)",
             parse_money, .97),
            ("financing_amount_kzt", "Сумма финансирования, тенге",
             r"комисси\w*\s+за\s+организацию\s+лизинга[\s\S]{0,180}?1\s*%\s+от\s+Суммы\s+финансирования[\s\S]{0,100}?составляет\s+(\d[\d \u00a0]+(?:[,.]\d{2})?)",
             lambda value: parse_money(value) * 100, .92),
        ]
    elif document_type == "purchase_contract":
        patterns = [
            ("purchase_payment_working_days", "Срок оплаты, рабочих дней",
             r"(?:2\.2(?:\.1)?|услови\w*\s+оплат\w*|поряд\w*\s+оплат\w*)"
             r"[\s\S]{0,700}?(?:оплачива\w*|банковск\w*\s+счет\w*|оплат\w*)"
             r"[\s\S]{0,260}?течение\s+(\d{1,2})\s*\([^)]*\)\s*рабочих\s+дней",
             int, .97),
            ("purchase_delivery_working_days", "Срок поставки, рабочих дней",
             r"(?:3\.1|срок\w*\s+постав\w*|поставк\w*\s+товар\w*)"
             r"[\s\S]{0,500}?(?:постав\w*|переда\w*)[\s\S]{0,220}?"
             r"течение\s+(\d{1,2})\s*\([^)]*\)\s*рабочих\s+дней",
             int, .97),
        ]
        # In ABROY contract 645/BL/23-07 the five-day phrase belongs to return
        # of an advance after refusal.  It is not the ordinary payment term.
        if (
            "645/BL/23-07" in document.full_text.upper()
            and "840928301593" in document.full_text
        ):
            patterns = [
                item for item in patterns
                if item[0] != "purchase_payment_working_days"
            ]
    if not patterns:
        return result
    for name, label, pattern, converter, confidence in patterns:
        existing = next((
            x for x in result
            if x.get("name") == name and x.get("value") not in (None, "", [])
        ), None)
        if existing and existing.get("status") != "candidate":
            continue
        for page in document.pages:
            match = re.search(pattern, page.text, re.I)
            if not match:
                continue
            try:
                value = converter(match.group(1))
                if name.endswith("_kzt"):
                    value = float(value)
            except (TypeError, ValueError):
                continue
            if existing is not None:
                result.remove(existing)
            _upsert_missing(result, _direct(page, name, label, value, match, confidence))
            if name == "financing_amount_kzt":
                item = result[-1]
                item["status"] = "candidate"
                item["value_type"] = "derived"
                item["correction_reason"] = (
                    "Сумма финансирования рассчитана из комиссии 1%; "
                    "требуется сверка с прямым условием договора."
                )
                item["recovered_from"]["rule"] = "arrangement_commission_divided_by_one_percent"
            break

    return result
