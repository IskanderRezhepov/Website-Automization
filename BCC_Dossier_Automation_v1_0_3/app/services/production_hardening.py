from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .validators import validate_iin_bin, validate_vin

BCC_PATTERNS = (
    "bcc leasing", "бисиcи лизинг", "бисиси лизинг", "банк центркредит",
    "АО «БИ-СИ-СИ ЛИЗИНГ»", "АО \"БИ-СИ-СИ ЛИЗИНГ\"",
)
CLIENT_FIELD_PRIORITY = (
    "lessee_name", "borrower_name", "principal_name", "customer_name",
    "recipient_name", "subsidy_recipient_name", "gps_customer",
)
CLIENT_ID_PRIORITY = (
    "lessee_iin_bin", "borrower_iin_bin", "principal_iin_bin",
    "customer_iin_bin", "recipient_iin_bin", "recipient_bin",
    "subsidy_recipient_bin", "gps_customer_iin_bin",
)
CONTRACT_FIELDS = {
    "contract_number", "agreement_number", "lease_contract_number",
    "purchase_contract_number", "linked_purchase_contract", "act_number",
    "policy_number", "insurance_policy_number", "guarantee_number",
}
VIN_KEYS = ("vin", "номер кузова", "кузов", "шасси", "chassis")
EQUIPMENT_TABLE_NAMES = {
    "asset_vin_rows", "equipment_rows", "vehicles", "transport_rows",
    "financing_objects", "asset_rows", "equipment",
}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _is_bcc(value: Any) -> bool:
    low = _text(value).casefold()
    return low == "bcc" or any(token.casefold() in low for token in BCC_PATTERNS)


def _valid_party_name(value: Any) -> bool:
    text = _text(value).strip(" ,.;:-")
    if not text or _is_bcc(text) or text.casefold() in {
        "не определён", "не определено", "лизингополучатель", "клиент",
    }:
        return False
    letters = sum(ch.isalpha() for ch in text)
    if letters < 3 or len(text) > 180:
        return False
    if re.search(r"\b(?:далее именуем|определение термин|участник сделки)\b", text, re.I):
        return False
    return True


def _warn(document: dict, field: str, message: str, severity: str = "medium") -> None:
    warnings = document.setdefault("warnings", [])
    normalized = _text(message).casefold()
    for existing in warnings:
        existing_message = existing.get("message_ru") or existing.get("message") if isinstance(existing, dict) else existing
        if _text(existing_message).casefold() == normalized:
            return
    warnings.append({"severity": severity, "field": field, "message": message, "message_ru": message})


def _iter_fields(result: dict):
    for document in result.get("documents", []):
        for item in document.get("fields", []):
            yield document, item


def normalize_contract_number(value: Any) -> str:
    text = _text(value)
    text = re.sub(r"(?i)\s+(?:от|dated)\s*$", "", text)
    text = re.sub(r"\s*([/\\-])\s*", r"\1", text)
    text = re.sub(r"[,:;]+$", "", text).strip()
    # Join an OCR line break before a short numeric suffix.
    text = re.sub(r"([/\-])\s+(\d{1,4})$", r"\1\2", text)
    return text


def harden_contract_numbers(result: dict) -> None:
    for document, item in _iter_fields(result):
        if str(item.get("name") or "").lower() not in CONTRACT_FIELDS:
            continue
        value = item.get("value")
        if isinstance(value, list):
            cleaned = [normalize_contract_number(v) for v in value]
            item["value"] = [v for v in dict.fromkeys(cleaned) if v]
        elif value not in (None, ""):
            cleaned = normalize_contract_number(value)
            if cleaned and cleaned != value:
                item.setdefault("original_value", value)
                item["value"] = cleaned
                item["normalized_value"] = cleaned
                item["status"] = "corrected"


def harden_client_identity(result: dict) -> None:
    client = result.setdefault("client", {})
    current_name = client.get("name")
    current_id = _digits(client.get("iin_bin"))

    names: list[tuple[int, str]] = []
    ids: list[tuple[int, str]] = []
    for document, item in _iter_fields(result):
        name = str(item.get("name") or "").lower()
        status = str(item.get("status") or "")
        if status in {"rejected", "candidate"}:
            continue
        if name in CLIENT_FIELD_PRIORITY and _valid_party_name(item.get("value")):
            score = len(CLIENT_FIELD_PRIORITY) - CLIENT_FIELD_PRIORITY.index(name)
            names.append((score, _text(item.get("value"))))
        if name in CLIENT_ID_PRIORITY:
            identifier = _digits(item.get("value"))
            if len(identifier) == 12:
                score = len(CLIENT_ID_PRIORITY) - CLIENT_ID_PRIORITY.index(name)
                ids.append((score, identifier))

    if not _valid_party_name(current_name) and names:
        client["name"] = sorted(names, reverse=True)[0][1]
    elif _is_bcc(current_name):
        client["name"] = sorted(names, reverse=True)[0][1] if names else None

    if len(current_id) != 12 and ids:
        client["iin_bin"] = sorted(ids, reverse=True)[0][1]
    elif len(current_id) == 12:
        client["iin_bin"] = current_id

    if not _valid_party_name(client.get("name")):
        for document in result.get("documents", []):
            _warn(document, "Клиент", "Наименование клиента не подтверждено в реквизитах документа.", "high")

    identifier = _digits(client.get("iin_bin"))
    if len(identifier) != 12:
        for document in result.get("documents", []):
            _warn(document, "ИИН/БИН клиента", "ИИН/БИН клиента не найден или содержит не 12 цифр.", "high")
    else:
        # Keep a structurally valid 12-digit identifier even when the checksum
        # cannot be confirmed. Real source systems and historical datasets may
        # contain legacy/synthetic identifiers. Checksum is advisory and must
        # never erase a value that is explicitly present in the document or
        # supplied by the client-folder name.
        validation = validate_iin_bin(identifier)
        client["iin_bin_validation"] = validation
        client["iin_bin"] = identifier
        if not validation.get("valid"):
            client["iin_bin_validation_note"] = (
                "Формат 12 цифр подтверждён; контрольная сумма не подтверждена. "
                "Значение сохранено по документу/папке и не блокирует результат."
            )


def _decimal(value: Any) -> Decimal | None:
    text = _text(value).replace("\u00a0", " ")
    text = re.sub(r"[^0-9,.-]", "", text.replace(" ", ""))
    if not text:
        return None
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _row_meaningful(row: dict) -> bool:
    blob = " ".join(_text(v) for v in row.values())
    if not blob or re.search(r"\b(?:doc id|подписант|руководитель|итого|всего)\b", blob, re.I):
        return False
    keys = {str(k).casefold() for k in row}
    values = [_text(v) for v in row.values() if _text(v)]
    signals = 0
    if any(any(t in k for t in (
        "модель", "марка", "производ", "вид техник", "наименование",
        "model", "brand", "manufacturer", "equipment_name", "asset_type", "vehicle_type"
    )) for k in keys):
        if any(re.search(r"[A-Za-zА-Яа-я]\w{2,}", v) for v in values):
            signals += 1
    if any("колич" in k or k in {"qty", "quantity", "count"} for k in keys):
        signals += 1
    if any(any(t in k for t in (
        "цен", "стоим", "сумм", "price", "amount", "unit_price", "total_amount"
    )) for k in keys):
        signals += 1
    if any(any(t in k for t in ("vin", "шасси", "серийн", "serial", "chassis")) for k in keys):
        signals += 1
    return signals >= 2




_HEADERISH_ASSET_MARKERS = (
    "год выпуска", "шығарылған жылы", "жылы", "vin", "vine", "№ п/п", "п/п",
    "количество", "саны", "цена", "бағасы", "стоимость", "құны", "итого", "барлығы",
    "технические характеристики", "техникалық сипаттама",
)


def _equipment_identity_values(row: dict) -> tuple[str, str, str, str]:
    """Return normalized (vin/serial, model, brand, type) evidence for an asset row."""
    identifier = _text(
        row.get("vin") or row.get("VIN") or row.get("vin_number")
        or row.get("serial_number") or row.get("chassis_number")
        or row.get("equipment_identifier") or row.get("identifier")
    )
    model = _text(
        row.get("model") or row.get("equipment_model") or row.get("Модель / комплектация")
        or row.get("equipment_name") or row.get("Наименование")
    )
    brand = _text(
        row.get("brand") or row.get("manufacturer") or row.get("Марка") or row.get("Производитель")
    )
    kind = _text(
        row.get("equipment_type") or row.get("asset_type") or row.get("vehicle_type") or row.get("Вид техники")
    )
    return identifier, model, brand, kind


def _looks_like_header_or_prose_asset_row(row: dict) -> bool:
    """Reject table headings/aggregate prose that accidentally became an asset row.

    This intentionally uses structural evidence rather than client/model names so it
    generalizes to new dossiers. A real asset row is kept when it has a concrete
    VIN/serial, model/brand, or a short clean asset type plus quantity/price evidence.
    """
    identifier, model, brand, kind = _equipment_identity_values(row)
    if identifier:
        return False
    if model or brand:
        return False

    blob = " ".join(_text(v) for v in row.values() if _text(v))
    low = blob.casefold()
    kind_low = kind.casefold()
    qty = next((_decimal(v) for k, v in row.items() if any(t in str(k).casefold() for t in ("колич", "quantity", "qty", "count", "саны"))), None)
    unit = next((_decimal(v) for k, v in row.items() if any(t in str(k).casefold() for t in ("цена", "unit_price", "unit price", "баға")) and "общ" not in str(k).casefold()), None)

    marker_hits = sum(1 for marker in _HEADERISH_ASSET_MARKERS if marker in low)
    # Typical malformed header/description: long phrase containing year/VIN labels,
    # but no actual identifier/model and no item quantity/unit price.
    if marker_hits >= 2 and qty is None and unit is None:
        return True
    if kind and (len(kind) > 80 or len(kind.split()) > 9) and qty is None and unit is None:
        return True
    if kind and re.search(r"\b20\d{2}\b", kind) and any(x in kind_low for x in ("vin", "vine", "жыл", "год")):
        return True
    if re.match(r"^\s*\d+\s+", kind) and marker_hits:
        return True
    # A bare generic type is allowed only when there is transactional evidence.
    if kind and not re.search(r"[A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺһ]{3,}", kind):
        return True
    return False


def _add_nonblocking_note(document: dict, field: str, message: str) -> None:
    warnings = document.setdefault("warnings", [])
    norm = _text(message).casefold()
    if any(_text((w.get("message_ru") or w.get("message")) if isinstance(w, dict) else w).casefold() == norm for w in warnings):
        return
    warnings.append({"severity": "medium", "field": field, "message": message, "message_ru": message})

def harden_equipment_tables(result: dict) -> None:
    for document in result.get("documents", []):
        for table in document.get("tables", []):
            name = str(table.get("name") or "").casefold()
            if name not in EQUIPMENT_TABLE_NAMES and not any(t in name for t in ("equipment", "asset", "vehicle", "transport", "техник")):
                continue
            rows = [r for r in table.get("rows", []) if isinstance(r, dict)]
            cleaned: list[dict] = []
            seen: set[tuple] = set()
            for row in rows:
                if not _row_meaningful(row):
                    continue
                if _looks_like_header_or_prose_asset_row(row):
                    _add_nonblocking_note(document, "Техника", "Служебная строка спецификации исключена из списка предметов лизинга.")
                    continue
                # Remove obvious numeric spill from model names.
                for key in list(row):
                    if any(t in str(key).casefold() for t in ("модель", "марка", "наименование")):
                        value = _text(row.get(key))
                        value = re.sub(r"\s+\d{1,3}(?=\s*$)", "", value) if re.search(r"[A-Za-zА-Яа-я]", value) else value
                        row[key] = value
                signature = tuple((str(k), _text(v).casefold()) for k, v in sorted(row.items()) if str(k) not in {"page", "confidence", "status"})
                if signature in seen:
                    continue
                seen.add(signature)
                # Arithmetic validation.
                quantity = next((_decimal(v) for k, v in row.items() if any(t in str(k).casefold() for t in ("колич", "quantity", "qty", "count"))), None)
                unit_price = next((_decimal(v) for k, v in row.items() if any(t in str(k).casefold() for t in ("цена", "unit_price", "unit price")) and "общ" not in str(k).casefold()), None)
                total = next((_decimal(v) for k, v in row.items() if any(t in str(k).casefold() for t in ("общая стоимость", "итоговая стоимость", "сумма", "total_amount", "total amount"))), None)
                if quantity and unit_price and total:
                    tolerance = max(Decimal("1"), abs(total) * Decimal("0.001"))
                    if abs(quantity * unit_price - total) > tolerance:
                        row["status"] = "Требует проверки"
                        row["validation_message"] = "Количество × цена не равно общей стоимости."
                        _warn(document, "Техника", "Количество × цена не равно общей стоимости в строке техники.", "high")
                cleaned.append(row)
            table["rows"] = cleaned




def _document_full_text(document: dict) -> str:
    parts: list[str] = []
    for page in document.get("page_texts", []):
        if isinstance(page, dict):
            parts.append(str(page.get("text") or ""))
        else:
            parts.append(str(page or ""))
    for item in document.get("fields", []):
        parts.extend([str(item.get("quote") or ""), str(item.get("raw_value") or "")])
    return "\n".join(parts)


def _equipment_rows(document: dict) -> list[dict]:
    rows: list[dict] = []
    for table in document.get("tables", []):
        name = str(table.get("name") or "").casefold()
        if name in EQUIPMENT_TABLE_NAMES or any(token in name for token in ("equipment", "asset", "vehicle", "transport", "техник")):
            rows.extend(row for row in table.get("rows", []) if isinstance(row, dict))
    return rows


def _asset_table(rows: list[dict], note: str) -> dict:
    return {
        "name": "asset_vin_rows",
        "label_ru": "Техника / транспорт",
        "columns": [
            {"key": "equipment_type", "label_ru": "Вид техники"},
            {"key": "equipment_name", "label_ru": "Наименование"},
            {"key": "model", "label_ru": "Модель / комплектация"},
            {"key": "manufacture_year", "label_ru": "Год выпуска"},
            {"key": "vin", "label_ru": "VIN"},
            {"key": "engine_model", "label_ru": "Модель двигателя"},
            {"key": "quantity", "label_ru": "Количество"},
            {"key": "unit_price_kzt", "label_ru": "Цена за единицу, тенге"},
            {"key": "total_amount_kzt", "label_ru": "Стоимость, тенге"},
            {"key": "page", "label_ru": "Страница"},
        ],
        "rows": rows,
        "row_count": len(rows),
        "notes": note,
        "confidence": 0.98,
    }


def recover_single_specification_equipment(result: dict) -> None:
    """Recover one multiline specification row at the final JSON stage.

    Some PDF tables are flattened into text and later enrichment replaces the
    earlier table.  This final-stage recovery is deliberately conservative: it
    runs only when no useful equipment row survived, the document contains an
    explicit specification header, a recognised manufacturer/model, quantity,
    unit price and total that reconcile arithmetically.
    """
    brand_re = re.compile(
        r"\b(XCMG|SANY|SHANTUI|LIUGONG|SDLG|CATERPILLAR|CAT|KOMATSU|HYUNDAI|DOOSAN|JCB|JAC|HOWO|SHACMAN|SITRAK|ZOOMLION|LONKING|FAW)\b",
        re.I,
    )
    money_re = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?)(?!\d)")
    for document in result.get("documents", []):
        if any(_row_meaningful(row) for row in _equipment_rows(document)):
            continue
        raw = _document_full_text(document)
        upper = raw.upper()
        if not any(token in upper for token in ("СПЕЦИФИКАЦ", "ЦЕНА ЗА ЕДИНИЦ", "КОЛИЧЕСТВО, ШТУК", "САНЫ, ДАНАСЫ")):
            continue
        brand_match = brand_re.search(raw)
        if not brand_match:
            continue
        brand = brand_match.group(1).upper()
        model_context = raw[max(0, brand_match.start() - 520): brand_match.end() + 320]
        explicit_model = re.search(r"\bмодель\s*[:№-]?\s*([A-Z0-9][A-Z0-9._/-]{2,30})\b", model_context, re.I)
        model = explicit_model.group(1).upper().strip(" .,-") if explicit_model else None
        if not model:
            # In multi-column PDF text the year can appear immediately after the
            # manufacturer while the actual model is on the next visual line.
            # Choose the first nearby mixed letter/digit token after the brand,
            # excluding VIN/DOC identifiers and pure years/numbers.
            model_window = raw[brand_match.end(): brand_match.end() + 300]
            for token in re.findall(r"\b[A-Z0-9][A-Z0-9._/-]{3,30}\b", model_window.upper()):
                compact = re.sub(r"[^A-Z0-9]", "", token)
                if len(compact) >= 17 or compact.isdigit() or compact.startswith(("KZPK", "DOCID")):
                    continue
                if any(ch.isalpha() for ch in compact) and any(ch.isdigit() for ch in compact):
                    model = token.strip(" .,-")
                    break
        if not model:
            continue
        region_start = max(0, brand_match.start() - 700)
        region = raw[region_start: brand_match.start() + 2600]
        amounts: list[Decimal] = []
        for token in money_re.findall(region):
            number = _decimal(token)
            if number is not None and number >= 100000:
                amounts.append(number)
        if len(amounts) < 2:
            continue
        # Preserve duplicate monetary values: for quantity=1 the unit price and
        # total are normally identical, so collapsing to set() loses the row.
        plausible = [value for value in amounts if value > 0]
        if len(plausible) < 2:
            continue
        unit_price = min(plausible)
        total = max(plausible)
        if total < unit_price:
            continue
        ratio = total / unit_price if unit_price else Decimal("0")
        quantity = int(ratio) if ratio == ratio.to_integral_value() and 1 <= ratio <= 1000 else None
        if not quantity:
            continue
        # Require an explicit isolated quantity close to the row. For quantity
        # one, allow the standard table row marker / quantity cell as evidence.
        quantity_pattern = rf"(?:^|\s){quantity}(?:\s|$)"
        if not re.search(quantity_pattern, region):
            continue
        before = raw[max(0, brand_match.start() - 220):brand_match.start()]
        equipment_type = "Техника"
        if re.search(r"экскаватор\s*[-–—]?\s*погрузчик", before, re.I) or (
            "экскаватор" in before.casefold() and "погрузчик" in before.casefold()
        ):
            equipment_type = "Экскаватор-погрузчик"
        elif re.search(r"(?:погрузчик\s+фронталь|фронтальн\s*ый\s+(?:колесн\s*ый\s+)?погрузчик|фронталь\w*\s+(?:колесн\w*\s+)?погрузчик)", before, re.I):
            equipment_type = "Фронтальный погрузчик"
        elif re.search(r"автокран", before, re.I):
            equipment_type = "Автокран"
        elif re.search(r"экскаватор", before, re.I):
            equipment_type = "Экскаватор"
        elif re.search(r"бульдозер", before, re.I):
            equipment_type = "Бульдозер"
        near_after = raw[brand_match.start(): brand_match.end() + 420]
        year_match = re.search(r"\b(20\d{2})\b\s*(?:ж\.?ш\.?|г\.?в\.?)", near_after, re.I)
        if not year_match:
            year_match = re.search(r"(?:год\s+выпуска|шығарылған\s+жылы)[^0-9]{0,30}(20\d{2})", region, re.I)
        if not year_match:
            year_match = re.search(r"\b(20\d{2})\b", near_after)
        engine_match = re.search(r"(?:Двигатель|Қозғалтқыш)\s*[:：]\s*([A-Z0-9][A-Z0-9 ._/-]{3,35}?)(?=[.\n;]|\s{2,})", region, re.I)
        vin_match = re.search(r"\bVIN\s*[:№-]?\s*([A-HJ-NPR-Z0-9]{17})\b", region, re.I | re.S)
        page = 1
        for page_item in document.get("page_texts", []):
            if isinstance(page_item, dict) and model in str(page_item.get("text") or "").upper():
                page = int(page_item.get("page") or 1)
                break
        row = {
            "equipment_type": equipment_type,
            "equipment_name": f"{equipment_type} {brand} {model}",
            "manufacturer": brand,
            "brand": brand,
            "model": model,
            "manufacture_year": year_match.group(1) if year_match else None,
            "engine_model": engine_match.group(1) if engine_match else None,
            "vin": vin_match.group(1).upper() if vin_match else None,
            "quantity": quantity,
            "unit_price_kzt": float(unit_price),
            "total_amount_kzt": float(total),
            "page": page,
            "source_method": "final_specification_recovery",
            "status": "corrected",
            "raw": f"{brand} {model}; количество {quantity}; цена {unit_price}; сумма {total}",
        }
        document.setdefault("tables", []).append(_asset_table(
            [row],
            "Многострочная спецификация восстановлена на финальном этапе; количество и цены прошли арифметическую проверку.",
        ))
        # Remove the completeness warning that this recovery resolves.
        document["warnings"] = [
            warning for warning in document.get("warnings", [])
            if "вид или модель предмета лизинга не определены" not in _text(
                warning.get("message_ru") or warning.get("message") if isinstance(warning, dict) else warning
            ).casefold()
        ]


def harden_vins(result: dict) -> None:
    vin_pattern = re.compile(r"(?<![A-Z0-9])[A-Z0-9]{17}(?![A-Z0-9])", re.I)
    for document in result.get("documents", []):
        candidates: dict[str, dict] = {}
        for item in document.get("fields", []):
            name = (str(item.get("name") or "") + " " + str(item.get("label_ru") or "")).casefold()
            if not any(key in name for key in VIN_KEYS):
                continue
            for source in (item.get("value"), item.get("raw_value"), item.get("quote")):
                for token in vin_pattern.findall(_text(source).upper()):
                    candidates[token] = validate_vin(token)
            validation = validate_vin(item.get("value"))
            if _text(item.get("value")) and not validation.get("valid"):
                item["status"] = "candidate"
                item["confidence"] = min(float(item.get("confidence") or 1), 0.45)
                item["validation"] = validation
        invalid = [vin for vin, check in candidates.items() if not check.get("valid")]
        if invalid:
            _warn(document, "VIN", "Найдены 17-значные идентификаторы, не прошедшие проверку VIN: " + "; ".join(invalid[:8]), "high")


def remove_signature_party_noise(result: dict) -> None:
    """Exclude electronic-signature holders from deal-party warnings.

    Signature IINs are discovered from full page text and identifier-table
    context, not only from the unresolved field quote. This matches real
    Documentolog/Sigex exports where the unresolved field itself may carry a
    generic quote while the signature context is stored elsewhere.
    """
    signature_terms = (
        "подписал", "подписано", "подпись №", "дата формирования подписи",
        "электронный документ", "электронной цифровой", "эцп",
        "сертификат", "sigex", "documentolog", "қол қой",
    )
    for document in result.get("documents", []):
        signature_values: set[str] = set()
        full_text = _document_full_text(document)
        # Look in bounded contexts around every 12-digit identifier.
        for match in re.finditer(r"(?<!\d)(\d{12})(?!\d)", full_text):
            context = full_text[max(0, match.start()-220):match.end()+220].casefold()
            if any(term in context for term in signature_terms):
                signature_values.add(match.group(1))
        for table in document.get("tables", []):
            for row in table.get("rows", []):
                if not isinstance(row, dict):
                    continue
                context = " ".join(str(v or "") for v in row.values())
                if any(term in context.casefold() for term in signature_terms):
                    for identifier in re.findall(r"(?<!\d)\d{12}(?!\d)", context):
                        signature_values.add(identifier)

        document["signature_iin_bins"] = sorted(signature_values)
        if not signature_values:
            continue
        kept_fields: list[dict] = []
        for item in document.get("fields", []):
            name = str(item.get("name") or "").casefold()
            values = item.get("value")
            value_list = list(values) if isinstance(values, (list, tuple, set)) else [values]
            cleaned_values = [v for v in value_list if _digits(v) not in signature_values]
            noise_field = any(t in name for t in (
                "unknown", "unresolved", "identifier", "party",
                "guarantor", "lessee", "borrower"
            ))
            if noise_field and len(cleaned_values) != len(value_list):
                if cleaned_values:
                    item["value"] = cleaned_values if isinstance(values, (list, tuple, set)) else cleaned_values[0]
                    item["notes"] = "ИИН подписанта ЭЦП исключён из кандидатов сторон сделки."
                    kept_fields.append(item)
                else:
                    item["status"] = "rejected"
                    item["notes"] = "ИИН относится к подписанту электронной подписи, а не к стороне сделки."
                    kept_fields.append(item)
                continue
            kept_fields.append(item)
        document["fields"] = kept_fields

        filtered_warnings = []
        for warning in document.get("warnings", []):
            text = _text((warning.get("message_ru") or warning.get("message")) if isinstance(warning, dict) else warning)
            if any(value in _digits(text) for value in signature_values):
                continue
            filtered_warnings.append(warning)
        document["warnings"] = filtered_warnings

        # Keep the identifier for traceability, but classify it correctly.
        for table in document.get("tables", []):
            for row in table.get("rows", []):
                if not isinstance(row, dict):
                    continue
                row_value = _digits(row.get("Значение") or row.get("value") or row.get("identifier"))
                if row_value in signature_values:
                    for key in list(row):
                        if str(key).casefold() in {"роль по контексту", "role", "context_role"}:
                            row[key] = "Подписант электронной подписи"


def recover_client_name_from_evidence(result: dict) -> None:
    """Recover the client's legal name next to the confirmed BIN/IIN."""
    client = result.setdefault("client", {})
    if _valid_party_name(client.get("name")):
        return
    identifier = _digits(client.get("iin_bin"))
    if len(identifier) != 12:
        return

    candidates: list[tuple[int, str]] = []
    for document in result.get("documents", []):
        text = _document_full_text(document)
        for occurrence in re.finditer(rf"(?<!\d){re.escape(identifier)}(?!\d)", text):
            context = text[max(0, occurrence.start()-500):occurrence.end()+250]
            patterns = (
                # Russian legal form before the name.
                (r"(?:индивидуальный\s+предприниматель|\bИП\b)\s*[«\"]?([^»\",;\n]{2,100})", "ИП", 120),
                (r"(?:товарищество\s+с\s+ограниченной\s+ответственностью|\bТОО\b|\bЖШС\b)\s*[«\"]?([^»\",;\n]{2,100})", "ТОО", 120),
                # Kazakhstan bilingual templates often put the name first:
                # “Калиев А.К. Жеке кәсіпкер, ЖСН 680...”
                (r"([А-ЯЁӘІҢҒҮҰҚӨҺ][А-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ-]+(?:\s+[А-ЯЁӘІҢҒҮҰҚӨҺ]\.[А-ЯЁӘІҢҒҮҰҚӨҺ]\.)?)\s+Жеке\s+кәсіпкер", "ИП", 150),
                (r"[«\"]([^»\"]{2,100})[»\"]\s+(?:жауапкершілігі\s+шектеулі\s+серіктестік|ЖШС)", "ТОО", 140),
            )
            for pattern, legal, base_score in patterns:
                for match in re.finditer(pattern, context, re.I):
                    name = _text(match.group(1)).strip(" «»\".,;:-")
                    if not name or _is_bcc(name) or not _valid_party_name(name):
                        continue
                    if len(name.split()) > 10:
                        continue
                    formatted = name if re.match(r"^(?:ИП|ТОО|ЖШС)\b", name, re.I) else f"{legal} «{name}»"
                    distance = abs(match.end() - (occurrence.start() - max(0, occurrence.start()-500)))
                    candidates.append((base_score - min(distance // 10, 100), formatted))
    if candidates:
        candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        client["name"] = candidates[0][1]


def suppress_redundant_party_warnings(result: dict) -> None:
    """Remove role/name candidates already resolved by the confirmed client."""
    client = result.get("client") or {}
    confirmed_name = _text(client.get("name"))
    confirmed_id = _digits(client.get("iin_bin"))
    if not (_valid_party_name(confirmed_name) and len(confirmed_id) == 12):
        return
    normalized_client = re.sub(r"[^a-zа-яәіңғүұқөһ0-9]", "", confirmed_name.casefold())
    for document in result.get("documents", []):
        for item in document.get("fields", []):
            name = str(item.get("name") or "").casefold()
            if name not in {
                "lessee_name", "borrower_name", "principal_name",
                "recipient_name", "customer_name"
            }:
                continue
            value = _text(item.get("value"))
            normalized_value = re.sub(r"[^a-zа-яәіңғүұқөһ0-9]", "", value.casefold())
            if _is_bcc(value) or not _valid_party_name(value):
                item["status"] = "rejected"
            elif normalized_value and (normalized_value in normalized_client or normalized_client in normalized_value):
                item["original_value"] = item.get("original_value", item.get("value"))
                item["value"] = confirmed_name
                item["normalized_value"] = confirmed_name
                item["status"] = "corrected"
                item["notes"] = "Сокращённое наименование приведено к подтверждённому имени клиента."
        party_names = {"lessee_name", "borrower_name", "principal_name", "recipient_name", "customer_name"}
        document["fields"] = [
            item for item in document.get("fields", [])
            if not (item.get("status") == "rejected" and str(item.get("name") or "").casefold() in party_names)
        ]

        filtered = []
        for warning in document.get("warnings", []):
            field = _text(warning.get("field") if isinstance(warning, dict) else "").casefold()
            message = _text((warning.get("message_ru") or warning.get("message")) if isinstance(warning, dict) else warning).casefold()
            role_warning = any(token in field or token in message for token in (
                "лизингополучател", "заёмщик", "заемщик", "получатель", "клиент"
            ))
            already_resolved = (
                "похоже на определение роли" in message
                or "не пройдена проверка наименования" in message
                or "значение требует ручного подтверждения" in message
                or any(
                    token and token in normalized_client
                    for token in re.findall(r"[a-zа-яәіңғүұқөһ]{4,}", message)
                )
            )
            if role_warning and already_resolved:
                continue
            filtered.append(warning)
        document["warnings"] = filtered


def require_identifiable_equipment(result: dict) -> None:
    """Block a ready result when an asset exists but its kind/model is unknown."""
    for document in result.get("documents", []):
        equipment_signal = False
        identified = False
        for item in document.get("fields", []):
            name = str(item.get("name") or "").casefold()
            if any(token in name for token in ("equipment", "asset", "vehicle", "transport")):
                equipment_signal = True
                value = _text(item.get("value"))
                if value and value.casefold() not in {"не определено", "не определён", "unknown"} and _valid_party_name(value):
                    identified = True
        for table in document.get("tables", []):
            table_name = str(table.get("name") or "").casefold()
            if table_name in EQUIPMENT_TABLE_NAMES or any(t in table_name for t in ("equipment", "asset", "vehicle", "transport", "техник")):
                rows = [r for r in table.get("rows", []) if isinstance(r, dict)]
                if rows:
                    equipment_signal = True
                for row in rows:
                    values = [_text(v) for k, v in row.items() if any(t in str(k).casefold() for t in (
                        "model", "модель", "brand", "марка", "equipment_name", "наименование", "asset_type", "вид техник"
                    ))]
                    if any(v and v.casefold() not in {"не определено", "не определён", "unknown"} for v in values):
                        identified = True
        full_text = _document_full_text(document).casefold()
        explicit_specification = any(token in full_text for token in (
            "спецификация", "цена за единицу", "количество, штук",
            "техническая характеристика", "предмет финансирования"
        ))
        if (equipment_signal or explicit_specification) and not identified:
            _warn(document, "Техника", "Вид или модель предмета лизинга не определены.", "high")


# v22: final universal normalisation for new client/specification templates.
def _clean_legal_client_name(value: Any) -> str:
    text = _text(value).strip(' ,.;:-')
    if not text:
        return text
    # Do not let a requisites label/address become part of the legal name.
    text = re.split(
        r"\b(?:Мекенжайы\s*/\s*Адрес|Адрес|Мекенжайы|БСН\s*/\s*БИН|БИН|ЖСН\s*/\s*ИИН|ИИН|ИИК|IBAN)\s*[:：]?",
        text,
        maxsplit=1,
        flags=re.I,
    )[0].strip(' ,.;:-«»"')
    # Normalize legal form and quotes without inventing the name itself.
    m = re.match(r"^(ИП|ТОО|ЖШС)\s*[«\"]?(.*?)[»\"]?$", text, re.I)
    if m:
        legal = m.group(1).upper()
        if legal == 'ЖШС':
            legal = 'ТОО'
        name = m.group(2).strip(' «»".,;:-')
        if name:
            return f'{legal} «{name}»'
    return text


def _client_evidence_candidates(text: str, identifier: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    if len(identifier) != 12:
        return out
    for occ in re.finditer(rf"(?<!\d){re.escape(identifier)}(?!\d)", text):
        context = text[max(0, occ.start()-650):occ.end()+220]
        patterns = (
            # Highest-confidence requisites/role headers. These are much safer than
            # narrative phrases such as Kazakh "деп аталатын ..." around the party.
            (r"(?:ЛИЗИНГ\s+АЛУШЫ\s*/\s*ЛИЗИНГОПОЛУЧАТЕЛЬ|ЛИЗИНГОПОЛУЧАТЕЛЬ)\s*:\s*(?:ЖК\s*/\s*ИП|ИП)\s*[«\"]?([^»\"\n,;]{2,100})", 'ИП', 360),
            (r"(?:ЛИЗИНГ\s+АЛУШЫ\s*/\s*ЛИЗИНГОПОЛУЧАТЕЛЬ|ЛИЗИНГОПОЛУЧАТЕЛЬ)\s*:\s*(?:ЖШС\s*/\s*ТОО|ТОО|ЖШС)\s*[«\"]?([^»\"\n,;]{2,120})", 'ТОО', 360),
            # Russian LLP / sole proprietor before exact BIN/IIN.
            (r"(?:Товарищество\s+с\s+ограниченной\s+ответственностью|ТОО|ЖШС)\s*[«\"]([^»\"]{2,120})[»\"]?\s*,?\s*(?:БИН|БСН)", 'ТОО', 220),
            (r"(?:Индивидуальный\s+предприниматель|ИП)\s*[«\"]?([^»\"\n,;]{2,100})[»\"]?[^\n]{0,160}?(?:ИИН|ЖСН)", 'ИП', 210),
            # Kazakh forms: “Name Жеке кәсіпкер, ЖСН ...” and quoted LLP name.
            (r"(?:деп\s+аталатын\s+)?([А-ЯЁӘІҢҒҮҰҚӨҺA-Z][А-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһA-Za-z0-9._-]*(?:\s+[А-ЯЁӘІҢҒҮҰҚӨҺA-Z][А-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһA-Za-z0-9._-]*){0,3})\s+Жеке\s+кәсіпкер[^\n]{0,80}?(?:ЖСН|ИИН)", 'ИП', 230),
            (r"[«\"]([^»\"]{2,120})[»\"]\s+жауапкершілігі\s+шектеулі\s+серіктестігі[^\n]{0,100}?(?:БСН|БИН)", 'ТОО', 240),
        )
        for pattern, legal, base in patterns:
            for m in re.finditer(pattern, context, re.I):
                name = _clean_legal_client_name(m.group(1))
                name = re.sub(r"\s+", " ", name).strip(' «»".,;:-')
                if not name or _is_bcc(name) or len(name) > 120:
                    continue
                # Remove common lead-in fragments accidentally captured before the name.
                name = re.sub(r"^(?:деп\s+аталатын|именуем(?:ое|ый|ая)\s+далее)\s+", "", name, flags=re.I)
                formatted = name if re.match(r"^(?:ИП|ТОО|ЖШС)\b", name, re.I) else f'{legal} «{name}»'
                formatted = _clean_legal_client_name(formatted)
                if _valid_party_name(formatted):
                    out.append((base, formatted))
    return out


def improve_client_legal_name(result: dict) -> None:
    client = result.setdefault('client', {})
    identifier = _digits(client.get('iin_bin'))
    current = _clean_legal_client_name(client.get('name'))
    if current:
        client['name'] = current
    if len(identifier) != 12:
        return
    candidates: list[tuple[int, str]] = []
    for document in result.get('documents', []):
        candidates.extend(_client_evidence_candidates(_document_full_text(document), identifier))
    if not candidates:
        return
    candidates.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
    best = candidates[0][1]
    cur_norm = re.sub(r"[^0-9a-zа-яәіңғүұқөһ]", "", current.casefold()) if current else ''
    best_norm = re.sub(r"[^0-9a-zа-яәіңғүұқөһ]", "", best.casefold())
    current_has_bad_tail = bool(re.search(r"мекенжай|адрес|облыс|район|улиц|көш", current, re.I))
    current_core = re.sub(r"^(?:тоо|ип|жшс)", "", cur_norm)
    best_core = re.sub(r"^(?:тоо|ип|жшс)", "", best_norm)
    # OCR/narrative capture often prepends one grammatical word before the real
    # legal name (e.g. a lower-case Kazakh/Russian lead-in). Prefer a high-score
    # role/requisites candidate when the current core merely ends with it.
    current_inner = re.sub(r"^(?:ТОО|ИП|ЖШС)\s*[«\"]?", "", current, flags=re.I).strip(' «»"')
    best_inner = re.sub(r"^(?:ТОО|ИП|ЖШС)\s*[«\"]?", "", best, flags=re.I).strip(' «»"')
    leading_fragment = bool(
        best_core and current_core.endswith(best_core) and current_core != best_core
        and len(current_core) - len(best_core) <= 24
        and current_inner.split() and current_inner.split()[0][:1].islower()
    )
    # Upgrade only when the evidence is clearly more complete or current value is polluted.
    if (
        not _valid_party_name(current)
        or current_has_bad_tail
        or leading_fragment
        or (current_core and best_core and current_core in best_core and len(best_core) >= len(current_core) + 3)
        or (len(current_core) <= 5 and len(best_core) > len(current_core))
        or (current_core == best_core and not re.match(r"^(?:ТОО|ИП|ЖШС)\b", current, re.I) and re.match(r"^(?:ТОО|ИП|ЖШС)\b", best, re.I))
    ):
        client['name'] = best
        for document in result.get('documents', []):
            for item in document.get('fields', []):
                if str(item.get('name') or '').casefold() in {
                    'lessee_name','borrower_name','principal_name','recipient_name','customer_name','client_name'
                }:
                    value = _text(item.get('value'))
                    norm = re.sub(r"[^0-9a-zа-яәіңғүұқөһ]", "", value.casefold())
                    if not value or norm in best_norm or best_norm in norm or norm == cur_norm:
                        item.setdefault('original_value', item.get('value'))
                        item['value'] = best
                        item['normalized_value'] = best
                        item['status'] = 'corrected'


def _money_candidates(text: str) -> list[Decimal]:
    vals: list[Decimal] = []
    for token in re.findall(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)(?!\d)", text):
        val = _decimal(token)
        if val is not None:
            vals.append(val)
    return vals


def repair_explicit_itogo_amounts(result: dict) -> None:
    """Prefer an explicit БАРЛЫҒЫ/ИТОГО amount over an OCR fragment."""
    amount_names = {
        'act_total_amount_kzt','equipment_total_kzt','purchase_total_kzt',
        'total_amount_kzt','lease_asset_value_kzt','asset_value_kzt',
    }
    for document in result.get('documents', []):
        full = _document_full_text(document)
        explicit_totals: list[tuple[int, Decimal, str]] = []
        for m in re.finditer(r"(?:БАРЛЫҒЫ\s*/?\s*)?ИТОГО\s*[:：]?", full, re.I):
            segment = full[m.end():m.end()+180]
            vals = [v for v in _money_candidates(segment) if v >= Decimal('100000')]
            if vals:
                explicit_totals.append((m.start(), max(vals), segment))
        if not explicit_totals:
            continue
        for item in document.get('fields', []):
            name = str(item.get('name') or '').casefold()
            label = str(item.get('label_ru') or '').casefold()
            if name not in amount_names and not any(t in label for t in ('общая стоимость', 'стоимость по акту', 'итого')):
                continue
            quote = str(item.get('quote') or '')
            local = None
            if re.search(r"(?:БАРЛЫҒЫ\s*/?\s*)?ИТОГО", quote, re.I):
                qm = re.search(r"(?:БАРЛЫҒЫ\s*/?\s*)?ИТОГО\s*[:：]?(.*)", quote, re.I | re.S)
                vals = [v for v in _money_candidates(qm.group(1)[:180] if qm else quote) if v >= Decimal('100000')]
                if vals:
                    local = max(vals)
            chosen = local or explicit_totals[-1][1]
            old = _decimal(item.get('value'))
            if old is None or abs(old - chosen) > max(Decimal('1'), chosen * Decimal('0.01')):
                item.setdefault('original_value', item.get('value'))
                item['value'] = float(chosen)
                item['normalized_value'] = float(chosen)
                item['status'] = 'corrected'
                item['confidence'] = max(float(item.get('confidence') or 0), 0.99)
                item['notes'] = 'Стоимость восстановлена по явной строке БАРЛЫҒЫ/ИТОГО.'


_EQUIPMENT_TYPE_PATTERNS = (
    ('Рефрижератор', r'\bрефрижератор\b'),
    ('Автобетоносмеситель', r'\bавтобетоносмесител'),
    ('Манипулятор', r'\bманипулятор\b'),
    ('Экскаватор-погрузчик', r'\bэкскаватор\s*[-–—]?\s*погрузчик'),
    ('Фронтальный погрузчик', r'\bфронтальн\w*\s+погрузчик'),
    ('Автокран', r'\bавтокран\b'),
    ('Бульдозер', r'\bбульдозер\b'),
    ('Экскаватор', r'\bэкскаватор\b'),
    ('Каток', r'\bкаток\b'),
    ('Самосвал', r'\bсамосвал\b'),
    ('Седельный тягач', r'\b(?:седельн\w*\s+тягач|тягач)\b'),
)


def _refine_row_from_vin_context(document: dict, row: dict) -> None:
    vin = _text(row.get('vin') or row.get('VIN') or row.get('vin_number'))
    if not vin:
        return
    text = _document_full_text(document)
    pos = text.upper().find(vin.upper())
    if pos < 0:
        return
    context = text[max(0,pos-500):pos+220]
    equipment_type = None
    for label, pattern in _EQUIPMENT_TYPE_PATTERNS:
        if re.search(pattern, context, re.I):
            equipment_type = label
            break
    if equipment_type:
        row['equipment_type'] = equipment_type
    # Common vehicle line: "Рефрижератор FAW 4x2, 2025 ... VIN ..."
    m = re.search(
        r"(?:Рефрижератор|Автобетоносмеситель|Самосвал|тягач|погрузчик|экскаватор|автокран)\s+"
        r"([A-ZА-Я0-9][A-ZА-Я0-9._/-]*(?:\s+[A-ZА-Я0-9][A-ZА-Я0-9._/-]*){0,3})"
        r"(?=\s*,?\s*20\d{2}|\s+VIN|\s+VINE)",
        context, re.I,
    )
    if m:
        model_text = re.sub(r"\s+", " ", m.group(1)).strip(' ,.;:-')
        # Avoid swallowing the equipment type itself.
        if model_text and 'VIN' not in model_text.upper():
            row['model'] = model_text
            row['equipment_name'] = f"{equipment_type or 'Техника'} {model_text}".strip()
            brand = model_text.split()[0]
            if re.fullmatch(r'[A-ZА-Я]{2,15}', brand, re.I):
                row['brand'] = row.get('brand') or brand.upper()
                row['manufacturer'] = row.get('manufacturer') or brand.upper()


def clean_asset_rows_and_recompute(result: dict) -> None:
    for document in result.get('documents', []):
        for table in document.get('tables', []):
            name = str(table.get('name') or '').casefold()
            if name != 'asset_vin_rows' and not any(t in name for t in ('vehicle','transport','equipment','asset','техник')):
                continue
            rows = [r for r in table.get('rows', []) if isinstance(r, dict)]
            cleaned: list[dict] = []
            seen_ids: set[tuple[str,str]] = set()
            for row in rows:
                _refine_row_from_vin_context(document, row)
                vin = _text(row.get('vin') or row.get('VIN') or row.get('vin_number'))
                serial = _text(row.get('serial_number') or row.get('chassis_number'))
                model = _text(row.get('model') or row.get('equipment_name') or row.get('brand') or row.get('manufacturer'))
                if model:
                    cleaned_model = re.split(r"\s*/\s*(?=[А-ЯA-Z])|\b(?:Шығарылған\s+жылы|Год\s+выпуска)\b", model, maxsplit=1, flags=re.I)[0].strip(' ,.;:-/')
                    if cleaned_model and len(cleaned_model) >= 2:
                        model = cleaned_model
                        if row.get('model') not in (None, ''):
                            row['model'] = cleaned_model
                qty = _decimal(row.get('quantity') or row.get('Количество'))
                unit = _decimal(row.get('unit_price_kzt') or row.get('Цена за единицу, тенге'))
                total = _decimal(row.get('total_amount_kzt') or row.get('Общая стоимость позиции, тенге') or row.get('Стоимость, тенге'))
                blob = ' '.join(_text(v) for v in row.values())
                if re.search(r'\b(?:doc id|электронный документ подписан|р/н\s*/\s*№|№\s*п/п|^п/п$)\b', blob, re.I):
                    continue
                if _looks_like_header_or_prose_asset_row(row):
                    _add_nonblocking_note(document, "Техника", "Служебная строка спецификации исключена из списка предметов лизинга.")
                    continue
                # A lone header/text fragment or lone total is not an asset row.
                if not (vin or serial or model):
                    continue
                if not (vin or serial) and not model:
                    continue
                # A weak model-only fragment needs at least quantity or price evidence.
                if not (vin or serial) and model and qty is None and unit is None and total is None:
                    continue
                key = (vin or serial, re.sub(r'\s+',' ',model.casefold()))
                if key in seen_ids and (vin or serial):
                    continue
                if vin or serial:
                    seen_ids.add(key)
                cleaned.append(row)
            table['rows'] = cleaned
            table['row_count'] = len(cleaned)
            if name == 'asset_vin_rows' or 'asset' in name or 'vehicle' in name or 'transport' in name:
                quantities = []
                for row in cleaned:
                    q = _decimal(row.get('quantity'))
                    quantities.append(int(q) if q is not None and q > 0 else 1)
                ids = {_text(r.get('vin') or r.get('serial_number') or r.get('chassis_number')) for r in cleaned}
                ids.discard('')
                table['summary'] = {
                    'total_quantity': sum(quantities) if quantities else None,
                    'unique_vin_count': len(ids),
                    'equipment_by_type': {
                        label: sum(int(_decimal(r.get('quantity')) or 1) for r in cleaned if _text(r.get('equipment_type')) == label)
                        for label in sorted({_text(r.get('equipment_type')) for r in cleaned if _text(r.get('equipment_type'))})
                    },
                    'total_identified_amount_kzt': sum(float(_decimal(r.get('total_amount_kzt')) or 0) for r in cleaned) or None,
                }



# v24: semantic separation of equipment model vs engine model and table synthesis.
_ENGINE_MODEL_PATTERNS = (
    r"(?:МОДЕЛЬ\s+ДВИГАТЕЛЯ|Модель\s+двигателя|ҚОЗҒАЛТҚЫШ\s+МОДЕЛІ)\s*[:：]?\s*([A-Z0-9][A-Z0-9._/-]*(?:\s+[A-Z0-9][A-Z0-9._/-]*){0,2})",
    r"(?:Двигатель|Қозғалтқыш)\s+[A-ZА-Я0-9._/-]{2,20}\s+(?:Модель|Үлгі)\s*[:：]?\s*([A-Z0-9][A-Z0-9._/-]*(?:\s+[A-Z0-9][A-Z0-9._/-]*){0,2})",
)

_EXPLICIT_EQUIPMENT_MODEL_PATTERNS = (
    # Brand/model labels in a specification cell. Handles page breaks after МОДЕЛЬ:.
    r"(?:МАРКА|Марка)\s*[:：]\s*([A-ZА-Я0-9._/-]{2,24})[\s\S]{0,120}?(?:МОДЕЛЬ|Модель)\s*[:：]\s*([A-Z0-9][A-Z0-9._/-]*(?:\s+[A-Z0-9][A-Z0-9._/-]*){0,2})",
    # Bilingual explicit label used by many dossiers.
    r"(?:Үлгі\s*/\s*Модель|Модель)\s*[:：]?\s*([A-ZА-Я0-9][A-ZА-Я0-9._/-]*(?:\s+[A-ZА-Я0-9][A-ZА-Я0-9._/-]*){0,2})",
)

_ENGINEISH_TOKENS = {
    'WEICHAI','YUCHAI','CUMMINS','SHANGCHAI','DEUTZ','ISUZU','YANMAR','PERKINS',
}


def _compact_model_token(value: str) -> str:
    value = re.sub(r"\s+", " ", _text(value)).strip(' ,.;:-')
    # Typical OCR/table extraction splits letter+digits: B 877F -> B877F.
    if re.fullmatch(r"[A-ZА-Я]\s+\d[A-ZА-Я0-9._/-]{2,20}", value, re.I):
        value = re.sub(r"\s+", "", value)
    return value


def _page_text_for_row(document: dict, row: dict) -> str:
    page_no = row.get('page') or row.get('Страница')
    try:
        page_no = int(float(page_no))
    except (TypeError, ValueError):
        page_no = None
    if page_no:
        for page in document.get('page_texts', []):
            if isinstance(page, dict) and int(page.get('page') or 0) == page_no:
                return str(page.get('text') or '')
    return ''


def _best_context_for_asset(document: dict, row: dict) -> str:
    full = _document_full_text(document)
    anchors = [
        _text(row.get('vin') or row.get('VIN') or row.get('vin_number')),
        _text(row.get('serial_number') or row.get('chassis_number')),
        _text(row.get('model')),
        _text(row.get('equipment_name')),
    ]
    for anchor in anchors:
        if not anchor or len(anchor) < 4:
            continue
        pos = full.casefold().find(anchor.casefold())
        if pos >= 0:
            return full[max(0, pos-850):pos+950]
    page_text = _page_text_for_row(document, row)
    return page_text or full[:18000]


def _extract_engine_model(context: str) -> str | None:
    for pattern in _ENGINE_MODEL_PATTERNS:
        m = re.search(pattern, context, re.I | re.S)
        if m:
            value = _compact_model_token(m.group(1))
            value = re.split(r"\s*(?:,|;|20\d{2}\s*(?:г|ж)|VIN|НОМЕР|ШАССИ)\b", value, maxsplit=1, flags=re.I)[0].strip()
            if value:
                return value
    return None


def _model_occurrence_is_engine(context: str, start: int) -> bool:
    prefix = context[max(0, start-55):start].casefold()
    return bool(re.search(r"(?:двигател|қозғалтқыш)[^\n,;:]{0,45}$", prefix, re.I))



_ASSET_MODEL_STOPWORDS = {
    'DOC','ID','ДВИГАТЕЛЯ','ДВИГАТЕЛЬ','ГОД','ВЫПУСКА','МОДЕЛЬ','ҮЛГІ',
    'МИЛЛИОН','ТЕНГЕ','ТЕҢГЕ','ЭЛЕКТРОННЫЙ','ДОКУМЕНТ','ПОДПИСАН',
}


def _valid_asset_model_candidate(value: str, engine_model: str | None = None) -> bool:
    value = _compact_model_token(value).upper()
    norm = _normalise_equipment_token(value)
    if not value or len(norm) < 4 or len(norm) > 32:
        return False
    if re.fullmatch(r'[\d .,/:-]+', value):
        return False
    if value.startswith(('KZPK', 'DOC ', 'ID ', 'HTTP')):
        return False
    words = set(re.findall(r'[A-ZА-ЯӘІҢҒҮҰҚӨҺ]+', value, re.I))
    if words & _ASSET_MODEL_STOPWORDS:
        return False
    if engine_model and norm == _normalise_equipment_token(engine_model):
        return False
    # A useful model normally contains both letters and digits, or a brand+model pair.
    has_letter = bool(re.search(r'[A-ZА-ЯӘІҢҒҮҰҚӨҺ]', value, re.I))
    has_digit = bool(re.search(r'\d', value))
    return has_letter and (has_digit or len(value.split()) >= 2)


def _scan_model_after_label(context: str, label_end: int, engine_model: str | None = None) -> tuple[str | None, str | None]:
    window = context[label_end:label_end + 650]
    # Keep content after Documentolog page footer; only remove footer phrases/IDs.
    window = re.sub(r'DOC\s+ID\s+KZ[A-Z0-9]+', ' ', window, flags=re.I)
    window = re.sub(r'Электронный\s+документ\s+подписан\s+в\s+Documentolog\s+Business', ' ', window, flags=re.I)
    # Search brand+model pairs first, then compact alphanumeric model codes.
    candidates: list[tuple[int, str, str | None]] = []
    for m in re.finditer(r'\b([A-ZА-ЯӘІҢҒҮҰҚӨҺ]{2,16})\s+([A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9][A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9._/-]{2,28})\b', window, re.I):
        brand = _compact_model_token(m.group(1)).upper()
        model_token = _compact_model_token(m.group(2)).upper()
        value = f'{brand} {model_token}'
        if brand in _ENGINEISH_TOKENS or not _valid_asset_model_candidate(value, engine_model):
            continue
        # Strong preference for known manufacturer-looking all-letter first token.
        score = 40 - min(m.start(), 35)
        if re.search(r'\d', model_token):
            score += 25
        candidates.append((score, value, brand))
    for m in re.finditer(r'\b([A-ZА-ЯӘІҢҒҮҰҚӨҺ][A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9._/-]{3,30})\b', window, re.I):
        value = _compact_model_token(m.group(1)).upper()
        if not _valid_asset_model_candidate(value, engine_model):
            continue
        score = 25 - min(m.start(), 20)
        if re.search(r'[A-ZА-ЯӘІҢҒҮҰҚӨҺ]', value, re.I) and re.search(r'\d', value):
            score += 20
        candidates.append((score, value, None))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, value, brand = candidates[0]
    return value, brand

def _extract_equipment_model(context: str, engine_model: str | None = None) -> tuple[str | None, str | None]:
    # Strong brand + model pair first, but validate because flattened PDF columns
    # can place a price immediately after the label instead of the visual model.
    pair = re.search(_EXPLICIT_EQUIPMENT_MODEL_PATTERNS[0], context, re.I | re.S)
    if pair:
        brand = _compact_model_token(pair.group(1)).upper()
        model = _compact_model_token(pair.group(2)).upper()
        model = re.split(r"\s*(?:,|;|ГОД|ШЫҒАРЫЛҒАН|ДВИГАТЕЛ|ҚОЗҒАЛТҚЫШ|VIN)\b", model, maxsplit=1, flags=re.I)[0].strip()
        if _valid_asset_model_candidate(model, engine_model):
            return model, brand
        scanned, scanned_brand = _scan_model_after_label(context, pair.end(0), engine_model)
        if scanned:
            return scanned, scanned_brand or brand

    # Explicit model labels. Exclude engine labels and validate their immediate
    # value; if table flattening displaced the value, scan the following cell text.
    label_re = re.compile(r"(?:Үлгі\s*/\s*Модель|МОДЕЛЬ|Модель)\s*[:：]?", re.I)
    for label in label_re.finditer(context):
        if _model_occurrence_is_engine(context, label.start()):
            continue
        tail = context[label.end():label.end()+120]
        direct = re.match(r"\s*([A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9][A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9._/-]*(?:\s+[A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9][A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9._/-]*){0,2})", tail, re.I)
        if direct:
            value = _compact_model_token(direct.group(1)).upper()
            value = re.split(r"\s*(?:,|;|ГОД|ШЫҒАРЫЛҒАН|ДВИГАТЕЛ|ҚОЗҒАЛТҚЫШ|VIN)\b", value, maxsplit=1, flags=re.I)[0].strip()
            if _valid_asset_model_candidate(value, engine_model):
                brand = value.split()[0] if len(value.split()) > 1 and re.fullmatch(r'[A-ZА-ЯӘІҢҒҮҰҚӨҺ]{2,18}', value.split()[0], re.I) else None
                return value, brand
        scanned, scanned_brand = _scan_model_after_label(context, label.end(), engine_model)
        if scanned:
            return scanned, scanned_brand

    # Common first-column descriptions: "SDLG Модель B 877F" or "XCMG QY25K5D".
    equipment_words = r"(?:автокран|бульдозер|экскаватор(?:-погрузчик)?|погрузчик|манипулятор|самосвал|тягач|рефрижератор|автобетоносмеситель)"
    for m in re.finditer(rf"{equipment_words}[\s\S]{{0,180}}?([A-ZА-ЯӘІҢҒҮҰҚӨҺ]{{2,15}})\s+(?:Модель\s*)?([A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9][A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9._/-]*(?:\s+[A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9][A-ZА-ЯӘІҢҒҮҰҚӨҺ0-9._/-]*)?)", context, re.I):
        brand = _compact_model_token(m.group(1)).upper()
        model = _compact_model_token(m.group(2)).upper()
        value = f"{brand} {model}".strip()
        if brand in _ENGINEISH_TOKENS or not _valid_asset_model_candidate(value, engine_model):
            continue
        return value, brand
    return None, None

def _normalise_equipment_token(value: Any) -> str:
    return re.sub(r'[^A-ZА-Я0-9]', '', _text(value).upper())


def _infer_equipment_type(context: str, current: Any = None) -> str | None:
    current_text = _text(current)
    generic = current_text.casefold() in {'', 'техника', 'транспорт', 'предмет лизинга', 'оборудование'}
    for label, pattern in _EQUIPMENT_TYPE_PATTERNS:
        if re.search(pattern, context, re.I):
            return label
    return None if generic else current_text


def _find_field(document: dict, names: set[str]) -> dict | None:
    for item in document.get('fields', []):
        if str(item.get('name') or '').casefold() in names and item.get('value') not in (None, '', []):
            return item
    return None


def _set_field_from_asset(document: dict, name: str, label: str, value: Any, confidence: float = .99) -> None:
    if value in (None, '', []):
        return
    fields = document.setdefault('fields', [])
    existing = next((f for f in fields if str(f.get('name') or '').casefold() == name.casefold()), None)
    if existing:
        old = existing.get('value')
        if _normalise_equipment_token(old) != _normalise_equipment_token(value):
            existing.setdefault('original_value', old)
            existing['value'] = value
            existing['normalized_value'] = value
            existing['status'] = 'corrected'
            existing['confidence'] = max(float(existing.get('confidence') or 0), confidence)
            existing['notes'] = 'Поле синхронизировано с явной строкой спецификации.'
        return
    fields.append({
        'name': name, 'label_ru': label, 'value': value, 'normalized_value': value,
        'status': 'corrected', 'confidence': confidence,
        'notes': 'Поле восстановлено из явной строки спецификации.',
    })


def _infer_brand_from_context(context: str, equipment_type: Any = None) -> str | None:
    # Explicit brand/manufacturer label is strongest.
    m = re.search(r"(?:МАРКА|Марка|Бренд|Производитель)\s*[:：]?\s*([A-ZА-ЯӘІҢҒҮҰҚӨҺ]{2,18})\b", context, re.I)
    if m:
        value = m.group(1).upper()
        if value not in _ENGINEISH_TOKENS and value not in {'МОДЕЛЬ','ДВИГАТЕЛЯ','ГОД'}:
            return value
    equipment_words = r"(?:автокран|бульдозер|экскаватор(?:-погрузчик)?|погрузчик|манипулятор|самосвал|тягач|рефрижератор|автобетоносмеситель)"
    generic_words = {
        'АВТОКРАН','БУЛЬДОЗЕР','ЭКСКАВАТОР','ПОГРУЗЧИК','МАНИПУЛЯТОР','САМОСВАЛ',
        'ТЯГАЧ','РЕФРИЖЕРАТОР','АВТОБЕТОНОСМЕСИТЕЛЬ','МОДЕЛЬ','ГОД','ВЫПУСКА',
    }
    # Prefer a type + brand followed reasonably soon by an explicit model label.
    for m in re.finditer(rf"{equipment_words}\s+([A-ZА-ЯӘІҢҒҮҰҚӨҺ]{{2,18}})\b[\s\S]{{0,80}}?(?:Модель|Үлгі)", context, re.I):
        value = m.group(1).upper()
        if value not in _ENGINEISH_TOKENS and value not in generic_words:
            return value
    # Flattened columns can duplicate the type; inspect every immediate token and
    # skip another equipment-word token until a manufacturer-like one is found.
    for m in re.finditer(rf"{equipment_words}\s+([A-ZА-ЯӘІҢҒҮҰҚӨҺ]{{2,18}})\b", context, re.I):
        value = m.group(1).upper()
        if value not in _ENGINEISH_TOKENS and value not in generic_words:
            return value
    return None

def _brand_model_display(brand: Any, model: Any) -> str:
    brand_t, model_t = _text(brand).upper(), _text(model)
    if not model_t:
        return ''
    if brand_t and not _normalise_equipment_token(model_t).startswith(_normalise_equipment_token(brand_t)):
        return f"{brand_t} {model_t}".strip()
    return model_t


def normalize_equipment_semantics(result: dict) -> None:
    """Separate asset model from engine model and synchronize fields/tables.

    The rules are label/context based and intentionally contain no client-specific
    names. This protects new folders with the same leasing/specification formats.
    """
    for document in result.get('documents', []):
        asset_tables = [
            table for table in document.get('tables', [])
            if str(table.get('name') or '').casefold() == 'asset_vin_rows'
            or any(t in str(table.get('name') or '').casefold() for t in ('vehicle','transport','equipment','asset','техник'))
        ]
        rows = [row for table in asset_tables for row in table.get('rows', []) if isinstance(row, dict)]

        for row in rows:
            context = _best_context_for_asset(document, row)
            engine_model = _extract_engine_model(context)
            equipment_model, brand = _extract_equipment_model(context, engine_model)
            equipment_type = _infer_equipment_type(context, row.get('equipment_type'))
            brand = brand or _infer_brand_from_context(context, equipment_type)
            if not brand and equipment_model:
                model_re = re.escape(_text(equipment_model))
                near = re.search(rf"\b([A-ZА-ЯӘІҢҒҮҰҚӨҺ]{{2,18}})\b[\s\S]{{0,70}}?(?:Модель\s*[:：]?\s*)?{model_re}", context, re.I)
                if near:
                    candidate_brand = near.group(1).upper()
                    if candidate_brand not in _ENGINEISH_TOKENS and candidate_brand not in {'МАНИПУЛЯТОР','АВТОКРАН','БУЛЬДОЗЕР','ЭКСКАВАТОР','ПОГРУЗЧИК','МОДЕЛЬ'}:
                        brand = candidate_brand

            current_model = _text(row.get('model'))
            current_engine = _text(row.get('engine_model'))
            model_is_engine = bool(
                current_model and engine_model
                and _normalise_equipment_token(current_model) == _normalise_equipment_token(engine_model)
            )
            if equipment_model and (model_is_engine or not current_model or current_model.casefold() in {'xcmg','sdlg','liugong','техника'}):
                row['model'] = equipment_model
            elif equipment_model and current_model and len(_normalise_equipment_token(equipment_model)) > len(_normalise_equipment_token(current_model)):
                # Prefer a clearly more specific explicitly labelled model.
                row['model'] = equipment_model
            if engine_model:
                row['engine_model'] = engine_model
            elif current_engine and current_model and _normalise_equipment_token(current_engine) == _normalise_equipment_token(current_model):
                row['engine_model'] = None
            if equipment_type:
                row['equipment_type'] = equipment_type
            if brand:
                row['brand'] = row.get('brand') or brand
                row['manufacturer'] = row.get('manufacturer') or brand
            if row.get('model'):
                display_model = _brand_model_display(row.get('brand'), row.get('model'))
                row['equipment_name'] = f"{row.get('equipment_type') or 'Техника'} {display_model}".strip()
            else:
                display_model = ''

            _set_field_from_asset(document, 'equipment_model', 'Марка / модель техники', display_model or row.get('model'))
            _set_field_from_asset(document, 'equipment_type', 'Вид техники', row.get('equipment_type'))
            _set_field_from_asset(document, 'engine_model', 'Модель двигателя', row.get('engine_model'))

        # If the extractor found good fields but the structured asset table was lost,
        # synthesize one conservative row so the Excel transport sheet is not empty.
        useful_rows = [r for r in rows if _text(r.get('model') or r.get('equipment_type') or r.get('vin'))]
        if not useful_rows:
            model_f = _find_field(document, {'equipment_model','vehicle_model','model'})
            type_f = _find_field(document, {'equipment_type','vehicle_type','asset_type'})
            vin_f = _find_field(document, {'vin','vehicle_vin','equipment_vin'})
            qty_f = _find_field(document, {'equipment_quantity','quantity','asset_quantity'})
            unit_f = _find_field(document, {'unit_price_kzt','equipment_unit_price_kzt'})
            total_f = _find_field(document, {'equipment_total_kzt','lease_asset_value_kzt','purchase_total_kzt','total_amount_kzt'})
            year_f = _find_field(document, {'manufacture_year','equipment_year','vehicle_year'})
            engine_f = _find_field(document, {'engine_model'})
            model = _text(model_f.get('value') if model_f else '')
            kind = _text(type_f.get('value') if type_f else '')
            vin = _text(vin_f.get('value') if vin_f else '')
            full = _document_full_text(document)
            explicit_spec = any(token in full.casefold() for token in ('спецификация','цена за единицу','количество, шт','количество, штук','предмет финансирования','барлығы/итого'))
            if explicit_spec and (model or kind or vin):
                context = full[:20000]
                eng = _extract_engine_model(context) or _text(engine_f.get('value') if engine_f else '')
                parsed_model, parsed_brand = _extract_equipment_model(context, eng)
                if parsed_model and (not model or (eng and _normalise_equipment_token(model) == _normalise_equipment_token(eng))):
                    model = parsed_model
                kind = _infer_equipment_type(context, kind) or kind
                parsed_brand = parsed_brand or _infer_brand_from_context(context, kind)
                row = {
                    'equipment_type': kind or None,
                    'equipment_name': f"{kind or 'Техника'} {_brand_model_display(parsed_brand, model)}".strip() if model else kind or None,
                    'brand': parsed_brand,
                    'manufacturer': parsed_brand,
                    'model': model or None,
                    'manufacture_year': year_f.get('value') if year_f else None,
                    'vin': vin or None,
                    'engine_model': eng or None,
                    'quantity': qty_f.get('value') if qty_f else 1,
                    'unit_price_kzt': unit_f.get('value') if unit_f else None,
                    'total_amount_kzt': total_f.get('value') if total_f else None,
                    'page': (model_f or type_f or vin_f or {}).get('page') if (model_f or type_f or vin_f) else None,
                    'source_method': 'final_field_to_asset_sync',
                    'status': 'corrected',
                }
                document.setdefault('tables', []).append(_asset_table([row], 'Структурированная строка восстановлена из подтверждённых полей документа.'))
                _set_field_from_asset(document, 'equipment_model', 'Марка / модель техники', _brand_model_display(parsed_brand, model) or model)
                _set_field_from_asset(document, 'equipment_type', 'Вид техники', kind)
                _set_field_from_asset(document, 'engine_model', 'Модель двигателя', eng)


def require_structured_equipment_consistency(result: dict) -> None:
    """A document cannot be auto-ready when equipment fields and asset rows disagree."""
    for document in result.get('documents', []):
        fields = document.get('fields', [])
        model = _find_field(document, {'equipment_model','vehicle_model','model'})
        kind = _find_field(document, {'equipment_type','vehicle_type','asset_type'})
        equipment_field_signal = bool(model or kind)
        rows = _equipment_rows(document)
        if equipment_field_signal and not rows:
            _warn(document, 'Техника', 'Техника найдена в основных полях, но структурированная строка не сформирована.', 'high')
            continue
        for row in rows:
            row_model = _text(row.get('model'))
            row_engine = _text(row.get('engine_model'))
            if row_model and row_engine and _normalise_equipment_token(row_model) == _normalise_equipment_token(row_engine):
                row['status'] = 'Требует проверки'
                _warn(document, 'Модель техники', 'Модель техники совпала с моделью двигателя; требуется проверка классификации.', 'high')
            if _text(row.get('equipment_type')).casefold() in {'техника','транспорт','оборудование'}:
                _warn(document, 'Вид техники', 'Вид техники определён слишком общо.', 'high')



def _find_specification_region(full: str) -> str | None:
    """Return the most likely real specification/table appendix region.

    Legal text often mentions the word 'specification' long before the actual
    appendix. Prefer an exact standalone СПЕЦИФИКАЦИЯ heading; otherwise fall
    back to a strong vehicle-table header (used by some purchase agreements).
    """
    exact = list(re.finditer(r"(?im)^\s*СПЕЦИФИКАЦИЯ\s*$", full))
    if exact:
        return full[exact[-1].start():]
    table_header = re.search(
        r"(?is)(?:^|\n)\s*(?:№\s*)?(?:Модель\s*/?\s*Комплектация|Модель.*?)"
        r".{0,2600}?(?:Номер\s+кузова|VIN).{0,2600}?(?:Цена\s+(?:автомобиля|за\s+единицу)|Стоимость)",
        full,
    )
    if table_header:
        return full[table_header.start():]
    # Last-resort: use the last occurrence rather than the first legal reference.
    mentions = list(re.finditer(r"СПЕЦИФИКАЦ", full, re.I))
    return full[mentions[-1].start():] if mentions else None


# v29: multi-page / multi-item specification recovery.
def recover_multiitem_specification_assets(result: dict) -> None:
    """Recover repeated VIN-based specification rows across continuation pages.

    Many BCC leasing applications print the specification header only on the first
    page and continue rows on following pages. Earlier extractors could stop at the
    page break. This pass reconstructs one row per VIN and preserves aggregate
    quantity/value evidence without inventing missing identities.
    """
    vin_re = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", re.I)
    money_re = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?)(?!\d)")
    for document in result.get('documents', []):
        pages = [p for p in document.get('page_texts', []) if isinstance(p, dict)]
        if not pages:
            continue
        full = '\n'.join(str(p.get('text') or '') for p in pages)
        spec = _find_specification_region(full)
        if not spec:
            continue
        # Rejoin VINs split by narrow PDF table columns, e.g.
        # MX1F441CBTK0\n08457 -> MX1F441CBTK008457. Join only when the
        # two alphanumeric chunks total exactly 17 characters.
        def _join_split_vin(m):
            a, b = m.group(1), m.group(2)
            joined = a + b
            return joined if len(joined) == 17 and re.search(r'\d', joined) else m.group(0)
        spec = re.sub(r'(?m)([A-HJ-NPR-Z0-9]{10,16})\s*\n\s*([A-HJ-NPR-Z0-9]{1,7})(?=\s*(?:\n|$))', _join_split_vin, spec, flags=re.I)
        # Stop at signatures / next major appendix where possible.
        cut = re.search(r"(?:ОТ\s+ЛИЗИНГОДАТЕЛЯ|ЛИЗИНГ\s+БЕРУШІДЕН|ГРАФИК\s+ПОГАШЕНИЯ|ПРИЛОЖЕНИЕ\s*№\s*2)", spec, re.I)
        if cut:
            spec = spec[:cut.start()]
        vins = []
        for m in vin_re.finditer(spec):
            vin=m.group(0).upper()
            if vin not in vins:
                vins.append(vin)
        if len(vins) < 2:
            continue

        # Aggregate evidence from the specification.
        qty = None
        total = None
        unit = None
        # Quantity + total is reliable only when quantity is a separate table cell/line.
        m = re.search(r"(?:БАРЛЫҒЫ\s*/?\s*ИТОГО|ИТОГО)\s*:?\s*\n\s*(\d{1,4})\s*\n\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?)", spec, re.I)
        if m:
            qty=int(m.group(1)); total=float(str(m.group(2)).replace('\u00a0',' ').replace(' ','').replace(',','.'))
        else:
            # Some purchase specifications print only the monetary total after ИТОГО.
            tm = re.search(r"(?:БАРЛЫҒЫ\s*/?\s*ИТОГО|ИТОГО)\s*:?\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?)", spec, re.I)
            if tm:
                total=float(str(tm.group(1)).replace('\u00a0',' ').replace(' ','').replace(',','.'))
                qty=len(vins)
        # First explicit unit price after the table heading is generally reliable.
        amounts=[]
        for mm in money_re.finditer(spec[:5000]):
            try:
                amounts.append(float(mm.group(1).replace('\u00a0',' ').replace(' ','').replace(',','.')))
            except ValueError:
                pass
        if qty and total and qty > 0:
            unit = total / qty
        elif len(amounts) >= 2:
            unit=min(amounts); total=max(amounts)

        rows=[]
        for vin in vins:
            pos=spec.upper().find(vin)
            before=spec[max(0,pos-420):pos]
            after=spec[pos:pos+240]
            # Model/name: prefer a short line containing letters immediately before year/description.
            lines=[re.sub(r'\s+',' ',x).strip() for x in before.splitlines() if x.strip()]
            model=None
            # Vehicle-table layout: item no / brand / model-name / trim / year / ... / VIN.
            # Prefer brand + model-name over trim-only values such as 'Luxe+'.
            tail_lines=lines[-12:]
            for yi in range(len(tail_lines)-1, -1, -1):
                if re.fullmatch(r'20\d{2}', tail_lines[yi]):
                    prev=tail_lines[max(0,yi-4):yi]
                    words=[x for x in prev if re.search(r'[A-Za-zА-Яа-я]',x) and not re.fullmatch(r'\d+',x)]
                    if len(words)>=2:
                        # Drop trim-looking last token when three textual tokens precede year.
                        cand=words[-3:-1] if len(words)>=3 else words[-2:]
                        model=' '.join(cand).strip()
                    break
            for line in reversed(lines):
                if model:
                    break
                if re.search(r'^(?:VIN|КОМПЛЕКТАЦ|ТИП ТРАНСМИССИИ|\d{4}\s*Г)', line, re.I):
                    continue
                if re.fullmatch(r'\d{1,3}', line):
                    continue
                if re.search(r'[A-Za-zА-Яа-я]', line) and len(line) <= 80:
                    if not any(t in line.upper() for t in ('DOC ID','ЭЛЕКТРОННЫЙ ДОКУМЕНТ','СПЕЦИФИКАЦ','НАИМЕНОВАН')):
                        model=line; break
            # More specific common pattern: row number + model + year.
            mm=re.search(r'(?:^|\n)\s*\d{1,3}\s*\n\s*([^\n]{2,80})\s*\n\s*(20\d{2})\s*г', before[-700:], re.I)
            year=None
            if mm:
                model=re.sub(r'\s+',' ',mm.group(1)).strip(); year=mm.group(2)
            if year is None:
                ym=re.search(r'(20\d{2})\s*г', before[-250:], re.I); year=ym.group(1) if ym else None
            color=None
            am=re.search(r'\n\s*([^\n]{2,60}(?:Черн|Бел|Сер|Син|Крас|Black|White|Grey|Gray|Blue|Red)[^\n]*)', after, re.I)
            if am:
                color=re.sub(r'\s+',' ',am.group(1)).strip(' ,.;:-')
            row={
                'equipment_type':'Автомобиль' if model and re.search(r'KIA|HYUNDAI|TOYOTA|GAC|GEELY|CHERY|HAVAL|CERATO', model, re.I) else None,
                'equipment_name':model, 'model':model, 'manufacture_year':year,
                'vin':vin, 'color':color, 'quantity':1,
                'unit_price_kzt':unit, 'total_amount_kzt':unit,
                'source_method':'v29_multi_page_specification', 'status':'corrected',
            }
            rows.append(row)
        if len(rows) != len(vins):
            continue
        # Replace weaker VIN table only when this pass demonstrably finds more unique assets.
        existing=[]
        for table in document.get('tables', []):
            if str(table.get('name') or '') == 'asset_vin_rows':
                existing=table.get('rows', []) or []
                break
        existing_ids={_text(r.get('vin')).upper() for r in existing if isinstance(r,dict) and _text(r.get('vin'))}
        if len(vins) <= len(existing_ids):
            continue
        document['tables']=[t for t in document.get('tables', []) if str(t.get('name') or '') != 'asset_vin_rows']
        table=_asset_table(rows, 'v29: многостраничная спецификация восстановлена по полному списку VIN.')
        table['summary']={'total_quantity': qty or len(rows), 'unique_vin_count':len(vins), 'total_identified_amount_kzt':total or (unit*len(rows) if unit else None)}
        document.setdefault('tables', []).append(table)
        _set_field_from_asset(document, 'equipment_quantity', 'Количество единиц техники', qty or len(rows))
        if unit is not None:
            _set_field_from_asset(document, 'equipment_unit_price_kzt', 'Цена за единицу техники', unit)
        if total is not None:
            _set_field_from_asset(document, 'equipment_total_kzt', 'Общая стоимость техники', total)


# v30: generic numbered specification recovery for multi-item rows without VINs.
def recover_numbered_specification_assets(result: dict) -> None:
    """Recover numbered multi-item specification rows even when VIN is absent.

    PDF text extraction frequently separates table columns into independent lines.
    Instead of requiring a visually intact row, this pass identifies item-number
    blocks, finds an arithmetically valid quantity/unit-price/row-total triplet
    inside each block, and reconstructs the commercial item identity from the
    leading description. It supports page continuations and fragmented model words.
    """
    money_line_re = re.compile(r"^\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?)\s*$")
    int_line_re = re.compile(r"^\s*(\d{1,4})\s*$")

    def money(v: str | None) -> float | None:
        if not v:
            return None
        try:
            return float(v.replace('\u00a0', ' ').replace(' ', '').replace(',', '.'))
        except Exception:
            return None

    def strip_noise(lines: list[str]) -> list[str]:
        out=[]
        for ln in lines:
            if re.search(r"DOC\s+ID|Электронный документ подписан|Количество страниц", ln, re.I):
                continue
            # Bare page number inserted by PDF extraction.
            if re.fullmatch(r"\s*\d{1,3}\s*", ln) and out and re.search(r"подписан", out[-1], re.I):
                continue
            out.append(ln.rstrip())
        return out

    def next_nonempty(lines: list[str], i: int) -> str:
        for x in lines[i+1:i+8]:
            if x.strip():
                return x.strip()
        return ''

    def item_starts(lines: list[str]) -> list[int]:
        candidates=[]
        for i, ln in enumerate(lines):
            m=int_line_re.match(ln)
            if not m:
                continue
            n=int(m.group(1))
            if n < 1 or n > 500:
                continue
            nxt=next_nonempty(lines, i)
            # A row number is followed by descriptive text; a quantity cell is
            # normally followed by another numeric/money cell.
            if not re.search(r"[A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺһ]{3,}", nxt):
                continue
            candidates.append((i,n))
        # Choose the longest increasing 1,2,3... sequence. This avoids years,
        # page numbers, and quantities that happen to be on their own lines.
        best=[]
        for k,(i,n) in enumerate(candidates):
            if n != 1:
                continue
            seq=[i]; expected=2
            for j,m in candidates[k+1:]:
                if m == expected:
                    seq.append(j); expected += 1
                elif m == 1 and len(seq) < 2:
                    break
            if len(seq) > len(best): best=seq
        return best

    def commercial_triplet(block_lines: list[str]) -> tuple[int,float,float] | None:
        # Search adjacent significant lines for either:
        #   unit price / qty / row total  (leasing table)
        # or qty / unit price / row total (purchase table).
        sig=[re.sub(r"\s+", " ", x).strip() for x in block_lines if x.strip()]
        for i in range(max(0,len(sig)-20), len(sig)-2):
            a,b,c=sig[i:i+3]
            ma,mb,mc=money_line_re.match(a),money_line_re.match(b),money_line_re.match(c)
            ia,ib=int_line_re.match(a),int_line_re.match(b)
            if ma and ib and mc:
                q=int(ib.group(1)); u=money(ma.group(1)); t=money(mc.group(1))
            elif ia and mb and mc:
                q=int(ia.group(1)); u=money(mb.group(1)); t=money(mc.group(1))
            else:
                continue
            if q <= 0 or q > 10000 or not u or not t:
                continue
            if abs(q*u-t) <= max(1.0, abs(t)*0.001):
                return q,u,t
        return None

    def repair_fragmentation(text: str) -> str:
        text=re.sub(r"\s+", " ", text).strip()
        # Common narrow-column Cyrillic splits for equipment types.
        text=re.sub(r"\bСамосв\s+ал\b", "Самосвал", text, flags=re.I)
        text=re.sub(r"\bСамосв\s+альный\b", "Самосвальный", text, flags=re.I)
        # Join uppercase manufacturer fragments immediately before 'модель'.
        text=re.sub(r"\b([A-ZА-Я]{3,})\s+([A-ZА-Я]{1,3})\b(?=\s+модел)", r"\1\2", text)
        return text

    def extract_identity(block_lines: list[str]) -> tuple[str|None,str|None,str|None,str|None]:
        # Only use the leading commercial description, before detailed technical prose.
        lead=[]
        for ln in block_lines[:22]:
            x=ln.strip()
            if not x: continue
            if money_line_re.match(x): continue
            if int_line_re.match(x) and lead: continue
            if re.search(r"(?:Колесная формула|Собственная масса|Внешние размеры|Габариты|Двигатель|Номинальная мощность|Размер грузового|Материал кузова|Объем кузова|Кабина:)", x, re.I):
                break
            lead.append(x)
        flat=repair_fragmentation(' '.join(lead))
        year_m=re.search(r"\b(20\d{2})\s*(?:г\.?\s*в\.?|г\.?|ж\.?)", flat, re.I)
        year=year_m.group(1) if year_m else None
        # Extract model phrase after 'модель' up to year; join model fragments only
        # when each chunk contains digits (SX32488 L344C -> SX32488L344C).
        model=None
        mm=re.search(r"модел[ьи]?\s+(.{1,60}?)(?=\s+20\d{2}\b|$)", flat, re.I)
        if mm:
            raw=mm.group(1).strip(' ,.;:-')
            parts=raw.split()
            if 1 < len(parts) <= 4 and all(re.search(r"\d", p) for p in parts): raw=''.join(parts)
            model=raw[:60]
        brand=None
        bm=re.search(r"(?:Самосвал|прицеп|погрузчик|экскаватор|автомобиль|тягач)?\s*([A-ZА-Я][A-ZА-Я0-9-]{2,})\s+модел", flat, re.I)
        if bm: brand=bm.group(1).upper()
        kind=None
        for pat,label in [
            (r"самосвальн\w*\s+прицеп|\bприцеп\b","Самосвальный прицеп"),
            (r"\bсамосвал\b","Самосвал"),
            (r"экскаватор[-\s]?погрузчик","Экскаватор-погрузчик"),
            (r"фронтальн\w*\s+погрузчик","Фронтальный погрузчик"),
            (r"автокран","Автокран"),(r"бульдозер","Бульдозер"),
            (r"экскаватор","Экскаватор"),(r"автомобил|легков","Автомобиль"),
            (r"тягач","Тягач")]:
            if re.search(pat,flat,re.I): kind=label; break
        # Name = short phrase through model/year, never technical prose.
        name=flat
        if year_m: name=flat[:year_m.end()]
        elif len(name)>180: name=name[:180]
        name=name.strip(' ,.;:-') or None
        return name,model,year,brand if brand else None,kind

    for document in result.get('documents', []):
        pages=[p for p in document.get('page_texts',[]) if isinstance(p,dict)]
        if not pages: continue
        full='\n'.join(str(p.get('text') or '') for p in pages)
        spec=_find_specification_region(full)
        if not spec: continue
        cut=re.search(r"(?:ОТ\s+ЛИЗИНГОДАТЕЛЯ|ОТ\s+ЛИЗИНГОПОЛУЧАТЕЛЯ|ЛИЗИНГ\s+АЛУШЫ\s+АТЫНАН|САТУШЫНЫҢ\s+атынан|ГРАФИК\s+ПОГАШЕНИЯ|ПРИЛОЖЕНИЕ\s*№\s*2)",spec,re.I)
        if cut: spec=spec[:cut.start()]
        lines=strip_noise(spec.splitlines())
        starts=item_starts(lines)
        if len(starts)<2: continue
        rows=[]
        for n,idx in enumerate(starts):
            end=starts[n+1] if n+1<len(starts) else len(lines)
            block=lines[idx+1:end]
            trip=commercial_triplet(block)
            if not trip: continue
            qty,unit,total=trip
            name,model,year,brand,kind=extract_identity(block)
            if not name: continue
            display=name
            rows.append({'equipment_type':kind,'equipment_name':display,'brand':brand,'model':model,
                         'manufacture_year':year,'vin':None,'quantity':qty,'unit_price_kzt':unit,
                         'total_amount_kzt':total,'source_method':'v30_numbered_specification','status':'corrected'})
        if len(rows)<2: continue
        recovered_sum=sum(float(r['total_amount_kzt']) for r in rows)
        recovered_qty=sum(int(r['quantity']) for r in rows)
        # Explicit appendix total if available.
        mtotal=re.search(r"(?:БАРЛЫҒЫ\s*/?\s*ИТОГО|Барлығы\s*/\s*Итого|ИТОГО)\s*:?\s*\n?\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?)",spec,re.I)
        explicit=money(mtotal.group(1)) if mtotal else None
        if explicit is not None and abs(recovered_sum-explicit)>max(2.0,abs(explicit)*0.001):
            continue
        existing=[r for r in _equipment_rows(document) if isinstance(r,dict) and _row_meaningful(r)]
        existing_vins={_text(r.get('vin')).upper() for r in existing if _text(r.get('vin'))}
        # Never override a fully enumerated VIN table such as AQ5/KIA 10-car lists.
        if existing_vins and len(existing_vins)>=recovered_qty: continue
        # Replace only if we have at least as many commercial positions as existing
        # meaningful non-VIN rows, with an explicit arithmetic reconciliation.
        if existing and not existing_vins and len(existing)>len(rows): continue
        document['tables']=[t for t in document.get('tables',[]) if str(t.get('name') or '')!='asset_vin_rows']
        table=_asset_table(rows,'v30: спецификация восстановлена по нумерованным коммерческим позициям, включая переносы страниц.')
        table['summary']={'total_quantity':recovered_qty,'unique_vin_count':0,
                          'total_identified_amount_kzt':explicit if explicit is not None else recovered_sum,
                          'item_positions':len(rows)}
        document.setdefault('tables',[]).append(table)
        _set_field_from_asset(document,'equipment_quantity','Количество единиц техники',recovered_qty)
        if len({round(float(r['unit_price_kzt']),2) for r in rows})==1:
            _set_field_from_asset(document,'equipment_unit_price_kzt','Цена за единицу техники',rows[0]['unit_price_kzt'])
        _set_field_from_asset(document,'equipment_total_kzt','Общая стоимость техники',explicit if explicit is not None else recovered_sum)

def finalize_continuation_specification_assets(result: dict) -> None:
    """Last-mile recovery for continuation-page commercial specification rows.

    Some downstream reconciliation stages can retain a stronger-looking but
    incomplete one-row equipment table.  Re-run the arithmetic numbered-row
    recovery immediately before dossier aggregation/export so a verified
    multi-position specification (for example SHACMAN + CHITIAN across pages)
    wins over an incomplete table.  The recovery itself already refuses to
    override a fully enumerated VIN table.
    """
    recover_multiitem_specification_assets(result)
    recover_numbered_specification_assets(result)
    harden_equipment_tables(result)
    normalize_equipment_semantics(result)
    clean_asset_rows_and_recompute(result)
    normalize_equipment_semantics(result)
    require_identifiable_equipment(result)
    require_structured_equipment_consistency(result)


def apply_production_hardening(result: dict, version: str = "35.0") -> None:
    harden_contract_numbers(result)
    remove_signature_party_noise(result)
    harden_client_identity(result)
    recover_client_name_from_evidence(result)
    improve_client_legal_name(result)
    harden_client_identity(result)
    repair_explicit_itogo_amounts(result)
    recover_single_specification_equipment(result)
    recover_multiitem_specification_assets(result)
    recover_numbered_specification_assets(result)
    harden_equipment_tables(result)
    normalize_equipment_semantics(result)
    clean_asset_rows_and_recompute(result)
    normalize_equipment_semantics(result)
    harden_vins(result)
    suppress_redundant_party_warnings(result)
    require_identifiable_equipment(result)
    require_structured_equipment_consistency(result)
    analysis = result.setdefault("analysis", {})
    analysis["quality_pipeline_version"] = version
    analysis["quality_checked_at_utc"] = datetime.now(timezone.utc).isoformat()
    analysis["quality_policy"] = "Сомнительные ключевые поля блокируются и требуют ручного подтверждения."
