from __future__ import annotations

import io
import json
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from .parsers.specific import parse_by_type
from .services.classifier import classify
from .services.candidate_resolver import resolve_candidates
from .services.client_registry import (
    get_client, list_clients, reconcile_client_party_names, register_result,
)
from .services.document_reader import OCRUnavailableError, SUPPORTED_EXTENSIONS, read_document
from .services.dossier import build_dossier_summary
from .services.exporter import save_excel, save_json
from .services.field_catalog import DOCUMENT_TYPES, FIELD_BY_NAME, FIELD_CATEGORIES
from .services.quality import ensure_review_audit, review_fields
from .services.review import apply_review, field_value_for_form
from .services.source_preview import render_source_page
from .services.source_locator import (
    enrich_field_locations,
    enrich_field_text_quotes,
    unresolved_pages,
)
from .services.page_reprocessor import PROFILE_MAP, merge_manual_fields, rebuild_read_document, reprocess_source_page
from .services.table_extractor import extract_tables
from .services.validators import validate_fields, validation_warnings
from .services.precision_postprocess import improve_fields
from .services.equipment_precision import improve_equipment_tables
from .services.safe_regression_fixes import (
    TYPE_LABELS, first_page_type, postprocess_fields, postprocess_tables, final_contract_hardening,
)
from .services.stability_guard import apply_stability_guard
from .services.insurance_gps import apply_insurance_gps
from .services.missing_field_pass import recover_missing_fields
from .services.comprehensive_extraction import enrich_all_readable_data
from .services.semantic_enrichment import enrich_document_semantics
from .services.production_hardening import apply_production_hardening, finalize_continuation_specification_assets
from .services.final_quality import apply_final_quality_gate
from .services.final_reconciliation import reconcile_final_warnings
from .services.release_sanitizer import apply_release_sanitizer

bp = Blueprint('main', __name__)

ANALYSIS_MODES = {
    "auto": {"label_ru": "Автоматический по страницам", "dpi": 150},
    "fast": {"label_ru": "Быстрый", "dpi": 150},
    "standard": {"label_ru": "Стандартный", "dpi": None},
    "accurate": {"label_ru": "Максимальная точность", "dpi": 230},
}


def _result_path(result_id: str, extension: str = "json") -> Path:
    if not result_id.isalnum():
        abort(404)
    return Path(current_app.config['RESULT_FOLDER']) / f"{result_id}.{extension}"


def _source_root(result_id: str) -> Path:
    if not result_id.isalnum():
        abort(404)
    return Path(current_app.config['RESULT_FOLDER']) / 'sources' / result_id


def _document_source_path(result: dict, document_index: int) -> Path:
    documents = result.get('documents', [])
    if document_index < 0 or document_index >= len(documents):
        abort(404)
    source_name = documents[document_index].get('source_file')
    if not source_name or Path(source_name).name != source_name:
        abort(404)
    path = _source_root(result['result_id']) / source_name
    if not path.exists() or not path.is_file():
        abort(404)
    return path


def _load_result(result_id: str) -> dict:
    path = _result_path(result_id, "json")
    if not path.exists():
        abort(404)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        abort(500)


def _save_result_bundle(result: dict) -> None:
    # True last-mile normalization. This runs for browser analysis, reviewed
    # results and folder-batch analysis, so the JSON, page and Excel always use
    # the same final data instead of relying on a later batch-only patch.
    apply_production_hardening(result)
    apply_final_quality_gate(result)
    apply_production_hardening(result)
    apply_release_sanitizer(result, version="35.0")
    reconcile_final_warnings(result, version="35.0")
    # v32: true last-mile continuation-page specification recovery.
    finalize_continuation_specification_assets(result)
    apply_release_sanitizer(result, version="35.0")
    if result.get("documents"):
        result["dossier"] = build_dossier_summary(result.get("documents", []))
    result_dir = Path(current_app.config['RESULT_FOLDER'])
    result_dir.mkdir(parents=True, exist_ok=True)
    result_id = result["result_id"]
    register_result(result_dir, result)
    save_json(result, result_dir / f"{result_id}.json")
    try:
        save_excel(result, result_dir / f"{result_id}.xlsx")
        result.setdefault("analysis", {})["excel_export"] = {
            "status": "ready",
            "message_ru": "Excel сформирован",
        }
    except Exception:
        current_app.logger.exception("Excel export failed for result %s", result_id)
        result.setdefault("analysis", {})["excel_export"] = {
            "status": "failed",
            "message_ru": "Анализ завершён, но Excel не удалось сформировать.",
        }
    save_json(result, result_dir / f"{result_id}.json")


def _normalise_tables(tables):
    """Return only template-safe table dictionaries.

    This protects the HTML renderer from malformed/legacy table objects and
    from attribute names that collide with dict methods.
    """
    if not isinstance(tables, (list, tuple)):
        return []
    safe_tables = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        columns = table.get('columns')
        rows = table.get('rows')
        if not isinstance(columns, (list, tuple)) or not isinstance(rows, (list, tuple)):
            continue
        safe_columns = []
        for column in columns:
            if not isinstance(column, dict):
                continue
            key = column.get('key')
            if not isinstance(key, str) or not key:
                continue
            safe_columns.append({
                'key': key,
                'label_ru': str(column.get('label_ru') or key),
            })
        if not safe_columns:
            continue
        safe_rows = []
        for row in rows:
            if isinstance(row, dict):
                safe_rows.append(dict(row))
        safe = dict(table)
        safe['columns'] = safe_columns
        safe['rows'] = safe_rows
        safe['row_count'] = len(safe_rows)
        safe['label_ru'] = str(safe.get('label_ru') or 'Структурированная таблица')
        safe['notes'] = str(safe.get('notes') or '')
        safe['confidence'] = safe.get('confidence', 0)
        safe['status'] = str(safe.get('status') or 'candidate')
        safe_tables.append(safe)
    return safe_tables




def _selection_from_form(form) -> dict:
    mode = (form.get("collection_mode") or "all").strip().lower()
    if mode != "custom":
        return {"mode": "all", "categories": [], "fields": []}
    valid_categories = {item["key"] for item in FIELD_CATEGORIES}
    categories = sorted({
        value for value in form.getlist("selected_categories")
        if value in valid_categories
    })
    fields = sorted({
        value for value in form.getlist("selected_fields")
        if value in FIELD_BY_NAME and value != "custom"
    })
    return {"mode": "custom", "categories": categories, "fields": fields}


def _filter_document_for_selection(document: dict, selection: dict) -> dict:
    if selection.get("mode") != "custom":
        return document
    selected_categories = set(selection.get("categories") or [])
    selected_fields = set(selection.get("fields") or [])
    filtered = dict(document)
    filtered["fields"] = [
        field for field in document.get("fields", [])
        if field.get("category") in selected_categories or field.get("name") in selected_fields
    ]
    table_categories = {
        "asset_vin_rows": "equipment",
        "insurance_rows": "insurance",
        "gps_rows": "gps",
        "payment_rows": "money",
        "insurance_gps_payment_rows": "money",
        "payment_schedule_rows": "money",
        "guarantor_rows": "party",
        "identifier_register_rows": "party",
        "equipment_specification_rows": "equipment",
        "tranche_rows": "money",
    }
    filtered["tables"] = [
        table for table in document.get("tables", [])
        if table_categories.get(table.get("name")) in selected_categories
    ]
    selected_names = {item.get("name") for item in filtered["fields"]}
    selected_labels = {item.get("label_ru") for item in filtered["fields"]}
    filtered["warnings"] = [
        warning for warning in document.get("warnings", [])
        if (
            not warning.get("field")
            or warning.get("field") in selected_names
            or warning.get("field") in selected_labels
        )
    ]
    return filtered

@bp.get('/')
def index():
    return render_template(
        'index.html',
        supported_formats=', '.join(sorted(SUPPORTED_EXTENSIONS)),
        analysis_modes=ANALYSIS_MODES,
        field_categories=FIELD_CATEGORIES,
    )


@bp.post('/analyze')
def analyze():
    uploaded_files = request.files.getlist('documents')
    if not uploaded_files or all(not file.filename for file in uploaded_files):
        return render_template('index.html', error='Выберите хотя бы один файл.', supported_formats=', '.join(sorted(SUPPORTED_EXTENSIONS)), analysis_modes=ANALYSIS_MODES, field_categories=FIELD_CATEGORIES)

    selection = _selection_from_form(request.form)
    if selection.get('mode') == 'custom' and not selection.get('categories') and not selection.get('fields'):
        return render_template(
            'index.html', error='Выберите хотя бы одну категорию или поле для поиска.',
            supported_formats=', '.join(sorted(SUPPORTED_EXTENSIONS)),
            analysis_modes=ANALYSIS_MODES, field_categories=FIELD_CATEGORIES,
        )

    analysis_mode = request.form.get('analysis_mode', 'standard')
    if analysis_mode not in ANALYSIS_MODES:
        analysis_mode = 'standard'
    started_total = time.perf_counter()
    result_id = uuid.uuid4().hex
    source_root = _source_root(result_id)
    source_root.mkdir(parents=True, exist_ok=True)
    documents = []
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            for document_index, uploaded in enumerate(uploaded_files):
                if not uploaded.filename:
                    continue
                original_name = Path(uploaded.filename).name
                suffix = Path(original_name).suffix.lower()
                safe_stem = secure_filename(Path(original_name).stem) or 'document'
                filename = f'{uuid.uuid4().hex}_{safe_stem}{suffix}'
                if suffix not in SUPPORTED_EXTENSIONS:
                    return render_template('index.html', error=f'Формат {suffix} пока не поддерживается.', supported_formats=', '.join(sorted(SUPPORTED_EXTENSIONS)), analysis_modes=ANALYSIS_MODES, field_categories=FIELD_CATEGORIES)
                local_path = temp_path / filename
                uploaded.save(local_path)
                source_filename = f"{document_index:03d}{suffix}"
                shutil.copy2(local_path, source_root / source_filename)

                document_started = time.perf_counter()
                read_started = time.perf_counter()
                mode_dpi = ANALYSIS_MODES[analysis_mode]['dpi'] or current_app.config['OCR_DPI']
                read_result = read_document(
                    local_path,
                    ocr_languages=current_app.config['OCR_LANGUAGES'],
                    ocr_dpi=mode_dpi,
                    min_digital_chars=current_app.config['MIN_DIGITAL_TEXT_CHARS'],
                    ocr_cache_dir=Path(current_app.config['OCR_CACHE_FOLDER']),
                    analysis_mode=analysis_mode,
                )
                read_seconds = time.perf_counter() - read_started
                extract_started = time.perf_counter()
                classification = classify(read_result.full_text, uploaded.filename)
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
                fields, explicit_lease_amount = postprocess_fields(
                    read_result, classification.key, fields, tables
                )
                # Field post-processing can repair authoritative prices, models
                # and identifiers. Reconcile the equipment table only after
                # those final values are available, otherwise an early OCR
                # amount can survive in Excel while the document card is right.
                tables = improve_equipment_tables(read_result, fields, tables)
                tables = _normalise_tables(postprocess_tables(
                    read_result, classification.key, fields, tables, explicit_lease_amount
                ))
                fields, tables = apply_insurance_gps(read_result, classification.key, fields, tables)
                field_names_before_recovery = {
                    item.get("name") for item in fields
                    if item.get("value") not in (None, "", [])
                }
                fields = recover_missing_fields(read_result, classification.key, fields)
                field_names_after_recovery = {
                    item.get("name") for item in fields
                    if item.get("value") not in (None, "", [])
                }
                if field_names_after_recovery != field_names_before_recovery:
                    fields, tables = apply_insurance_gps(
                        read_result, classification.key, fields, tables
                    )
                fields, tables = enrich_all_readable_data(
                    read_result, classification.key, fields, tables
                )
                fields, tables = enrich_document_semantics(
                    read_result, classification.key, fields, tables
                )
                # Table post-processing and specialised insurance/GPS recovery
                # may replace a previously enriched asset table or add final
                # model/serial fields. Reconcile once more at the true end of
                # extraction so JSON, dossier summary and Excel see the same
                # equipment identity.
                tables = improve_equipment_tables(read_result, fields, tables)
                fields, tables = apply_stability_guard(fields, tables)
                fields = final_contract_hardening(read_result, classification.key, fields)
                fields = validate_fields(fields)
                fields = ensure_review_audit(fields)
                # Add evidence quotes before quality review so an explicitly
                # printed value is never reported as lacking direct evidence.
                fields = enrich_field_text_quotes(fields, read_result.pages)
                warnings = review_fields(classification.key, fields) + validation_warnings(fields)
                extract_seconds = time.perf_counter() - extract_started

                page_layouts = [
                    {
                        'page': page.page_number,
                        'width': page.page_width,
                        'height': page.page_height,
                        'words': page.layout_words,
                    }
                    for page in read_result.pages
                ]
                page_methods = [
                    {'page': page.page_number, 'method': page.extraction_method,
                     'char_count': page.char_count, 'quality': page.quality,
                     'cache_hit': page.cache_hit,
                     'layout_word_count': len(page.layout_words),
                     'analysis_profile': page.analysis_profile}
                    for page in read_result.pages
                ]
                fields = enrich_field_locations(fields, page_layouts)

                documents.append({
                    'filename': uploaded.filename,
                    'source_file': source_filename,
                    'preview_available': suffix in {'.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'},
                    'source_type': read_result.source_type,
                    'page_count': read_result.page_count,
                    'used_ocr': read_result.used_ocr,
                    'page_layouts': page_layouts,
                    'page_methods': page_methods,
                    'page_texts': [
                        {'page': page.page_number, 'text': page.text}
                        for page in read_result.pages
                    ],
                    'unresolved_pages': unresolved_pages(page_methods, classification.key),
                    'document_type': classification.key,
                    'document_type_label_ru': classification.label_ru,
                    'classification_confidence': classification.confidence,
                    'matched_keywords': classification.matched_keywords,
                    'classification_alternatives': classification.alternatives,
                    'fields': fields,
                    'tables': tables,
                    'warnings': warnings,
                    'timing': {
                        'reading_ocr_seconds': round(read_seconds, 2),
                        'extraction_seconds': round(extract_seconds, 2),
                        'total_seconds': round(time.perf_counter() - document_started, 2),
                        'cached_ocr_pages': sum(1 for page in read_result.pages if page.cache_hit),
                    },
                })
    except OCRUnavailableError as exc:
        shutil.rmtree(source_root, ignore_errors=True)
        return render_template('index.html', error=f'{exc} Установите Tesseract OCR и языки rus, kaz, eng.', supported_formats=', '.join(sorted(SUPPORTED_EXTENSIONS)), analysis_modes=ANALYSIS_MODES, field_categories=FIELD_CATEGORIES)
    except Exception as exc:
        shutil.rmtree(source_root, ignore_errors=True)
        return render_template('index.html', error=f'Ошибка обработки: {exc}', supported_formats=', '.join(sorted(SUPPORTED_EXTENSIONS)), analysis_modes=ANALYSIS_MODES, field_categories=FIELD_CATEGORIES)

    reconcile_client_party_names(documents)
    for document in documents:
        document["fields"] = ensure_review_audit(document.get("fields", []))
        document["warnings"] = review_fields(
            document.get("document_type", "unknown"), document.get("fields", [])
        ) + validation_warnings(document.get("fields", []))
    dossier = build_dossier_summary(documents, selection=selection)
    visible_documents = [_filter_document_for_selection(document, selection) for document in documents]
    result = {
        'result_id': result_id,
        'analysis': {
            'mode': analysis_mode,
            'mode_label_ru': ANALYSIS_MODES[analysis_mode]['label_ru'],
            'total_seconds': round(time.perf_counter() - started_total, 2),
            'collection': selection,
        },
        'documents': visible_documents,
        'dossier': dossier,
    }
    _save_result_bundle(result)
    return render_template(
        'results.html',
        result=result,
        field_value_for_form=field_value_for_form,
        field_categories=FIELD_CATEGORIES,
        document_types=DOCUMENT_TYPES,
        saved=False,
    )


@bp.get('/results/<result_id>')
def show_result(result_id: str):
    result = _load_result(result_id)
    return render_template(
        'results.html',
        result=result,
        field_value_for_form=field_value_for_form,
        field_categories=FIELD_CATEGORIES,
        document_types=DOCUMENT_TYPES,
        saved=request.args.get("saved") == "1",
    )


@bp.post('/review/<result_id>')
def review_result(result_id: str):
    result = _load_result(result_id)
    updated, _changed_count = apply_review(result, request.form)
    for document in updated.get("documents", []):
        document["fields"] = validate_fields(document.get("fields", []))
        document["fields"] = enrich_field_locations(
            document.get("fields", []),
            document.get("page_layouts", []),
        )
        document["unresolved_pages"] = unresolved_pages(
            document.get("page_methods", []),
            document.get("document_type", "unknown"),
        )
        document["warnings"] = review_fields(
            document.get("document_type", "unknown"),
            document.get("fields", []),
        ) + validation_warnings(document.get("fields", []))
    reconcile_client_party_names(updated.get("documents", []))
    for document in updated.get("documents", []):
        document["warnings"] = review_fields(
            document.get("document_type", "unknown"),
            document.get("fields", []),
        ) + validation_warnings(document.get("fields", []))
    collection = updated.get("analysis", {}).get("collection", {})
    if collection.get("mode") != "custom":
        updated["dossier"] = build_dossier_summary(updated.get("documents", []))
    _save_result_bundle(updated)
    return redirect(url_for("main.show_result", result_id=result_id, saved="1"))


@bp.get('/history')
def history():
    clients = list_clients(Path(current_app.config['RESULT_FOLDER']))
    return render_template('history.html', clients=clients)


@bp.get('/clients/<client_key>')
def client_card(client_key: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", client_key):
        abort(404)
    result_folder = Path(current_app.config['RESULT_FOLDER'])
    client = get_client(result_folder, client_key)
    if not client:
        abort(404)

    results = []
    for item in client.get("results", []):
        path = result_folder / f"{item.get('result_id')}.json"
        if not path.exists():
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        results.append(result)

    aggregate_documents = [
        document
        for result in results
        for document in result.get("documents", [])
    ]
    aggregate_dossier = build_dossier_summary(aggregate_documents)
    return render_template(
        'client.html',
        client=client,
        results=results,
        dossier=aggregate_dossier,
    )


@bp.get('/source/<result_id>/<int:document_index>')
def source_document(result_id: str, document_index: int):
    result = _load_result(result_id)
    path = _document_source_path(result, document_index)
    return send_file(path, as_attachment=False, download_name=result['documents'][document_index].get('filename') or path.name)


@bp.get('/preview/<result_id>/<int:document_index>/<int:page_number>.png')
def preview_page(result_id: str, document_index: int, page_number: int):
    result = _load_result(result_id)
    source_path = _document_source_path(result, document_index)
    document = result['documents'][document_index]
    layout = next(
        (item for item in document.get('page_layouts', []) if item.get('page') == page_number),
        None,
    )
    query = request.args.get('q', '')[:500]
    try:
        payload = render_source_page(source_path, page_number, layout=layout, query=query)
    except ValueError as exc:
        abort(400, description=str(exc))
    return send_file(io.BytesIO(payload), mimetype='image/png', max_age=0)



@bp.post('/reprocess-page/<result_id>/<int:document_index>/<int:page_number>')
def reprocess_page(result_id: str, document_index: int, page_number: int):
    result = _load_result(result_id)
    source_path = _document_source_path(result, document_index)
    document = result['documents'][document_index]
    if not document.get('page_texts'):
        abort(400, description='Этот результат создан старой версией. Загрузите документ заново.')

    profile = request.form.get('profile', 'accurate')
    languages = request.form.get('languages', current_app.config['OCR_LANGUAGES'])
    allowed_languages = {
        current_app.config['OCR_LANGUAGES'], 'rus', 'kaz', 'eng',
        'rus+kaz', 'rus+eng', 'kaz+eng', 'rus+kaz+eng',
    }
    if languages not in allowed_languages:
        languages = current_app.config['OCR_LANGUAGES']
    try:
        rotation = int(request.form.get('rotation', '0'))
        page_result = reprocess_source_page(
            source_path, page_number, profile=profile,
            languages=languages, rotation=rotation,
        )
    except (ValueError, RuntimeError) as exc:
        abort(400, description=str(exc))

    page_texts = document.setdefault('page_texts', [])
    existing_text = next((item for item in page_texts if item.get('page') == page_number), None)
    if existing_text is None:
        existing_text = {'page': page_number}
        page_texts.append(existing_text)
    existing_text['text'] = page_result['text']

    page_layouts = document.setdefault('page_layouts', [])
    layout = next((item for item in page_layouts if item.get('page') == page_number), None)
    if layout is None:
        layout = {'page': page_number}
        page_layouts.append(layout)
    layout.update({
        'width': page_result['width'], 'height': page_result['height'],
        'words': page_result['layout_words'],
    })

    page_methods = document.setdefault('page_methods', [])
    method = next((item for item in page_methods if item.get('page') == page_number), None)
    if method is None:
        method = {'page': page_number}
        page_methods.append(method)
    method.update({
        'method': 'ocr', 'char_count': page_result['char_count'],
        'quality': page_result['quality'], 'cache_hit': False,
        'layout_word_count': len(page_result['layout_words']),
        'reprocessed': True, 'ocr_profile': page_result['profile'],
        'ocr_profile_label_ru': page_result['profile_label_ru'],
        'ocr_languages': page_result['languages'],
        'rotation': page_result['rotation'],
    })

    read_result = rebuild_read_document(document)
    classification = classify(read_result.full_text, document.get('filename', ''))
    corrected_type = first_page_type(read_result, classification.key)
    if corrected_type != classification.key:
        classification.key = corrected_type
        classification.label_ru = TYPE_LABELS.get(corrected_type, classification.label_ru)
        classification.confidence = max(classification.confidence, 0.98)
    new_fields = parse_by_type(read_result, classification.key)
    new_fields = resolve_candidates(read_result, new_fields, classification.key)
    new_tables = _normalise_tables(extract_tables(read_result, classification.key))
    new_fields = improve_fields(read_result, classification.key, new_fields, new_tables)
    new_tables = improve_equipment_tables(read_result, new_fields, new_tables)
    new_fields = improve_fields(read_result, classification.key, new_fields, new_tables)
    new_fields, explicit_lease_amount = postprocess_fields(
        read_result, classification.key, new_fields, new_tables
    )
    new_tables = improve_equipment_tables(read_result, new_fields, new_tables)
    new_tables = _normalise_tables(postprocess_tables(
        read_result, classification.key, new_fields, new_tables, explicit_lease_amount
    ))
    new_fields, new_tables = apply_insurance_gps(read_result, classification.key, new_fields, new_tables)
    new_fields = recover_missing_fields(read_result, classification.key, new_fields)
    new_fields, new_tables = apply_insurance_gps(read_result, classification.key, new_fields, new_tables)
    new_tables = improve_equipment_tables(read_result, new_fields, new_tables)
    new_fields, new_tables = apply_stability_guard(new_fields, new_tables)
    new_fields = final_contract_hardening(read_result, classification.key, new_fields)
    new_fields = validate_fields(new_fields)
    new_fields = merge_manual_fields(new_fields, document.get('fields', []))
    new_fields = enrich_field_locations(new_fields, page_layouts)
    document['fields'] = new_fields
    document['tables'] = new_tables
    document['document_type'] = classification.key
    document['document_type_label_ru'] = classification.label_ru
    document['classification_confidence'] = classification.confidence
    document['matched_keywords'] = classification.matched_keywords
    document['classification_alternatives'] = classification.alternatives
    document['used_ocr'] = True
    document['unresolved_pages'] = unresolved_pages(page_methods, classification.key)
    document['warnings'] = review_fields(classification.key, new_fields) + validation_warnings(new_fields)
    document.setdefault('reprocessing_history', []).append({
        'page': page_number, 'profile': profile, 'languages': languages,
        'rotation': rotation, 'quality': page_result['quality'],
        'char_count': page_result['char_count'],
    })

    collection = result.get("analysis", {}).get("collection", {})
    if collection.get("mode") == "custom":
        result["documents"][document_index] = _filter_document_for_selection(document, collection)
    else:
        result['dossier'] = build_dossier_summary(result.get('documents', []))
    _save_result_bundle(result)
    return redirect(url_for('main.show_result', result_id=result_id, saved='1'))

@bp.get('/download/<result_id>/<kind>')
def download(result_id: str, kind: str):
    extension = {'json': 'json', 'excel': 'xlsx'}.get(kind)
    if extension is None or not result_id.isalnum():
        abort(404)
    path = _result_path(result_id, extension)
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f'результат_анализа.{extension}')
