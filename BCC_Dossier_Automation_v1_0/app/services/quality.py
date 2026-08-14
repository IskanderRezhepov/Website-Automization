
from __future__ import annotations

import re


REQUIRED_BY_TYPE = {
    "real_estate_registration_notice": [
        ("registration_notice_number", "Номер уведомления о регистрации"),
        ("registration_notice_date", "Дата уведомления о регистрации"),
        ("property_address", "Адрес недвижимости"),
        ("cadastral_number", "Кадастровый номер"),
    ],
    "purchase_contract": [
        ("purchase_contract_number", "Номер договора купли-продажи"),
        ("total_amount_kzt", "Общая стоимость договора"),
    ],
    "lease_contract": [
        ("lease_contract_number", "Номер договора лизинга"),
        ("lease_asset_value_kzt", "Стоимость предмета лизинга"),
    ],
    "acceptance_act": [
        ("act_number", "Номер акта"),
        ("linked_purchase_contract", "Связанный родительский договор"),
    ],
    "payment_schedule": [
        ("lease_contract_number", "Номер договора лизинга"),
        ("loan_amount_kzt", "Сумма займа / транша"),
    ],
    "addendum": [
        ("lease_contract_number", "Номер основного договора"),
    ],
    "cash_pledge_agreement": [
        ("pledge_contract_number", "Номер договора залога"),
        ("pledge_amount_kzt", "Сумма денежного залога"),
        ("deposit_iban", "Счёт денежного залога"),
    ],
    "direct_debit_agreement": [
        ("linked_guarantee_contract_number", "Связанный договор гарантии"),
        ("sender_iban", "Счёт отправителя"),
    ],
    "credit_line_agreement": [
        ("credit_line_number", "Номер соглашения об открытии КЛ"),
        ("credit_line_amount_kzt", "Сумма кредитной линии"),
    ],
    "cash_pledge_agreement": [
        ("pledge_contract_number", "Номер договора залога"),
        ("pledge_amount_kzt", "Сумма денежного залога"),
    ],
    "subsidy_agreement": [
        ("subsidy_contract_number", "Номер договора субсидирования"),
        ("financing_amount_kzt", "Сумма финансирования"),
    ],
}

DIRECT_EVIDENCE_FIELDS = {
    "purchase_contract_number", "purchase_contract_date", "total_amount_kzt",
    "lease_contract_number", "lease_contract_date", "lease_asset_value_kzt",
    "seller_name", "buyer_name", "lessee_name", "lessor_name",
    "insurance_policy_number", "insurance_contract_date",
    "insurance_sum_kzt", "insurance_premium_kzt",
    "insurance_beneficiary", "gps_contract_number", "gps_contract_date",
    "gps_provider", "gps_customer", "gps_monthly_fee_kzt",
    "gps_annual_fee_kzt", "gps_delivery_working_days",
}

def ensure_review_audit(fields: list[dict]) -> list[dict]:
    """Give every non-final machine value a consistent auditable provenance."""
    for item in fields:
        value = item.get("value")

        if (
            item.get("name") == "engine_displacement_cm3"
            and re.search(
                r"Объ[её]м\s+двигателя\s*,?\s*м[³3]",
                str(item.get("quote") or ""),
                re.I,
            )
        ):
            item["status"] = "candidate"
            item["value_type"] = "direct"
            item["notes"] = (
                "Число прочитано напрямую, но единица в источнике напечатана "
                "как м³ и требует проверки (вероятно, см³)."
            )
        if (
            isinstance(value, str)
            and item.get("name") in {
                "lessee_name", "buyer_name", "seller_name", "insurance_holder",
                "insurance_company", "gps_provider", "gps_customer",
            }
            and "«" in value and "»" not in value
        ):
            item.setdefault("raw_value", value)
            item["value"] = value.rstrip(" ,;:") + "»"
            item["normalized_value"] = item["value"]
            item.setdefault("correction_reason", "Закрыта незавершённая кавычка в наименовании стороны.")
            item.setdefault("recovered_from", {
                "method": item.get("extraction_method") or "party_name_normalization",
                "page": item.get("page"),
                "quote": item.get("quote"),
            })
            if item.get("status") == "extracted":
                item["status"] = "corrected"
        if item.get("status") not in {"candidate", "corrected"}:
            continue
        item.setdefault("raw_value", item.get("original_value", item.get("value")))
        item.setdefault("normalized_value", item.get("value"))
        item.setdefault(
            "correction_reason",
            item.get("notes") or (
                "Значение требует проверки."
                if item.get("status") == "candidate"
                else "Значение нормализовано автоматически."
            ),
        )
        item.setdefault("recovered_from", {
            "method": item.get("extraction_method") or "unknown",
            "page": item.get("page"),
            "quote": item.get("quote"),
        })
    return fields


def review_fields(document_type: str, fields: list[dict]) -> list[dict]:
    warnings: list[dict] = []
    by_name = {item["name"]: item for item in fields}

    for item in fields:
        value = item.get("value")

        if item.get("confidence", 0) < 0.6 and item.get("status") != "candidate":
            warnings.append(
                {
                    "severity": "medium",
                    "field": item["label_ru"],
                    "message": "Низкая уверенность OCR; требуется сверка с оригиналом.",
                }
            )

        if item["name"].endswith("_number") and isinstance(value, str):
            short_numeric_allowed = item["name"] in {
                "act_number", "addendum_number",
                "linked_insurance_appendix_number",
            }
            invalid_short = len(value) < 3 and not (
                short_numeric_allowed and re.fullmatch(r"\d{1,2}", value)
            )
            if not re.search(r"\d", value) or invalid_short:
                warnings.append(
                    {
                        "severity": "high",
                        "field": item["label_ru"],
                        "message": f"Подозрительный номер: {value}",
                    }
                )

        if item.get("extraction_method") == "filename":
            warnings.append(
                {
                    "severity": "medium",
                    "field": item["label_ru"],
                    "message": "Значение взято из имени файла и требует подтверждения по тексту.",
                }
            )

        if (
            item.get("name") in DIRECT_EVIDENCE_FIELDS
            and item.get("status") in {"extracted", "corrected"}
            and item.get("value_type", "direct") == "direct"
            and not item.get("quote")
            and not (
                item.get("name") == "gps_contract_date"
                and item.get("extraction_method") == "targeted"
                and item.get("page")
                and float(item.get("confidence") or 0) >= 0.95
            )
        ):
            warnings.append({
                "severity": "medium",
                "field": item["label_ru"],
                "message": "Нет прямой цитаты из документа; значение требует сверки.",
            })

    for required_name, required_label in REQUIRED_BY_TYPE.get(document_type, []):
        if (
            document_type == "addendum"
            and required_name == "lease_contract_number"
            and "linked_subsidy_contract" in by_name
        ):
            continue
        present = required_name in by_name

        # A PDF package can contain several acceptance acts. In that case
        # act_numbers is the correct document-level result.
        if (
            document_type == "acceptance_act"
            and required_name == "act_number"
            and ("act_numbers" in by_name or "act_package_detected" in by_name)
        ):
            present = True
        if (
            document_type == "acceptance_act"
            and required_name == "linked_purchase_contract"
            and (
                "linked_lease_contract_number" in by_name
                or "lease_contract_number" in by_name
            )
        ):
            present = True

        if not present:
            warnings.append(
                {
                    "severity": "high",
                    "field": required_label,
                    "message": "Ключевое поле не извлечено.",
                }
            )

    if document_type == "payment_schedule":
        amount = by_name.get("loan_amount_kzt")
        if amount and amount.get("value_type") == "calculated":
            warnings.append(
                {
                    "severity": "medium",
                    "field": "Сумма займа / транша",
                    "message": (
                        "Сумма рассчитана из первой строки графика и должна быть "
                        "подтверждена по договору или титульной части графика."
                    ),
                }
            )

    for item in fields:
        if (
            item.get("name") == "engine_displacement_cm3"
            and re.search(
                r"Объ[её]м\s+двигателя\s*,?\s*м[³3]",
                str(item.get("quote") or ""),
                re.I,
            )
        ):
            warnings.append({
                "severity": "medium",
                "field": item["label_ru"],
                "message": (
                    "Число извлечено, но единица объёма двигателя в источнике "
                    "напечатана как м³; вероятную единицу см³ нужно подтвердить."
                ),
            })

    if document_type == "gps_service_contract":
        monthly = by_name.get("gps_monthly_fee_kzt", {}).get("value")
        annual = by_name.get("gps_annual_fee_kzt", {}).get("value")
        quantity = by_name.get("gps_device_quantity", {}).get("value") or 1
        try:
            if monthly is not None and annual is not None:
                expected = float(monthly) * int(quantity) * 12
                if abs(expected - float(annual)) > max(1.0, float(annual) * .01):
                    warnings.append({
                        "severity": "high",
                        "field": "Абонентская плата GPS",
                        "message": (
                            f"Арифметическое противоречие: {monthly} × "
                            f"{quantity} объект(а) × 12 "
                            f"не равно {annual} тенге."
                        ),
                    })
        except (TypeError, ValueError):
            pass

    if document_type == "acceptance_act":
        amount = by_name.get("act_total_amount_kzt", {}).get("value")
        try:
            if amount is not None and float(amount) < 10000:
                warnings.append(
                    {
                        "severity": "high",
                        "field": "Общая стоимость по акту",
                        "message": "Сумма выглядит нереалистично малой.",
                    }
                )
        except (TypeError, ValueError):
            pass

        groups = by_name.get("asset_identifier_groups", {}).get("value")
        if isinstance(groups, dict) and len(groups) > 1:
            warnings.append(
                {
                    "severity": "medium",
                    "field": "VIN / номера шасси",
                    "message": (
                        "Найдены несколько групп 17-значных кодов. "
                        "Расчётное количество основано на крупнейшей группе и требует сверки."
                    ),
                }
            )

    return warnings
