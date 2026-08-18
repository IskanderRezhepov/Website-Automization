from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re

from .financial_reconciliation import build_financial_checks


ROLE_FIELD_NAMES = {
    "lessee_iin_bin": "Лизингополучатель",
    "borrower_iin_bin": "Заёмщик",
    "buyer_iin_bin": "Покупатель",
    "seller_iin_bin": "Продавец",
    "lessor_iin_bin": "Лизингодатель",
    "guarantor_iin_bin": "Гарант",
    "principal_iin_bin": "Принципал",
    "beneficiary_iin_bin": "Бенефициар",
    "insurance_beneficiary_iin_bin": "Выгодоприобретатель по страхованию",
    "direct_debit_beneficiary_iin_bin": "Бенефициар прямого дебетования",
    "payment_beneficiary_iin_bin": "Получатель платежа",
    "insurance_holder_iin_bin": "Страхователь",
    "insured_iin_bin": "Застрахованный",
    "beneficiary_excess_iin_bin": "Выгодоприобретатель сверх остатка долга",
    "pledger_iin_bin": "Залогодатель",
    "subsidy_recipient_bin": "Получатель субсидии",
    "gps_customer_iin_bin": "Заказчик GPS",
    "payment_payer_iin_bin": "Плательщик",
    "direct_debit_sender_iin_bin": "Отправитель прямого дебетования",
    "recipient_bin": "Получатель субсидии",
}

CLIENT_IDENTITY_FIELDS = {
    "lessee_iin_bin", "borrower_iin_bin", "principal_iin_bin",
    "insurance_holder_iin_bin", "insured_iin_bin", "gps_customer_iin_bin",
    "direct_debit_sender_iin_bin", "payment_payer_iin_bin",
    "subsidy_recipient_bin", "recipient_bin",
}

CONTRACT_PRIMARY_FIELDS = {
    "lease_contract_number": "Договор финансового лизинга",
    "purchase_contract_number": "Договор купли-продажи",
    "credit_line_number": "Соглашение о кредитной линии",
    "subsidy_contract_number": "Договор субсидирования",
    "pledge_contract_number": "Договор залога",
    "guarantee_contract_number": "Договор гарантии",
    "gps_contract_number": "Договор GPS / мониторинга",
}

PRIMARY_DOCUMENT_TYPES = {
    "lease_contract_number": "lease_contract",
    "purchase_contract_number": "purchase_contract",
    "credit_line_number": "credit_line_agreement",
    "subsidy_contract_number": "subsidy_agreement",
    "pledge_contract_number": "pledge_contract",
    "guarantee_contract_number": "guarantee_contract",
    "gps_contract_number": "gps_service_contract",
}

CONTRACT_LINK_FIELDS = {
    "linked_purchase_contract": "purchase_contract_number",
    "linked_lease_contract": "lease_contract_number",
    "main_lease_contract_number": "lease_contract_number",
    "related_lease_contract_number": "lease_contract_number",
    "linked_subsidy_contract": "subsidy_contract_number",
    "linked_lease_contract_number": "lease_contract_number",
    "linked_guarantee_contract_number": "guarantee_contract_number",
    "insurance_linked_contract": "lease_contract_number",
    "linked_subsidy_contract": "subsidy_contract_number",
}

AMOUNT_GROUPS = {
    "Стоимость предмета / договора / акта": {
        "purchase": {"total_amount_kzt"},
        "lease": {"lease_asset_value_kzt"},
        "act": {"act_total_amount_kzt"},
        "insurance": {"insurance_sum_kzt", "insurance_actual_value_kzt"},
    },
    "Сумма финансирования / транша": {
        "lease": {"financing_amount_kzt", "loan_amount_kzt"},
        "schedule": {"loan_amount_kzt", "principal_total_kzt"},
    },
}

REQUIRED_FIELDS = {
    "lease_contract": {
        "lease_contract_number", "lease_contract_date", "lessee_name",
        "lessee_iin_bin", "lease_asset_value_kzt", "financing_amount_kzt",
        "nominal_rate_percent", "advance_payment_kzt",
        "arrangement_commission_kzt",
        "lease_term_months",
    },
    "purchase_contract": {
        "purchase_contract_number", "purchase_contract_date", "buyer_name",
        "seller_name", "total_amount_kzt", "purchase_payment_working_days",
        "purchase_delivery_working_days",
    },
    "insurance_contract": {
        "insurance_policy_number", "insurance_contract_date", "insurance_company",
        "insurance_company_iin_bin", "insurance_holder", "insurance_holder_iin_bin",
        "insurance_sum_kzt", "insurance_premium_kzt", "insurance_start_date",
        "insurance_end_date",
    },
    "real_estate_registration_notice": {
        "registration_notice_number", "registration_notice_date",
        "property_address", "cadastral_number",
    },
    "gps_service_contract": {
        "gps_contract_number", "gps_contract_date", "gps_provider", "gps_customer",
        "gps_monthly_fee_kzt", "gps_annual_fee_kzt", "gps_start_date", "gps_end_date",
        "gps_delivery_working_days",
    },
    "payment_order": {
        "insurance_payment_order_number", "insurance_payment_date",
        "insurance_payment_amount_kzt", "payment_payer",
        "payment_payer_iin_bin", "payment_payee", "payment_payee_iin_bin",
        "payment_purpose",
    },
    "addendum": {
        "addendum_number", "addendum_date", "linked_lease_contract_number",
        "changed_clause",
    },
}

FIELD_ALIASES = {
    "lease_contract_date": {"lease_contract_date", "contract_date"},
    "purchase_contract_date": {"purchase_contract_date", "contract_date"},
    "insurance_contract_date": {"insurance_contract_date", "contract_date"},
    "gps_contract_date": {"gps_contract_date", "contract_date"},
    "total_amount_kzt": {"total_amount_kzt", "purchase_total_kzt"},
    "payment_payer_iin_bin": {
        "payment_payer_iin_bin", "recipient_iin_bin",
    },
    "payment_payee_iin_bin": {
        "payment_payee_iin_bin", "beneficiary_iin_bin",
    },
}


def _normalize_number(value: object) -> str:
    text = str(value or "").upper().strip()
    text = text.replace("\\", "/").replace("|", "/").replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"/+", "/", text)
    return text.strip(".,;:")


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _field_index(document: dict) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = defaultdict(list)
    for item in document.get("fields", []):
        if item.get("status") in {"candidate", "rejected"}:
            continue
        validation = item.get("validation") or {}
        if validation and not validation.get("valid") and item.get("status") not in {"confirmed", "corrected"}:
            continue
        index[item.get("name", "")].append(item)
    return index


def _evidence(document: dict, item: dict) -> dict:
    return {
        "filename": document.get("filename"),
        "document_type": document.get("document_type"),
        "document_type_label_ru": document.get("document_type_label_ru"),
        "field": item.get("label_ru"),
        "value": item.get("value"),
        "page": item.get("page"),
        "confidence": item.get("confidence"),
    }


def _identity_summary(documents: list[dict]) -> tuple[list[dict], list[dict]]:
    roles: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    checks: list[dict] = []

    for document in documents:
        for item in document.get("fields", []):
            name = item.get("name")
            if name not in ROLE_FIELD_NAMES or item.get("status") in {"candidate", "rejected"}:
                continue
            role_name = name
            if name == "beneficiary_iin_bin":
                if document.get("document_type") == "insurance_contract":
                    role_name = "insurance_beneficiary_iin_bin"
                elif document.get("document_type") == "direct_debit_agreement":
                    role_name = "direct_debit_beneficiary_iin_bin"
                elif document.get("document_type") == "payment_order":
                    role_name = "payment_beneficiary_iin_bin"
            value = _normalize_number(item.get("value"))
            if re.fullmatch(r"\d{12}", value):
                roles[role_name][value].append(_evidence(document, item))

    summaries = []
    for role_name, values in roles.items():
        all_evidence = [e for evidence in values.values() for e in evidence]
        transaction_scoped = role_name.startswith("payment_")
        status = (
            "transaction_values"
            if transaction_scoped
            else "consistent" if len(values) == 1 else "conflict"
        )
        summaries.append({
            "role": role_name,
            "label_ru": ROLE_FIELD_NAMES[role_name],
            "values": sorted(values),
            "status": status,
            "evidence": all_evidence,
        })
        if len(all_evidence) >= 2 and not transaction_scoped:
            checks.append({
                "category": "ИИН/БИН",
                "check": f"{ROLE_FIELD_NAMES[role_name]}: единый идентификатор",
                "status": "match" if status == "consistent" else "mismatch",
                "message": (
                    f"Для роли «{ROLE_FIELD_NAMES[role_name]}» во всех проверенных полях найден один идентификатор: {next(iter(values))}."
                    if status == "consistent"
                    else "Для одной роли найдены разные ИИН/БИН: " + ", ".join(sorted(values))
                ),
                "evidence": all_evidence,
            })

    client_values: dict[str, list[dict]] = defaultdict(list)
    client_documents: dict[str, set[str]] = defaultdict(set)
    for document in documents:
        for item in document.get("fields", []):
            if (
                item.get("name") not in CLIENT_IDENTITY_FIELDS
                or item.get("status") in {"candidate", "rejected"}
            ):
                continue
            value = _normalize_number(item.get("value"))
            if not re.fullmatch(r"\d{12}", value):
                continue
            client_values[value].append(_evidence(document, item))
            client_documents[value].add(str(document.get("filename") or ""))
    repeated = {
        value: evidence for value, evidence in client_values.items()
        if len(client_documents[value]) >= 2
    }
    if repeated:
        all_evidence = [item for group in repeated.values() for item in group]
        checks.append({
            "category": "ИИН/БИН",
            "check": "Клиент досье: междокументное подтверждение",
            "status": "match" if len(repeated) == 1 else "mismatch",
            "message": (
                f"Клиент подтверждён в нескольких документах по ИИН/БИН "
                f"{next(iter(repeated))}."
                if len(repeated) == 1
                else "В клиентских ролях нескольких документов найдены разные "
                     "ИИН/БИН: " + ", ".join(sorted(repeated)) + "."
            ),
            "evidence": all_evidence,
        })
    return summaries, checks


def _contract_checks(documents: list[dict]) -> tuple[list[dict], list[dict]]:
    primaries: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    links: list[tuple[str, str, str, dict]] = []

    for document in documents:
        index = _field_index(document)
        for field_name in CONTRACT_PRIMARY_FIELDS:
            for item in index.get(field_name, []):
                value = _normalize_number(item.get("value"))
                if value:
                    evidence = _evidence(document, item)
                    if document.get("document_type") == PRIMARY_DOCUMENT_TYPES.get(field_name):
                        primaries[field_name][value].append(evidence)
                    else:
                        # A main-contract number printed inside an addendum,
                        # policy or lease application is a reference, not proof
                        # that the main document itself was uploaded.
                        links.append((field_name, field_name, value, evidence))
        for link_name, target_name in CONTRACT_LINK_FIELDS.items():
            for item in index.get(link_name, []):
                value = _normalize_number(item.get("value"))
                if value:
                    links.append((link_name, target_name, value, _evidence(document, item)))

    registry = []
    for field_name, values in primaries.items():
        registry.append({
            "field": field_name,
            "label_ru": CONTRACT_PRIMARY_FIELDS[field_name],
            "values": sorted(values),
            "evidence": [e for group in values.values() for e in group],
        })

    checks = []
    for link_name, target_name, value, evidence in links:
        known = primaries.get(target_name, {})
        if not known:
            status = "not_enough_data"
            message = f"В досье нет основного документа для проверки номера {value}."
        elif value in known:
            status = "match"
            message = f"Связанный номер {value} совпадает с основным документом."
        else:
            status = "mismatch"
            message = f"Связанный номер {value} не совпадает с найденными основными номерами: {', '.join(sorted(known))}."
        checks.append({
            "category": "Связи договоров",
            "check": f"{link_name} → {CONTRACT_PRIMARY_FIELDS.get(target_name, target_name)}",
            "status": status,
            "message": message,
            "evidence": [evidence] + [e for group in known.values() for e in group],
        })
    return registry, checks


def _amount_checks(documents: list[dict]) -> list[dict]:
    checks: list[dict] = []
    for group_label, role_fields in AMOUNT_GROUPS.items():
        found: list[tuple[str, Decimal, dict]] = []
        for document in documents:
            index = _field_index(document)
            for role, names in role_fields.items():
                for name in names:
                    for item in index.get(name, []):
                        value = _decimal(item.get("value"))
                        if value is not None and value > 0:
                            found.append((role, value, _evidence(document, item)))
        if len(found) < 2:
            continue

        unique = sorted({value for _, value, _ in found})
        # One tenge tolerance covers harmless decimal/rounding differences.
        spread = max(unique) - min(unique)
        status = "match" if spread <= Decimal("1") else "mismatch"
        checks.append({
            "category": "Суммы",
            "check": group_label,
            "status": status,
            "message": (
                f"Суммы совпадают: {unique[0]:,.2f} тенге."
                if status == "match"
                else "Найдены разные суммы: " + ", ".join(f"{value:,.2f}" for value in unique) + " тенге."
            ),
            "evidence": [evidence for _, _, evidence in found],
        })
    return checks


def _property_checks(documents: list[dict]) -> list[dict]:
    """Reconcile one real-estate asset across lease, registration and insurance."""
    checks: list[dict] = []
    definitions = (
        ("cadastral_number", "Кадастровый номер"),
        ("property_internal_number", "Внутренний номер недвижимости"),
        ("property_address", "Адрес недвижимости"),
    )
    for field_name, label in definitions:
        found: dict[str, list[dict]] = defaultdict(list)
        for document in documents:
            for item in _field_index(document).get(field_name, []):
                raw = str(item.get("value") or "")
                if field_name == "property_address":
                    value = re.sub(r"[^0-9a-zа-яәіңғүұқөһ]+", "", raw.casefold().replace("ё", "е"))
                else:
                    value = re.sub(r"\s+", "", raw).casefold()
                if value:
                    found[value].append(_evidence(document, item))
        evidence = [item for group in found.values() for item in group]
        if len(evidence) < 2:
            continue
        status = "match" if len(found) == 1 else "mismatch"
        checks.append({
            "category": "Недвижимость",
            "check": label,
            "status": status,
            "message": (
                f"{label} совпадает в {len(evidence)} документах."
                if status == "match"
                else f"Для поля «{label}» найдены противоречащие значения."
            ),
            "evidence": evidence,
        })
    return checks



def _equipment_checks(documents: list[dict]) -> tuple[list[dict], dict]:
    """Compare VIN, quantities, types and amounts across linked documents."""
    records: list[dict] = []
    for document in documents:
        index = _field_index(document)
        normalized = {
            "equipment_type": next((i.get("value") for i in index.get("equipment_type", []) if i.get("value")), None),
            "model": next((i.get("value") for i in index.get("equipment_model", []) if i.get("value")), None),
            "quantity": next((i.get("value") for i in index.get("equipment_quantity", []) if i.get("value") not in (None, "")), None),
            "unit_price_kzt": next((
                i.get("value")
                for name in ("equipment_unit_price_kzt", "unit_price_kzt")
                for i in index.get(name, [])
                if i.get("value") not in (None, "")
            ), None),
            "total_amount_kzt": next((i.get("value") for name in ("equipment_total_kzt", "purchase_total_kzt", "lease_asset_value_kzt") for i in index.get(name, []) if i.get("value") not in (None, "")), None),
            "serial_number": next((i.get("value") for i in index.get("serial_number", []) if i.get("value")), None),
        }
        for table in document.get("tables", []):
            if table.get("name") != "asset_vin_rows":
                continue
            for row in table.get("rows", []):
                enriched = dict(row)
                for key, value in normalized.items():
                    if enriched.get(key) in (None, "") and value not in (None, ""):
                        enriched[key] = value
                records.append({
                    "filename": document.get("filename"),
                    "document_type": document.get("document_type"),
                    "document_type_label_ru": document.get("document_type_label_ru"),
                    "vin": str(enriched.get("vin") or enriched.get("serial_number") or "").upper() or None,
                    "equipment_type": enriched.get("equipment_type"),
                    "model": enriched.get("model"),
                    "manufacture_year": enriched.get("manufacture_year"),
                    "quantity": enriched.get("quantity"),
                    "unit_price_kzt": enriched.get("unit_price_kzt"),
                    "total_amount_kzt": enriched.get("total_amount_kzt"),
                    "page": enriched.get("page"),
                    "_source_row": row,
                })

    if not records:
        # Some addenda and guarantees describe a single asset in ordinary fields
        # instead of an asset_vin_rows table. Include those documents in the dossier
        # equipment summary without inventing VINs or prices.
        field_records: list[dict] = []
        for document in documents:
            index = _field_index(document)
            equipment_type = next((i.get("value") for i in index.get("equipment_type", []) if i.get("value")), None)
            model = next((i.get("value") for i in index.get("equipment_model", []) if i.get("value")), None)
            quantity_item = next((i.get("value") for i in index.get("equipment_quantity", []) if i.get("value") not in (None, "")), None)
            if not (equipment_type or model):
                continue
            quantity = int(quantity_item) if str(quantity_item or "").isdigit() else 1
            field_records.append({
                "filename": document.get("filename"),
                "document_type": document.get("document_type"),
                "document_type_label_ru": document.get("document_type_label_ru"),
                "vin": None,
                "equipment_type": equipment_type,
                "model": model,
                "manufacture_year": None,
                "quantity": quantity,
                "unit_price_kzt": None,
                "total_amount_kzt": None,
                "page": None,
                "source": "fields",
            })
        if not field_records:
            return [], {
                "documents_with_equipment": 0,
                "unique_vin_count": 0,
                "total_quantity": None,
                "types": {},
                "records": [],
            }
        records.extend(field_records)

    documents_with_equipment = len({item["filename"] for item in records})
    unique_vins = sorted({item["vin"] for item in records if item.get("vin")})
    types: dict[str, int] = defaultdict(int)
    quantity_by_document: dict[str, int] = defaultdict(int)
    for item in records:
        label = item.get("equipment_type") or "Не определено"
        quantity = item.get("quantity")
        if isinstance(quantity, int):
            quantity_by_document[item["filename"]] += quantity

    # Resolve repeated descriptions of the same physical asset across contracts.
    # VIN/serial is authoritative.  Some lease specifications lose the identifier
    # in OCR while a linked purchase/insurance row keeps it.  Attach such a weak
    # row to an authoritative row only when there is exactly one compatible
    # candidate.  This is deliberately dossier-level: document-level enrichment
    # cannot borrow evidence from another document.
    authoritative = [item for item in records if item.get("vin")]
    inferred_identity = False
    for item in records:
        if item.get("vin"):
            continue
        item_amount = item.get("unit_price_kzt") or item.get("total_amount_kzt")
        item_type = str(item.get("equipment_type") or "").strip().casefold()
        item_model = str(item.get("model") or "").strip().casefold()
        item_year = str(item.get("manufacture_year") or "").strip()
        compatible = []
        for candidate in authoritative:
            candidate_amount = (
                candidate.get("unit_price_kzt") or candidate.get("total_amount_kzt")
            )
            candidate_type = str(candidate.get("equipment_type") or "").strip().casefold()
            candidate_model = str(candidate.get("model") or "").strip().casefold()
            candidate_year = str(candidate.get("manufacture_year") or "").strip()
            amount_matches = (
                item_amount not in (None, "")
                and candidate_amount not in (None, "")
                and abs(Decimal(str(item_amount)) - Decimal(str(candidate_amount))) <= Decimal("1")
            )
            type_matches = not item_type or not candidate_type or item_type == candidate_type
            model_matches = not item_model or not candidate_model or item_model == candidate_model
            year_matches = not item_year or not candidate_year or item_year == candidate_year
            if amount_matches and type_matches and model_matches and year_matches:
                compatible.append(candidate)
        candidate_vins = {candidate.get("vin") for candidate in compatible}
        if len(candidate_vins) != 1:
            continue
        source = compatible[0]
        item["vin"] = source.get("vin")
        item["model"] = item.get("model") or source.get("model")
        item["equipment_type"] = (
            item.get("equipment_type") or source.get("equipment_type")
        )
        item["identity_source"] = "inferred_from_linked_document"
        source_row = item.get("_source_row")
        if isinstance(source_row, dict):
            source_row["vin"] = item.get("vin")
            source_row["serial_number"] = (
                source_row.get("serial_number") or item.get("vin")
            )
            source_row["model"] = source_row.get("model") or item.get("model")
            source_row["equipment_type"] = (
                source_row.get("equipment_type") or item.get("equipment_type")
            )
            source_row["identity_source"] = "inferred_from_linked_document"
        inferred_identity = True

    # VIN is authoritative; rows that still have no identifier use a conservative
    # commercial fingerprint and clearly label the result as an estimate.
    entity_keys = set()
    has_fingerprint_only = False
    for item in records:
        if item.get("vin"):
            key = ("vin", item["vin"])
        else:
            fingerprint = (
                str(item.get("equipment_type") or "").strip().casefold(),
                str(item.get("model") or "").strip().casefold(),
                str(item.get("manufacture_year") or "").strip(),
                item.get("quantity") or 1,
                item.get("unit_price_kzt") or item.get("total_amount_kzt"),
            )
            key = ("fingerprint",) + fingerprint
            has_fingerprint_only = True
        entity_keys.add(key)
    entity_quantities: dict[tuple, int] = {}
    for key in entity_keys:
        matching_quantities = []
        for item in records:
            item_key = (
                ("vin", item.get("vin"))
                if item.get("vin")
                else (
                    "fingerprint",
                    str(item.get("equipment_type") or "").strip().casefold(),
                    str(item.get("model") or "").strip().casefold(),
                    str(item.get("manufacture_year") or "").strip(),
                    item.get("quantity") or 1,
                    item.get("unit_price_kzt") or item.get("total_amount_kzt"),
                )
            )
            if item_key == key:
                quantity = item.get("quantity")
                matching_quantities.append(
                    int(quantity) if isinstance(quantity, int) and quantity > 0 else 1
                )
        entity_quantities[key] = (
            1 if key[0] == "vin" else max(matching_quantities or [1])
        )
    estimated_physical_assets = sum(entity_quantities.values())
    for key in entity_keys:
        matching = next(
            item for item in records
            if (("vin", item.get("vin")) if item.get("vin") else (
                "fingerprint",
                str(item.get("equipment_type") or "").strip().casefold(),
                str(item.get("model") or "").strip().casefold(),
                str(item.get("manufacture_year") or "").strip(),
                item.get("quantity") or 1,
                item.get("unit_price_kzt") or item.get("total_amount_kzt"),
            )) == key
        )
        label = matching.get("equipment_type") or "Не определено"
        types[label] += matching.get("quantity") if isinstance(matching.get("quantity"), int) else 1

    checks: list[dict] = []
    records_by_vin: dict[str, list[dict]] = defaultdict(list)
    for item in records:
        if item.get("vin"):
            records_by_vin[item["vin"]].append(item)

    # VIN consistency: each VIN should preserve type/model and amount where stated.
    for vin, vin_records in records_by_vin.items():
        if len({item["filename"] for item in vin_records}) < 2:
            continue
        types_found = {str(item.get("equipment_type")).strip() for item in vin_records if item.get("equipment_type")}
        models_found = {str(item.get("model")).strip().upper() for item in vin_records if item.get("model")}
        prices_found = {
            Decimal(str(item["unit_price_kzt"]))
            for item in vin_records if item.get("unit_price_kzt") is not None
        }
        mismatches = []
        if len(types_found) > 1:
            mismatches.append("разные виды техники")
        if len(models_found) > 1:
            mismatches.append("разные модели")
        if len(prices_found) > 1 and max(prices_found) - min(prices_found) > Decimal("1"):
            mismatches.append("разная цена за единицу")
        status = "mismatch" if mismatches else "match"
        checks.append({
            "category": "Техника",
            "check": f"VIN {vin}",
            "status": status,
            "message": (
                "VIN согласован между документами."
                if status == "match"
                else "По одному VIN найдены " + ", ".join(mismatches) + "."
            ),
            "evidence": [
                {
                    "filename": item["filename"],
                    "document_type": item["document_type"],
                    "document_type_label_ru": item["document_type_label_ru"],
                    "field": "Техника / VIN",
                    "value": " · ".join(filter(None, [
                        item.get("equipment_type"),
                        item.get("model"),
                        item.get("vin"),
                    ])),
                    "page": item.get("page"),
                    "confidence": None,
                }
                for item in vin_records
            ],
        })

    # Compare VIN sets only when at least two documents carry VINs.
    vin_sets: dict[str, set[str]] = defaultdict(set)
    doc_labels: dict[str, str] = {}
    for item in records:
        if item.get("vin"):
            vin_sets[item["filename"]].add(item["vin"])
            doc_labels[item["filename"]] = item.get("document_type_label_ru") or item["filename"]
    if len(vin_sets) >= 2:
        unique_sets = {tuple(sorted(values)) for values in vin_sets.values()}
        status = "match" if len(unique_sets) == 1 else "mismatch"
        checks.append({
            "category": "Техника",
            "check": "Комплект VIN между документами",
            "status": status,
            "message": (
                f"Комплект из {len(next(iter(vin_sets.values())))} VIN совпадает."
                if status == "match"
                else "Наборы VIN в документах различаются."
            ),
            "evidence": [
                {
                    "filename": filename,
                    "document_type": None,
                    "document_type_label_ru": doc_labels.get(filename),
                    "field": "Набор VIN",
                    "value": ", ".join(sorted(values)),
                    "page": None,
                    "confidence": None,
                }
                for filename, values in vin_sets.items()
            ],
        })

    # Quantity comparison when more than one document has an explicit total.
    known_quantities = {
        filename: value for filename, value in quantity_by_document.items() if value > 0
    }
    if len(known_quantities) >= 2:
        status = "match" if len(set(known_quantities.values())) == 1 else "mismatch"
        checks.append({
            "category": "Техника",
            "check": "Количество техники между документами",
            "status": status,
            "message": (
                f"Количество совпадает: {next(iter(known_quantities.values()))} единиц."
                if status == "match"
                else "Количество различается: " + ", ".join(
                    f"{filename}: {quantity}" for filename, quantity in known_quantities.items()
                )
            ),
            "evidence": [
                {
                    "filename": filename,
                    "document_type": None,
                    "document_type_label_ru": None,
                    "field": "Количество техники",
                    "value": quantity,
                    "page": None,
                    "confidence": None,
                }
                for filename, quantity in known_quantities.items()
            ],
        })

    for item in records:
        item.pop("_source_row", None)

    return checks, {
        "documents_with_equipment": documents_with_equipment,
        "document_record_count": len(records),
        "estimated_unique_physical_assets": estimated_physical_assets,
        "deduplication_confidence": (
            0.95 if entity_keys and inferred_identity and not has_fingerprint_only
            else 0.99 if entity_keys and not has_fingerprint_only
            else 0.75 if entity_keys
            else None
        ),
        "unique_vin_count": len(unique_vins),
        "total_quantity": max(known_quantities.values()) if known_quantities else len(entity_keys) or None,
        "types": dict(sorted(types.items())),
        "records": records,
    }


def _completeness(documents: list[dict]) -> list[dict]:
    labels = {
        "lease_contract": "Договор финансового лизинга",
        "purchase_contract": "Договор купли-продажи",
        "acceptance_act": "Акт приёма-передачи",
        "payment_schedule": "График платежей",
        "insurance_contract": "Договор / полис страхования",
        "insurance_appendix": "Приложение к страховому договору",
        "gps_service_contract": "Договор GPS / спутникового мониторинга",
        "payment_order": "Платёжное поручение",
        "real_estate_registration_notice":
            "Уведомление о государственной регистрации недвижимости",
        "addendum": "Дополнительное соглашение",
        "direct_debit_agreement": "Соглашение о прямом дебетовании",
        "subsidy_agreement": "Договор субсидирования",
        "guarantee_contract": "Договор гарантии",
        "pledge_contract": "Договор залога / ипотеки",
    }
    present = {doc.get("document_type") for doc in documents}
    primary_fields = {
        "lease_contract": {"lease_contract_number"},
        "purchase_contract": {"purchase_contract_number"},
        "insurance_contract": {"insurance_policy_number"},
        "gps_service_contract": {"gps_contract_number"},
        "guarantee_contract": {"guarantee_contract_number"},
    }
    loaded_numbers: dict[str, set[str]] = defaultdict(set)
    for document in documents:
        document_type = document.get("document_type")
        for item in document.get("fields", []):
            if item.get("name") in primary_fields.get(document_type, set()):
                value = _normalize_number(item.get("value"))
                if value:
                    loaded_numbers[document_type].add(value)
            if (
                item.get("name") == "insurance_document_part"
                and str(item.get("value") or "").lower().startswith("приложение")
            ):
                appendix_number = re.search(r"\d+", str(item.get("value")))
                if appendix_number:
                    loaded_numbers["insurance_appendix"].add(
                        appendix_number.group(0)
                    )
    referenced: dict[str, set[str]] = defaultdict(set)
    for document in documents:
        index = _field_index(document)
        for item in index.get("mandatory_kasko", []):
            if item.get("value") is True:
                referenced["insurance_contract"].add(
                    "обязательный договор КАСКО (номер не указан)"
                )
        for item in index.get("mandatory_gps", []):
            if item.get("value") is True:
                referenced["gps_service_contract"].add(
                    "обязательный договор GPS (номер не указан)"
                )
        if document.get("document_type") == "insurance_appendix":
            for item in index.get("insurance_policy_number", []):
                value = _normalize_number(item.get("value"))
                if value:
                    referenced["insurance_contract"].add(value)
        for field_name, target_type in {
            "lease_contract_number": "lease_contract",
            "linked_lease_contract_number": "lease_contract",
            "purchase_contract_number": "purchase_contract",
            "linked_purchase_contract": "purchase_contract",
            "guarantee_contract_number": "guarantee_contract",
            "linked_guarantee_contract_number": "guarantee_contract",
            "insurance_linked_contract": "lease_contract",
            "linked_insurance_policy_number": "insurance_contract",
            "linked_insurance_appendix_number": "insurance_appendix",
            "linked_subsidy_contract": "subsidy_agreement",
        }.items():
            for item in index.get(field_name, []):
                value = _normalize_number(item.get("value"))
                if value:
                    referenced[target_type].add(value)
        for table in document.get("tables", []):
            if table.get("name") == "guarantor_rows":
                for row in table.get("rows", []):
                    value = _normalize_number(row.get("guarantee_number"))
                    if value:
                        referenced["guarantee_contract"].add(value)
            elif table.get("name") == "collateral_rows":
                for row in table.get("rows", []):
                    value = _normalize_number(row.get("pledge_contract_number"))
                    if value:
                        referenced["pledge_contract"].add(value)

    for key in ("insurance_contract", "gps_service_contract"):
        explicit = {
            value for value in referenced.get(key, set())
            if not value.startswith("обязательный договор")
        }
        if explicit:
            referenced[key] = explicit

    result = []
    # Report what is actually loaded. Do not invent a universal four-document
    # checklist: dossier composition differs by product and transaction stage.
    for key in sorted(present):
        if not key or key == "unknown":
            continue
        label = labels.get(key, key)
        result.append({
            "document_type": key,
            "label_ru": label,
            "present": True,
            "status": "present",
            "referenced_numbers": sorted(loaded_numbers.get(key, set())),
            "message": f"{label} загружен в текущем пакете.",
        })
    # Missing means explicitly referenced but not represented by the same
    # contract/policy number in the upload.
    for key, refs in sorted(referenced.items()):
        loaded = loaded_numbers.get(key, set())
        missing = sorted(
            ref for ref in refs
            if ref not in loaded
            and not (
                ref.startswith("обязательный договор")
                and bool(loaded)
            )
        )
        if not missing:
            continue
        label = labels.get(key, key)
        result.append({
            "document_type": key,
            "label_ru": label,
            "present": False,
            "status": "referenced_not_uploaded",
            "referenced_numbers": missing,
            "message": (
                f"{label} {', '.join(missing)} упомянут, "
                "но не загружен в текущем пакете."
            ),
        })
    return result


def _field_completeness(documents: list[dict]) -> list[dict]:
    result = []
    for document in documents:
        configured_required = REQUIRED_FIELDS.get(document.get("document_type"))
        required = set(configured_required or ())
        if not required:
            continue
        direct = set(_field_index(document))
        warning = {
            item.get("name") for item in document.get("fields", [])
            if item.get("status") in {"candidate", "corrected"}
            and item.get("value") not in (None, "", [])
        }
        present = direct | warning
        # Delivery is applicable only when this GPS contract actually contains
        # and exposes a delivery term. Service-only contracts must not be
        # marked incomplete for a clause they do not have.
        if (
            document.get("document_type") == "gps_service_contract"
            and "gps_delivery_working_days" not in present
        ):
            required.discard("gps_delivery_working_days")
        if (
            document.get("document_type") == "gps_service_contract"
            and not ({"gps_start_date", "gps_end_date"} & present)
        ):
            # Service contracts do not universally define a service period.
            # Missing non-existent start/end dates must not create false alerts.
            required.discard("gps_start_date")
            required.discard("gps_end_date")
        if (
            document.get("document_type") == "addendum"
            and "linked_subsidy_contract" in present
        ):
            required.discard("linked_lease_contract_number")
            required.add("linked_subsidy_contract")
        # Some signed GPS appendices charge for an explicit period shorter than
        # a year.  A directly stated total for that period satisfies the
        # commercial-term requirement without inventing an annual figure.
        if (
            document.get("document_type") == "gps_service_contract"
            and "gps_annual_fee_kzt" not in present
            and "gps_service_fee_kzt" in present
        ):
            required.discard("gps_annual_fee_kzt")
        missing = sorted(
            name for name in required
            if not (FIELD_ALIASES.get(name, {name}) & present)
        )
        warning_required = sorted(
            name for name in required
            if FIELD_ALIASES.get(name, {name}) & warning
            and not (FIELD_ALIASES.get(name, {name}) & direct)
        )
        result.append({
            "filename": document.get("filename"),
            "document_type": document.get("document_type"),
            "required_fields": len(required),
            "extracted_fields": len(required) - len(missing),
            "directly_extracted_fields": sum(
                bool(FIELD_ALIASES.get(name, {name}) & direct) for name in required
            ),
            "warning_fields": warning_required,
            "warning_field_count": len(warning_required),
            "percentage": round((len(required) - len(missing)) / len(required) * 100, 1),
            "missing": missing,
            "complete": not missing,
        })
    return result


def _selected_field_completeness(documents: list[dict], selection: dict | None) -> dict | None:
    if not selection or selection.get("mode") != "custom":
        return None
    requested = set(selection.get("fields") or [])
    categories = set(selection.get("categories") or [])
    if categories:
        from .field_catalog import FIELD_BY_NAME
        requested.update(
            name for name, item in FIELD_BY_NAME.items()
            if item.get("category") in categories
        )
    found, candidate, unrecognized, not_applicable = set(), set(), set(), set()
    for document in documents:
        for item in document.get("fields", []):
            name = item.get("name")
            if name not in requested:
                continue
            state = item.get("availability")
            if state == "not_applicable":
                not_applicable.add(name)
            elif state == "unrecognized":
                unrecognized.add(name)
            elif item.get("status") in {"candidate", "corrected"} or item.get("value_type") != "direct" or not item.get("quote"):
                candidate.add(name)
            elif item.get("status") != "rejected" and item.get("value") not in (None, "", []):
                found.add(name)
    candidate -= found
    missing = requested - found - candidate - unrecognized - not_applicable
    assessed = len(requested - not_applicable)
    return {
        "requested_count": len(requested),
        "directly_extracted_count": len(found),
        "warning_count": len(candidate),
        "missing_count": len(missing),
        "unrecognized_count": len(unrecognized),
        "not_applicable_count": len(not_applicable),
        "percentage": round((len(found) + len(candidate)) / assessed * 100, 1) if assessed else 100.0,
        "verified_percentage": round(len(found) / assessed * 100, 1) if assessed else 100.0,
        "fields": {
            "found": sorted(found), "warning": sorted(candidate),
            "not_found": sorted(missing), "unrecognized": sorted(unrecognized),
            "not_applicable": sorted(not_applicable),
        },
    }


def build_dossier_summary(documents: list[dict], selection: dict | None = None) -> dict:
    identities, identity_checks = _identity_summary(documents)
    contracts, contract_checks = _contract_checks(documents)
    equipment_checks, equipment_summary = _equipment_checks(documents)
    financial_checks, financial_summary = build_financial_checks(documents)
    checks = (
        identity_checks + contract_checks + _amount_checks(documents)
        + _property_checks(documents) + equipment_checks + financial_checks
    )
    field_completeness = _field_completeness(documents)
    for item in field_completeness:
        if item["complete"]:
            continue
        checks.append({
            "category": "Полнота данных",
            "check": f"Обязательные поля: {item['filename']}",
            "status": "not_enough_data",
            "message": (
                "Нельзя выполнить все предусмотренные проверки: отсутствуют поля "
                + ", ".join(item["missing"]) + "."
            ),
            "evidence": [],
        })

    dossier_completeness = _completeness(documents)
    represented_missing_refs = {
        _normalize_number(value)
        for check in checks
        if check.get("status") == "not_enough_data"
        for value in re.findall(r"[A-ZА-Я0-9]+(?:/[A-ZА-Я0-9._-]+){2,}", check.get("message", ""))
    }
    for item in dossier_completeness:
        if item.get("present"):
            continue
        refs = [
            value for value in item.get("referenced_numbers", [])
            if _normalize_number(value) not in represented_missing_refs
        ]
        if not refs:
            continue
        checks.append({
            "category": "Комплектность досье",
            "check": item.get("label_ru"),
            "status": "not_enough_data",
            "message": item.get("message"),
            "evidence": [],
        })

    counts = {
        "match": sum(check["status"] == "match" for check in checks),
        "mismatch": sum(check["status"] == "mismatch" for check in checks),
        "not_enough_data": sum(check["status"] == "not_enough_data" for check in checks),
    }
    document_rule_warnings = sum(len(document.get("warnings", [])) for document in documents)
    warning_breakdown = {
        "corrected": 0,
        "candidate": 0,
        "calculated": 0,
        "without_quote": 0,
    }
    field_warnings = 0
    for document in documents:
        for item in document.get("fields", []):
            if item.get("value") in (None, "", []):
                continue
            reason = None
            if item.get("status") == "corrected":
                reason = "corrected"
            elif item.get("status") == "candidate":
                reason = "candidate"
            elif item.get("status") == "calculated" or item.get("value_type") != "direct":
                reason = "calculated"
            elif not item.get("quote"):
                reason = "without_quote"
            if reason:
                warning_breakdown[reason] += 1
                field_warnings += 1
    document_warnings = document_rule_warnings + field_warnings
    incomplete_documents = sum(not item["complete"] for item in field_completeness)
    missing_expected_documents = sum(not item["present"] for item in dossier_completeness)
    selected_completeness = _selected_field_completeness(documents, selection)
    documents_requiring_attention = sum(
        bool(document.get("warnings"))
        or any(
            item.get("status") in {"candidate", "corrected", "calculated"}
            or item.get("value_type") != "direct"
            or (item.get("value") not in (None, "", []) and not item.get("quote"))
            for item in document.get("fields", [])
        )
        or any(
            item["filename"] == document.get("filename") and not item["complete"]
            for item in field_completeness
        )
        for document in documents
    )
    # A referenced but absent parent contract is itself an attention item even
    # when every uploaded document is internally complete.
    documents_requiring_attention += missing_expected_documents
    return {
        "identities": identities,
        "contracts": contracts,
        "equipment": equipment_summary,
        "financial": financial_summary,
        "checks": checks,
        "completeness": dossier_completeness,
        "field_completeness": field_completeness,
        "selected_field_completeness": selected_completeness,
        "counts": counts,
        "review": {
            "document_warnings": document_warnings,
            "field_warnings": field_warnings,
            "document_rule_warnings": document_rule_warnings,
            "warning_breakdown": warning_breakdown,
            "documents_requiring_attention": documents_requiring_attention,
            "incomplete_documents": incomplete_documents,
            "field_incomplete_documents": incomplete_documents,
            "missing_expected_documents": missing_expected_documents,
            "comparison_scope_message": (
                f"{counts['match']} проверок пройдено, {counts['mismatch']} не пройдено, "
                f"{counts['not_enough_data']} не выполнено из-за недостатка данных. "
                "Результат относится только к перечисленным проверкам."
            ),
        },
        "status": (
            "attention"
            if counts["mismatch"] or document_warnings or incomplete_documents or missing_expected_documents
            else ("ok" if checks else "insufficient")
        ),
    }
