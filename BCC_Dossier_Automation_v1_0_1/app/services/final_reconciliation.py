from __future__ import annotations

import re
from typing import Any

from .quality import REQUIRED_BY_TYPE

CLIENT_OBSOLETE_PHRASES = (
    "иин/бин клиента не найден",
    "иин/бин клиента не определ",
    "наименование или иин/бин клиента не подтверждены",
    "наименование клиента не подтверждено",
    "имя клиента не определено",
)
EQUIPMENT_WARNING_PHRASE = "вид или модель предмета лизинга не определены"
ASSET_EXPECTED_TYPES = {"lease_contract", "purchase_contract", "acceptance_act"}
CRITICAL_FIELD_TOKENS = (
    "vin", "serial_number", "chassis", "contract_number", "agreement_number",
    "total_amount", "asset_value", "purchase_total", "financing_amount",
    "iin_bin", "client_name", "lessee_name", "borrower_name",
    "equipment_model", "equipment_quantity",
)
NON_BLOCKING_CANDIDATE_NAMES = {
    "other_money_amounts", "other_dates", "other_numbers", "unresolved_identifiers",
    "unresolved_iin_bins", "asset_identifier_groups",
}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _norm_company(value: Any) -> str:
    text = _text(value).casefold().replace("ё", "е")
    text = re.sub(r"[«»\"'`]+", " ", text)
    text = re.sub(
        r"\b(?:тоо|ип|ао|тд|llp|ltd|inc|общество с ограниченной ответственностью|"
        r"индивидуальный предприниматель)\b", " ", text, flags=re.I,
    )
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _client_valid(result: dict) -> tuple[bool, str, str]:
    client = result.get("client") or {}
    name = _text(client.get("name"))
    identifier = _digits(client.get("iin_bin"))
    blocked = not name or _norm_company(name) in {"bcc", "bcc leasing", "лизингополучатель", "клиент"}
    return bool(not blocked and len(identifier) == 12), name, identifier


def _document_text(document: dict) -> str:
    return "\n".join(
        str(page.get("text") or "") if isinstance(page, dict) else str(page or "")
        for page in document.get("page_texts", [])
    )


def _is_lease_application_text(document: dict) -> bool:
    text = _document_text(document).casefold().replace("ё", "е")[:16000]
    has_application = bool(re.search(
        r"заявлени[ея].{0,120}(?:о\s+присоединении|к\s+договору\s+присоединения)",
        text, re.I | re.S,
    ))
    has_lease = any(token in text for token in (
        "лизингополучатель", "лизингодатель", "финансового лизинга",
        "договор лизинга", "предмет лизинга",
    ))
    return has_application and has_lease


def _signature_iin_bins(document: dict) -> set[str]:
    ids = {str(value) for value in (document.get("signature_iin_bins") or []) if re.fullmatch(r"\d{12}", str(value))}
    text = _document_text(document)
    if not text:
        return ids
    # Only inspect local windows around explicit electronic-signature markers.
    for marker in re.finditer(
        r"(?:электронн(?:ая|ой|ый)\s+(?:цифровая\s+)?подпис|эцп|подписан\s+в\s+documentolog|сертификат\s+(?:эцп|подпис))",
        text, re.I,
    ):
        window = text[max(0, marker.start() - 450): marker.end() + 700]
        ids.update(re.findall(r"(?<!\d)\d{12}(?!\d)", window))
    return ids


def _field_values(document: dict, names: set[str]) -> list[str]:
    out = []
    for item in document.get("fields", []):
        if str(item.get("name") or "") not in names:
            continue
        value = item.get("value")
        if isinstance(value, (list, tuple, set)):
            out.extend(_text(v) for v in value if _text(v))
        elif _text(value):
            out.append(_text(value))
    return out


def _party_warning_resolved(document: dict, field: str, message: str, client_name: str) -> bool:
    blob = f"{field} {message}".casefold()
    if not any(t in blob for t in ("лизингополуч", "заемщик", "заёмщик", "получатель", "клиент")):
        return False
    target = _norm_company(client_name)
    if not target:
        return False
    values = _field_values(document, {"lessee_name", "borrower_name", "principal_name", "recipient_name", "client_name"})
    values += re.findall(r"(?::|—)\s*([^.;]+)", message)
    for value in values:
        norm = _norm_company(value)
        if norm and (norm == target or (len(norm) >= 4 and (norm in target or target in norm))):
            return True
    # Role-only placeholder is harmless once the client itself is confirmed.
    if any(token in blob for token in ("значение похоже на определение роли", "не пройдена проверка наименования")):
        return True
    return False




def _guarantor_warning_resolved(document: dict, field: str, message: str) -> bool:
    blob = f"{field} {message}".casefold()
    if not any(t in blob for t in ("гарант", "поручител")):
        return False
    if "значение требует ручного подтверждения" not in blob and "не подтверж" not in blob:
        return False
    # An explicit 12-digit identifier classified by context as guarantor/
    # surety is stronger evidence than a generic upstream candidate warning.
    for table in document.get("tables", []):
        for row in table.get("rows", []):
            if not isinstance(row, dict):
                continue
            role = _text(row.get("Роль по контексту") or row.get("role") or row.get("context_role")).casefold()
            value = _digits(row.get("Значение") or row.get("value") or row.get("identifier"))
            if len(value) == 12 and any(t in role for t in ("гарант", "поручител")):
                return True
    for item in document.get("fields", []):
        name = str(item.get("name") or "").casefold()
        value = _digits(item.get("value"))
        quote = _text(item.get("quote")).casefold()
        if len(value) == 12 and any(t in name for t in ("guarant", "surety", "guarantee")) and any(t in quote for t in ("гарант", "поручител", "договор гарант")):
            return True
    text = _document_text(document)
    if re.search(r"(?:договор\w*\s+гарант|личн\w*\s+гарант|поручител)[\s\S]{0,260}?(?:ИИН|ЖСН)\s*\d{12}", text, re.I):
        return True
    return False

def _strong_asset_context(document: dict) -> bool:
    doc_type = str(document.get("document_type") or "")
    full_text = "\n".join(
        str(p.get("text") or "") for p in document.get("page_texts", []) if isinstance(p, dict)
    ).casefold()
    explicit = any(t in full_text for t in (
        "спецификация", "цена за единицу", "количество, штук", "количество шт",
        "техническая характеристика", "предмет финансирования", "vin",
    ))
    if explicit:
        return True
    if doc_type not in ASSET_EXPECTED_TYPES:
        return False
    # A document type alone is not enough; require at least one asset-oriented extracted field/table.
    for item in document.get("fields", []):
        name = str(item.get("name") or "").casefold()
        if any(t in name for t in ("equipment", "vehicle", "asset_", "vin", "serial_number")):
            return True
    for table in document.get("tables", []):
        name = str(table.get("name") or "").casefold()
        if any(t in name for t in ("equipment", "vehicle", "asset", "vin", "техник")) and table.get("rows"):
            return True
    return False


def _required_labels(document_type: str) -> set[str]:
    return {label.casefold() for _, label in REQUIRED_BY_TYPE.get(document_type, [])}


def _canonical_warning(warning: Any) -> tuple[str, str, str]:
    if isinstance(warning, dict):
        severity = _text(warning.get("severity") or "medium").casefold()
        field = _text(warning.get("field") or warning.get("label_ru"))
        message = _text(warning.get("message_ru") or warning.get("message") or warning.get("warning"))
    else:
        severity, field, message = "medium", "", _text(warning)
    return severity, field, message


def _is_obsolete_warning(result: dict, document: dict, warning: Any) -> bool:
    severity, field, message = _canonical_warning(warning)
    lower = f"{field} {message}".casefold()
    client_ok, client_name, client_id = _client_valid(result)

    if client_ok and any(p in lower for p in CLIENT_OBSOLETE_PHRASES):
        return True
    if client_ok and _party_warning_resolved(document, field, message, client_name):
        return True
    if _guarantor_warning_resolved(document, field, message):
        return True

    signature_ids = _signature_iin_bins(document)
    mentioned = set(re.findall(r"(?<!\d)\d{12}(?!\d)", message))
    if signature_ids and ("неопредел" in lower or "unknown" in lower):
        if mentioned and mentioned <= signature_ids:
            return True

    # A generic unresolved-identifier candidate with no concrete value is audit
    # noise rather than a business risk. Concrete extra identifiers remain as
    # non-blocking review notes unless they are confirmed contract parties.
    if "неопредел" in lower and any(token in lower for token in ("иин/бин", "бин/иин", "iin/bin")):
        if not mentioned:
            return True
        if client_id and mentioned <= {client_id}:
            return True

    # Leasing applications repeatedly reference a future acceptance act. Such
    # references must not make an act number mandatory even if an upstream
    # classifier was confused by the repeated phrase.
    if "номер акта" in lower and _is_lease_application_text(document):
        return True

    if EQUIPMENT_WARNING_PHRASE in lower and not _strong_asset_context(document):
        return True

    if "ключевое поле не извлечено" in lower:
        required = _required_labels(str(document.get("document_type") or ""))
        if field.casefold() not in required:
            return True

    return False


def _dedupe_warnings(warnings: list[Any]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for warning in warnings:
        severity, field, message = _canonical_warning(warning)
        if not message:
            continue
        # Candidate generic warning + a concrete warning for the same field: keep concrete only.
        key = (field.casefold(), re.sub(r"\s+", " ", message.casefold()))
        if key in seen:
            continue
        concrete_digits = re.findall(r"\b\d{6,}\b", message)
        if "значение требует ручного подтверждения" in message.casefold() and any(
            r.get("field", "").casefold() == field.casefold()
            and re.findall(r"\b\d{6,}\b", r.get("message", ""))
            for r in result
        ):
            continue
        if concrete_digits:
            result[:] = [r for r in result if not (
                r.get("field", "").casefold() == field.casefold()
                and "значение требует ручного подтверждения" in r.get("message", "").casefold()
            )]
        seen.add(key)
        result.append({"severity": severity or "medium", "field": field, "message": message, "message_ru": message})
    return result


def _critical_candidate(field: dict) -> bool:
    name = str(field.get("name") or "")
    if name in NON_BLOCKING_CANDIDATE_NAMES:
        return False
    return any(token in name for token in CRITICAL_FIELD_TOKENS)


def _normalise_warning_severity(document: dict, warning: dict) -> dict:
    field = _text(warning.get("field")).casefold()
    message = _text(warning.get("message")).casefold()
    if any(token in field for token in ("неопределённые иин/бин", "неопределенные иин/бин", "неопределённый иин/бин")):
        # An additional unidentified identifier is useful for audit, but it does
        # not invalidate the confirmed client/contract by itself.
        warning["severity"] = "medium"
    if "низкая уверенность ocr" in message:
        warning["severity"] = "medium"
    return warning


def reconcile_final_warnings(result: dict, version: str = "24.0") -> None:
    """Reconcile warnings *after* all recovery/correction passes.

    This does not hide unresolved risk. It removes only warnings disproved by the
    final data, deduplicates noise, and separates blocking findings from
    informational review notes.
    """
    client_ok, _, _ = _client_valid(result)
    total_blocking = 0
    total_notes = 0

    for document in result.get("documents", []):
        filtered = [w for w in document.get("warnings", []) if not _is_obsolete_warning(result, document, w)]
        warnings = [_normalise_warning_severity(document, w) for w in _dedupe_warnings(filtered)]

        # Promote unresolved business-critical candidates to one explicit warning.
        existing_fields = {w["field"].casefold() for w in warnings}
        for field in document.get("fields", []):
            if field.get("status") != "candidate" or not _critical_candidate(field):
                continue
            label = _text(field.get("label_ru") or field.get("name") or "Ключевое поле")
            if label.casefold() not in existing_fields:
                warnings.append({
                    "severity": "high", "field": label,
                    "message": "Значение требует ручного подтверждения.",
                    "message_ru": "Значение требует ручного подтверждения.",
                })
                existing_fields.add(label.casefold())

        # Table candidates are blocking only when they represent a concrete asset/VIN row.
        # Do not duplicate an already explicit VIN-conflict warning.
        vin_conflict_ids: set[str] = set()
        for warning in warnings:
            if warning.get("field", "").casefold() == "vin" and "конфликт" in warning.get("message", "").casefold():
                vin_conflict_ids.update(re.findall(r"[A-HJ-NPR-Z0-9]{17}", warning.get("message", "").upper()))
        for table in document.get("tables", []):
            for row in table.get("rows", []):
                if not isinstance(row, dict) or row.get("status") not in {"candidate", "Требует проверки"}:
                    continue
                value = _text(row.get("vin") or row.get("serial_number") or row.get("model"))
                row_ids = set(re.findall(r"[A-HJ-NPR-Z0-9]{17}", value.upper()))
                if vin_conflict_ids and row_ids and row_ids <= vin_conflict_ids:
                    continue
                if value:
                    msg = f"Строка техники / VIN требует проверки: {value}"
                    if not any(w["message"] == msg for w in warnings):
                        warnings.append({"severity": "high", "field": "Техника / VIN", "message": msg, "message_ru": msg})

        if not client_ok and not any(w["field"].casefold() == "клиент" for w in warnings):
            warnings.insert(0, {
                "severity": "high", "field": "Клиент",
                "message": "Наименование или ИИН/БИН клиента не подтверждены.",
                "message_ru": "Наименование или ИИН/БИН клиента не подтверждены.",
            })

        warnings = [_normalise_warning_severity(document, w) for w in _dedupe_warnings(warnings)]
        document["warnings"] = warnings
        blocking = [w for w in warnings if w.get("severity") in {"high", "critical"}]
        notes = [w for w in warnings if w.get("severity") not in {"high", "critical"}]
        document["blocking_warnings"] = blocking
        document["review_notes"] = notes
        document["readiness"] = "review" if blocking else "ready"
        total_blocking += len(blocking)
        total_notes += len(notes)

    analysis = result.setdefault("analysis", {})
    analysis["quality_pipeline_version"] = version
    analysis["blocking_warning_count"] = total_blocking
    analysis["review_note_count"] = total_notes
    analysis["result_readiness"] = "review" if total_blocking else "ready"
    analysis["warning_reconciliation"] = "final-state, type-aware, deduplicated"
