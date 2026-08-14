from __future__ import annotations

import re
from datetime import datetime
from typing import Any

BCC_LEASING_BIN = "020140001503"
EMPTY = {"", "не определено", "не определён", "не определена", "не применимо", "n/a", "none", "null"}


def _text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _norm(v: Any) -> str:
    return re.sub(r"[^0-9a-zа-яёәіңғүұқөһ]+", "", _text(v).casefold())


def _digits(v: Any) -> str:
    return re.sub(r"\D", "", _text(v))


def _number(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    s = _text(v).replace("\xa0", " ")
    s = re.sub(r"[^0-9,.-]", "", s.replace(" ", ""))
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _field_map(document: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for field in document.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "")
        if name:
            out.setdefault(name, []).append(field)
    return out


def _values(document: dict, *names: str) -> list[Any]:
    fmap = _field_map(document)
    values: list[Any] = []
    for name in names:
        for field in fmap.get(name, []):
            value = field.get("value")
            if value not in (None, "", []):
                values.append(value)
    return values


def _first(document: dict, *names: str) -> Any:
    values = _values(document, *names)
    return values[0] if values else None


def _family(document: dict) -> str:
    return _text((document.get("analysis") or {}).get("asset_family")).casefold()


def _is_registration(document: dict) -> bool:
    # A family guess must never override an explicit leasing/purchase contract
    # classification. This catches contradictions such as the OPL regression,
    # where an equipment document was once accidentally tagged as registration.
    t = (_text(document.get("document_type")) + " " + _text(document.get("document_type_label_ru"))).casefold()
    if any(x in t for x in ("lease", "purchase", "лизинг", "купли", "продаж")):
        return False
    return _family(document) == "registration" or "регистрац" in t


def _is_contract(document: dict) -> bool:
    t = _text(document.get("document_type")).casefold()
    label = _text(document.get("document_type_label_ru")).casefold()
    return any(x in t + " " + label for x in ("lease", "purchase", "лизинг", "купли", "продаж")) and not _is_registration(document)


def _equipment_rows(document: dict) -> list[dict]:
    rows: list[dict] = []
    for table in document.get("tables", []) or []:
        if not isinstance(table, dict) or table.get("name") != "asset_vin_rows":
            continue
        for row in table.get("rows", []) or []:
            if isinstance(row, dict) and str(row.get("status") or "").casefold() not in {"rejected", "candidate"}:
                rows.append(row)
    return rows


def _row_value(row: dict, tokens: tuple[str, ...]) -> Any:
    for key, value in row.items():
        low = str(key).casefold()
        if any(token in low for token in tokens) and value not in (None, ""):
            return value
    return None


def _add(checks: list[dict], status: str, code: str, title: str, detail: str, value: Any = "", action: str = "") -> None:
    severity = {"PASS": 0, "CHECK": 1, "ERROR": 2}[status]
    checks.append({
        "status": status,
        "severity": severity,
        "code": code,
        "title": title,
        "detail": detail,
        "value": _text(value),
        "action": action or ("Проверка не требуется." if status == "PASS" else "Сверить с исходным документом."),
    })


def build_work_review(data: dict) -> dict:
    """Create conservative production-readiness checks for one exported result.

    The validator does not try to guess missing business facts.  It cross-checks
    independently extracted representations and blocks READY when they conflict.
    """
    documents = [d for d in data.get("documents", []) or [] if isinstance(d, dict)]
    checks: list[dict] = []
    current_year = datetime.now().year
    client = data.setdefault("client", {})
    registration_only = bool(documents) and all(_is_registration(d) for d in documents)

    # Safe summary repair: when exactly one explicit lessee name and one 12-digit
    # lessee identifier are present, use them instead of an accidental
    # "Не применимо"/BCC Leasing summary value. This is deterministic evidence
    # reconciliation, not a guess.
    if not registration_only:
        strong_names = []
        strong_ids = []
        for d in documents:
            for value in _values(d, "lessee_name", "client_name"):
                text = _text(value)
                if text and text.casefold() not in EMPTY:
                    strong_names.append(text)
            for value in _values(d, "lessee_iin_bin", "lessee_bin", "client_iin_bin"):
                digits = _digits(value)
                if len(digits) == 12 and digits != BCC_LEASING_BIN:
                    strong_ids.append(digits)
        uniq_names = list(dict.fromkeys(strong_names))
        uniq_ids = list(dict.fromkeys(strong_ids))
        current_name = _text(client.get("name"))
        current_id = _digits(client.get("iin_bin"))
        if len(uniq_names) == 1 and len(uniq_ids) == 1 and (current_name.casefold() in EMPTY or current_id == BCC_LEASING_BIN or len(current_id) != 12):
            client.update({
                "name": uniq_names[0],
                "iin_bin": uniq_ids[0],
                "role": "lessee",
                "role_label_ru": "Лизингополучатель",
                "source": "v34_validation_reconciliation",
                "confidence": max(float(client.get("confidence") or 0), 0.99),
            })

    client_name = _text(client.get("name"))
    client_id = _digits(client.get("iin_bin"))

    # Client / IIN-BIN reconciliation.
    if registration_only:
        _add(checks, "PASS", "client_na_registration", "Клиент / ИИН-БИН", "Для регистрационного уведомления клиент не является обязательным полем.", "Не применимо")
    else:
        lessee_names: list[str] = []
        lessee_ids: list[str] = []
        for d in documents:
            for value in _values(d, "lessee_name", "client_name"):
                if _text(value).casefold() not in EMPTY:
                    lessee_names.append(_text(value))
            for value in _values(d, "lessee_iin_bin", "lessee_bin", "client_iin_bin"):
                digits = _digits(value)
                if len(digits) == 12:
                    lessee_ids.append(digits)
        known_names = list(dict.fromkeys(lessee_names))
        known_ids = list(dict.fromkeys(lessee_ids))
        if client_name.casefold() in EMPTY:
            if known_names:
                _add(checks, "ERROR", "client_summary_missing", "Клиент", "В документах найден лизингополучатель, но в сводке клиент отсутствует/помечен как неприменимый.", "; ".join(known_names), "Проверить клиента; результат нельзя считать готовым до устранения расхождения.")
            else:
                _add(checks, "ERROR", "client_missing", "Клиент", "Имя клиента не определено.")
        elif _digits(client_id) == BCC_LEASING_BIN:
            _add(checks, "ERROR", "bcc_as_client", "Клиент", "В качестве клиента указан БИН BCC Leasing; для лизингового досье это признак перепутанной роли стороны.", client_id)
        else:
            if known_names and not any(_norm(client_name) == _norm(x) or _norm(client_name) in _norm(x) or _norm(x) in _norm(client_name) for x in known_names):
                _add(checks, "ERROR", "client_name_conflict", "Клиент", "Клиент в сводке не совпадает с найденным лизингополучателем.", f"Сводка: {client_name}; документ: {'; '.join(known_names)}")
            else:
                _add(checks, "PASS", "client_name_ok", "Клиент", "Имя клиента согласовано с извлечёнными сторонами договора.", client_name)
        if len(client_id) != 12:
            _add(checks, "ERROR", "client_id_invalid", "ИИН/БИН клиента", "ИИН/БИН клиента должен содержать 12 цифр.", client.get("iin_bin"))
        elif known_ids and client_id not in known_ids:
            _add(checks, "ERROR", "client_id_conflict", "ИИН/БИН клиента", "ИИН/БИН в сводке не совпадает с ИИН/БИН найденного лизингополучателя.", f"Сводка: {client_id}; документ: {'; '.join(known_ids)}")
        else:
            _add(checks, "PASS", "client_id_ok", "ИИН/БИН клиента", "ИИН/БИН имеет корректный формат и не конфликтует с найденной стороной.", client_id)

    for index, document in enumerate(documents, start=1):
        prefix = f"Документ {index}: {_text(document.get('filename'))}"
        if _is_registration(document):
            _add(checks, "PASS", f"doc{index}_family", f"{prefix} — тип", "Документ распознан как регистрационный; договорные/технические проверки не применяются.")
            continue

        # Contract identity.
        contract_number = _first(document, "lease_contract_number", "purchase_contract_number", "contract_number")
        contract_date = _first(document, "lease_contract_date", "purchase_contract_date", "contract_date")
        if _is_contract(document):
            _add(checks, "PASS" if contract_number else "ERROR", f"doc{index}_contract_number", f"{prefix} — номер договора", "Номер договора найден." if contract_number else "Номер договора не найден.", contract_number)
            _add(checks, "PASS" if contract_date else "ERROR", f"doc{index}_contract_date", f"{prefix} — дата договора", "Дата договора найдена." if contract_date else "Дата договора не найдена.", contract_date)

        # Main monetary value: at least one authoritative business amount.
        amount_names = (
            "lease_asset_value_kzt", "leasing_asset_value_kzt", "total_amount_kzt",
            "purchase_total_amount_kzt", "contract_amount_kzt", "financing_amount_kzt",
        )
        amount = _first(document, *amount_names)
        if _is_contract(document):
            _add(checks, "PASS" if _number(amount) not in (None, 0) else "CHECK", f"doc{index}_amount", f"{prefix} — основная сумма", "Основная денежная сумма найдена." if _number(amount) not in (None, 0) else "Не удалось уверенно определить основную сумму договора.", amount)

        family = _family(document)
        if family in {"real_estate", "realestate", "property"}:
            _add(checks, "PASS", f"doc{index}_equipment_na", f"{prefix} — техника", "Документ относится к недвижимости; техническая таблица техники не требуется.")
            continue

        rows = _equipment_rows(document)
        field_qty = _number(_first(document, "equipment_quantity", "asset_quantity", "vehicle_quantity"))
        row_qty = 0.0
        vins: list[str] = []
        bad_years: list[str] = []
        arithmetic_errors: list[str] = []
        for row_no, row in enumerate(rows, start=1):
            qty = _number(_row_value(row, ("quantity", "колич", "qty")))
            row_qty += qty if qty is not None else 1.0
            vin = _text(_row_value(row, ("vin",)))
            if vin:
                vins.append(re.sub(r"\s+", "", vin).upper())
            year = _number(_row_value(row, ("year", "год", "manufacture")))
            if year is not None and not (1950 <= year <= current_year + 2):
                bad_years.append(f"строка {row_no}: {int(year)}")
            unit = _number(_row_value(row, ("unit_price", "unitprice", "цена", "unit cost")))
            total = _number(_row_value(row, ("total_price", "total_amount", "стоимость", "итого", "amount")))
            if qty is not None and unit is not None and total is not None:
                expected = qty * unit
                tolerance = max(1.0, abs(total) * 0.001)
                if abs(expected - total) > tolerance:
                    arithmetic_errors.append(f"строка {row_no}: {qty:g} × {unit:g} ≠ {total:g}")

        # Also validate scalar manufacture_year fields even if table is empty.
        for value in _values(document, "manufacture_year", "equipment_year", "vehicle_year"):
            year = _number(value)
            if year is not None and not (1950 <= year <= current_year + 2):
                bad_years.append(f"поле: {int(year)}")
        if bad_years:
            _add(checks, "ERROR", f"doc{index}_year", f"{prefix} — год выпуска", "Обнаружен неправдоподобный год выпуска.", "; ".join(dict.fromkeys(bad_years)), "Проверить год по спецификации/техпаспорту.")
        elif rows or _values(document, "manufacture_year", "equipment_year", "vehicle_year"):
            _add(checks, "PASS", f"doc{index}_year", f"{prefix} — год выпуска", "Годы выпуска находятся в допустимом диапазоне.")

        if arithmetic_errors:
            _add(checks, "ERROR", f"doc{index}_row_math", f"{prefix} — арифметика техники", "Количество × цена за единицу не совпадает с итогом строки.", "; ".join(arithmetic_errors))
        elif rows:
            _add(checks, "PASS", f"doc{index}_row_math", f"{prefix} — арифметика техники", "Строки техники не содержат выявленных арифметических конфликтов.")

        has_equipment_fields = bool(_values(document, "equipment_type", "equipment_model", "vehicle_model", "asset_name", "equipment_name"))
        if rows:
            if field_qty is not None and abs(row_qty - field_qty) > 0.001:
                _add(checks, "ERROR", f"doc{index}_qty_conflict", f"{prefix} — количество техники", "Количество в полях документа не совпадает с количеством строк/единиц в структурированной таблице.", f"Поле: {field_qty:g}; таблица: {row_qty:g}")
            else:
                _add(checks, "PASS", f"doc{index}_qty", f"{prefix} — количество техники", "Количество согласовано с таблицей техники.", int(row_qty) if row_qty.is_integer() else row_qty)
        elif has_equipment_fields or field_qty:
            _add(checks, "CHECK", f"doc{index}_table_missing", f"{prefix} — таблица техники", "Техника определена в полях документа, но структурированная таблица оборудования отсутствует.", f"Количество: {field_qty or 'не определено'}", "Проверить модель/количество вручную; при корректности можно подтвердить.")

        if vins:
            unique_vins = list(dict.fromkeys(vins))
            invalid = [vin for vin in unique_vins if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin)]
            if invalid:
                _add(checks, "ERROR", f"doc{index}_vin_format", f"{prefix} — VIN", "Есть VIN неправильной длины/структуры.", "; ".join(invalid[:5]))
            elif row_qty and abs(len(unique_vins) - row_qty) > 0.001:
                _add(checks, "CHECK", f"doc{index}_vin_qty", f"{prefix} — VIN / количество", "Количество уникальных VIN не совпадает с количеством техники.", f"VIN: {len(unique_vins)}; количество: {row_qty:g}")
            else:
                _add(checks, "PASS", f"doc{index}_vin", f"{prefix} — VIN", "VIN имеют допустимую структуру и согласованы с количеством.", len(unique_vins))

    # Existing extraction warnings are surfaced without turning every generic
    # high-confidence caveat into a hard block. Only explicit critical warnings
    # are ERROR; other warnings require manual review (CHECK).
    seen_warning_messages: set[str] = set()
    for document in documents:
        sources = []
        if document.get("blocking_warnings") is not None:
            sources.extend(document.get("blocking_warnings") or [])
        else:
            sources.extend(document.get("warnings", []) or [])
        if document.get("review_notes") is not None:
            sources.extend(document.get("review_notes") or [])
        for warning in sources:
            if isinstance(warning, dict):
                message = _text(warning.get("message_ru") or warning.get("message") or warning.get("warning"))
                severity = _text(warning.get("severity") or "high").casefold()
            else:
                message = _text(warning)
                severity = "high"
            if not message or message in seen_warning_messages:
                continue
            seen_warning_messages.add(message)
            status = "ERROR" if severity == "critical" else "CHECK"
            _add(checks, status, f"existing_warning_{len(seen_warning_messages)}", "Предупреждение анализатора", message)

    errors = sum(1 for c in checks if c["status"] == "ERROR")
    review = sum(1 for c in checks if c["status"] == "CHECK")
    passed = sum(1 for c in checks if c["status"] == "PASS")
    if errors:
        overall = "BLOCKED"
        label = "НЕ ГОТОВО — есть критические расхождения"
    elif review:
        overall = "REVIEW_REQUIRED"
        label = "ТРЕБУЕТ РУЧНОЙ ПРОВЕРКИ"
    else:
        overall = "READY"
        label = "ГОТОВО К ИСПОЛЬЗОВАНИЮ"
    return {
        "version": "35.0",
        "overall": overall,
        "label_ru": label,
        "counts": {"pass": passed, "check": review, "error": errors, "total": len(checks)},
        "checks": checks,
    }
