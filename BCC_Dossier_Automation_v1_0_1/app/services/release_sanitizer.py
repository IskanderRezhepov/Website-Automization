from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

HEADER_TOKENS = (
    'үлгісі комплектация', 'модель комплектация', 'модель / комплектация',
    'техникалық сипаттама', 'технические характеристики', 'наименование товара',
    'марка модель', 'марка / модель', 'год выпуска vin', 'шығарылған жылы vin',
    'комплектация модель', 'моделі комплектация',
)
BAD_EQUIPMENT_PROSE = (
    'техникасының суреті', 'техниканың суреті', 'суреті', 'изображение техники',
    'техника не определено', 'техника не определён', 'техника unknown',
    'фотография техники', 'фото техники', 'технические характеристики',
    'техникалық сипаттама', 'үлгісі комплектация', 'модель комплектация',
)
DERIVATIVE_TOKENS = ('акт к договор', 'дополнительное соглашение', 'допик', 'приложение')
BCC_BIN = '020140001503'


def _text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _digits(value: Any) -> str:
    return re.sub(r'\D', '', str(value or ''))


def _full_text(document: dict) -> str:
    chunks: list[str] = []
    for page in document.get('page_texts', []):
        if isinstance(page, dict):
            chunks.append(str(page.get('text') or ''))
        else:
            chunks.append(str(page or ''))
    if not chunks:
        for f in document.get('fields', []):
            chunks.extend([str(f.get('quote') or ''), str(f.get('raw_value') or '')])
    return '\n'.join(chunks)


def _field(document: dict, *names: str) -> dict | None:
    wanted = {n.casefold() for n in names}
    candidates = [
        f for f in document.get('fields', [])
        if str(f.get('name') or '').casefold() in wanted
        and f.get('value') not in (None, '', [])
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda f: (
            0 if str(f.get('status') or '').casefold() in {'rejected', 'candidate'} else 1,
            float(f.get('confidence') or 0),
        ),
    )


def _upsert_field(
    document: dict,
    name: str,
    label_ru: str,
    value: Any,
    *,
    quote: str = '',
    page: int | None = None,
    confidence: float = 0.995,
    notes: str = '',
) -> dict:
    fields = document.setdefault('fields', [])
    existing = next((f for f in fields if str(f.get('name') or '').casefold() == name.casefold()), None)
    if existing is None:
        existing = {'name': name, 'label_ru': label_ru}
        fields.append(existing)
    elif existing.get('value') != value:
        existing.setdefault('original_value', existing.get('value'))
    existing.update({
        'value': value,
        'normalized_value': value,
        'confidence': max(float(existing.get('confidence') or 0), confidence),
        'status': 'corrected',
        'validation': {'valid': True, 'message': 'Подтверждено финальной контекстной проверкой.'},
    })
    if quote:
        existing['quote'] = quote
    if page:
        existing['page'] = page
    if notes:
        existing['notes'] = notes
    return existing


def _page_for_text(document: dict, needle: str) -> int | None:
    if not needle:
        return None
    n = needle.casefold()
    for page in document.get('page_texts', []):
        if not isinstance(page, dict):
            continue
        if n in str(page.get('text') or '').casefold():
            try:
                return int(page.get('page') or 1)
            except Exception:
                return 1
    return None


def _looks_real_identifier(value: Any) -> bool:
    return len(_digits(value)) == 12


def _party_core(name: Any) -> str:
    text = _text(name)
    text = re.sub(
        r'^(?:товарищество\s+с\s+ограниченной\s+ответственностью|индивидуальный\s+предприниматель|тоо|ип|жшс)\s*',
        '', text, flags=re.I,
    )
    text = text.strip(' «»".,;:-')
    return text


def _name_occurrences(full: str, party_name: str) -> list[tuple[int, int]]:
    core = _party_core(party_name)
    if not core:
        return []
    candidates = [core]
    # For longer legal names, also allow the content inside quotes and a compact
    # sequence of significant words. This survives OCR line wrapping.
    quoted = re.findall(r'[«"]([^»"]{2,120})[»"]', _text(party_name))
    candidates.extend(quoted)
    words = [w for w in re.findall(r'[A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺһ0-9]+', core) if len(w) >= 2]
    if len(words) >= 2:
        candidates.append(r'\s+'.join(re.escape(w) for w in words))

    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        if not candidate:
            continue
        pattern = candidate if '\\s+' in candidate else re.escape(candidate)
        for m in re.finditer(pattern, full, re.I):
            if m.span() not in seen:
                seen.add(m.span())
                spans.append(m.span())
    return spans


def _extract_lessee_identifier(document: dict, party_name: str) -> tuple[str, str, int | None] | None:
    """Bind a 12-digit IIN/BIN to the *same party name* and lessee role.

    This intentionally does not trust an upstream field called lessee_iin_bin,
    because OCR/table logic can mislabel the buyer's (BCC Leasing) BIN. The legal
    party name and identifier must occur together in source text, with lessee role
    evidence nearby.
    """
    full = _full_text(document)
    if not full or not party_name:
        return None
    spans = _name_occurrences(full, party_name)

    role_re = re.compile(r'Лизингополучател\w*|Лизинг\s+алушы\w*', re.I)
    best: tuple[float, str, str, int | None] | None = None
    if not spans:
        # Conservative OCR fallback: accept a non-BCC identifier only when it is
        # very close to an explicit lessee-role phrase and not closer to a seller
        # or buyer role. This handles spelling/transliteration differences in the
        # party name without guessing among several 12-digit numbers.
        fallback: list[tuple[float, str, str, int | None]] = []
        for m in re.finditer(r'(?<!\d)(\d{12})(?!\d)', full):
            identifier = m.group(1)
            if identifier == BCC_BIN and 'bcc leasing' not in party_name.casefold():
                continue
            left = max(0, m.start() - 420)
            right = min(len(full), m.end() + 520)
            local = full[left:right]
            roles = list(role_re.finditer(local))
            if not roles:
                continue
            id_local = m.start() - left
            role_distance = min(abs(r.start() - id_local) for r in roles)
            if role_distance > 360:
                continue
            other_roles = list(re.finditer(r'Продавец|Сатушы|Покупатель|Сатып\s+алушы', local, re.I))
            other_distance = min((abs(r.start() - id_local) for r in other_roles), default=10_000)
            if other_distance + 40 < role_distance:
                continue
            score = 900 - role_distance
            prefix = full[max(0, m.start()-45):m.start()]
            if re.search(r'(?:БИН|ИИН|БСН|ЖСН)\s*$', prefix, re.I):
                score += 250
            quote = _text(full[max(0, m.start()-300):min(len(full), m.end()+420)])
            fallback.append((score, identifier, quote, _page_for_text(document, identifier)))
        if fallback:
            fallback.sort(key=lambda x: x[0], reverse=True)
            if len(fallback) == 1 or fallback[0][0] >= fallback[1][0] + 120:
                winner = fallback[0]
                return winner[1], winner[2], winner[3]
        return None
    for start, end in spans:
        center = (start + end) // 2
        left = max(0, start - 800)
        right = min(len(full), end + 1000)
        window = full[left:right]
        for m in re.finditer(r'(?<!\d)(\d{12})(?!\d)', window):
            identifier = m.group(1)
            absolute = left + m.start()
            distance = abs(absolute - center)
            if distance > 850:
                continue
            # The BCC Leasing BIN is never the dossier client when the chosen
            # client name is another party.
            if identifier == BCC_BIN and 'bcc leasing' not in party_name.casefold():
                continue
            score = 1200 - min(distance, 1000)
            local_left = max(0, absolute - 520)
            local_right = min(len(full), absolute + 720)
            local = full[local_left:local_right]
            if role_re.search(local):
                score += 500
            # Explicit IIN/BIN label right next to the number is strong evidence.
            prefix = full[max(0, absolute - 45):absolute]
            if re.search(r'(?:БИН|ИИН|БСН|ЖСН)\s*$', prefix, re.I):
                score += 300
            # Same textual line/paragraph as party name.
            between = full[min(center, absolute):max(center, absolute)]
            if '\n\n' not in between and len(between) < 260:
                score += 250
            quote = full[max(0, min(start, absolute) - 120):min(len(full), max(end, absolute + 12) + 220)]
            page = _page_for_text(document, identifier)
            candidate = (score, identifier, _text(quote), page)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best and best[0] >= 700:
        return best[1], best[2], best[3]
    return None


def _clear_resolved_client_warnings(result: dict) -> None:
    client = result.get('client') or {}
    if not _text(client.get('name')) or len(_digits(client.get('iin_bin'))) != 12:
        return
    noise = (
        'иин/бин клиента не определ',
        'иин/бин клиента не найден',
        'наименование или иин/бин клиента не подтвержден',
    )
    for document in result.get('documents', []):
        cleaned = []
        for warning in document.get('warnings', []):
            if isinstance(warning, dict):
                message = _text(warning.get('message_ru') or warning.get('message')).casefold()
            else:
                message = _text(warning).casefold()
            if any(token in message for token in noise):
                continue
            cleaned.append(warning)
        document['warnings'] = cleaned
        if 'blocking_warnings' in document:
            document['blocking_warnings'] = [
                w for w in document.get('blocking_warnings', [])
                if not any(token in _text(w.get('message_ru') or w.get('message') if isinstance(w, dict) else w).casefold() for token in noise)
            ]


def _reject_bad_colors(document: dict) -> None:
    for f in document.get('fields', []):
        name = str(f.get('name') or '').casefold()
        label = str(f.get('label_ru') or '').casefold()
        if 'color' not in name and 'цвет' not in label and 'түс' not in label:
            continue
        value = _text(f.get('value'))
        low = value.casefold()
        if not value:
            continue
        bad = (
            len(value) > 50
            or any(t in low for t in ('цветовосприяти', 'по каталогу', 'передач', 'изображен', 'может отличаться'))
            or len(value.split()) > 7
        )
        if bad:
            f['original_value'] = f.get('value')
            f['value'] = 'Не определено'
            f['status'] = 'rejected'
            f['validation'] = {'valid': False, 'message': 'Исключён фрагмент текста, не являющийся значением цвета.'}


def _asset_identity(row: dict) -> str:
    parts = []
    for key, value in row.items():
        k = str(key).casefold()
        if any(token in k for token in ('vin','шасси','chassis','serial','серийн','model','модель','brand','марка','manufacturer','equipment_name','наименование','asset_type','equipment_type','вид техник')):
            parts.append(_text(value))
    return ' '.join(parts).strip()


def _bad_equipment_value(value: Any) -> bool:
    text = _text(value)
    low = text.casefold()
    if not text or text.casefold() in {'не определено', 'не определён'}:
        return False
    if any(token in low for token in HEADER_TOKENS + BAD_EQUIPMENT_PROSE):
        return True
    # Technical/specification labels are not asset identities.  Future PDFs often
    # flatten a characteristics table into rows and those labels can otherwise be
    # mistaken for an equipment type/model.
    technical_labels = (
        'колесная формула', 'дөңгелек формула', 'грузоподъемность шасси',
        'грузоподъемность', 'полная масса', 'разрешенная масса',
        'колесная база', 'дөңгелек база', 'общая длина шасси',
        'ширина шасси', 'высота шасси', 'снаряженная масса',
        'технические характеристики', 'техникалық сипаттама',
    )
    if any(token in low for token in technical_labels):
        return True
    if re.fullmatch(r'[\d\s.,/-]+', text):
        return True
    if low in {'техники', 'техника', 'автомобиля', 'автомобиль', 'шасси'}:
        return True
    if len(text) > 90 or len(text.split()) > 12:
        return True
    # Phrases ending in punctuation/grammar rather than a noun/model are usually
    # prose fragments, not an equipment class.
    if re.search(r'(?:суреті|изображени[ея]|фотографи[ия]|описани[ея])\s*[;,.]?$', low):
        return True
    return False


def _is_header_asset(row: dict) -> bool:
    identity = _asset_identity(row)
    low = identity.casefold()
    if not identity:
        return True
    if _bad_equipment_value(identity):
        return True
    label_hits = sum(token in low for token in ('vin', 'год выпуска', 'количество', 'цена', 'стоимость', 'комплектация'))
    concrete_vin = bool(re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', identity, re.I))
    mixed_model = bool(re.search(r'\b(?=[A-ZА-Я0-9./_-]*[A-ZА-Я])(?=[A-ZА-Я0-9./_-]*\d)[A-ZА-Я0-9][A-ZА-Я0-9./_-]{2,}\b', identity, re.I))
    if label_hits >= 2 and not (concrete_vin or mixed_model):
        return True
    return False


def _safe_int(value: Any) -> int | None:
    text = _text(value).replace(' ', '')
    if not re.fullmatch(r'\d{1,4}', text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _authoritative_contract_total(document: dict) -> Decimal | None:
    """Return the best explicit whole-contract amount, if available."""
    preferred = (
        'total_amount_kzt', 'purchase_total_kzt', 'contract_total_kzt',
        'lease_object_value_kzt', 'equipment_total_kzt',
    )
    for name in preferred:
        f = _field(document, name)
        if not f:
            continue
        amount = _parse_money(_text(f.get('value')))
        if amount is not None and amount > 0:
            return amount
    return None


def _money_keys(row: dict) -> tuple[list[str], list[str]]:
    unit_keys: list[str] = []
    total_keys: list[str] = []
    for key in row:
        low = str(key).casefold()
        if any(t in low for t in ('unit_price', 'цена за единицу', 'цена/ед', 'баға')):
            unit_keys.append(key)
        elif any(t in low for t in ('total_amount', 'общая стоимость', 'стоимость позиции', 'сумма', 'итого')):
            total_keys.append(key)
    return unit_keys, total_keys


def _repair_concatenated_equipment_price(document: dict, row: dict, qty: int | None) -> None:
    """Repair only a high-confidence table/OCR concatenation artifact.

    Example seen in production: quantity ``1`` + amount ``15 490 000`` became
    ``115490000``.  We only correct when the erroneous numeric string is exactly
    ``<quantity><authoritative contract total>``.  This deliberately avoids
    overwriting legitimate line-item prices in multi-asset contracts.
    """
    if qty is None or qty < 1 or qty > 9:
        return
    authoritative = _authoritative_contract_total(document)
    if authoritative is None or authoritative != authoritative.to_integral_value():
        return
    auth_digits = str(int(authoritative))
    bad_digits = f"{qty}{auth_digits}"
    unit_keys, total_keys = _money_keys(row)
    repaired = False
    for key in unit_keys + total_keys:
        value = _parse_money(_text(row.get(key)))
        if value is None or value != value.to_integral_value():
            continue
        if str(int(value)) == bad_digits:
            row[key] = int(authoritative)
            repaired = True
    if repaired:
        row['status'] = 'corrected'
        note = _text(row.get('notes'))
        extra = 'Цена исправлена: исключена склейка количества с суммой договора.'
        row['notes'] = f"{note} {extra}".strip()


def _sanitize_equipment(document: dict) -> None:
    # Reject bad headline fields as well as bad structured rows.
    for f in document.get('fields', []):
        name = str(f.get('name') or '').casefold()
        label = str(f.get('label_ru') or '').casefold()
        if name in {'equipment_model','vehicle_model','model','equipment_type','vehicle_type','asset_type'} or any(
            token in label for token in ('марка / модель техники','вид техники','модель техники')
        ):
            if _bad_equipment_value(f.get('value')):
                f.setdefault('original_value', f.get('value'))
                f['value'] = 'Не определено'
                f['normalized_value'] = 'Не определено'
                f['status'] = 'rejected'
                f['confidence'] = min(float(f.get('confidence') or 1), 0.2)
                f['validation'] = {'valid': False, 'message': 'Исключён заголовок/описательный фрагмент вместо модели или вида техники.'}

    for table in document.get('tables', []):
        name = str(table.get('name') or '').casefold()
        if name != 'asset_vin_rows' and not any(t in name for t in ('equipment','asset','vehicle','transport','техник')):
            continue
        cleaned = []
        seen = set()
        for row in table.get('rows', []):
            if not isinstance(row, dict) or _is_header_asset(row):
                continue
            # A bad model/type in an otherwise valid row should be blanked rather
            # than allowing the bad label to contaminate the summary.
            for key in list(row):
                k = str(key).casefold()
                if any(t in k for t in ('model','модель','equipment_type','asset_type','vehicle_type','вид техник')) and _bad_equipment_value(row.get(key)):
                    row[key] = None
            qty_key = next((k for k in row if any(t in str(k).casefold() for t in ('quantity','qty','колич','саны'))), None)
            qty = _safe_int(row.get(qty_key)) if qty_key is not None else None
            _repair_concatenated_equipment_price(document, row, qty)
            identity = _asset_identity(row)
            concrete_vin = bool(re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', identity, re.I))
            normalized_identity = re.sub(r'[^a-zа-яәіңғүұқөһ0-9]+', ' ', identity.casefold()).strip()
            if normalized_identity in {'техника', 'техника не определено', 'техника не определён', 'unknown'} and not concrete_vin:
                continue
            if qty is not None and qty > 100 and not concrete_vin:
                continue
            # Rows with no remaining concrete equipment identity are noise.
            if not identity and not concrete_vin:
                continue
            signature = tuple(sorted((str(k), _text(v).casefold()) for k, v in row.items() if k not in {'page','confidence','status','notes'}))
            if signature in seen:
                continue
            seen.add(signature)
            cleaned.append(row)
        table['rows'] = cleaned
        table['row_count'] = len(cleaned)


def _parse_money(value: str) -> Decimal | None:
    text = value.replace('\u00a0', ' ').strip()
    text = re.sub(r'[^0-9,.-]', '', text.replace(' ', ''))
    if not text:
        return None
    if text.count(',') == 1 and '.' not in text:
        text = text.replace(',', '.')
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _recover_purchase_total(document: dict) -> None:
    if str(document.get('document_type') or '') != 'purchase_contract':
        return
    full = _full_text(document)
    if not full:
        return
    patterns = (
        r'(?:Общая\s+стоимость\s+(?:настоящего\s+)?Договора|Общая\s+стоимость)[^\d]{0,80}(?:составляет\s*)?(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
        r'Цена\s+Автомобил(?:я|ей)[^\d]{0,80}составляет\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
        r'Стоимость\s+Автомобил(?:я|ей)[^\d]{0,80}(?:составляет\s*)?(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
        r'(?:БАРЛЫҒЫ\s*/?\s*)?ИТОГО\s*[:：]\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
    )
    candidates: list[tuple[int, Decimal, str]] = []
    for rank, pattern in enumerate(patterns):
        for m in re.finditer(pattern, full, re.I | re.S):
            amount = _parse_money(m.group(1))
            if amount is None or amount < Decimal('100000') or amount > Decimal('1000000000000'):
                continue
            # Earlier semantic patterns are preferred; within the same rank use
            # the largest explicit total to avoid an advance/payment tranche.
            candidates.append((100 - rank * 10, amount, _text(m.group(0))))
    if not candidates:
        return
    max_rank = max(c[0] for c in candidates)
    same_rank = [c for c in candidates if c[0] == max_rank]
    _, amount, quote = max(same_rank, key=lambda c: c[1])
    value: int | float = int(amount) if amount == amount.to_integral_value() else float(amount)
    _upsert_field(
        document, 'total_amount_kzt', 'Общая стоимость договора, тенге', value,
        quote=quote, page=_page_for_text(document, quote[:80]),
        notes='Стоимость восстановлена по явной формулировке цены/общей стоимости договора.',
    )
    # Remove obsolete missing-field warning after a confirmed recovery.
    document['warnings'] = [
        w for w in document.get('warnings', [])
        if 'общая стоимость договора' not in _text(w.get('field') if isinstance(w, dict) else '').casefold()
        and 'общая стоимость договора' not in _text(w.get('message_ru') or w.get('message') if isinstance(w, dict) else w).casefold()
    ]


def _bind_client_to_lessee(result: dict) -> None:
    # Use the already resolved legal client/lessee name, then independently bind
    # its identifier from source text. This prevents a mislabeled buyer BIN from
    # entering the final summary.
    client = result.setdefault('client', {})
    current_name = _text(client.get('name'))
    role_noise = ('алатын', 'аталатын', 'деп атал', 'лизинг алушы', 'лизингополучател')
    if any(token in current_name.casefold() for token in role_noise):
        current_name = ''
    name_candidates: list[tuple[int, str, dict]] = []
    for document in result.get('documents', []):
        dtype = str(document.get('document_type') or '')
        if dtype not in {'lease_contract', 'purchase_contract'}:
            continue
        name_f = _field(document, 'lessee_name', 'borrower_name', 'customer_name', 'client_name')
        name = _text(name_f.get('value')) if name_f else ''
        if name and 'bcc leasing' not in name.casefold() and not any(token in name.casefold() for token in role_noise):
            score = 3 if dtype == 'lease_contract' else 2
            if current_name and _party_core(current_name).casefold() == _party_core(name).casefold():
                score += 5
            name_candidates.append((score, name, document))

    if current_name and 'bcc leasing' not in current_name.casefold():
        # Prefer an exact current client name and find a document that contains it.
        for document in result.get('documents', []):
            if _name_occurrences(_full_text(document), current_name):
                name_candidates.append((10, current_name, document))

    if not name_candidates:
        return
    name_candidates.sort(key=lambda x: x[0], reverse=True)
    selected_name = name_candidates[0][1]

    best_id: tuple[int, str, str, int | None, dict] | None = None
    for score, name, document in name_candidates:
        identity = _extract_lessee_identifier(document, name)
        if not identity:
            continue
        identifier, quote, page = identity
        candidate = (score, identifier, quote, page, document)
        if best_id is None or candidate[0] > best_id[0]:
            best_id = candidate

    client['name'] = selected_name
    client['role'] = 'lessee_iin_bin'
    client['role_label_ru'] = 'Лизингополучатель'
    if best_id:
        _, identifier, quote, page, document = best_id
        client['iin_bin'] = identifier
        client['client_key'] = identifier
        client['evidence'] = {
            'filename': document.get('filename'),
            'page': page,
            'field': 'ИИН/БИН — Лизингополучатель',
        }
        _upsert_field(
            document, 'lessee_iin_bin', 'ИИН/БИН — Лизингополучатель', identifier,
            quote=quote, page=page,
            notes='Идентификатор привязан к тому же наименованию Лизингополучателя в исходном тексте.',
        )



def _repair_leasing_party_identifier_labels(result: dict) -> None:
    """Keep lessor and lessee identifiers semantically separate in lease sheets."""
    client = result.get('client') or {}
    client_id = _digits(client.get('iin_bin'))
    client_name = _text(client.get('name'))
    if len(client_id) != 12 or not client_name or 'bcc leasing' in client_name.casefold():
        return
    for document in result.get('documents', []):
        if str(document.get('document_type') or '') != 'lease_contract':
            continue
        full = _full_text(document)
        # Always make the resolved dossier client explicit as lessee.
        identity = _extract_lessee_identifier(document, client_name)
        if identity and identity[0] == client_id:
            identifier, quote, page = identity
            _upsert_field(
                document, 'lessee_iin_bin', 'ИИН/БИН — Лизингополучатель', identifier,
                quote=quote, page=page,
                notes='Финальная привязка ИИН/БИН к Лизингополучателю.',
            )
        # If an upstream lessor field accidentally contains the client ID, repair
        # it only when BCC Leasing's known BIN is explicitly present in the source.
        lessor = _field(document, 'lessor_iin_bin')
        if lessor and _digits(lessor.get('value')) == client_id and BCC_BIN in full:
            lessor['original_value'] = lessor.get('value')
            lessor['value'] = BCC_BIN
            lessor['normalized_value'] = BCC_BIN
            lessor['label_ru'] = 'ИИН/БИН — Лизингодатель'
            lessor['status'] = 'corrected'
            lessor['confidence'] = max(float(lessor.get('confidence') or 0), 0.995)
            lessor['validation'] = {'valid': True, 'message': 'Привязано к Лизингодателю BCC Leasing по исходному тексту.'}
            lessor['notes'] = 'Исправлена ошибочная подстановка ИИН/БИН Лизингополучателя в поле Лизингодателя.'


def _suppress_resolved_secondary_warnings(result: dict) -> None:
    """Drop cosmetic warnings when the final value is already confidently resolved."""
    for document in result.get('documents', []):
        fields = {str(f.get('name') or '').casefold(): f for f in document.get('fields', [])}
        seller_ok = bool(_text((fields.get('seller_name') or {}).get('value')))
        client_ok = bool(_text((result.get('client') or {}).get('name'))) and len(_digits((result.get('client') or {}).get('iin_bin'))) == 12
        equipment_identified = False
        for table in document.get('tables', []):
            name = str(table.get('name') or '').casefold()
            if name == 'asset_vin_rows' or any(t in name for t in ('equipment','asset','vehicle','transport','техник')):
                if any(isinstance(r, dict) and not _is_header_asset(r) for r in table.get('rows', [])):
                    equipment_identified = True
                    break
        def keep_warning(warning: Any) -> bool:
            message = _text(warning.get('message_ru') or warning.get('message') if isinstance(warning, dict) else warning).casefold()
            field = _text(warning.get('field') if isinstance(warning, dict) else '').casefold()
            if seller_ok and ('кавыч' in message and 'продав' in (field + ' ' + message)):
                return False
            # Seller is not a primary party of the leasing agreement itself. A
            # noisy seller-name candidate from a linked purchase reference must
            # not make an otherwise resolved lease result look unusable.
            if str(document.get('document_type') or '') == 'lease_contract' and 'продав' in (field + ' ' + message):
                if any(t in message for t in ('не пройдена проверка наименования', 'похоже на определение роли', 'не подтвержден')):
                    return False
            if client_ok and any(t in message for t in ('иин/бин клиента не', 'наименование клиента не подтверж')):
                return False
            if equipment_identified and any(t in message for t in (
                'вид или модель предмета лизинга не определ',
                'вид техники определён слишком общо',
                'марка / модель техники: значение требует ручного подтверждения',
            )):
                return False
            return True
        document['warnings'] = [w for w in document.get('warnings', []) if keep_warning(w)]
        if 'blocking_warnings' in document:
            document['blocking_warnings'] = [w for w in document.get('blocking_warnings', []) if keep_warning(w)]
        if 'review_notes' in document:
            document['review_notes'] = [w for w in document.get('review_notes', []) if keep_warning(w)]


def _reject_derivative_contract_identity(document: dict) -> None:
    for f in document.get('fields', []):
        if str(f.get('name') or '').casefold() not in {'purchase_contract_number','lease_contract_number','linked_purchase_contract'}:
            continue
        quote = _text(f.get('quote')).casefold()
        if any(token in quote for token in DERIVATIVE_TOKENS) and not re.search(r'(?<!к )\bдоговор(?:а|у|ом)?\b', quote):
            f['status'] = 'candidate'
            f.setdefault('validation', {'valid': False, 'message': 'Номер найден только в контексте производного документа (акт/допсоглашение/приложение).'})


def apply_release_sanitizer(result: dict, version: str = '32.0') -> None:
    for document in result.get('documents', []):
        _reject_bad_colors(document)
        _sanitize_equipment(document)
        _recover_purchase_total(document)
        _reject_derivative_contract_identity(document)
    _bind_client_to_lessee(result)
    _repair_leasing_party_identifier_labels(result)
    _clear_resolved_client_warnings(result)
    _suppress_resolved_secondary_warnings(result)
    analysis = result.setdefault('analysis', {})
    analysis['quality_pipeline_version'] = version
    analysis['release_profile'] = 'future-safe-clean-v32'

# ---------------------------------------------------------------------------
# v33 adaptive future-document guard
# ---------------------------------------------------------------------------

_REAL_ESTATE_TOKENS = (
    'недвижим', 'нежилое помещение', 'жилое помещение', 'кадастров',
    'рыночная стоимость недвижимости', 'технические характеристики недвижимости',
    'жылжымайтын мүлік', 'кадастр', 'площадь помещения',
)
_REGISTRATION_TOKENS = (
    'уведомление о государственной регистрации', 'государственной регистрации прав',
    'кадастровый номер', 'регистрационное уведомление', 'тіркеу туралы хабарлама',
)
_EQUIPMENT_TOKENS = (
    'спецификация', 'vin', 'год выпуска', 'модель', 'марка техники',
    'наименование товара', 'техникалық сипаттама', 'технические характеристики автомобиля',
)
_MODEL_STOP_TOKENS = (
    ' осы шарт', ' шарт бойынша', ' бойынша', ' по настоящему', ' настоящего договора',
    ' технические характеристики', ' техникалық сипаттама', ' год выпуска', ' габарит',
    ' колесная формула', ' дөңгелек', ' двигатель', ' қозғалтқыш', ' количество',
    ' стоимость', ' цена за', ' общая стоимость', ' приложение', ' қосымша', ' doc id',
)
_SPEC_HEADER_NOISE = (
    'спецификация', 'наименование товара', 'тауардың атауы', 'количество единиц',
    'бірліктер саны', 'цена за единицу', 'бағасы', 'общая стоимость', 'жалпы құны',
    'год выпуска', 'шығарылған', 'страна производитель', 'цвет', 'түсі',
    '№ п/п', 'р/т №', 'количество', 'единиц, шт', 'ққс', 'ндс',
)
_KNOWN_BRANDS = (
    'HYUNDAI', 'ГАЗ', 'GAZ', 'FAW', 'ТОНАР', 'TONAR', 'KIA', 'JCB', 'GAC',
    'SHACMAN', 'CHITIAN', 'XCMG', 'SDLG', 'LIUGONG', 'HOWO', 'SITRAK',
    'MERCEDES', 'TOYOTA', 'LEXUS', 'GEELY', 'CHERY', 'HAVAL', 'JAC', 'КАМАЗ',
    'МАЗ', 'УРАЛ', 'VOLVO', 'MAN', 'SCANIA', 'IVECO', 'FORD', 'CHEVROLET',
)


def _document_family(document: dict) -> str:
    dtype = str(document.get('document_type') or '').casefold()
    text = _full_text(document).casefold()
    if dtype == 'real_estate_registration_notice' or any(t in text for t in _REGISTRATION_TOKENS):
        if 'договор финансового лизинга' not in text[:5000] and 'қаржылық лизинг шарты' not in text[:5000]:
            return 'registration'
    real_score = sum(1 for t in _REAL_ESTATE_TOKENS if t in text)
    equipment_score = sum(1 for t in _EQUIPMENT_TOKENS if t in text)
    if real_score >= 2 and ('договор финансового лизинга' in text or dtype == 'lease_contract'):
        return 'real_estate'
    if equipment_score >= 2 or dtype == 'purchase_contract':
        return 'equipment'
    return 'other'


def _money_number(raw: str) -> int | float | None:
    value = _parse_money(raw)
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _recover_real_estate_semantics(document: dict) -> None:
    if _document_family(document) != 'real_estate':
        return
    full = _full_text(document)
    # The acquisition/leasing cost is legally distinct from an appraisal/market value.
    lease_patterns = (
        r'Стоимость\s+Предмета\s+лизинга\s+(?:составляет|равна)\s*(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
        r'Лизинг\s+затының\s+құны[^\d]{0,120}(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
    )
    for pattern in lease_patterns:
        m = re.search(pattern, full, re.I | re.S)
        if not m:
            continue
        value = _money_number(m.group(1))
        if value is None:
            continue
        _upsert_field(
            document, 'lease_asset_value_kzt', 'Стоимость предмета лизинга, тенге', value,
            quote=_text(m.group(0)), page=_page_for_text(document, m.group(1)), confidence=0.999,
            notes='v33: стоимость предмета лизинга взята из прямой формулировки и не смешивается с рыночной оценкой.',
        )
        break

    # If an explicit real-estate appendix has two totals, keep the last one as
    # market/appraisal value only when the header explicitly says market value.
    if re.search(r'Рыночная\s+стоимость', full, re.I):
        region_match = re.search(r'Технические\s+характеристики\s+недвижимости(.{0,7000})', full, re.I | re.S)
        region = region_match.group(1) if region_match else full
        itogo = re.search(
            r'(?:ЖИЫНЫ\s*/?\s*ИТОГО|ИТОГО)\s*:\s*[^\n]*?(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)\s+(\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{1,2})?)',
            region, re.I,
        )
        if itogo:
            market = _money_number(itogo.group(2))
            if market is not None:
                _upsert_field(
                    document, 'market_value_kzt', 'Рыночная стоимость недвижимости, тенге', market,
                    quote=_text(itogo.group(0)), page=_page_for_text(document, itogo.group(2)), confidence=0.997,
                    notes='v33: рыночная стоимость недвижимости хранится отдельно от стоимости предмета лизинга.',
                )


def _strip_model_tail(value: Any) -> str:
    text = _text(value)
    if not text:
        return ''
    low = ' ' + text.casefold()
    cut = len(text)
    for token in _MODEL_STOP_TOKENS:
        pos = low.find(token)
        if pos >= 0:
            # ``low`` has one artificial leading space.
            cut = min(cut, max(0, pos - 1))
    text = text[:cut].strip(' ,.;:-/|')
    # Label-like values are never models.
    if re.search(r'^(?:колесн\w*\s+формул|дөңгелек|модель\s+двигател|техникалық\s+сипаттама|технические\s+характеристики)$', text, re.I):
        return ''
    return text[:100]


def _clean_model_fields(document: dict) -> None:
    for f in document.get('fields', []):
        name = str(f.get('name') or '').casefold()
        if name not in {'equipment_model', 'vehicle_model', 'model'}:
            continue
        current = _text(f.get('value'))
        cleaned = _strip_model_tail(current)
        if cleaned and cleaned != current:
            f.setdefault('original_value', current)
            f['value'] = cleaned
            f['normalized_value'] = cleaned
            f['status'] = 'corrected'
            f['confidence'] = max(float(f.get('confidence') or 0), 0.98)
            f['notes'] = 'v33: удалён текст договора/заголовка, попавший после марки/модели.'
        elif not cleaned and current:
            f.setdefault('original_value', current)
            f['value'] = None
            f['normalized_value'] = None
            f['status'] = 'candidate'
    for table in document.get('tables', []):
        for row in table.get('rows', []):
            if not isinstance(row, dict):
                continue
            for key in list(row):
                if any(t in str(key).casefold() for t in ('model', 'модель', 'equipment_name', 'наименование')):
                    current = _text(row.get(key))
                    if not current:
                        continue
                    cleaned = _strip_model_tail(current)
                    if cleaned:
                        row[key] = cleaned
                    elif any(t in str(key).casefold() for t in ('model', 'модель')):
                        row[key] = None


def _explicit_lessee_from_source(document: dict) -> tuple[str, str, str, int | None] | None:
    full = _full_text(document)
    if not full:
        return None
    # Locate an explicit lessee-role statement, then resolve name + identifier
    # in a wide bilingual window. This is a fallback, not a replacement for the
    # normal party parser.
    roles = list(re.finditer(r'далее\s+именуем\w*\s*[«"]Лизингополучатель[»"]|«Лизинг\s+алушы»\s+деп\s+аталатын', full, re.I))
    for role in roles:
        window_start = max(0, role.start() - 1800)
        window_end = min(len(full), role.end() + 350)
        window = full[window_start:window_end]
        ids = list(re.finditer(r'(?<!\d)(\d{12})(?!\d)', window))
        if not ids:
            continue
        # Prefer a non-BCC ID closest to the role phrase.
        role_local = role.start() - window_start
        candidates = [(abs(m.start() - role_local), m.group(1), m) for m in ids if m.group(1) != BCC_BIN]
        if not candidates:
            continue
        candidates.sort(key=lambda x: x[0])
        _, identifier, idm = candidates[0]
        # Russian company/IP declarations are strongest and portable across layouts.
        before_role = window[:max(role_local, 1)]
        name = ''
        patterns = (
            r'Индивидуальн\w*\s+предпринимател\w*\s+([A-ZА-ЯӘІҢҒҮҰҚӨҺ][A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺ\- ]{1,90}?)(?=,|\n)',
            r'(?:Товарищество\s+с\s+ограниченной\s+ответственностью|ТОО)\s*[«"]([^»"\n]{2,100})[»"]',
            r'(?:Акционерное\s+общество|АО)\s*[«"]([^»"\n]{2,100})[»"]',
        )
        for pattern in patterns:
            matches = list(re.finditer(pattern, before_role, re.I))
            if matches:
                raw_name = _text(matches[-1].group(1))
                if 'индивидуаль' in matches[-1].group(0).casefold():
                    name = f'ИП {raw_name}'
                elif 'товариществ' in matches[-1].group(0).casefold() or re.search(r'\bТОО\b', matches[-1].group(0), re.I):
                    name = f'ТОО «{raw_name}»'
                elif 'акционер' in matches[-1].group(0).casefold() or re.search(r'\bАО\b', matches[-1].group(0), re.I):
                    name = f'АО «{raw_name}»'
                break
        if not name:
            # Kazakh IP fallback: identifier is often followed by surname + "Жеке кәсіпкер".
            tail = window[idm.end():idm.end()+260]
            km = re.search(r'([A-ZА-ЯӘІҢҒҮҰҚӨҺ][A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺ\-]{1,50})\s+Жеке\s+кәсіпкер', tail, re.I)
            if km:
                name = f'ИП {km.group(1)}'
        if name:
            quote = _text(window[max(0, min(idm.start(), role_local)-250):min(len(window), max(idm.end(), role_local)+300)])
            return name, identifier, quote, _page_for_text(document, identifier)
    return None


def _recover_explicit_lessee(result: dict) -> None:
    client = result.setdefault('client', {})
    client_id = _digits(client.get('iin_bin'))
    client_name = _text(client.get('name'))
    role_noise = ('алатын', 'аталатын', 'деп атал', 'лизинг алушы', 'лизингополучател')
    needs = (
        not client_name or 'bcc leasing' in client_name.casefold()
        or any(token in client_name.casefold() for token in role_noise)
        or len(client_id) != 12 or client_id == BCC_BIN
    )
    for document in result.get('documents', []):
        if str(document.get('document_type') or '') not in {'purchase_contract', 'lease_contract'}:
            continue
        recovered = _explicit_lessee_from_source(document)
        if not recovered:
            continue
        name, identifier, quote, page = recovered
        if needs:
            client.update({'name': name, 'iin_bin': identifier, 'client_key': identifier, 'role': 'lessee_iin_bin', 'role_label_ru': 'Лизингополучатель'})
            client_name, client_id = name, identifier
            needs = False
        _upsert_field(document, 'lessee_name', 'Лизингополучатель', name, quote=quote, page=page, confidence=0.997,
                      notes='v33: сторона восстановлена по явной формулировке «Лизингополучатель».')
        _upsert_field(document, 'lessee_iin_bin', 'ИИН/БИН — Лизингополучатель', identifier, quote=quote, page=page, confidence=0.999,
                      notes='v33: идентификатор связан с явной стороной Лизингополучателя.')


def _parse_amount_text(value: str) -> float | None:
    parsed = _parse_money(value)
    return float(parsed) if parsed is not None else None


def _clean_spec_name(name: str) -> str:
    text = re.sub(r'\s+', ' ', name).strip(' ,.;:-')
    for token in _SPEC_HEADER_NOISE:
        text = re.sub(re.escape(token), ' ', text, flags=re.I)
    text = re.sub(r'\s+', ' ', text).strip(' ,.;:-')
    return _strip_model_tail(text)


def _infer_asset_identity(name: str) -> tuple[str | None, str | None, str | None]:
    clean = _clean_spec_name(name)
    if not clean:
        return None, None, None
    kind = None
    kind_rules = (
        (r'полуприцеп.*самосвал|полуприцеп-самосвал', 'Полуприцеп-самосвал'),
        (r'рефрижератор', 'Рефрижератор'),
        (r'промтоварн\w*\s+фургон', 'Промтоварный фургон'),
        (r'самосвальн\w*\s+прицеп', 'Самосвальный прицеп'),
        (r'самосвал', 'Самосвал'), (r'экскаватор[-\s]?погрузчик', 'Экскаватор-погрузчик'),
        (r'автомобил|\bгаз\s*\d|\bhyundai\b|\bkia\b|\bfaw\b', 'Автомобиль'),
        (r'прицеп', 'Прицеп'), (r'погрузчик', 'Погрузчик'),
    )
    for pattern, label in kind_rules:
        if re.search(pattern, clean, re.I):
            kind = label
            break
    model = None
    upper = clean.upper()
    for brand in _KNOWN_BRANDS:
        idx = upper.find(brand)
        if idx < 0:
            continue
        tail = clean[idx:]
        # Keep brand and a compact model designation, but stop before prose.
        tail = _strip_model_tail(tail)
        model_match = re.match(r'(.{2,70}?)(?=\s+(?:ПРОМТОВАРН|/ПРОМТОВАРН|САМОСВАЛ|РЕФРИЖЕРАТОР|ӨНЕРКӘСІПТІК|ЖҮК\s+КӨЛІГІ|ГОД\s+ВЫПУСКА|20\d{2}\b)|$)', tail, re.I)
        model = _text(model_match.group(1) if model_match else tail)
        if brand == 'FAW' and model.upper().startswith('FAW'):
            model = 'FAW'
        break
    if not model:
        # Compact alphanumeric vehicle/equipment designations.
        mm = re.search(r'\b([A-ZА-Я]{2,12}\s*[A-Z0-9][A-Z0-9\-./ ]{1,28})\b', clean, re.I)
        if mm:
            model = _text(mm.group(1))
    return kind, model, clean


def _one_row_spec_candidate(spec: str) -> dict | None:
    lines = spec.splitlines()
    money = r'\d{1,3}(?:[ \u00a0]\d{3})+(?:[,.]\d{2})?'
    # Strongest case: visually intact row with name, quantity, unit price and total.
    direct = re.compile(rf'^\s*(?:\d{{1,3}}\s+)?(?P<name>[^\n]{{2,180}}?\S)\s{{2,}}(?P<qty>\d{{1,3}})\s{{2,}}(?P<unit>{money})\s{{2,}}(?P<total>{money})\s*$', re.I)
    for i, line in enumerate(lines):
        m = direct.match(line)
        if not m:
            continue
        qty = int(m.group('qty'))
        unit = _parse_amount_text(m.group('unit'))
        total = _parse_amount_text(m.group('total'))
        if not unit or not total or qty <= 0 or abs(qty * unit - total) > max(2.0, total * 0.001):
            continue
        name_parts = [m.group('name')]
        # Some names continue on following lines (e.g. TONAR model).
        for nxt in lines[i+1:i+5]:
            s = _text(nxt)
            if not s or re.search(r'^(?:Барлығы|Итого|ЖИЫНЫ|Всего|год выпуска|Год выпуска)', s, re.I):
                break
            if re.search(money, s) or re.fullmatch(r'\d{1,4}', s):
                break
            if len(s) <= 70 and re.search(r'[A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺ]', s):
                name_parts.append(s)
            else:
                break
        name = _clean_spec_name(' '.join(name_parts))
        if not name:
            continue
        year = None
        ym = re.search(r'\b(20\d{2})\s*г?\.?', ' '.join(name_parts + lines[i+1:i+8]), re.I)
        if ym:
            year = ym.group(1)
        kind, model, clean = _infer_asset_identity(name)
        return {'equipment_type': kind, 'equipment_name': clean, 'model': model, 'manufacture_year': year,
                'quantity': qty, 'unit_price_kzt': unit, 'total_amount_kzt': total, 'status': 'corrected',
                'source_method': 'v33_single_commercial_spec'}

    # Column-split one-row layout: identify a unique commercial total and a compact
    # descriptive block before the technical-characteristics section.
    amounts = []
    for m in re.finditer(money, spec):
        val = _parse_amount_text(m.group(0))
        if val and val >= 100000:
            amounts.append(val)
    if not amounts:
        return None
    # Prefer an explicit final total, then infer unit price for quantity one.
    tm = re.search(rf'(?:Барлығы\s*/?\s*Итого|Итого|Общая\s+стоимость\s+прописью)[^\d]{{0,80}}(?P<total>{money})', spec, re.I | re.S)
    total = _parse_amount_text(tm.group('total')) if tm else max(amounts)
    if not total:
        return None
    # Single-row layouts often have an explicit quantity 1 around the first price.
    qty = 1
    # Description lives after headers but before technical prose / total wording.
    region = spec[:min(len(spec), 4500)]
    desc_lines = []
    started = False
    for raw in region.splitlines():
        line = _text(raw)
        if not line:
            continue
        low = line.casefold()
        if any(tok in low for tok in _SPEC_HEADER_NOISE):
            continue
        if re.search(r'^(?:Барлығы|Итого|Общая стоимость|Технические характеристики|Техникалық сипаттама)', line, re.I):
            if started:
                break
            continue
        if re.fullmatch(r'\d{1,4}', line) or re.fullmatch(money, line):
            continue
        if re.search(r'DOC ID|Электронный документ', line, re.I):
            continue
        if re.search(r'[A-Za-zА-Яа-яӘәІіҢңҒғҮүҰұҚқӨөҺ]', line):
            desc_lines.append(line)
            if any(brand.casefold() in low for brand in _KNOWN_BRANDS) or re.search(r'автомобил|рефрижератор|полуприцеп|самосвал|фургон|прицеп', line, re.I):
                started = True
        if started and len(desc_lines) >= 7:
            break
    if not started:
        return None
    name = _clean_spec_name(' '.join(desc_lines[-7:]))
    kind, model, clean = _infer_asset_identity(name)
    if not (model or kind):
        return None
    ym = re.search(r'\b(20\d{2})\b', name)
    year = ym.group(1) if ym else None
    return {'equipment_type': kind, 'equipment_name': clean, 'model': model, 'manufacture_year': year,
            'quantity': qty, 'unit_price_kzt': total, 'total_amount_kzt': total, 'status': 'corrected',
            'source_method': 'v33_single_commercial_spec'}


def _recover_single_commercial_spec(document: dict) -> None:
    if _document_family(document) != 'equipment':
        return
    full = _full_text(document)
    mentions = list(re.finditer(r'(?im)^\s*СПЕЦИФИКАЦИЯ\s*$', full))
    if not mentions:
        return
    spec = full[mentions[-1].start():]
    cut = re.search(r'(?:ПРИЛОЖЕНИЕ\s*№\s*2|2-қосымша|ОТ\s+ПРОДАВЦА|САТУШЫНЫҢ\s+атынан|Гарантийн)', spec, re.I)
    if cut:
        spec = spec[:cut.start()]
    candidate = _one_row_spec_candidate(spec)
    if not candidate:
        return
    # Do not replace a clearly stronger enumerated VIN/multi-item table.
    meaningful = []
    for table in document.get('tables', []):
        if str(table.get('name') or '') == 'asset_vin_rows':
            meaningful.extend([r for r in table.get('rows', []) if isinstance(r, dict) and _text(r.get('vin') or r.get('model') or r.get('equipment_name'))])
    if len(meaningful) > 1 or any(_text(r.get('vin')) for r in meaningful):
        return
    current_model = _field(document, 'equipment_model', 'vehicle_model', 'model')
    current_model_text = _strip_model_tail(current_model.get('value') if current_model else '')
    candidate_model = _text(candidate.get('model'))
    # Replace only when current structured identity is missing/obviously label-like,
    # or the candidate is more specific and arithmetically verified.
    if meaningful and current_model_text and candidate_model and len(current_model_text) >= len(candidate_model):
        return
    columns = [
        {'key': 'equipment_type', 'label_ru': 'Вид техники'},
        {'key': 'equipment_name', 'label_ru': 'Наименование'},
        {'key': 'model', 'label_ru': 'Модель / комплектация'},
        {'key': 'manufacture_year', 'label_ru': 'Год выпуска'},
        {'key': 'quantity', 'label_ru': 'Количество'},
        {'key': 'unit_price_kzt', 'label_ru': 'Цена за единицу, тенге'},
        {'key': 'total_amount_kzt', 'label_ru': 'Общая стоимость позиции, тенге'},
    ]
    document['tables'] = [t for t in document.get('tables', []) if str(t.get('name') or '') != 'asset_vin_rows']
    document.setdefault('tables', []).append({
        'name': 'asset_vin_rows', 'label_ru': 'Техника / предмет договора', 'columns': columns,
        'rows': [candidate], 'row_count': 1, 'status': 'corrected', 'confidence': 0.995,
        'notes': 'v33: одна коммерческая позиция восстановлена из явной спецификации с проверкой количества и цены.',
        'summary': {'total_quantity': int(candidate.get('quantity') or 1), 'unique_vin_count': 0,
                    'total_identified_amount_kzt': candidate.get('total_amount_kzt'), 'item_positions': 1},
    })
    if candidate.get('equipment_type'):
        _upsert_field(document, 'equipment_type', 'Вид техники', candidate['equipment_type'], confidence=0.992,
                      notes='v33: восстановлено из коммерческой строки спецификации.')
    if candidate.get('model'):
        _upsert_field(document, 'equipment_model', 'Марка / модель техники', candidate['model'], confidence=0.995,
                      notes='v33: восстановлено из коммерческой строки спецификации.')
    if candidate.get('manufacture_year'):
        _upsert_field(document, 'manufacture_year', 'Год выпуска', candidate['manufacture_year'], confidence=0.99)
    _upsert_field(document, 'equipment_quantity', 'Количество единиц техники', int(candidate.get('quantity') or 1), confidence=0.995)
    _upsert_field(document, 'equipment_unit_price_kzt', 'Цена за единицу техники', candidate.get('unit_price_kzt'), confidence=0.995)
    _upsert_field(document, 'equipment_total_kzt', 'Общая стоимость техники', candidate.get('total_amount_kzt'), confidence=0.995)


def _suppress_wrong_family_warnings(document: dict) -> None:
    family = _document_family(document)
    if family not in {'real_estate', 'registration'}:
        return
    equipment_noise = (
        'вид или модель предмета лизинга не определ', 'модель техники', 'вид техники',
        'структурированная таблица техники', 'техника:', 'vin', 'оборудован',
    )
    registration_noise = (
        'иин/бин клиента не определ', 'иин/бин клиента не найден',
        'имя клиента не определ', 'наименование клиента не подтвержден',
        'наименование или иин/бин клиента не подтвержден',
    ) if family == 'registration' else ()
    def keep_warning(w: Any) -> bool:
        if isinstance(w, dict):
            text = (_text(w.get('field')) + ' ' + _text(w.get('message_ru') or w.get('message'))).casefold()
        else:
            text = _text(w).casefold()
        return not any(token in text for token in equipment_noise + registration_noise)
    document['warnings'] = [w for w in document.get('warnings', []) if keep_warning(w)]
    if 'blocking_warnings' in document:
        document['blocking_warnings'] = [w for w in document.get('blocking_warnings', []) if keep_warning(w)]
    # Do not export accidental equipment tables for real estate/registration docs.
    document['tables'] = [
        t for t in document.get('tables', [])
        if not (str(t.get('name') or '') == 'asset_vin_rows' and family in {'real_estate', 'registration'})
    ]


def _normalize_nonclient_document_result(result: dict) -> None:
    docs = [d for d in result.get('documents', []) if isinstance(d, dict)]
    if not docs or not all(_document_family(d) == 'registration' for d in docs):
        return
    # A registration notification can be perfectly valid without identifying the
    # leasing client. Do not turn that absence into a false failure.
    result['client'] = {
        'name': 'Не применимо', 'iin_bin': 'Не применимо', 'role': 'Не применимо',
        'confidence': 1.0, 'source': 'document_family_registration',
    }


def _apply_document_family_metadata(result: dict) -> None:
    families = {}
    for document in result.get('documents', []):
        family = _document_family(document)
        document.setdefault('analysis', {})['asset_family'] = family
        families[document.get('filename') or str(len(families) + 1)] = family
    result.setdefault('analysis', {})['document_families'] = families


# Preserve the prior implementation under a private name, then wrap it with the
# adaptive v33 guard. This keeps the proven v32 behavior intact and adds only
# conservative, late-stage corrections.
_apply_release_sanitizer_v32 = apply_release_sanitizer


def apply_release_sanitizer(result: dict, version: str = '35.0') -> None:
    _apply_release_sanitizer_v32(result, version=version)
    _recover_explicit_lessee(result)
    for document in result.get('documents', []):
        _recover_real_estate_semantics(document)
        _recover_single_commercial_spec(document)
        _clean_model_fields(document)
        _suppress_wrong_family_warnings(document)
        _sanitize_equipment(document)
    # Rebind after explicit party recovery so dossier summary cannot inherit BCC's BIN.
    _bind_client_to_lessee(result)
    _repair_leasing_party_identifier_labels(result)
    _clear_resolved_client_warnings(result)
    _suppress_resolved_secondary_warnings(result)
    _normalize_nonclient_document_result(result)
    _apply_document_family_metadata(result)
    analysis = result.setdefault('analysis', {})
    analysis['quality_pipeline_version'] = version
    analysis['release_profile'] = 'work-ready-reviewed-v35'

# v33.0 refinement: robust whitespace money tokens, nearest-party role binding,
# and generic single-row specification reconstruction based on PDF text order.
_V33_MONEY = r'\d{1,3}(?:[\s\u00a0]+\d{3})+(?:[,.]\d{1,2})?'


def _recover_real_estate_semantics(document: dict) -> None:
    if _document_family(document) != 'real_estate':
        return
    full = _full_text(document)
    lease_patterns = (
        rf'Стоимость\s+Предмета\s+лизинга\s+(?:составляет|равна)\s*({_V33_MONEY})',
        rf'Лизинг\s+затының\s+құны[^\d]{{0,160}}({_V33_MONEY})',
    )
    for pattern in lease_patterns:
        m = re.search(pattern, full, re.I | re.S)
        if not m:
            continue
        value = _money_number(m.group(1))
        if value is not None:
            _upsert_field(document, 'lease_asset_value_kzt', 'Стоимость предмета лизинга, тенге', value,
                          quote=_text(m.group(0)), page=_page_for_text(document, m.group(1)), confidence=0.999,
                          notes='v33: прямая стоимость предмета лизинга отделена от рыночной оценки.')
            break
    if re.search(r'Рыночная\s+стоимость', full, re.I):
        region_m = re.search(r'Технические\s+характеристики\s+недвижимости(.{0,9000})', full, re.I | re.S)
        region = region_m.group(1) if region_m else full
        itogo = re.search(rf'(?:ЖИЫНЫ\s*/?\s*ИТОГО|ИТОГО)\s*:\s*[^\n]*?({_V33_MONEY})\s+({_V33_MONEY})', region, re.I)
        if itogo:
            market = _money_number(itogo.group(2))
            if market is not None:
                _upsert_field(document, 'market_value_kzt', 'Рыночная стоимость недвижимости, тенге', market,
                              quote=_text(itogo.group(0)), page=_page_for_text(document, itogo.group(2)), confidence=0.997,
                              notes='v33: рыночная стоимость хранится отдельно от стоимости предмета лизинга.')


def _explicit_lessee_from_source(document: dict) -> tuple[str, str, str, int | None] | None:
    full = _full_text(document)
    if not full:
        return None
    role_patterns = (
        r'далее\s+именуем\w*\s*[«"]Лизингополучатель[»"]',
        r'именуем\w*\s+в\s+дальнейшем\s*[«"]Лизингополучатель[»"]',
        r'«Лизинг\s+алушы»\s+деп\s+аталатын',
    )
    roles = []
    for pat in role_patterns:
        roles.extend(re.finditer(pat, full, re.I))
    roles.sort(key=lambda m: m.start())
    for role in roles:
        start = max(0, role.start() - 1800)
        end = min(len(full), role.end() + 180)
        window = full[start:end]
        role_local = role.start() - start
        before = window[:role_local]
        entity_matches: list[tuple[int, int, str]] = []
        entity_patterns = (
            (r'Индивидуальн\w*\s+предпринимател\w*\s*[«"]?([^,»"\n]{2,90})[»"]?(?=,|\n)', 'ИП'),
            (r'(?:Товарищество\s+с\s+ограниченной\s+ответственностью|ТОО)\s*[«"]([^»"\n]{2,100})[»"]', 'ТОО'),
            (r'(?:Акционерное\s+общество|АО)\s*[«"]([^»"\n]{2,100})[»"]', 'АО'),
        )
        for pat, prefix in entity_patterns:
            for m in re.finditer(pat, before, re.I):
                raw = _text(m.group(1))
                if not raw or 'bcc leasing' in raw.casefold():
                    # BCC can only be the lessee when explicitly named as such,
                    # which is not a normal BCC Leasing dossier configuration.
                    continue
                name = f'{prefix} «{raw}»' if prefix in {'ТОО', 'АО'} else f'ИП {raw}'
                entity_matches.append((m.start(), m.end(), name))
        if not entity_matches:
            continue
        # Nearest legal entity declaration before the role marker wins.
        em_start, em_end, name = max(entity_matches, key=lambda x: x[1])
        center = (em_start + em_end) // 2
        ids = []
        for m in re.finditer(r'(?<!\d)(\d{12})(?!\d)', window):
            ident = m.group(1)
            if ident == BCC_BIN:
                continue
            distance = min(abs(m.start() - center), abs(m.start() - role_local))
            if distance <= 1100:
                ids.append((distance, ident, m))
        if not ids:
            continue
        ids.sort(key=lambda x: x[0])
        _, identifier, idm = ids[0]
        quote = _text(window[max(0, em_start-120):min(len(window), role_local+180)])
        return name, identifier, quote, _page_for_text(document, identifier)
    return None


def _find_single_spec_region(full: str) -> str | None:
    # BCC/Documentolog templates use several bilingual title variants, including
    # СПЕЦИФИКАЦИЯСЫ/СПЕЦИФИКАЦИЯ. Match the Russian keyword anywhere on a
    # compact title line instead of requiring it to be the first token.
    matches = list(re.finditer(r'(?im)^\s*[^\n]{0,45}СПЕЦИФИКАЦИЯ(?:\s*/\s*[^\n]+)?\s*$', full))
    if not matches:
        return None
    spec = full[matches[-1].start():]
    cut = re.search(r'(?:ПРИЛОЖЕНИЕ\s*№\s*2|2-қосымша|ОТ\s+ПРОДАВЦА|САТУШЫНЫҢ\s+атынан|Гарантийн|Кепілдік\s+міндеттемесі)', spec, re.I)
    if cut:
        spec = spec[:cut.start()]
    return spec


def _single_spec_description(lines: list[str]) -> tuple[int, int, list[str]] | None:
    brand_tokens = tuple(x.casefold() for x in _KNOWN_BRANDS)
    type_re = re.compile(r'автомобил|рефрижератор|полуприцеп|самосвал|фургон|прицеп|погрузчик|экскаватор|тягач', re.I)
    start = None
    for i, raw in enumerate(lines):
        s = _text(raw)
        if not s:
            continue
        low = s.casefold()
        if any(tok in low for tok in _SPEC_HEADER_NOISE):
            continue
        if type_re.search(s) or any(tok in low for tok in brand_tokens):
            start = i
            break
    if start is None:
        return None
    desc = []
    end = start
    for j in range(start, min(len(lines), start + 10)):
        s = _text(lines[j])
        if not s:
            continue
        if j > start and (re.fullmatch(r'20\d{2}', s) or re.fullmatch(r'\d{1,4}', s) or re.fullmatch(_V33_MONEY, s)):
            end = j
            break
        if j > start and re.search(r'^(?:Белый|Ақ|Черн|Сер|Син|Крас|Республика\s+Казахстан|Қазақстан\s+Республика)', s, re.I):
            end = j
            break
        if j > start and re.search(r'^(?:Габарит|Снаряженн|Грузоподъем|Полная масса|Колесная база|Ходовая часть|Двигатель|Общая длина|Ширина|Высота)', s, re.I):
            end = j
            break
        desc.append(s)
        end = j + 1
    return start, end, desc


def _one_row_spec_candidate(spec: str) -> dict | None:
    lines = spec.splitlines()
    desc_info = _single_spec_description(lines)
    if not desc_info:
        return None
    start, desc_end, desc = desc_info
    name = _clean_spec_name(' '.join(desc))
    kind, model, clean = _infer_asset_identity(name)
    if not (kind or model):
        return None
    year = None
    ym = re.search(r'(20\d{2})', ' '.join(desc + [_text(x) for x in lines[desc_end:desc_end+8]]))
    if ym:
        year = ym.group(1)

    # Find a quantity cell followed later by one or two money cells. PDF table
    # extraction frequently inserts year/color/country columns between them.
    qty = None
    qty_idx = None
    unit = None
    total = None
    for i in range(desc_end, min(len(lines), desc_end + 55)):
        s = _text(lines[i])
        if not re.fullmatch(r'\d{1,3}', s):
            continue
        q = int(s)
        if q <= 0 or q > 100:
            continue
        amounts = []
        for k in range(i+1, min(len(lines), i+24)):
            t = _text(lines[k])
            if re.search(r'^(?:Барлығы|Итого|Общая стоимость)', t, re.I):
                # total wording starts; amounts following it are summary amounts.
                pass
            if re.fullmatch(_V33_MONEY, t):
                val = _parse_amount_text(t)
                if val:
                    amounts.append(val)
                    if len(amounts) >= 2:
                        break
        if amounts:
            qty, qty_idx = q, i
            unit = amounts[0]
            total = amounts[1] if len(amounts) >= 2 else None
            break
    # Explicit total is authoritative if row has only one price column.
    total_match = re.search(rf'(?:Барлығы\s*/?\s*Итого|Барлығы/Итого|Итого|Общая\s+стоимость\s+прописью)\s*:?[^\d]{{0,80}}({_V33_MONEY})', spec, re.I | re.S)
    explicit_total = _parse_amount_text(total_match.group(1)) if total_match else None
    if qty is None:
        qty = 1
    if unit is None and explicit_total is not None and qty == 1:
        unit = explicit_total
    if total is None:
        total = explicit_total if explicit_total is not None else (unit * qty if unit else None)
    if not unit or not total:
        return None
    if abs(qty * unit - total) > max(2.0, total * 0.001):
        # If the first amount was actually a total and quantity=1, this remains valid.
        if qty == 1 and explicit_total and abs(unit - explicit_total) <= max(2.0, explicit_total * 0.001):
            total = explicit_total
        else:
            return None
    display = _text(' '.join(x for x in (kind, model) if x)) or clean
    return {
        'equipment_type': kind, 'equipment_name': display, 'model': model,
        'manufacture_year': year, 'quantity': qty, 'unit_price_kzt': unit,
        'total_amount_kzt': total, 'status': 'corrected',
        'source_method': 'v33_single_commercial_spec',
    }


def _recover_single_commercial_spec(document: dict) -> None:
    if _document_family(document) != 'equipment':
        return
    spec = _find_single_spec_region(_full_text(document))
    if not spec:
        return
    candidate = _one_row_spec_candidate(spec)
    if not candidate:
        return
    meaningful = []
    for table in document.get('tables', []):
        if str(table.get('name') or '') == 'asset_vin_rows':
            for row in table.get('rows', []):
                if not isinstance(row, dict) or _is_header_asset(row):
                    continue
                identity = _text(row.get('vin') or row.get('model') or row.get('equipment_name') or row.get('equipment_type'))
                if identity and not _bad_equipment_value(identity):
                    meaningful.append(row)
    # Preserve genuinely richer multi-item/VIN results, but do not let two
    # flattened technical-characteristic labels block a verified one-row
    # commercial specification recovery.
    if len(meaningful) > 1 or any(_text(r.get('vin')) for r in meaningful):
        return
    current_model = _field(document, 'equipment_model', 'vehicle_model', 'model')
    current_clean = _strip_model_tail(current_model.get('value') if current_model else '')
    cand_model = _text(candidate.get('model'))
    # Replace clearly label-like or missing current identity. If current value is a
    # real compact model, retain it and only fill structured row/quantity/price.
    bad_current = (not current_clean or re.search(r'колесн\w*\s+формул|дөңгелек|осы шарт|бойынша', current_clean, re.I))
    columns = [
        {'key': 'equipment_type', 'label_ru': 'Вид техники'}, {'key': 'equipment_name', 'label_ru': 'Наименование'},
        {'key': 'model', 'label_ru': 'Модель / комплектация'}, {'key': 'manufacture_year', 'label_ru': 'Год выпуска'},
        {'key': 'quantity', 'label_ru': 'Количество'}, {'key': 'unit_price_kzt', 'label_ru': 'Цена за единицу, тенге'},
        {'key': 'total_amount_kzt', 'label_ru': 'Общая стоимость позиции, тенге'},
    ]
    document['tables'] = [t for t in document.get('tables', []) if str(t.get('name') or '') != 'asset_vin_rows']
    document.setdefault('tables', []).append({
        'name': 'asset_vin_rows', 'label_ru': 'Техника / предмет договора', 'columns': columns,
        'rows': [candidate], 'row_count': 1, 'status': 'corrected', 'confidence': 0.995,
        'notes': 'v33: одна коммерческая позиция восстановлена из спецификации по описанию, количеству и стоимости.',
        'summary': {'total_quantity': int(candidate.get('quantity') or 1), 'unique_vin_count': 0,
                    'total_identified_amount_kzt': candidate.get('total_amount_kzt'), 'item_positions': 1},
    })
    if candidate.get('equipment_type') and bad_current:
        _upsert_field(document, 'equipment_type', 'Вид техники', candidate['equipment_type'], confidence=0.992)
    if cand_model and bad_current:
        _upsert_field(document, 'equipment_model', 'Марка / модель техники', cand_model, confidence=0.995)
    if candidate.get('manufacture_year'):
        _upsert_field(document, 'manufacture_year', 'Год выпуска', candidate['manufacture_year'], confidence=0.99)
    _upsert_field(document, 'equipment_quantity', 'Количество единиц техники', int(candidate.get('quantity') or 1), confidence=0.995)
    _upsert_field(document, 'equipment_unit_price_kzt', 'Цена за единицу техники', candidate.get('unit_price_kzt'), confidence=0.995)
    _upsert_field(document, 'equipment_total_kzt', 'Общая стоимость техники', candidate.get('total_amount_kzt'), confidence=0.995)

# Final v33 refinements discovered by cross-layout regression.
def _explicit_lessee_from_source(document: dict) -> tuple[str, str, str, int | None] | None:
    full = _full_text(document)
    if not full:
        return None

    def parse_role(role: re.Match, *, look_after: bool = False):
        start = max(0, role.start() - (500 if look_after else 2000))
        end = min(len(full), role.end() + (1600 if look_after else 220))
        window = full[start:end]
        role_local = role.start() - start
        scope = window[role_local:role_local+1500] if look_after else window[:role_local]
        scope_offset = role_local if look_after else 0
        entity_matches: list[tuple[int, int, str]] = []
        patterns = (
            (r'Индивидуальн\w*\s+предпринимател\w*\s*[«"]?([^,»"\n]{2,90})[»"]?(?=,|\n)', 'ИП'),
            (r'(?:Товарищество\s+с\s+ограниченной\s+ответственностью|ТОО)\s*[«"]([^»"\n]{2,100})[»"]', 'ТОО'),
            (r'(?:Акционерное\s+общество|АО)\s*[«"]([^»"\n]{2,100})[»"]', 'АО'),
        )
        for pat, prefix in patterns:
            for m in re.finditer(pat, scope, re.I):
                raw = _text(m.group(1))
                low = raw.casefold()
                if not raw or 'bcc leasing' in low or 'банк центркредит' in low:
                    continue
                name = f'{prefix} «{raw}»' if prefix in {'ТОО', 'АО'} else f'ИП {raw}'
                entity_matches.append((scope_offset + m.start(), scope_offset + m.end(), name))
        if not entity_matches:
            # Kazakh legal-form fallback around the role phrase.
            km = re.search(r'[«"]([^»"\n]{2,100})[»"]\s+(?:жауапкершілігі\s+шектеулі\s+серіктестігі|Жеке\s+кәсіпкер)', scope, re.I)
            if km:
                prefix = 'ИП' if 'жеке кәсіпкер' in km.group(0).casefold() else 'ТОО'
                raw = _text(km.group(1)); name = f'{prefix} «{raw}»' if prefix == 'ТОО' else f'ИП {raw}'
                entity_matches.append((scope_offset + km.start(), scope_offset + km.end(), name))
        if not entity_matches:
            return None
        # For Russian marker the entity is immediately before it; for Kazakh it
        # is normally immediately after it.
        if look_after:
            em_start, em_end, name = min(entity_matches, key=lambda x: x[0])
        else:
            em_start, em_end, name = max(entity_matches, key=lambda x: x[1])
        center = (em_start + em_end) // 2
        ids = []
        for m in re.finditer(r'(?<!\d)(\d{12})(?!\d)', window):
            ident = m.group(1)
            if ident == BCC_BIN:
                continue
            distance = abs(m.start() - center)
            if distance <= 1000:
                ids.append((distance, ident, m))
        if not ids:
            return None
        ids.sort(key=lambda x: x[0])
        _, identifier, idm = ids[0]
        quote = _text(window[max(0, min(em_start, idm.start())-100):min(len(window), max(em_end, idm.end())+260)])
        return name, identifier, quote, _page_for_text(document, identifier)

    # Prefer Russian role markers because the legal entity precedes the marker
    # and is therefore much less ambiguous than the Kazakh preposed marker.
    russian_roles = []
    for pat in (
        r'далее\s+именуем\w*\s*[«"]Лизингополучатель[»"]',
        r'именуем\w*\s+в\s+дальнейшем\s*[«"]Лизингополучатель[»"]',
    ):
        russian_roles.extend(re.finditer(pat, full, re.I))
    for role in sorted(russian_roles, key=lambda m: m.start()):
        parsed = parse_role(role, look_after=False)
        if parsed:
            return parsed
    for role in re.finditer(r'«Лизинг\s+алушы»\s+деп\s+аталатын', full, re.I):
        parsed = parse_role(role, look_after=True)
        if parsed:
            return parsed
    return None


def _single_spec_description(lines: list[str]) -> tuple[int, int, list[str]] | None:
    brand_tokens = tuple(x.casefold() for x in _KNOWN_BRANDS)
    type_re = re.compile(r'автомобил|рефрижератор|полуприцеп|самосвал|фургон|прицеп|погрузчик|экскаватор|тягач', re.I)
    start = None
    for i, raw in enumerate(lines):
        s = _text(raw)
        if not s:
            continue
        low = s.casefold()
        has_identity = type_re.search(s) or any(tok in low for tok in brand_tokens)
        # Header words can appear inside a real item description (e.g.
        # "ГАЗ 27527, год выпуска 2026г."); only discard them when no concrete
        # brand/type identity is present on the same line.
        if not has_identity and any(tok in low for tok in _SPEC_HEADER_NOISE):
            continue
        if has_identity:
            start = i
            break
    if start is None:
        return None
    desc = []
    end = start
    for j in range(start, min(len(lines), start + 10)):
        s = _text(lines[j])
        if not s:
            continue
        if j > start and (re.fullmatch(r'20\d{2}', s) or re.fullmatch(r'\d{1,4}', s) or re.fullmatch(_V33_MONEY, s)):
            end = j; break
        if j > start and re.search(r'^(?:Белый|Ақ|Черн|Сер|Син|Крас|Республика\s+Казахстан|Қазақстан\s+Республика)', s, re.I):
            end = j; break
        if j > start and re.search(r'^(?:Габарит|Снаряженн|Грузоподъем|Полная масса|Колесная база|Ходовая часть|Двигатель|Общая длина|Ширина|Высота)', s, re.I):
            end = j; break
        desc.append(s); end = j + 1
    return start, end, desc

# Compact model cleanup for year-bearing one-line descriptions (e.g. ГАЗ 27527, год выпуска 2026г.).
_infer_asset_identity_v33_base = _infer_asset_identity

def _infer_asset_identity(name: str) -> tuple[str | None, str | None, str | None]:
    kind, model, clean = _infer_asset_identity_v33_base(name)
    if model:
        model = re.sub(r'\s*,?\s*(?:год\s+выпуска\s*)?20\d{2}\s*г?\.?\s*$', '', model, flags=re.I).strip(' ,.;:-')
        model = _strip_model_tail(model)
    if clean:
        clean = re.sub(r'\s+', ' ', clean).strip()
    return kind, model or None, clean or None

# Handle legal names wrapped across PDF lines and keep asset-row identity compact.
_explicit_lessee_from_source_v33_prev = _explicit_lessee_from_source

def _explicit_lessee_from_source(document: dict) -> tuple[str, str, str, int | None] | None:
    full = _full_text(document)
    if not full:
        return None

    def entity_candidates(scope: str, offset: int = 0):
        out=[]
        patterns=(
            (r'Индивидуальн\w*\s+предпринимател\w*\s*[«"]?([^,»"]{2,100}?)[»"]?(?=,|\n)', 'ИП'),
            (r'(?:Товарищество\s+с\s+ограниченной\s+ответственностью|ТОО)\s*[«"]([^»"]{2,120})[»"]', 'ТОО'),
            (r'(?:Акционерное\s+общество|АО)\s*[«"]([^»"]{2,120})[»"]', 'АО'),
        )
        for pat,prefix in patterns:
            for m in re.finditer(pat,scope,re.I):
                raw=_text(m.group(1)); low=raw.casefold()
                if not raw or 'bcc leasing' in low or 'банк центркредит' in low:
                    continue
                # Avoid a pattern swallowing multiple legal clauses after line wrapping.
                if len(raw)>100 or any(t in low for t in ('в лице','атынан','далее имен')):
                    continue
                name=f'{prefix} «{raw}»' if prefix in {'ТОО','АО'} else f'ИП {raw}'
                out.append((offset+m.start(),offset+m.end(),name))
        return out

    russian=[]
    for pat in (r'далее\s+именуем\w*\s*[«"]Лизингополучатель[»"]',r'именуем\w*\s+в\s+дальнейшем\s*[«"]Лизингополучатель[»"]'):
        russian.extend(re.finditer(pat,full,re.I))
    for role in sorted(russian,key=lambda m:m.start()):
        start=max(0,role.start()-2200); window=full[start:role.end()+150]; role_local=role.start()-start
        ents=entity_candidates(window[:role_local],0)
        if not ents: continue
        es,ee,name=max(ents,key=lambda x:x[1]); center=(es+ee)//2
        ids=[]
        for m in re.finditer(r'(?<!\d)(\d{12})(?!\d)',window):
            if m.group(1)==BCC_BIN: continue
            dist=abs(m.start()-center)
            if dist<=1200: ids.append((dist,m.group(1),m))
        if not ids: continue
        ids.sort(key=lambda x:x[0]); _,ident,idm=ids[0]
        quote=_text(window[max(0,min(es,idm.start())-100):min(len(window),max(ee,idm.end())+250)])
        return name,ident,quote,_page_for_text(document,ident)
    # Kazakh marker fallback: entity follows the role phrase.
    for role in re.finditer(r'«Лизинг\s+алушы»\s+деп\s+аталатын',full,re.I):
        start=role.start(); window=full[start:min(len(full),role.end()+1800)]; role_local=role.end()-start
        ents=entity_candidates(window[role_local:],role_local)
        if not ents: continue
        es,ee,name=min(ents,key=lambda x:x[0]); center=(es+ee)//2
        ids=[]
        for m in re.finditer(r'(?<!\d)(\d{12})(?!\d)',window):
            if m.group(1)==BCC_BIN: continue
            dist=abs(m.start()-center)
            if dist<=1000: ids.append((dist,m.group(1),m))
        if ids:
            ids.sort(key=lambda x:x[0]); _,ident,idm=ids[0]
            quote=_text(window[max(0,min(es,idm.start())-80):min(len(window),max(ee,idm.end())+220)])
            return name,ident,quote,_page_for_text(document,ident)
    return None

_one_row_spec_candidate_v33_prev = _one_row_spec_candidate

def _one_row_spec_candidate(spec: str) -> dict | None:
    # Repair common PDF column line-breaks inside equipment names before the
    # generic parser sees them. This is deliberately limited to lexical splits
    # that form known equipment terms, not arbitrary line joining.
    spec = re.sub(r'(?i)\bРефрижер\s*\n\s*атор\b', 'Рефрижератор', spec)
    spec = re.sub(r'(?i)\bАвтомоб\s*\n\s*иль\b', 'Автомобиль', spec)
    candidate=_one_row_spec_candidate_v33_prev(spec)
    if not candidate:
        return None
    model=_text(candidate.get('model'))
    # Keep table identity concise so downstream header/noise filters do not reject
    # a valid long vehicle description simply because type+name+model duplicate.
    if model:
        candidate['equipment_name']=model
    elif candidate.get('equipment_type'):
        candidate['equipment_name']=candidate.get('equipment_type')
    return candidate
