from __future__ import annotations

import re

CLIENT_BLOCKLIST = (
    "bcc leasing", "банк центркредит", "лизингодатель", "лизинг беруші",
)
SIGNATURE_WORDS = (
    "электронной цифровой подпись", "электрондық цифрлық қолтаңба",
    "эцп", "подписант", "сертификат", "дата подписания",
)
KEY_FIELDS = {
    "client_name", "lessee_name", "borrower_name", "principal_name",
    "lease_contract_number", "contract_number", "agreement_number",
    "lease_asset_value_kzt", "purchase_total_kzt", "total_amount_kzt",
    "vin", "serial_number", "equipment_model", "equipment_quantity",
}


def _warning(document: dict, field: str, message: str, severity: str = "medium") -> None:
    warnings = document.setdefault("warnings", [])
    key = (field.casefold(), message.casefold())
    for item in warnings:
        if isinstance(item, dict):
            old = (str(item.get("field") or "").casefold(), str(item.get("message_ru") or item.get("message") or "").casefold())
            if old == key:
                return
        elif str(item).casefold() == message.casefold():
            return
    warnings.append({"severity": severity, "field": field, "message": message, "message_ru": message})


def _clean_contract_number(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = re.sub(r"\s+", " ", value).strip(" ,;:")
    text = re.sub(r"\s+(?:от|dated)\s*$", "", text, flags=re.I)
    text = re.sub(r"[-–—]\s*$", "", text)
    return text


def _looks_generic_party(value: object) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    if not text:
        return True
    if any(token in text for token in CLIENT_BLOCKLIST):
        return True
    if text in {"лизингополучатель", "получатель", "заемщик", "заёмщик", "клиент", "покупатель"}:
        return True
    return False


def _clean_model(value: object, quote: object = None) -> object:
    if not isinstance(value, str):
        return value
    text = re.sub(r"\s+", " ", value).strip(" ,.;:-")
    # OCR often attaches the first group of a nearby amount to a brand:
    # "CATERPILLAR 31" next to "31 820 250". A bare 1–3 digit suffix is
    # not accepted as a model unless the source explicitly labels it as model.
    m = re.match(r"^(.*\D)\s+(\d{1,3})$", text)
    q = str(quote or "")
    if m and re.search(rf"\b{re.escape(m.group(2))}[\s\u00a0]\d{{3}}[\s\u00a0]\d{{3}}\b", q):
        return m.group(1).strip()
    return text


def _signature_candidate(field: dict) -> bool:
    quote = str(field.get("quote") or "").casefold()
    notes = str(field.get("notes") or "").casefold()
    return any(word in quote or word in notes for word in SIGNATURE_WORDS)


def apply_final_quality_gate(result: dict) -> None:
    """Conservative last pass: never export a silent high-risk error.

    This pass does not try to guess missing values. It cleans deterministic OCR
    artefacts and blocks the ready status whenever a business-critical value is
    ambiguous or structurally implausible.
    """
    client = result.setdefault("client", {})
    client_name = str(client.get("name") or "").strip()
    client_id = re.sub(r"\D", "", str(client.get("iin_bin") or ""))
    client_ok = bool(client_name and not _looks_generic_party(client_name) and len(client_id) == 12)

    for document in result.get("documents", []):
        fields = document.get("fields", [])
        # Deterministic cleanup and suppression of signature-only IDs.
        for field in fields:
            name = str(field.get("name") or "")
            if "contract_number" in name or name in {"agreement_number", "act_number"}:
                field["value"] = _clean_contract_number(field.get("value"))
                field["normalized_value"] = field.get("value")
            if name in {"equipment_model", "vehicle_model", "model"}:
                cleaned = _clean_model(field.get("value"), field.get("quote"))
                if cleaned != field.get("value"):
                    field["original_value"] = field.get("value")
                    field["value"] = cleaned
                    field["normalized_value"] = cleaned
                    field["status"] = "corrected"
                    field["notes"] = "Удалён фрагмент суммы, ошибочно присоединённый OCR к модели."
            if name in {"unresolved_iin_bins", "unresolved_identifiers"} and _signature_candidate(field):
                field["status"] = "rejected"
                field["notes"] = "Идентификатор относится к блоку электронной подписи и не является стороной сделки."

        # Remove rejected technical candidates from user-facing output.
        fields[:] = [f for f in fields if f.get("status") != "rejected"]

        # Clean table models and perform structural checks.
        for table in document.get("tables", []):
            if table.get("name") != "asset_vin_rows":
                continue
            for row in table.get("rows", []):
                if not isinstance(row, dict):
                    continue
                row["model"] = _clean_model(row.get("model"), row.get("raw"))
                qty = row.get("quantity")
                try:
                    qty_n = int(float(qty)) if qty not in (None, "") else None
                except (TypeError, ValueError):
                    qty_n = None
                if qty_n is not None and qty_n <= 0:
                    row["status"] = "Требует проверки"
                    _warning(document, "Количество техники", f"Некорректное количество: {qty}.", "high")
                unit = row.get("unit_price_kzt")
                total = row.get("total_amount_kzt")
                try:
                    if qty_n and unit not in (None, "") and total not in (None, ""):
                        expected = qty_n * float(unit)
                        difference = abs(expected - float(total))
                        if difference > max(1.0, abs(float(total)) * 0.005):
                            row["status"] = "Требует проверки"
                            _warning(document, "Стоимость техники", "Количество × цена не совпадает с общей стоимостью.", "high")
                except (TypeError, ValueError):
                    pass

        # Generic or role-only party candidates must not block a valid client.
        if client_ok:
            for field in fields:
                if field.get("name") in {"lessee_name", "borrower_name", "principal_name"} and _looks_generic_party(field.get("value")):
                    field["status"] = "rejected"
            fields[:] = [f for f in fields if f.get("status") != "rejected"]

        # Strict readiness gate for business-critical candidate values.
        for field in fields:
            name = str(field.get("name") or "")
            if field.get("status") == "candidate" and (
                name in KEY_FIELDS or any(token in name for token in ("vin", "contract_number", "amount", "iin_bin"))
            ):
                _warning(document, field.get("label_ru") or name or "Ключевое поле", "Значение требует ручного подтверждения.", "high")

        if not client_ok:
            _warning(document, "Клиент", "Наименование или ИИН/БИН клиента не подтверждены.", "high")

    analysis = result.setdefault("analysis", {})
    analysis["strict_quality_gate"] = True
    analysis["quality_policy"] = (
        "Автоматический статус разрешён только при отсутствии сомнений в ключевых полях; "
        "неоднозначные значения не угадываются и направляются на проверку."
    )
