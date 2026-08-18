from __future__ import annotations

import itertools
import re
from copy import deepcopy


OCR_DIGIT_SUBSTITUTIONS = {
    "O": "0",
    "О": "0",
    "Q": "0",
    "D": "0",
    "I": "1",
    "І": "1",
    "L": "1",
    "|": "1",
    "Z": "2",
    "З": "3",
    "S": "5",
    "Б": "6",
    "B": "8",
    "В": "8",
    "G": "6",
}

OCR_ALNUM_SUBSTITUTIONS = {
    **OCR_DIGIT_SUBSTITUTIONS,
    "0": "O",
    "1": "I",
    "2": "Z",
    "5": "S",
    "8": "B",
}

IDENTIFIER_FIELD_SUFFIXES = ("_iin_bin",)
IBAN_FIELD_SUFFIXES = ("_iban",)
VIN_FIELD_NAMES = {
    "vin",
    "vin_candidates",
    "asset_vins",
}
PARTY_NAME_FIELD_NAMES = {
    "lessee_name", "borrower_name", "buyer_name", "seller_name",
    "lessor_name", "guarantor_name", "principal_name", "beneficiary_name",
    "recipient_name", "subsidy_recipient_name", "insurance_company",
    "insurance_holder", "insurance_beneficiary", "gps_provider", "gps_customer",
}


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _compact_upper(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def validate_iin_bin(value: object) -> dict:
    """
    Validate a 12-digit Kazakhstan IIN/BIN checksum.

    The function is intentionally strict: malformed values remain visible but
    are marked invalid so the operator can confirm or correct them.
    """
    number = _digits(value)
    if len(number) != 12:
        return {
            "kind": "iin_bin",
            "valid": False,
            "status": "invalid",
            "message": "ИИН/БИН должен содержать 12 цифр.",
            "normalised": number,
        }

    digits = [int(ch) for ch in number]
    first_weights = list(range(1, 12))
    checksum = sum(d * w for d, w in zip(digits[:11], first_weights)) % 11

    if checksum == 10:
        second_weights = [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]
        checksum = sum(d * w for d, w in zip(digits[:11], second_weights)) % 11

    valid = checksum != 10 and checksum == digits[11]
    return {
        "kind": "iin_bin",
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "message": (
            "Контрольная сумма ИИН/БИН корректна."
            if valid
            else "Контрольная сумма ИИН/БИН не совпадает."
        ),
        "normalised": number,
    }


def _iban_mod97(value: str) -> int | None:
    if not re.fullmatch(r"[A-Z0-9]+", value):
        return None
    rearranged = value[4:] + value[:4]
    numeric = "".join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged)
    remainder = 0
    for chunk_start in range(0, len(numeric), 7):
        chunk = str(remainder) + numeric[chunk_start:chunk_start + 7]
        remainder = int(chunk) % 97
    return remainder


def validate_iban(value: object) -> dict:
    iban = _compact_upper(value)
    if len(iban) != 20:
        return {
            "kind": "iban",
            "valid": False,
            "status": "invalid",
            "message": "Казахстанский IBAN должен содержать 20 символов.",
            "normalised": iban,
        }
    if not iban.startswith("KZ"):
        return {
            "kind": "iban",
            "valid": False,
            "status": "invalid",
            "message": "Ожидается казахстанский IBAN с префиксом KZ.",
            "normalised": iban,
        }
    if not re.fullmatch(r"KZ\d{2}[A-Z0-9]{16}", iban):
        return {
            "kind": "iban",
            "valid": False,
            "status": "invalid",
            "message": "После KZ должны идти две контрольные цифры и 16 символов счёта.",
            "normalised": iban,
        }
    remainder = _iban_mod97(iban)
    valid = remainder == 1
    return {
        "kind": "iban",
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "message": (
            "Контрольная сумма IBAN корректна."
            if valid
            else "Контрольная сумма IBAN не совпадает."
        ),
        "normalised": iban,
    }


def validate_vin(value: object) -> dict:
    vin = _compact_upper(value)
    allowed = bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin))
    has_digit = any(ch.isdigit() for ch in vin)
    valid = allowed and has_digit
    return {
        "kind": "vin",
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "message": (
            "Структура VIN корректна."
            if valid
            else "VIN должен содержать 17 допустимых символов без I, O и Q."
        ),
        "normalised": vin,
    }


def validate_party_name(value: object, quote: object = None) -> dict:
    raw_text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,.;:")
    text = raw_text
    quote_compact = re.sub(r"\s+", " ", str(quote or ""))
    if text.count("«") == 1 and text.count("»") == 0:
        repaired = text + "»"
        if repaired.casefold() in quote_compact.casefold():
            text = repaired
    has_letters = sum(char.isalpha() for char in text) >= 2
    plausible_length = 3 <= len(text) <= 220
    not_number = not re.fullmatch(r"[\d\s.,:/§№-]+", text)
    balanced_quotes = text.count("«") == text.count("»")
    semantic_noise = bool(re.search(
        r"\b(?:участник\w*\s+(?:лизингов\w+\s+)?сделк\w*|"
        r"далее\s+именуем|определени\w+\s+термин\w*)\b",
        text,
        re.I,
    ))
    has_legal_form = bool(re.search(
        r"(?:^|\s)(?:ТОО|АО|ИП|ЖК|ЖШС|Акционерн\w+\s+общество|"
        r"Товариществ\w+\s+с\s+ограниченн\w+\s+ответственност\w*)\b",
        text,
        re.I,
    ))
    has_quoted_name = bool(re.search(r"[«\"“][^»\"”]{2,}[»\"”]", text))
    looks_like_person = bool(re.fullmatch(
        r"[А-ЯЁІҢҒҮҰҚӨҺ][А-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ'’-]+"
        r"(?:\s+[А-ЯЁІҢҒҮҰҚӨҺ][А-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ'’-]+){1,3}",
        text,
    ))
    plausible_entity = has_legal_form or has_quoted_name or looks_like_person

    evidence_supported = True
    quote_text = re.sub(r"\s+", " ", str(quote or "")).casefold()
    if quote_text:
        evidence_supported = text.casefold() in quote_text
        # Normalised typographic quotes may legitimately differ from OCR/source.
        if not evidence_supported:
            simplified = re.sub(r"[«»\"“”'`]", "", text.casefold())
            simplified_quote = re.sub(r"[«»\"“”'`]", "", quote_text)
            evidence_supported = simplified in simplified_quote
        # Legal forms are often expanded in the source and OCR commonly mixes
        # visually identical Cyrillic/Latin letters (BCC -> ВСС).  Compare the
        # quoted organisation core after those harmless transformations.
        if not evidence_supported:
            def normalised_core(core: str) -> str:
                core = core.upper().translate(str.maketrans({
                    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H",
                    "К": "K", "М": "M", "О": "O", "Р": "P", "Т": "T",
                    "Х": "X", "У": "Y",
                }))
                return re.sub(r"[^A-ZА-ЯЁ0-9]+", "", core)

            value_matches = re.findall(r"[«\"“]([^»\"”]{2,160})[»\"”]", text)
            quote_matches = re.findall(r"[«\"“]([^»\"”]{2,160})[»\"”]", quote_text)
            value_cores = [normalised_core(x) for x in (value_matches or [text])]
            quote_cores = [normalised_core(x) for x in (quote_matches or [quote_text])]
            evidence_supported = any(
                value_core and value_core in quote_core
                for value_core in value_cores
                for quote_core in quote_cores
            )

    valid = (
        has_letters and plausible_length and not_number and balanced_quotes
        and evidence_supported and plausible_entity and not semantic_noise
    )
    problems = []
    if not (has_letters and plausible_length and not_number):
        problems.append("значение не похоже на имя человека или организации")
    if not balanced_quotes:
        problems.append("кавычки в наименовании не сбалансированы")
    if not evidence_supported:
        problems.append("значение не найдено в приведённой цитате")
    if semantic_noise or not plausible_entity:
        problems.append("значение похоже на определение роли, а не на сторону договора")
    return {
        "kind": "party_name",
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "message": (
            "Наименование стороны прошло структурную проверку."
            if valid else "Не пройдена проверка наименования: " + "; ".join(problems) + "."
        ),
        "normalised": text,
        "original": raw_text,
        "suggestions": [],
    }


def _candidate_variants(value: str, substitutions: dict[str, str], max_changes: int = 2) -> list[str]:
    positions = [
        index for index, char in enumerate(value)
        if char in substitutions and substitutions[char] != char
    ]
    variants: set[str] = set()
    for change_count in range(1, min(max_changes, len(positions)) + 1):
        for selected in itertools.combinations(positions, change_count):
            chars = list(value)
            for index in selected:
                chars[index] = substitutions[chars[index]]
            variants.add("".join(chars))
    return sorted(variants)


def correction_suggestions(value: object, kind: str, limit: int = 5) -> list[str]:
    raw = _compact_upper(value)

    if kind == "iin_bin":
        compact = re.sub(r"[\s\-_.]", "", raw)
        candidates = _candidate_variants(compact, OCR_DIGIT_SUBSTITUTIONS, max_changes=2)
        valid = []
        for candidate in candidates:
            result = validate_iin_bin(candidate)
            if result["valid"]:
                valid.append(result["normalised"])
        return valid[:limit]

    if kind == "iban":
        compact = re.sub(r"[^A-Z0-9]", "", raw)
        candidates = _candidate_variants(compact, OCR_ALNUM_SUBSTITUTIONS, max_changes=2)
        valid = []
        for candidate in candidates:
            result = validate_iban(candidate)
            if result["valid"]:
                valid.append(result["normalised"])
        return valid[:limit]

    if kind == "vin":
        compact = re.sub(r"[^A-Z0-9]", "", raw)
        candidates = _candidate_variants(compact, OCR_ALNUM_SUBSTITUTIONS, max_changes=2)
        valid = []
        for candidate in candidates:
            result = validate_vin(candidate)
            if result["valid"]:
                valid.append(result["normalised"])
        return valid[:limit]

    return []


def _field_kind(field: dict) -> str | None:
    name = str(field.get("name") or "")
    label = str(field.get("label_ru") or "").upper()

    if name.endswith(IDENTIFIER_FIELD_SUFFIXES) or "ИИН/БИН" in label:
        return "iin_bin"
    if name.endswith(IBAN_FIELD_SUFFIXES) or "IBAN" in label or "ИИК" in label:
        return "iban"
    if name in VIN_FIELD_NAMES or name.endswith("_vin") or "VIN" in label:
        return "vin"
    if name in PARTY_NAME_FIELD_NAMES:
        return "party_name"
    return None


def _validate_single(value: object, kind: str) -> dict:
    if kind == "iin_bin":
        result = validate_iin_bin(value)
    elif kind == "iban":
        result = validate_iban(value)
    elif kind == "vin":
        result = validate_vin(value)
    else:
        return {}

    if not result["valid"]:
        result["suggestions"] = correction_suggestions(value, kind)
    else:
        result["suggestions"] = []
    return result


def validate_field(field: dict) -> dict:
    kind = _field_kind(field)
    if not kind:
        return field

    updated = deepcopy(field)
    value = updated.get("value")

    if isinstance(value, list):
        items = []
        for item in value:
            validation = _validate_single(item, kind)
            items.append({
                "value": item,
                **validation,
            })
        valid_count = sum(bool(item.get("valid")) for item in items)
        updated["validation"] = {
            "kind": kind,
            "status": (
                "valid" if items and valid_count == len(items)
                else "partial" if valid_count
                else "invalid"
            ),
            "valid": bool(items) and valid_count == len(items),
            "message": f"Проверено значений: {len(items)}, корректных: {valid_count}.",
            "items": items,
        }
        return updated

    if kind == "party_name":
        updated["validation"] = validate_party_name(value, updated.get("quote"))
        normalised = updated["validation"].get("normalised")
        if updated["validation"].get("valid") and normalised and normalised != value:
            updated["original_value"] = value
            updated["value"] = normalised
            updated["status"] = "corrected"
            note = "Наименование нормализовано по подтверждающей цитате."
            updated["notes"] = f"{str(updated.get('notes') or '').strip()} {note}".strip()
    else:
        updated["validation"] = _validate_single(value, kind)
    return updated


def validate_fields(fields: list[dict]) -> list[dict]:
    validated = [validate_field(field) for field in fields]

    for field in validated:
        validation = field.get("validation") or {}
        if not validation or validation.get("valid"):
            continue
        # Manual confirmations remain manual facts, but the validation warning
        # stays visible. Automatically extracted invalid values are downgraded.
        if field.get("status") not in {"confirmed", "corrected"}:
            field["status"] = "candidate"
            note = "Формальная проверка значения не пройдена."
            existing = str(field.get("notes") or "").strip()
            field["notes"] = f"{existing} {note}".strip()
            field["confidence"] = min(float(field.get("confidence") or 0.0), 0.55)

    return validated


def validation_warnings(fields: list[dict]) -> list[dict]:
    warnings = []
    for field in fields:
        validation = field.get("validation") or {}
        if not validation or validation.get("valid"):
            continue
        suggestions = validation.get("suggestions") or []
        if validation.get("items"):
            suggestions = [
                suggestion
                for item in validation["items"]
                for suggestion in item.get("suggestions", [])
            ][:5]
        message = validation.get("message") or "Значение не прошло формальную проверку."
        if suggestions:
            message += " Возможные OCR-исправления: " + ", ".join(suggestions) + "."
        warnings.append({
            "severity": "high",
            "field": field.get("label_ru") or field.get("name") or "Поле",
            "message": message,
        })
    return warnings
