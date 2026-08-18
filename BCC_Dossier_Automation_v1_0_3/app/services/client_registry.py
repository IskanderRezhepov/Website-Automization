from __future__ import annotations

import json
import re
from collections import Counter
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


CLIENT_ROLE_PRIORITY = (
    ("principal_iin_bin", "Принципал"),
    ("lessee_iin_bin", "Лизингополучатель"),
    ("lessee_bin", "Лизингополучатель"),
    ("borrower_iin_bin", "Заёмщик"),
    ("borrower_bin", "Заёмщик"),
    ("subsidy_recipient_bin", "Получатель субсидии"),
    ("gps_customer_iin_bin", "Заказчик"),
    ("insurance_holder_iin_bin", "Страхователь / клиент"),
    ("payment_payer_iin_bin", "Плательщик / клиент"),
    ("direct_debit_sender_iin_bin", "Отправитель денег / клиент"),
    ("recipient_bin", "Получатель"),
    ("recipient_iin_bin", "Получатель"),
    # In leasing purchase contracts the buyer is normally the lessor, not the
    # dossier client, so it is deliberately the last fallback.
    ("buyer_iin_bin", "Покупатель"),
)

CLIENT_NAME_FIELDS = (
    "lessee_name",
    "borrower_name",
    "buyer_name",
    "recipient_name",
    "subsidy_recipient_name",
    "principal_name",
    "gps_customer",
    "payment_payer",
    "direct_debit_sender",
    "insurance_holder",
)

ROLE_NAME_TO_IDENTIFIER_FIELDS = {
    "lessee_name": ("lessee_iin_bin", "lessee_bin"),
    "borrower_name": ("borrower_iin_bin", "borrower_bin"),
    "buyer_name": ("buyer_iin_bin",),
    "seller_name": ("seller_iin_bin",),
    "recipient_name": ("recipient_iin_bin", "recipient_bin"),
    "subsidy_recipient_name": ("subsidy_recipient_bin",),
    "principal_name": ("principal_iin_bin",),
    "gps_customer": ("gps_customer_iin_bin",),
    "payment_payer": ("payment_payer_iin_bin",),
    "direct_debit_sender": ("direct_debit_sender_iin_bin",),
    "insurance_holder": ("insurance_holder_iin_bin", "insurance_holder_bin"),
}


def _normalise_identifier(value: object) -> str | None:
    text = re.sub(r"\D", "", str(value or ""))
    return text if len(text) == 12 else None



def _normalise_client_name(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    # Convert simple straight quotes to Kazakhstan/Russian typographic quotes.
    text = re.sub(r'^(ИП|ТОО|АО)\s+["“]([^"”]+)["”]?$', r'\1 «\2»', text, flags=re.IGNORECASE)
    # Close a missing right quote without changing already balanced names.
    if '«' in text and '»' not in text:
        # A trailing full stop can be part of initials (for example Н.Р.).
        text = text.rstrip(' ,;:') + '»'
    # Remove accidental duplicated closing quotes.
    text = re.sub(r'»{2,}$', '»', text)
    # A party name must contain letters.  Reject page numbers, clause numbers
    # and other OCR fragments before they can become the dossier client.
    if len(text) < 3 or sum(char.isalpha() for char in text) < 2:
        return None
    if re.fullmatch(r"[\d\s.,:/§№()\-]+", text):
        return None
    return text


def _field_is_usable_name(field: dict | None) -> bool:
    if not field or field.get("status") in {"candidate", "rejected"}:
        return False
    validation = field.get("validation") or {}
    if validation and not validation.get("valid"):
        return False
    return _normalise_client_name(field.get("value")) is not None


def reconcile_client_party_names(documents: list[dict]) -> None:
    """Normalise names only through the identifier belonging to the same role.

    A document can contain the seller, buyer and lessee at once.  Merely
    finding the client's IIN somewhere in that document is therefore not
    sufficient evidence to overwrite ``buyer_name`` or ``seller_name``.
    """
    def comparable_name(value: object) -> str:
        text = _normalise_client_name(value) or ""
        text = re.sub(r"^(?:ИП|ТОО|АО)\s*", "", text, flags=re.I)
        transliteration = str.maketrans({
            "а":"a","ә":"a","б":"b","в":"v","г":"g","ғ":"g","д":"d",
            "е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"i","к":"k",
            "қ":"q","л":"l","м":"m","н":"n","ң":"n","о":"o","ө":"o",
            "п":"p","р":"r","с":"s","т":"t","у":"u","ұ":"u","ү":"u",
            "ф":"f","х":"h","һ":"h","ц":"c","ч":"ch","ш":"sh","щ":"sh",
            "ы":"y","і":"i","э":"e","ю":"yu","я":"ya","ь":"","ъ":"",
        })
        text = text.casefold().translate(transliteration)
        return re.sub(r"[^a-z0-9]+", "", text)

    # First recover a missing role identifier from a uniquely matching legal
    # name elsewhere in the dossier. This is especially useful for OCR-heavy
    # GPS scans whose contract number preserves the client name.
    known_entities: list[tuple[str, str, str]] = []
    for document in documents:
        fields_by_name = {item.get("name"): item for item in document.get("fields", [])}
        for name_field, identifier_fields in ROLE_NAME_TO_IDENTIFIER_FIELDS.items():
            name_item = fields_by_name.get(name_field)
            if not name_item:
                continue
            identifier = next((
                _normalise_identifier(fields_by_name[field_name].get("value"))
                for field_name in identifier_fields
                if field_name in fields_by_name
            ), None)
            key = comparable_name(name_item.get("value"))
            canonical = _normalise_client_name(name_item.get("value"))
            if identifier and key and canonical:
                known_entities.append((identifier, key, canonical))
    entity_map: dict[tuple[str, str], str] = {}
    for identifier, key, canonical in known_entities:
        current = entity_map.get((identifier, key))
        if current is None or (
            ("«" in canonical and "»" in canonical),
            len(canonical),
        ) > (
            ("«" in current and "»" in current),
            len(current),
        ):
            entity_map[(identifier, key)] = canonical
    known_entities = [
        (identifier, key, canonical)
        for (identifier, key), canonical in entity_map.items()
    ]

    for document in documents:
        fields = document.get("fields", [])
        fields_by_name = {item.get("name"): item for item in fields}
        for name_field, identifier_fields in ROLE_NAME_TO_IDENTIFIER_FIELDS.items():
            name_item = fields_by_name.get(name_field)
            if not name_item or any(name in fields_by_name for name in identifier_fields):
                continue
            key = comparable_name(name_item.get("value"))
            if not key:
                continue
            ranked = sorted(
                (
                    (SequenceMatcher(None, key, entity_key).ratio(),
                     identifier, canonical)
                    for identifier, entity_key, canonical in known_entities
                ),
                reverse=True,
            )
            if not ranked or ranked[0][0] < .78:
                continue
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .08:
                continue
            _, identifier, canonical = ranked[0]
            target = identifier_fields[0]
            fields.append({
                "name": target,
                "label_ru": "ИИН/БИН — " + name_field.replace("_", " "),
                "value": identifier,
                "page": None,
                "quote": None,
                "confidence": .90,
                "extraction_method": "cross_document_party_match",
                "value_type": "derived",
                "status": "corrected",
                "notes": "Идентификатор восстановлен по уникально совпавшему наименованию стороны.",
                "recovered_from": {
                    "method": "cross_document_party_name",
                    "canonical_name": canonical,
                },
            })
            name_item["original_value"] = name_item.get("value")
            name_item["value"] = canonical
            name_item["normalized_value"] = canonical
            name_item["status"] = "corrected"

    names_by_identifier: dict[str, list[str]] = {}
    role_identifiers_by_document: list[dict[str, str]] = []
    for document in documents:
        fields_by_name = {item.get("name"): item for item in document.get("fields", [])}
        role_ids: dict[str, str] = {}
        for name_field, identifier_fields in ROLE_NAME_TO_IDENTIFIER_FIELDS.items():
            identifier = next(
                (
                    _normalise_identifier(fields_by_name[field_name].get("value"))
                    for field_name in identifier_fields
                    if field_name in fields_by_name
                    and _normalise_identifier(fields_by_name[field_name].get("value"))
                ),
                None,
            )
            if not identifier:
                continue
            role_ids[name_field] = identifier
            item = fields_by_name.get(name_field)
            if not item:
                continue
            validation = item.get("validation") or {}
            if not _field_is_usable_name(item):
                continue
            name = _normalise_client_name(item.get("value"))
            if name:
                names_by_identifier.setdefault(identifier, []).append(name)
        role_identifiers_by_document.append(role_ids)

    canonical_by_identifier = {
        identifier: max(
            Counter(names),
            key=lambda value: (
                Counter(names)[value],
                bool(re.match(r"^(ИП|ТОО|АО)\b", value, re.IGNORECASE)),
                "«" in value and "»" in value,
                len(value),
            ),
        )
        for identifier, names in names_by_identifier.items()
        if names
    }

    for index, document in enumerate(documents):
        for field in document.get("fields", []):
            identifier = role_identifiers_by_document[index].get(field.get("name"))
            canonical = canonical_by_identifier.get(identifier)
            if not identifier or not canonical:
                continue
            validation = field.get("validation") or {}
            invalid = field.get("status") in {"candidate", "rejected"} or (
                validation and not validation.get("valid")
            )
            current = _normalise_client_name(field.get("value"))
            if not invalid and current == canonical:
                continue
            field["original_value"] = field.get("value")
            field["raw_value"] = field.get("value")
            field["value"] = canonical
            field["normalized_value"] = canonical
            field["correction_reason"] = (
                "Наименование восстановлено по совпадающему ИИН/БИН и "
                "согласованному юридическому имени в другом документе."
            )
            field["recovered_from"] = {
                "method": "cross_document_iin",
                "role_safe": True,
                "iin_bin": identifier,
                "role_field": field.get("name"),
                "canonical_name": canonical,
            }
            field["status"] = "corrected"
            field["confidence"] = min(max(float(field.get("confidence") or 0), 0.8), 0.9)
            field["validation"] = {
                "kind": "party_name",
                "valid": True,
                "status": "valid",
                "message": "Наименование восстановлено по тому же ИИН в других документах досье.",
                "normalised": canonical,
                "suggestions": [],
            }
            field["notes"] = (
                f"{str(field.get('notes') or '').strip()} "
                "Восстановлено междокументной сверкой по совпадающему ИИН."
            ).strip()

    # Keep document tables synchronized with the corrected field layer so the
    # exported Excel cannot disagree with the dossier checks.
    table_mappings = {
        "gps_rows": {
            "customer": "gps_customer",
            "customer_iin_bin": "gps_customer_iin_bin",
            "provider": "gps_provider",
            "provider_iin_bin": "gps_provider_iin_bin",
        },
        "insurance_gps_payment_rows": {
            "payer": "payment_payer",
            "payer_iin_bin": "payment_payer_iin_bin",
            "payee": "payment_payee",
            "payee_iin_bin": "payment_payee_iin_bin",
        },
        "insurance_rows": {
            "holder": "insurance_holder",
            "holder_iin_bin": "insurance_holder_iin_bin",
            "insured": "insured_name",
            "insured_iin_bin": "insured_iin_bin",
        },
    }
    for document in documents:
        final_values = {
            item.get("name"): item.get("value")
            for item in document.get("fields", [])
            if item.get("value") not in (None, "", [])
            and item.get("status") not in {"candidate", "rejected"}
        }
        for table in document.get("tables", []):
            mapping = table_mappings.get(table.get("name"))
            if not mapping:
                continue
            for row in table.get("rows", []):
                for column, field_name in mapping.items():
                    value = final_values.get(field_name)
                    if value not in (None, ""):
                        row[column] = value

def _usable_fields(documents: Iterable[dict]):
    for document in documents:
        for field in document.get("fields", []):
            if field.get("status") in {"candidate", "rejected"}:
                continue
            yield document, field


def identify_client(documents: list[dict]) -> dict:
    """Choose a stable primary client from confirmed/extracted party fields."""
    fields = list(_usable_fields(documents))

    identifier = None
    role = None
    role_label = None
    evidence = None
    for field_name, label in CLIENT_ROLE_PRIORITY:
        for document, field in fields:
            if field.get("name") != field_name:
                continue
            candidate = _normalise_identifier(field.get("value"))
            if candidate:
                identifier = candidate
                role = field_name
                role_label = label
                evidence = {
                    "filename": document.get("filename"),
                    "page": field.get("page"),
                    "field": field.get("label_ru"),
                }
                break
        if identifier:
            break

    name = None
    # Prefer the name belonging to the selected identifier role.
    role_name_map = {
        "lessee_iin_bin": "lessee_name", "lessee_bin": "lessee_name",
        "borrower_iin_bin": "borrower_name", "borrower_bin": "borrower_name",
        "recipient_iin_bin": "recipient_name", "recipient_bin": "recipient_name",
        "buyer_iin_bin": "buyer_name",
        "subsidy_recipient_bin": "subsidy_recipient_name",
        "principal_iin_bin": "principal_name",
        "gps_customer_iin_bin": "gps_customer",
        "insurance_holder_iin_bin": "insurance_holder",
        "payment_payer_iin_bin": "payment_payer",
        "direct_debit_sender_iin_bin": "direct_debit_sender",
    }
    preferred_name_field = role_name_map.get(role)
    paired_names: list[str] = []
    if identifier:
        for document in documents:
            fields_by_name = {
                field.get("name"): field for field in document.get("fields", [])
            }
            for name_field, identifier_fields in ROLE_NAME_TO_IDENTIFIER_FIELDS.items():
                field = fields_by_name.get(name_field)
                if not _field_is_usable_name(field):
                    continue
                same_role_identifier = next(
                    (
                        _normalise_identifier(fields_by_name[field_name].get("value"))
                        for field_name in identifier_fields
                        if field_name in fields_by_name
                        and fields_by_name[field_name].get("status") not in {"candidate", "rejected"}
                        and _normalise_identifier(fields_by_name[field_name].get("value"))
                    ),
                    None,
                )
                # Cross-document and cross-role evidence is safe only when the
                # name is paired with the exact selected identifier.
                if same_role_identifier == identifier:
                    paired_name = _normalise_client_name(field.get("value"))
                    if paired_name:
                        paired_names.append(paired_name)
    if paired_names:
        counts = Counter(paired_names)
        looks_like_person_iin = bool(
            re.fullmatch(r"\d{6}[1-6]\d{5}", identifier or "")
        )
        name = max(
            counts,
            key=lambda value: (
                counts[value],
                looks_like_person_iin and bool(re.match(r"^ИП\b", value, re.I)),
                bool(re.match(r"^(ИП|ТОО|АО)\b", value, re.I)),
                "«" in value and "»" in value,
                len(value),
            ),
        )

    if not name:
        # The fallback is allowed only for the selected identifier and the
        # exact corresponding role.  Using buyer/gps/insurance names here can
        # silently turn a lessor into the client.
        allowed_fallback_fields = {preferred_name_field} if preferred_name_field else set()
        for name_field in CLIENT_NAME_FIELDS:
            if name_field not in allowed_fallback_fields:
                continue
            for _document, field in fields:
                if field.get("name") == name_field and _field_is_usable_name(field):
                    name = _normalise_client_name(field.get("value"))
                    break
            if name:
                break

    if not name and identifier:
        # Find nearby role name fields in the same document when available.
        role_prefix = (role or "").replace("_iin_bin", "")
        for _document, field in fields:
            if field.get("name") in {f"{role_prefix}_name", f"{role_prefix}_company_name"}:
                name = _normalise_client_name(field.get("value"))
                if name:
                    break

    if not identifier and not name:
        # A standalone act may name the lessee clearly but contain no BIN/IIN.
        # Preserve the role/name for display while keeping the result
        # technically unidentified so it cannot merge with another client.
        name_only_roles = (
            ("lessee_name", "Лизингополучатель"),
            ("borrower_name", "Заёмщик"),
            ("principal_name", "Принципал"),
            ("direct_debit_sender", "Отправитель денег / клиент"),
        )
        for field_name, label in name_only_roles:
            for document, item in fields:
                if (
                    item.get("name") == field_name
                    and _field_is_usable_name(item)
                ):
                    name = _normalise_client_name(item.get("value"))
                    role = field_name
                    role_label = label
                    evidence = {
                        "filename": document.get("filename"),
                        "page": item.get("page"),
                        "field": item.get("label_ru"),
                    }
                    break
            if name:
                break

    return {
        "client_key": identifier or "unidentified",
        "iin_bin": identifier,
        "name": name,
        "role": role,
        "role_label_ru": role_label,
        "evidence": evidence,
        "identified": bool(identifier),
    }


def _registry_path(result_folder: Path) -> Path:
    return result_folder / "clients_index.json"


def load_registry(result_folder: Path) -> dict:
    path = _registry_path(result_folder)
    if not path.exists():
        return {"version": 1, "clients": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "clients": {}}
    if not isinstance(data, dict) or not isinstance(data.get("clients"), dict):
        return {"version": 1, "clients": {}}
    return data


def save_registry(result_folder: Path, registry: dict) -> None:
    result_folder.mkdir(parents=True, exist_ok=True)
    path = _registry_path(result_folder)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def register_result(result_folder: Path, result: dict) -> dict:
    client = identify_client(result.get("documents", []))
    result["client"] = client

    now = datetime.now(timezone.utc).isoformat()
    created_at = result.get("created_at") or now
    result["created_at"] = created_at
    result["updated_at"] = now

    registry = load_registry(result_folder)
    # Results without a reliable client identifier must stay separate.
    # Otherwise unrelated unknown clients would be merged into one card.
    key = client["client_key"]
    if key == "unidentified":
        key = f"unidentified-{result['result_id']}"
        client["client_key"] = key
    client_entry = registry["clients"].setdefault(key, {
        "client_key": key,
        "iin_bin": client.get("iin_bin"),
        "name": client.get("name"),
        "role_label_ru": client.get("role_label_ru"),
        "created_at": created_at,
        "updated_at": now,
        "results": [],
    })

    # Improve metadata when a later dossier identifies a name.
    for metadata_key in ("iin_bin", "name", "role_label_ru"):
        if client.get(metadata_key):
            client_entry[metadata_key] = client[metadata_key]
    client_entry["updated_at"] = now

    result_id = result["result_id"]
    summary = {
        "result_id": result_id,
        "created_at": created_at,
        "updated_at": now,
        "document_count": len(result.get("documents", [])),
        "document_types": sorted({
            doc.get("document_type_label_ru") or doc.get("document_type") or "Неизвестно"
            for doc in result.get("documents", [])
        }),
        "filenames": [doc.get("filename") for doc in result.get("documents", [])],
        "dossier_status": result.get("dossier", {}).get("status", "insufficient"),
        "review_status": result.get("review", {}).get("status"),
        "equipment_quantity": sum(
            table.get("summary", {}).get("total_quantity") or 0
            for doc in result.get("documents", [])
            for table in doc.get("tables", [])
            if table.get("name") == "asset_vin_rows"
        ),
    }

    existing = next(
        (item for item in client_entry["results"] if item.get("result_id") == result_id),
        None,
    )
    if existing is None:
        client_entry["results"].append(summary)
    else:
        existing.update(summary)

    client_entry["results"].sort(
        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
        reverse=True,
    )
    save_registry(result_folder, registry)
    return client


def list_clients(result_folder: Path) -> list[dict]:
    registry = load_registry(result_folder)
    clients = list(registry["clients"].values())
    for client in clients:
        client["analysis_count"] = len(client.get("results", []))
        client["latest_result"] = client.get("results", [None])[0] if client.get("results") else None
    clients.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return clients


def get_client(result_folder: Path, client_key: str) -> dict | None:
    return load_registry(result_folder).get("clients", {}).get(client_key)
