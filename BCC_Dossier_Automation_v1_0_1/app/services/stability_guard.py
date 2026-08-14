from __future__ import annotations

import re
from copy import deepcopy


ROLE_PRIORITY = {
    # Most specific contractual roles first.
    "principal_iin_bin": 160,
    "borrower_iin_bin": 150,
    "lessee_iin_bin": 150,
    "lessor_iin_bin": 150,
    "seller_iin_bin": 150,
    "buyer_iin_bin": 150,
    "financial_agency_iin_bin": 150,
    "leasing_company_iin_bin": 150,
    "beneficiary_iin_bin": 145,
    "recipient_iin_bin": 120,
    "sender_iin_bin": 120,
    "guarantor_iin_bin": 110,
    "bank_bin": 160,
    "fund_iin_bin": 220,
    "lessor_representative_iin_bin": 155,

    "principal_iban": 160,
    "borrower_iban": 150,
    "lessee_iban": 150,
    "lessor_iban": 150,
    "seller_iban": 150,
    "buyer_iban": 150,
    "financial_agency_iban": 150,
    "leasing_company_iban": 150,
    "beneficiary_iban": 145,
    "recipient_iban": 120,
    "sender_iban": 120,
    "guarantor_iban": 110,
    "bank_iban": 160,
}

STATUS_PRIORITY = {
    "corrected": 5,
    "confirmed": 5,
    "extracted": 4,
    "candidate": 2,
    "rejected": 0,
}

MULTI_ROLE_FIELDS = {
    # Insurance contracts legitimately assign the same legal entity to several
    # simultaneous roles (policyholder, insured and excess beneficiary).
    # These fields must not compete in the global one-value/one-role guard.
    "insurance_holder_iin_bin",
    "insured_iin_bin",
    "beneficiary_excess_iin_bin",
    # A payment order legitimately describes the same transaction party using
    # both banking terminology (sender/beneficiary) and user-facing labels.
    "payment_payer_iin_bin",
    "payment_payee_iin_bin",
    "payment_payer_iban",
    "payment_payee_iban",
}


def _scalar_role_field(item: dict) -> bool:
    name = str(item.get("name") or "")
    if name in MULTI_ROLE_FIELDS:
        return False
    value = item.get("value")
    if isinstance(value, list) or value in (None, ""):
        return False
    return (
        name.endswith("_iban")
        or name.endswith("_iin_bin")
        or name.endswith("_bin")
        or name in {"bank_bin", "bank_iban", "fund_iin_bin"}
    )


def _choose_unique_roles(fields: list[dict]) -> list[dict]:
    best: dict[str, tuple[tuple, dict]] = {}
    passthrough: list[dict] = []

    for item in fields:
        if not _scalar_role_field(item):
            passthrough.append(item)
            continue

        value = str(item.get("value"))
        name = str(item.get("name") or "")
        score = (
            ROLE_PRIORITY.get(name, 0),
            STATUS_PRIORITY.get(str(item.get("status") or ""), 0),
            float(item.get("confidence") or 0),
        )
        current = best.get(value)
        if current is None or score > current[0]:
            best[value] = (score, item)

    return passthrough + [entry[1] for entry in best.values()]


def _guard_false_technical_fields(fields: list[dict]) -> list[dict]:
    risky = {"drive_type", "interior_color", "exterior_color"}
    color_fields = {
        "vehicle_color", "equipment_color", "exterior_color", "interior_color",
    }
    color_header_artifacts = {
        "БІРЛІКТЕР", "БИРЛИКТЕР", "ЕДИНИЦ", "ЕДИНИЦЫ",
        "КОЛИЧЕСТВО", "КОЛИЧЕСТВОЕДИНИЦ", "САНЫ",
    }
    result = []
    for item in fields:
        if item.get("name") in color_fields:
            normalized_color = re.sub(
                r"[^0-9A-ZА-ЯЁӘІҢҒҮҰҚӨҺ]",
                "",
                str(item.get("value") or "").upper(),
            )
            if normalized_color in color_header_artifacts:
                continue
        if (
            item.get("name") == "engine_displacement_cm3"
            and re.search(
                r"Объ[её]м\s+двигателя\s*,?\s*м[³3]",
                str(item.get("quote") or ""),
                re.I,
            )
        ):
            item = deepcopy(item)
            item["status"] = "candidate"
            item["notes"] = (
                "В источнике указана сомнительная единица м³; "
                "значение нельзя считать подтверждённым см³."
            )
            result.append(item)
            continue
        if item.get("name") not in risky:
            result.append(item)
            continue
        value = str(item.get("value") or "")
        quote = str(item.get("quote") or "")
        legal_prose = bool(re.search(
            r"гарант|исключени|не\s+распространя|жиклер|омыва|"
            r"расход\w*\s+жидкост|повреждени|естественн\w+\s+износ",
            quote + " " + value,
            re.I,
        ))
        malformed = (
            len(value) > 45
            or bool(re.search(
                r"\b(?:котор|выше|жидкост|жиклер|омыва)\b",
                value,
                re.I,
            ))
        )
        explicit_label = bool(re.search(
            r"(?:^|\n|\|)\s*(?:Привод|Интерьер|Экстерьер)"
            r"\s*[:\-–—]\s*",
            quote,
            re.I,
        ))
        if explicit_label and not legal_prose and not malformed:
            result.append(item)
    return result


def _used_values(fields: list[dict], tables: list[dict]) -> set[str]:
    values = {
        str(item.get("value"))
        for item in fields
        if not isinstance(item.get("value"), list)
        and item.get("status") not in {"candidate", "rejected"}
        and item.get("value") not in (None, "")
    }
    for table in tables:
        name = table.get("name")
        for row in table.get("rows", []):
            if name == "guarantor_rows":
                for key in ("iin_bin", "guarantee_number"):
                    if row.get(key) not in (None, ""):
                        values.add(str(row[key]))
            elif name == "tranche_rows":
                for key in ("tranche_number", "amount_kzt"):
                    if row.get(key) not in (None, ""):
                        values.add(str(row[key]))
            elif name == "asset_vin_rows":
                if row.get("vin"):
                    values.add(str(row["vin"]))
    return values


def _clean_candidate_lists(fields: list[dict], tables: list[dict]) -> list[dict]:
    used = _used_values(fields, tables)
    result = []
    for item in fields:
        value = item.get("value")
        if isinstance(value, list):
            if item.get("name") in {
                "guarantee_contract_numbers",
                "guarantor_iin_bins",
                "insurance_linked_contracts",
            }:
                result.append(item)
                continue
            cleaned = []
            for entry in value:
                text = str(entry)
                numeric = re.sub(r"\D", "", text)
                # Remove values already promoted to a field/table.
                if text in used:
                    continue
                # Also match formatted/unformatted amounts.
                if numeric and any(re.sub(r"\D", "", used_value) == numeric for used_value in used):
                    continue
                cleaned.append(entry)
            item = deepcopy(item)
            item["value"] = cleaned
            if not cleaned:
                continue
        result.append(item)
    return result


def _guard_equipment_tables(fields: list[dict], tables: list[dict]) -> list[dict]:
    document_totals = [
        float(item.get("value"))
        for item in fields
        if item.get("name") in {
            "lease_asset_value_kzt", "purchase_total_kzt",
            "financing_amount_kzt", "total_amount_kzt",
        }
        and isinstance(item.get("value"), (int, float))
        and float(item.get("value")) >= 1_000_000
    ]
    reference_total = max(document_totals) if document_totals else None

    result = deepcopy(tables)
    for table in result:
        if table.get("name") != "asset_vin_rows":
            continue
        rows = []
        for row in table.get("rows", []):
            amount = row.get("total_amount_kzt")
            equipment_type = str(row.get("equipment_type") or "")
            model = str(row.get("model") or "")
            header_like = (
                equipment_type.upper().startswith(("Р/С", "№", "НАИМЕНОВАН"))
                or model.upper().startswith(("Р/С", "№", "НАИМЕНОВАН"))
            )
            tiny_false_amount = (
                isinstance(amount, (int, float))
                and amount in {12, 16}
                and reference_total is not None
            )
            if header_like or tiny_false_amount:
                continue
            rows.append(row)

        table["rows"] = rows
        table["row_count"] = len(rows)
        if not rows:
            table["status"] = "candidate"
            table["notes"] = (
                "Автоматическая строка спецификации отклонена как заголовок "
                "таблицы или процент НДС. Требуется повторное распознавание страницы."
            )
            continue

        summary = table.setdefault("summary", {})
        summary["total_quantity"] = sum(
            int(row.get("quantity") or 0) for row in rows
        ) or None
        summary["total_identified_amount_kzt"] = sum(
            float(row.get("total_amount_kzt") or 0) for row in rows
        ) or None
        summary["unique_vin_count"] = len({
            row.get("vin") for row in rows if row.get("vin")
        })
    return result


def _deduplicate_semantic_output(
    fields: list[dict], tables: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Remove display duplicates without collapsing distinct legal roles."""
    rank = {"confirmed": 6, "corrected": 5, "extracted": 4, "calculated": 3,
            "candidate": 2, "rejected": 0}
    selected: dict[tuple[str, str], tuple[tuple[float, float], dict]] = {}
    order: list[tuple[str, str]] = []
    for item in fields:
        value = item.get("value")
        comparable = (
            tuple(str(entry) for entry in value)
            if isinstance(value, list)
            else str(value)
        )
        key = (str(item.get("label_ru") or item.get("name") or ""), str(comparable))
        score = (
            float(rank.get(str(item.get("status") or ""), 0)),
            float(item.get("confidence") or 0),
        )
        if key not in selected:
            order.append(key)
            selected[key] = (score, item)
        elif score > selected[key][0]:
            selected[key] = (score, item)

    cleaned_tables = deepcopy(tables)
    for table in cleaned_tables:
        columns = []
        seen_keys: set[str] = set()
        seen_labels: set[str] = set()
        for column in table.get("columns", []):
            key = str(column.get("key") or "")
            label = str(column.get("label_ru") or key).strip().casefold()
            if not key or key in seen_keys or label in seen_labels:
                continue
            seen_keys.add(key)
            seen_labels.add(label)
            columns.append(column)
        table["columns"] = columns
    return [selected[key][1] for key in order], cleaned_tables


def _synchronise_table_field_status(
    fields: list[dict], tables: list[dict]
) -> list[dict]:
    """Keep candidate/rejected status consistent in every table view."""
    result = deepcopy(tables)
    by_name = {str(item.get("name") or ""): item for item in fields}
    by_label = {
        str(item.get("label_ru") or "").strip().casefold(): item
        for item in fields
    }
    risky_technical = {"drive_type", "interior_color", "exterior_color"}
    usable_names = {
        name for name, item in by_name.items()
        if item.get("status") not in {"candidate", "rejected"}
        and item.get("value") not in (None, "", [])
    }

    for table in result:
        if table.get("name") == "equipment_specification_rows":
            if not any(
                column.get("key") == "status"
                for column in table.get("columns", [])
            ):
                table.setdefault("columns", []).append({
                    "key": "status",
                    "label_ru": "Статус",
                })
            kept = []
            for row in table.get("rows", []):
                item = by_label.get(
                    str(row.get("parameter") or "").strip().casefold()
                )
                if item and item.get("name") in risky_technical:
                    if item.get("name") not in usable_names:
                        continue
                if item and item.get("status") == "candidate":
                    row["value"] = f"Кандидат: {row.get('value')}"
                    row["status"] = "Требует проверки"
                    table["status"] = "candidate"
                elif item and item.get("status") == "rejected":
                    continue
                else:
                    row["status"] = "Подтверждено"
                kept.append(row)
            table["rows"] = kept
            table["row_count"] = len(kept)

        if table.get("name") == "asset_vin_rows":
            table["columns"] = [
                column for column in table.get("columns", [])
                if not (
                    column.get("key") in risky_technical
                    and column.get("key") not in usable_names
                )
            ]
            for row in table.get("rows", []):
                for key in risky_technical:
                    if key not in usable_names:
                        row.pop(key, None)
                for key, item in by_name.items():
                    if (
                        item.get("status") == "candidate"
                        and key in row
                        and row.get(key) not in (None, "")
                    ):
                        row[key] = f"Кандидат: {row[key]}"
                        table["status"] = "candidate"
    return result


def _reconcile_identifier_register(
    fields: list[dict], tables: list[dict]
) -> list[dict]:
    role_labels = {
        "lessee_iin_bin": "Лизингополучатель",
        "lessor_iin_bin": "Лизингодатель",
        "lessor_representative_iin_bin": "Представитель лизингодателя",
        "seller_iin_bin": "Продавец",
        "buyer_iin_bin": "Покупатель",
        "direct_debit_sender_iin_bin": "Отправитель",
        "beneficiary_iin_bin": "Бенефициар",
        "direct_debit_beneficiary_iin_bin": "Бенефициар",
        "bank_bin": "Банк",
        "gps_customer_iin_bin": "Заказчик GPS",
        "gps_provider_iin_bin": "Поставщик GPS",
        "insurance_holder_iin_bin": "Страхователь",
        "insurance_company_iin_bin": "Страховщик",
    }
    canonical = {}
    for item in fields:
        label = role_labels.get(str(item.get("name") or ""))
        value = str(item.get("value") or "")
        if (
            label
            and re.fullmatch(r"\d{12}", value)
            and item.get("status") not in {"candidate", "rejected"}
        ):
            canonical[value] = label

    result = deepcopy(tables)
    for table in result:
        if table.get("name") != "identifier_register_rows":
            continue
        unique = {}
        for row in table.get("rows", []):
            value = str(row.get("value") or "")
            if value in canonical:
                row["role"] = canonical[value]
            key = (str(row.get("type") or ""), value)
            current = unique.get(key)
            if current is None or (
                bool(row.get("role")),
                value in canonical,
            ) > (
                bool(current.get("role")),
                value in canonical,
            ):
                unique[key] = row
        table["rows"] = list(unique.values())
        table["row_count"] = len(table["rows"])
    return result


def apply_stability_guard(fields: list[dict], tables: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply only global invariants; do not reinterpret document semantics."""
    prepared = [
        item for item in deepcopy(fields)
        if not (
            item.get("name") in {"borrower_iin_bin", "borrower_bin"}
            and str(item.get("value")) == "970840000277"
        )
    ]
    prepared = _guard_false_technical_fields(prepared)
    guarded_fields = _choose_unique_roles(prepared)
    guarded_tables = _guard_equipment_tables(guarded_fields, tables)
    guarded_fields = _clean_candidate_lists(guarded_fields, guarded_tables)
    guarded_tables = _synchronise_table_field_status(
        guarded_fields, guarded_tables
    )
    guarded_tables = _reconcile_identifier_register(
        guarded_fields, guarded_tables
    )
    return _deduplicate_semantic_output(guarded_fields, guarded_tables)
