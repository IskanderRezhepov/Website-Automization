from __future__ import annotations

import json
import re
import hashlib
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .document_reader import SUPPORTED_EXTENSIONS
from .final_quality import apply_final_quality_gate


OUTPUT_PREFIXES = ("АНАЛИЗ_", "ДОСЬЕ_")


def normalize_iin_bin(value: object) -> str:
    """Return a canonical 12-digit Kazakhstan IIN/BIN or an empty string."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 12 else ""




def source_folder_iin_bin(source: Path, root: Path) -> str:
    """Return the nearest exact 12-digit client-folder name, if present.

    The folder name is explicit operator-provided metadata. It is used only as
    a fallback when the document result lacks a client identifier; it never
    replaces a different 12-digit identifier already confirmed in the result.
    """
    source = Path(source).resolve()
    root = Path(root).resolve()
    for parent in (source.parent, *source.parents):
        if parent == root.parent:
            break
        candidate = normalize_iin_bin(parent.name)
        if candidate and re.fullmatch(r"\D*\d{12}\D*", parent.name):
            return candidate
        if parent == root:
            break
    return ""


def apply_folder_identity_fallback(item: "BatchItem", root: Path, explicit_target: str = "") -> None:
    """Preserve a client BIN/IIN from explicit filter or client folder metadata."""
    result = item.result or {}
    client = result.setdefault("client", {})
    existing = normalize_iin_bin(client.get("iin_bin"))
    candidate = normalize_iin_bin(explicit_target) or source_folder_iin_bin(item.source, root)
    if existing or not candidate:
        if existing:
            client["iin_bin"] = existing
        return
    # Folder metadata is trusted for files stored in that exact client folder.
    client["iin_bin"] = candidate
    client["iin_bin_source"] = "client_folder" if not explicit_target else "explicit_filter"
    result.setdefault("analysis", {})["client_iin_bin_from_folder"] = candidate


def discover_documents(folder: Path, recursive: bool = True) -> list[Path]:
    """Find supported source documents in a stable, user-visible order."""
    folder = Path(folder).expanduser().resolve()
    iterator: Iterable[Path] = folder.rglob("*") if recursive else folder.iterdir()
    documents = []
    excluded_dirs = {"_REVIEW_UNMATCHED", "_PRODUCTION_READY", "_QUARANTINE"}
    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            relative_parts = set(path.relative_to(folder).parts[:-1])
        except Exception:
            relative_parts = set(path.parts[:-1])
        if relative_parts & excluded_dirs:
            continue
        if path.name.upper().startswith(OUTPUT_PREFIXES):
            continue
        documents.append(path)
    return sorted(
        documents,
        key=lambda item: (
            len(item.relative_to(folder).parts),
            str(item.relative_to(folder)).casefold(),
        ),
    )


def result_contains_iin_bin(result: dict, target_iin_bin: str) -> bool:
    """Match an IIN/BIN only against complete scalar values, never substrings."""
    target = normalize_iin_bin(target_iin_bin)
    if not target:
        return True

    def walk(value: object) -> bool:
        if isinstance(value, dict):
            return any(walk(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(walk(item) for item in value)
        text = str(value or "")
        # Field values can contain a harmless ``БИН:``/``ИИН №`` prefix, but
        # a 12-digit sequence inside a free-text quote is not an exact match.
        remainder = re.sub(r"(?i)\b(?:б[иі]н|иин|iin|bin)\b", "", text)
        remainder = re.sub(r"[\d\s№#:+\-–—.,;()]+", "", remainder)
        return not remainder and normalize_iin_bin(text) == target

    return walk(result)


def result_link_keys(result: dict) -> set[str]:
    """Collect conservative document-link keys such as contract numbers."""
    keys: set[str] = set()
    for document in result.get("documents", []):
        for field_item in document.get("fields", []):
            name = str(field_item.get("name") or "").lower()
            if not (
                "contract_number" in name
                or name in {
                    "agreement_number",
                    "act_number",
                    "guarantee_number",
                    "policy_number",
                    "insurance_policy_number",
                    "linked_purchase_contract",
                    "linked_insurance_policy_number",
                }
            ):
                continue
            value = field_item.get("value")
            values = value if isinstance(value, (list, tuple, set)) else [value]
            for raw in values:
                normalized = re.sub(r"[^0-9A-ZА-Я]", "", str(raw or "").upper())
                if len(normalized) >= 6 and not normalized.isdigit():
                    keys.add(normalized)
    return keys


def _safe_output_stem(value: str, limit: int = 52) -> str:
    """Build a short ZIP/Windows-portable filename.

    Prefer the stable document identifier (usually F123...) instead of copying
    a long Cyrillic title into the generated workbook name.  Some ZIP tools
    expand every Cyrillic character to ``#Uxxxx`` and otherwise exceed the
    filesystem filename limit during extraction.
    """
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:8]
    identifier = re.search(r"(?i)(?:^|[^A-ZА-Я0-9])(F-?\d{5,})", value)
    if identifier:
        token = identifier.group(1).upper()
        return f"{token}_{digest}"
    ascii_only = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(" ._")
    ascii_only = ascii_only[:limit].rstrip(" ._") or "document"
    return f"{ascii_only}_{digest}"


def individual_output_path(source: Path, target_iin_bin: str = "") -> Path:
    """Return the single deterministic Excel path belonging to ``source``."""
    bin_suffix = f"_{target_iin_bin}" if target_iin_bin else ""
    filename = f"АНАЛИЗ_{_safe_output_stem(source.stem)}{bin_suffix}.xlsx"
    return source.parent / filename


def remove_previous_individual_outputs(source: Path) -> int:
    """Remove only generated Excel variants that belong to ``source``.

    Older releases avoided overwriting by adding ``_2``, ``_3`` and optional
    IIN/BIN suffixes.  Keeping those files breaks the one-document/one-Excel
    contract, so a new successful result replaces all such generated variants.
    """
    safe_stem = _safe_output_stem(source.stem)
    pattern = re.compile(
        rf"^АНАЛИЗ_{re.escape(safe_stem)}"
        rf"(?:_\d{{12}})?(?:_\d+)?\.xlsx$",
        re.IGNORECASE,
    )
    removed = 0
    for path in source.parent.glob("АНАЛИЗ_*.xlsx"):
        if path.is_file() and pattern.fullmatch(path.name):
            path.unlink()
            removed += 1
    return removed


def remove_legacy_dossier_outputs(folder: Path, recursive: bool = True) -> int:
    """Remove obsolete generated dossier workbooks from a selected tree."""
    iterator = folder.rglob("ДОСЬЕ_*.xlsx") if recursive else folder.glob("ДОСЬЕ_*.xlsx")
    removed = 0
    for path in iterator:
        if path.is_file():
            path.unlink()
            removed += 1
    return removed



def mark_ambiguous_single_asset_vins(result: dict) -> None:
    """Never silently choose between competing VIN/chassis identifiers."""
    vin_re = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", re.I)

    def similar(left: str, right: str) -> bool:
        if len(left) != 17 or len(right) != 17 or left == right:
            return False
        distance = sum(a != b for a, b in zip(left, right))
        shared_suffix = 0
        for a, b in zip(reversed(left), reversed(right)):
            if a != b:
                break
            shared_suffix += 1
        return distance <= 5 and (shared_suffix >= 6 or distance <= 3)

    for document in result.get("documents", []):
        asset_tables = [t for t in document.get("tables", []) if t.get("name") == "asset_vin_rows"]
        rows = [row for table in asset_tables for row in table.get("rows", []) if isinstance(row, dict)]
        # Multiple actual assets legitimately have multiple VINs. Ambiguity logic
        # applies only when the document represents zero or one physical asset.
        if len(rows) > 1:
            continue

        candidates: list[str] = []
        vin_fields = []
        for field in document.get("fields", []):
            name = str(field.get("name") or "").lower()
            label = str(field.get("label_ru") or "").lower()
            if any(token in name or token in label for token in ("vin", "идентификатор", "шасси")):
                vin_fields.append(field)
                for source in (field.get("value"), field.get("quote"), field.get("raw_value")):
                    candidates.extend(v.upper() for v in vin_re.findall(str(source or "").upper()))

        for table in document.get("tables", []):
            for row in table.get("rows", []):
                if not isinstance(row, dict):
                    continue
                row_type = str(row.get("type") or row.get("identifier_type") or "").casefold()
                for key, value in row.items():
                    key_text = str(key).casefold()
                    if (
                        "vin" in key_text or "шасси" in key_text or "identifier" in key_text
                        or row_type == "vin" or str(row.get("Тип") or "").casefold() == "vin"
                    ):
                        candidates.extend(v.upper() for v in vin_re.findall(str(value or "").upper()))

        unique = list(dict.fromkeys(candidates))
        related = [value for value in unique if any(similar(value, other) for other in unique)]
        related = list(dict.fromkeys(related))
        if len(related) < 2:
            continue

        display = " / ".join(related[:8])
        review_value = f"Требует проверки: {display}"
        for field in vin_fields:
            field["original_value"] = field.get("original_value", field.get("value"))
            field["value"] = review_value
            field["normalized_value"] = review_value
            field["status"] = "candidate"
            field["confidence"] = min(float(field.get("confidence") or 1), 0.45)
            field["notes"] = "Найдены конфликтующие VIN/номер шасси; автоматический выбор отключён."
            field.setdefault("validation", {}).update({"valid": False, "message": "VIN требует ручного подтверждения."})

        for row in rows:
            for key in list(row):
                if "vin" in str(key).lower() or "шасси" in str(key).lower() or "identifier" in str(key).lower():
                    row[key] = review_value
            row["confidence"] = min(float(row.get("confidence") or 1), 0.45)
            row["status"] = "Требует проверки"

        # Also mark the identifier registry so users do not see two apparently
        # confirmed VIN rows below a single selected VIN.
        for table in document.get("tables", []):
            for row in table.get("rows", []):
                if not isinstance(row, dict):
                    continue
                values = " ".join(str(v or "") for v in row.values())
                if any(v in values.upper() for v in related):
                    row["status"] = "Требует проверки"

        warnings = document.setdefault("warnings", [])
        message = f"Конфликт VIN/номера шасси: {display}. Автоматический выбор отключён."
        if not any(message == str(w.get("message") if isinstance(w, dict) else w) for w in warnings):
            warnings.append({"severity": "high", "field": "VIN", "message": message, "message_ru": message})



def _clean_party_name(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,.;:-")
    if not text:
        return None
    lowered = text.casefold()
    banned = ("bcc leasing", "банк центркредит", "лизингополучатель", "лизинг алушы", "лизингодатель")
    if any(token in lowered for token in banned):
        return None
    if len(text) < 2 or len(text) > 140:
        return None
    return text


def repair_client_identity(result: dict) -> None:
    """Recover the actual client name and remove a false BCC Leasing assignment."""
    client = result.setdefault("client", {})
    identifier = normalize_iin_bin(client.get("iin_bin"))
    current = _clean_party_name(client.get("name"))

    name_candidates: list[str] = []
    client_fields = {"lessee_name", "borrower_name", "principal_name", "recipient_name", "customer_name"}
    for document in result.get("documents", []):
        for item in document.get("fields", []):
            name = str(item.get("name") or "").lower()
            value = item.get("value")
            if name in client_fields:
                cleaned = _clean_party_name(value)
                if cleaned and item.get("status") not in {"candidate", "rejected"}:
                    name_candidates.append(cleaned)
                elif not cleaned and value:
                    item["status"] = "rejected"
                    item["value"] = "Не определено"
                    item["validation"] = {"valid": False, "message": "Значение относится к лизингодателю или названию роли."}
            if not identifier and name in {"lessee_iin_bin", "lessee_bin", "borrower_iin_bin", "principal_iin_bin"}:
                identifier = normalize_iin_bin(value)

        # The client's legal name is often visible immediately before its BIN.
        if identifier:
            for item in document.get("fields", []):
                quote = str(item.get("quote") or "")
                if identifier not in re.sub(r"\D", "", quote):
                    continue
                # Prefer the last meaningful quoted name before the client's BIN.
                prefix_to_bin = re.split(rf"(?:БСН|БИН|ЖСН|ИИН)\s*{identifier}", quote, maxsplit=1, flags=re.I)[0]
                quoted_names = re.findall(r'[«"]([^»"]{2,100})[»"]', prefix_to_bin)
                for quoted in reversed(quoted_names):
                    cleaned = _clean_party_name(quoted)
                    if not cleaned:
                        continue
                    tail = prefix_to_bin[prefix_to_bin.rfind(quoted):].casefold()
                    legal = "ИП" if "жеке кәсіпкер" in tail or re.search(r"\bИП\b", tail, re.I) else "ТОО"
                    if not re.match(r"^(?:ТОО|ЖШС|ИП)\b", cleaned, re.I):
                        cleaned = f"{legal} «{cleaned.strip('«»\" ')}»"
                    name_candidates.append(cleaned)
                    break

                patterns = [
                    rf'[«"]([^»"]{{2,100}})[»"][^\n]{{0,180}}?(?:БСН|БИН|ЖСН|ИИН)\s*{identifier}',
                    rf'(?:ТОО|ЖШС|ИП|Жеке\s+кәсіпкер|Товарищество[^«"]{{0,80}})[\s«"]+([^»"\n,]{{2,100}})[»"]?[^\n]{{0,180}}?(?:БСН|БИН|ЖСН|ИИН)\s*{identifier}',
                ]
                found_name = False
                for pattern in patterns:
                    for match in re.finditer(pattern, quote, re.I):
                        cleaned = _clean_party_name(match.group(1))
                        if not cleaned:
                            continue
                        legal = "ИП" if "жеке кәсіпкер" in match.group(0).casefold() or re.search(r"\bИП\b", match.group(0), re.I) else "ТОО"
                        if not re.match(r"^(?:ТОО|ЖШС|ИП)\b", cleaned, re.I):
                            cleaned = f"{legal} «{cleaned.strip('«»\" ')}»"
                        name_candidates.append(cleaned)
                        found_name = True
                        break
                    if found_name:
                        break

    chosen = current or (name_candidates[0] if name_candidates else None)
    if chosen:
        client["name"] = chosen
    else:
        client["name"] = None
    if identifier:
        client["iin_bin"] = identifier
    client.setdefault("role_label_ru", "Лизингополучатель")


def propagate_client_names(items: list[BatchItem]) -> None:
    """Share a confirmed client name between documents with the same exact BIN."""
    names: dict[str, str] = {}
    for item in items:
        repair_client_identity(item.result or {})
        identifier, name = result_client_identity(item.result or {})
        cleaned = _clean_party_name(name)
        if identifier and cleaned:
            names.setdefault(identifier, cleaned)
    for item in items:
        result = item.result or {}
        identifier, name = result_client_identity(result)
        if identifier and not _clean_party_name(name) and identifier in names:
            client = result.setdefault("client", {})
            client["name"] = names[identifier]
            client.setdefault("role_label_ru", "Лизингополучатель")


@dataclass
class BatchItem:
    source: Path
    matched: bool = False
    output: Path | None = None
    error: str | None = None
    result: dict | None = field(default=None, repr=False)
    generated_excel: Path | None = field(default=None, repr=False)
    match_reason: str | None = None


@dataclass
class BatchReport:
    folder: Path
    target_iin_bin: str
    discovered: int
    matched: int
    outputs: list[Path]
    combined_output: Path | None
    combined_outputs: list[Path]
    items: list[BatchItem]
    stopped: bool = False

    @property
    def failed(self) -> int:
        return sum(bool(item.error) for item in self.items)

    @property
    def processed(self) -> int:
        return len(self.items)


class BatchControl:
    """Thread-safe pause/resume/stop control for sequential folder analysis.

    A running OCR/export operation is allowed to finish. Control is applied at
    the safe boundary before the next source document starts.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._paused = False
        self._stop_requested = False

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    @property
    def stop_requested(self) -> bool:
        with self._condition:
            return self._stop_requested

    def pause(self) -> None:
        with self._condition:
            if not self._stop_requested:
                self._paused = True

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._paused = False
            self._condition.notify_all()

    def wait_until_ready(self) -> bool:
        """Wait while paused and return False when no next file should start."""
        with self._condition:
            while self._paused and not self._stop_requested:
                self._condition.wait()
            return not self._stop_requested


class FlaskAnalyzer:
    """Run the existing verified web analysis pipeline without a browser."""

    def __init__(self, app, result_folder: Path, analysis_mode: str = "auto"):
        self.app = app
        self.result_folder = Path(result_folder)
        self.analysis_mode = analysis_mode
        self.app.config["RESULT_FOLDER"] = str(self.result_folder)
        self.result_folder.mkdir(parents=True, exist_ok=True)
        self.client = self.app.test_client()

    def analyze(self, paths: list[Path]) -> tuple[dict, Path]:
        before = {
            path.stem for path in self.result_folder.glob("*.json")
            if path.name != "clients_index.json"
        }
        streams = [path.open("rb") for path in paths]
        try:
            response = self.client.post(
                "/analyze",
                data={
                    "documents": [
                        (stream, path.name) for stream, path in zip(streams, paths)
                    ],
                    "analysis_mode": self.analysis_mode,
                    "collection_mode": "all",
                },
                content_type="multipart/form-data",
            )
        finally:
            for stream in streams:
                stream.close()

        if response.status_code != 200:
            raise RuntimeError(f"Сервер анализа вернул код {response.status_code}.")

        created = [
            path for path in self.result_folder.glob("*.json")
            if path.name != "clients_index.json" and path.stem not in before
        ]
        if len(created) != 1:
            html = response.get_data(as_text=True)
            match = re.search(r'<div class="alert error">(.*?)</div>', html, re.S)
            message = re.sub(r"<[^>]+>", " ", match.group(1)).strip() if match else ""
            raise RuntimeError(message or "Результат анализа не был создан.")

        result_path = created[0]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        excel_path = result_path.with_suffix(".xlsx")
        if not excel_path.exists():
            export = (result.get("analysis") or {}).get("excel_export") or {}
            raise RuntimeError(
                export.get("message_ru") or "Excel-файл анализа не был создан."
            )
        return result, excel_path


ProgressCallback = Callable[[str], None]


def result_client_identity(result: dict) -> tuple[str | None, str | None]:
    """Return the confirmed dossier client identifier and display name."""
    client = result.get("client") or {}
    identifier = normalize_iin_bin(client.get("iin_bin"))
    name = str(client.get("name") or "").strip() or None
    if identifier:
        return identifier, name
    # Results produced by test doubles or older saved versions may not have the
    # top-level client block yet. Use only client-role fields, never every BIN.
    priority = (
        "principal_iin_bin", "lessee_iin_bin", "borrower_iin_bin",
        "subsidy_recipient_bin", "gps_customer_iin_bin",
        "direct_debit_sender_iin_bin", "payment_payer_iin_bin",
        "recipient_iin_bin", "recipient_bin",
    )
    for field_name in priority:
        for document in result.get("documents", []):
            for item in document.get("fields", []):
                if (
                    item.get("name") == field_name
                    and item.get("status") not in {"candidate", "rejected"}
                ):
                    identifier = normalize_iin_bin(item.get("value"))
                    if identifier:
                        return identifier, name
    return None, name



def result_primary_link_keys(result: dict) -> set[str]:
    """Return numbers that identify the uploaded document itself."""
    keys: set[str] = set()
    primary_names = {
        "contract_number", "agreement_number", "act_number",
        "policy_number", "insurance_policy_number",
    }
    for document in result.get("documents", []):
        for field_item in document.get("fields", []):
            name = str(field_item.get("name") or "").lower()
            if name not in primary_names:
                continue
            values = field_item.get("value")
            values = values if isinstance(values, (list, tuple, set)) else [values]
            for raw in values:
                normalized = re.sub(r"[^0-9A-ZА-Я]", "", str(raw or "").upper())
                if len(normalized) >= 6 and not normalized.isdigit():
                    keys.add(normalized)
    return keys

def group_batch_items(items: list[BatchItem]) -> list[list[BatchItem]]:
    """Group only documents that belong to the same concrete transaction.

    A client folder can legitimately contain many independent leasing deals.
    Sharing only the client's BIN is therefore not enough: documents are joined
    only when they share an exact normalized contract/policy/act reference.
    Unrelated applications remain separate and are never compared by VIN, sums
    or equipment quantity.
    """
    usable = [item for item in items if item.matched and not item.error and item.result]
    if not usable:
        return []

    identities = [result_client_identity(item.result or {})[0] for item in usable]
    links = [result_link_keys(item.result or {}) for item in usable]
    primary_links = [result_primary_link_keys(item.result or {}) for item in usable]
    parent = list(range(len(usable)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(usable)):
        for right in range(left + 1, len(usable)):
            same_client = (
                identities[left] and identities[right]
                and identities[left] == identities[right]
            )
            one_unidentified = not identities[left] or not identities[right]
            # Two lease applications can mention the same guarantee or GPS
            # agreement without belonging to the same transaction.  At least
            # one side of the match must therefore be a number identifying the
            # uploaded document itself; related-reference ↔ related-reference
            # intersections are deliberately ignored.
            shared_reference = bool(
                (primary_links[left] & links[right])
                or (primary_links[right] & links[left])
            )
            if shared_reference and (same_client or one_unidentified):
                union(left, right)

    groups: dict[int, list[BatchItem]] = {}
    for index, item in enumerate(usable):
        groups.setdefault(find(index), []).append(item)
    return sorted(groups.values(), key=lambda group: str(group[0].source).casefold())


def logical_client_document_count(member: BatchItem, usable_items: list[BatchItem]) -> int:
    """Count source documents belonging to the same client, not the whole tree."""
    member_identity, member_name = result_client_identity(member.result or {})
    if member_identity:
        return sum(
            1 for candidate in usable_items
            if result_client_identity(candidate.result or {})[0] == member_identity
        )
    normalized_name = re.sub(r"\W+", "", str(member_name or "").casefold())
    if normalized_name:
        same_name = sum(
            1 for candidate in usable_items
            if re.sub(r"\W+", "", str(result_client_identity(candidate.result or {})[1] or "").casefold()) == normalized_name
        )
        if same_name:
            return same_name
    return sum(1 for candidate in usable_items if candidate.source.parent == member.source.parent)


def run_folder_batch(
    folder: Path,
    target_iin_bin: str = "",
    *,
    recursive: bool = True,
    analysis_mode: str = "auto",
    save_individual: bool = True,
    save_combined: bool = False,
    progress: ProgressCallback | None = None,
    app_factory=None,
    control: BatchControl | None = None,
) -> BatchReport:
    """Analyze a folder sequentially and save Excel results beside sources."""
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError("Выбранная папка не существует.")

    target = normalize_iin_bin(target_iin_bin)
    if target_iin_bin and not target:
        raise ValueError("БИН/ИИН должен содержать ровно 12 цифр.")
    if analysis_mode not in {"auto", "fast", "standard", "accurate"}:
        raise ValueError("Неизвестный режим анализа.")

    documents = discover_documents(folder, recursive=recursive)
    if not documents:
        raise ValueError("В папке нет поддерживаемых документов.")


    items: list[BatchItem] = []
    outputs: list[Path] = []
    matched_sources: list[Path] = []
    production_records: list[dict] = []
    log = progress or (lambda _message: None)
    removed_dossiers = remove_legacy_dossier_outputs(folder, recursive=recursive)
    if removed_dossiers:
        log(
            f"Удалено старых лишних Excel-досье: {removed_dossiers}. "
            "Теперь создаётся один Excel на документ."
        )
    if save_combined:
        log(
            "Создание объединённых Excel-досье отключено: "
            "действует правило «один документ — один Excel»."
        )

    with tempfile.TemporaryDirectory(prefix="credit_dossier_batch_") as temp_dir:
        if app_factory is None:
            from .direct_analyzer import DirectAnalyzer
            analyzer = DirectAnalyzer(Path(temp_dir) / "results", analysis_mode=analysis_mode)
        else:
            analyzer = FlaskAnalyzer(
                app_factory(), Path(temp_dir) / "results", analysis_mode=analysis_mode
            )
        total = len(documents)
        for index, source in enumerate(documents, start=1):
            if control is not None and not control.wait_until_ready():
                log(
                    "Обработка остановлена пользователем. "
                    "Оставшиеся файлы не запускались."
                )
                break
            item = BatchItem(source=source)
            items.append(item)
            log(f"[{index}/{total}] Анализ: {source.name}")
            try:
                result, generated_excel = analyzer.analyze([source])
                item.result = result
                item.generated_excel = generated_excel
                item.matched = result_contains_iin_bin(result, target)
                if item.matched:
                    item.match_reason = "БИН/ИИН" if target else "все документы"
                    log("  Совпадение подтверждено.")
                else:
                    log(f"  Прямой БИН/ИИН {target} не найден.")
            except Exception as exc:  # keep the rest of the folder running
                item.error = str(exc)
                log(f"  Ошибка: {exc}")

        # A schedule, act or annex may omit the client's BIN but still contain
        # the same explicit contract number. Expand only through exact,
        # normalized contract-number intersections.
        if target:
            known_links = set().union(*(
                result_link_keys(item.result or {})
                for item in items if item.matched and not item.error
            ))
            changed = True
            while changed and known_links:
                changed = False
                for item in items:
                    if item.matched or item.error or not item.result:
                        continue
                    links = result_link_keys(item.result)
                    shared = sorted(known_links & links)
                    if not shared:
                        continue
                    item.matched = True
                    item.match_reason = f"связанный договор {shared[0]}"
                    known_links.update(links)
                    changed = True
                    log(f"Связан по договору: {item.source.name}")

        # Reconcile all related documents before exporting any individual file.
        # Every workbook still contains only its source document's extracted data,
        # while dossier checks use the complete client folder/group.
        from .dossier import build_dossier_summary
        from .exporter import save_excel

        usable_items = [
            item for item in items
            if item.matched and not item.error and item.result
        ]
        # Normalize client identities and safety-critical identifiers before
        # building any dossier summary or workbook.
        propagate_client_names(usable_items)
        from .production_hardening import (
            apply_production_hardening,
            remove_signature_party_noise,
            suppress_redundant_party_warnings,
            require_identifiable_equipment,
            finalize_continuation_specification_assets,
        )
        for candidate in usable_items:
            apply_folder_identity_fallback(candidate, folder, target)
            apply_production_hardening(candidate.result or {})
            from .release_sanitizer import apply_release_sanitizer
            apply_release_sanitizer(candidate.result or {}, version="35.0")
            mark_ambiguous_single_asset_vins(candidate.result or {})
            apply_final_quality_gate(candidate.result or {})
            # Final-quality rules can create warnings after production cleanup.
            # Prune only warnings that are provably redundant/noise, then run
            # the equipment completeness gate once more.
            remove_signature_party_noise(candidate.result or {})
            suppress_redundant_party_warnings(candidate.result or {})
            require_identifiable_equipment(candidate.result or {})
            from .final_reconciliation import reconcile_final_warnings
            reconcile_final_warnings(candidate.result or {}, version="35.0")
            # v32: recover verified continuation-page commercial rows after all
            # stages that may have preferred a shorter equipment table.
            finalize_continuation_specification_assets(candidate.result or {})
            from .release_sanitizer import apply_release_sanitizer
            apply_release_sanitizer(candidate.result or {}, version="35.0")

        for member in usable_items:
            member_identity, _ = result_client_identity(member.result or {})
            member_primary = result_primary_link_keys(member.result or {})
            member_links = result_link_keys(member.result or {})
            related = [member]
            for candidate in usable_items:
                if candidate is member:
                    continue
                candidate_identity, _ = result_client_identity(candidate.result or {})
                if (
                    member_identity and candidate_identity
                    and member_identity != candidate_identity
                ):
                    continue
                candidate_primary = result_primary_link_keys(candidate.result or {})
                candidate_links = result_link_keys(candidate.result or {})
                # Direct relation only. Do not transitively merge two separate
                # leasing applications merely because both mention one common
                # guarantee, insurance or GPS agreement.
                if (member_primary & candidate_links) or (candidate_primary & member_links):
                    related.append(candidate)

            group_documents = [
                document
                for related_item in related
                for document in (related_item.result or {}).get("documents", [])
            ]
            shared_dossier = build_dossier_summary(group_documents)
            group_size = len(group_documents)
            result = member.result or {}
            result["dossier"] = shared_dossier
            analysis = result.setdefault("analysis", {})
            analysis["compact_export"] = True
            analysis["folder_document_count"] = logical_client_document_count(member, usable_items)
            analysis["source_tree_document_count"] = len(documents)
            analysis["documents_used_for_reconciliation"] = group_size
            analysis["show_dossier_sheet"] = False

        for item in items:
            if not item.matched or item.error or not item.result:
                continue
            matched_sources.append(item.source)
            if save_individual:
                remove_previous_individual_outputs(item.source)
                output = individual_output_path(item.source, target)
                save_excel(item.result, output)
                item.output = output
                outputs.append(output)
                from .production_mode import route_output
                routed = route_output(folder, item.source, output, item.result)
                production_records.append(routed)
                log(f"Сохранён: {output.name}")
                log(f"  Production Gate: {routed['decision']}")

        if production_records:
            from .production_mode import write_manifest
            manifest_path = write_manifest(folder, production_records)
            approved = sum(1 for r in production_records if r.get('decision') == 'AUTO_APPROVED')
            quarantined = len(production_records) - approved
            log(f"Production Gate: AUTO_APPROVED={approved}; QUARANTINED={quarantined}.")
            log(f"Манифест: {manifest_path.name}")

        combined_output = None
        combined_outputs: list[Path] = []
        stopped = bool(control and control.stop_requested)

    return BatchReport(
        folder=folder,
        target_iin_bin=target,
        discovered=len(documents),
        matched=len(matched_sources),
        outputs=outputs,
        combined_output=combined_output,
        combined_outputs=combined_outputs,
        items=items,
        stopped=stopped,
    )
