from __future__ import annotations

import time
import uuid
from pathlib import Path

from ..parsers.specific import parse_by_type
from .candidate_resolver import resolve_candidates
from .classifier import classify
from .client_registry import reconcile_client_party_names
from .comprehensive_extraction import enrich_all_readable_data
from .document_reader import read_document
from .dossier import build_dossier_summary
from .equipment_precision import improve_equipment_tables
from .exporter import save_excel
from .final_quality import apply_final_quality_gate
from .final_reconciliation import reconcile_final_warnings
from .insurance_gps import apply_insurance_gps
from .missing_field_pass import recover_missing_fields
from .page_reprocessor import PROFILE_MAP
from .precision_postprocess import improve_fields
from .production_hardening import apply_production_hardening, finalize_continuation_specification_assets
from .quality import ensure_review_audit, review_fields
from .release_sanitizer import apply_release_sanitizer
from .safe_regression_fixes import TYPE_LABELS, first_page_type, postprocess_fields, postprocess_tables, final_contract_hardening
from .semantic_enrichment import enrich_document_semantics
from .source_locator import enrich_field_locations, enrich_field_text_quotes, unresolved_pages
from .stability_guard import apply_stability_guard
from .table_extractor import extract_tables
from .validators import validate_fields, validation_warnings


MODE_DPI = {
    "auto": 150,
    "fast": 150,
    "standard": 190,
    "accurate": 230,
}


def _normalise_tables(tables):
    if not isinstance(tables, (list, tuple)):
        return []
    safe_tables = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        columns = table.get("columns")
        rows = table.get("rows")
        if not isinstance(columns, (list, tuple)) or not isinstance(rows, (list, tuple)):
            continue
        safe_columns = []
        for column in columns:
            if not isinstance(column, dict):
                continue
            key = column.get("key")
            if not isinstance(key, str) or not key:
                continue
            safe_columns.append({"key": key, "label_ru": str(column.get("label_ru") or key)})
        if not safe_columns:
            continue
        safe_rows = [dict(row) for row in rows if isinstance(row, dict)]
        safe = dict(table)
        safe["columns"] = safe_columns
        safe["rows"] = safe_rows
        safe["row_count"] = len(safe_rows)
        safe["label_ru"] = str(safe.get("label_ru") or "Структурированная таблица")
        safe["notes"] = str(safe.get("notes") or "")
        safe["confidence"] = safe.get("confidence", 0)
        safe["status"] = str(safe.get("status") or "candidate")
        safe_tables.append(safe)
    return safe_tables


class DirectAnalyzer:
    """Run the verified extraction pipeline directly, without Flask/test-client."""

    def __init__(self, result_folder: Path, analysis_mode: str = "auto"):
        self.result_folder = Path(result_folder)
        self.analysis_mode = analysis_mode if analysis_mode in MODE_DPI else "auto"
        self.result_folder.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.result_folder / "ocr_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _analyze_one(self, path: Path, document_index: int) -> dict:
        started = time.perf_counter()
        read_started = time.perf_counter()
        read_result = read_document(
            path,
            ocr_languages="rus+kaz+eng",
            ocr_dpi=MODE_DPI[self.analysis_mode],
            min_digital_chars=80,
            ocr_cache_dir=self.cache_dir,
            analysis_mode=self.analysis_mode,
        )
        read_seconds = time.perf_counter() - read_started
        extract_started = time.perf_counter()

        classification = classify(read_result.full_text, path.name)
        corrected_type = first_page_type(read_result, classification.key)
        if corrected_type != classification.key:
            classification.key = corrected_type
            classification.label_ru = TYPE_LABELS.get(corrected_type, classification.label_ru)
            classification.confidence = max(classification.confidence, 0.98)

        fields = parse_by_type(read_result, classification.key)
        fields = resolve_candidates(read_result, fields, classification.key)
        tables = _normalise_tables(extract_tables(read_result, classification.key))
        fields = improve_fields(read_result, classification.key, fields, tables)
        tables = improve_equipment_tables(read_result, fields, tables)
        fields = improve_fields(read_result, classification.key, fields, tables)
        fields, explicit_lease_amount = postprocess_fields(read_result, classification.key, fields, tables)
        tables = improve_equipment_tables(read_result, fields, tables)
        tables = _normalise_tables(postprocess_tables(read_result, classification.key, fields, tables, explicit_lease_amount))
        fields, tables = apply_insurance_gps(read_result, classification.key, fields, tables)
        before = {item.get("name") for item in fields if item.get("value") not in (None, "", [])}
        fields = recover_missing_fields(read_result, classification.key, fields)
        after = {item.get("name") for item in fields if item.get("value") not in (None, "", [])}
        if after != before:
            fields, tables = apply_insurance_gps(read_result, classification.key, fields, tables)
        fields, tables = enrich_all_readable_data(read_result, classification.key, fields, tables)
        fields, tables = enrich_document_semantics(read_result, classification.key, fields, tables)
        tables = improve_equipment_tables(read_result, fields, tables)
        fields, tables = apply_stability_guard(fields, tables)
        fields = final_contract_hardening(read_result, classification.key, fields)
        fields = validate_fields(fields)
        fields = ensure_review_audit(fields)
        fields = enrich_field_text_quotes(fields, read_result.pages)
        warnings = review_fields(classification.key, fields) + validation_warnings(fields)
        extract_seconds = time.perf_counter() - extract_started

        page_layouts = [
            {"page": page.page_number, "width": page.page_width, "height": page.page_height, "words": page.layout_words}
            for page in read_result.pages
        ]
        page_methods = [
            {
                "page": page.page_number,
                "method": page.extraction_method,
                "char_count": page.char_count,
                "quality": page.quality,
                "cache_hit": page.cache_hit,
                "layout_word_count": len(page.layout_words),
                "analysis_profile": page.analysis_profile,
            }
            for page in read_result.pages
        ]
        fields = enrich_field_locations(fields, page_layouts)
        return {
            "filename": path.name,
            "source_file": path.name,
            "preview_available": path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"},
            "source_type": read_result.source_type,
            "page_count": read_result.page_count,
            "used_ocr": read_result.used_ocr,
            "page_layouts": page_layouts,
            "page_methods": page_methods,
            "page_texts": [{"page": page.page_number, "text": page.text} for page in read_result.pages],
            "unresolved_pages": unresolved_pages(page_methods, classification.key),
            "document_type": classification.key,
            "document_type_label_ru": classification.label_ru,
            "classification_confidence": classification.confidence,
            "matched_keywords": classification.matched_keywords,
            "classification_alternatives": classification.alternatives,
            "fields": fields,
            "tables": tables,
            "warnings": warnings,
            "timing": {
                "reading_ocr_seconds": round(read_seconds, 2),
                "extraction_seconds": round(extract_seconds, 2),
                "total_seconds": round(time.perf_counter() - started, 2),
                "cached_ocr_pages": sum(1 for page in read_result.pages if page.cache_hit),
            },
        }

    def analyze(self, paths: list[Path]) -> tuple[dict, Path]:
        started = time.perf_counter()
        documents = [self._analyze_one(Path(path), index) for index, path in enumerate(paths)]
        reconcile_client_party_names(documents)
        for document in documents:
            document["fields"] = ensure_review_audit(document.get("fields", []))
            document["warnings"] = review_fields(document.get("document_type", "unknown"), document.get("fields", [])) + validation_warnings(document.get("fields", []))

        result = {
            "result_id": uuid.uuid4().hex,
            "analysis": {
                "mode": self.analysis_mode,
                "mode_label_ru": self.analysis_mode,
                "total_seconds": round(time.perf_counter() - started, 2),
                "collection": {"mode": "all", "categories": [], "fields": []},
                "engine": "direct-v35",
            },
            "documents": documents,
            "dossier": build_dossier_summary(documents),
        }
        # Same true last-mile sequence used by the web path.
        apply_production_hardening(result, version="35.0")
        apply_final_quality_gate(result)
        apply_production_hardening(result, version="35.0")
        apply_release_sanitizer(result, version="35.0")
        reconcile_final_warnings(result, version="35.0")
        finalize_continuation_specification_assets(result)
        apply_release_sanitizer(result, version="35.0")
        result["dossier"] = build_dossier_summary(result.get("documents", []))

        excel_path = self.result_folder / f"{result['result_id']}.xlsx"
        save_excel(result, excel_path)
        return result, excel_path
