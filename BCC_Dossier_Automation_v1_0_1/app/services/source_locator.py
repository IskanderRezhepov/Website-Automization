from __future__ import annotations

import re
from copy import deepcopy


ROLE_WORDS = (
    "ЛИЗИНГОПОЛУЧАТЕЛ", "ЛИЗИНГ АЛУШ", "ЛИЗИНГОДАТЕЛ", "ЛИЗИНГ БЕРУШ",
    "ЗАЕМЩИК", "ҚАРЫЗ АЛУШ", "ГАРАНТ", "КЕПІЛГЕР", "ПОЛУЧАТЕЛ", "АЛУШЫ",
    "ПРОДАВЕЦ", "САТУШЫ", "ПОКУПАТЕЛ", "САТЫП АЛУШЫ", "БЕНЕФИЦИАР",
    "ОТПРАВИТЕЛ", "ЖӨНЕЛТУШІ", "БАНКОВСКИЙ СЧЕТ", "ИИК", "IBAN", "БИН", "ИИН",
)


def _normalise(value: object) -> str:
    text = str(value or "").strip().upper()
    # JSON stores many whole-tenge amounts as floats (for example 394800.0),
    # while the document prints ``394 800``.  Treat the serialization-only
    # decimal suffix as insignificant, otherwise the locator searches for the
    # non-existent digit sequence ``3948000`` and loses the audit citation.
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        text = text.split(".", 1)[0]
    return re.sub(r"[^0-9A-ZА-ЯӘҒҚҢӨҰҮІҺ]", "", text)


def _display_quote(words: list[dict], start: int, end: int, radius: int = 10) -> str:
    left = max(0, start - radius)
    right = min(len(words), end + radius + 1)
    return " ".join(
        str(item.get("text") or "").strip()
        for item in words[left:right]
        if str(item.get("text") or "").strip()
    )[:700]


def _box(words: list[dict]) -> list[float]:
    return [
        min(float(item.get("x0", 0)) for item in words),
        min(float(item.get("y0", 0)) for item in words),
        max(float(item.get("x1", 0)) for item in words),
        max(float(item.get("y1", 0)) for item in words),
    ]


def _same_location(left: dict, right: dict) -> bool:
    if left["page"] != right["page"]:
        return False
    a, b = left["box"], right["box"]
    return (
        abs(a[0] - b[0]) < 4 and abs(a[1] - b[1]) < 4
        and abs(a[2] - b[2]) < 8 and abs(a[3] - b[3]) < 8
    )


def _context_score(quote: str, target: str) -> int:
    upper = quote.upper()
    score = sum(2 for word in ROLE_WORDS if word in upper)
    if target.startswith("KZ") and any(word in upper for word in ("ИИК", "IBAN", "СЧЕТ", "СЧЁТ", "ЖСК")):
        score += 6
    if len(target) == 12 and any(word in upper for word in ("БИН", "ИИН", "БСН", "ЖСН")):
        score += 6
    if len(target) == 17 and "VIN" in upper:
        score += 6
    return score


def locate_value(page_layouts: list[dict], value: object, limit: int = 5) -> list[dict]:
    """Find only real occurrences and remove duplicate OCR/digital-layer boxes."""
    target = _normalise(value)
    if len(target) < 3:
        return []

    found: list[dict] = []
    for layout in page_layouts or []:
        words = [item for item in layout.get("words", []) if isinstance(item, dict)]
        tokens = [_normalise(item.get("text")) for item in words]

        for start, token in enumerate(tokens):
            if not token:
                continue

            matches: list[tuple[int, str]] = []
            if token == target:
                matches.append((start, "exact"))

            combined = ""
            for end in range(start, min(len(tokens), start + 12)):
                if not tokens[end]:
                    continue
                combined += tokens[end]
                if combined == target:
                    matches.append((end, "joined"))
                    break
                if len(combined) >= len(target):
                    break

            for end, match_type in matches:
                selected = words[start:end + 1]
                quote = _display_quote(words, start, end)
                item = {
                    "page": int(layout.get("page") or 1),
                    "quote": quote,
                    "box": _box(selected),
                    "match_type": match_type,
                    "context_score": _context_score(quote, target),
                }
                if not any(_same_location(item, prior) for prior in found):
                    found.append(item)

    # Prefer occurrences with labels/role context; one real occurrence per page
    # is usually enough for review unless boxes are clearly far apart.
    found.sort(key=lambda item: (-item["context_score"], item["page"], item["box"][1], item["box"][0]))
    result: list[dict] = []
    used_pages: set[int] = set()
    for item in found:
        # Candidate review needs the correct page, not multiple OCR boxes on
        # the same page. Keep the strongest contextual occurrence per page.
        if item["page"] in used_pages:
            continue
        if any(_same_location(item, prior) for prior in result):
            continue
        result.append(item)
        used_pages.add(item["page"])
        if len(result) >= limit:
            break
    return result


def enrich_field_locations(fields: list[dict], page_layouts: list[dict]) -> list[dict]:
    enriched = []
    for field in fields or []:
        item = deepcopy(field)
        item.pop("source_locations", None)
        value = item.get("value")

        if isinstance(value, list):
            locations = {}
            for candidate in value[:30]:
                candidate_found = locate_value(page_layouts, candidate)
                if candidate_found:
                    locations[str(candidate)] = candidate_found
            if locations:
                item["source_locations"] = locations
        elif value not in (None, ""):
            candidate_found = locate_value(page_layouts, value)
            if candidate_found:
                item["source_locations"] = {str(value): candidate_found}
                if not item.get("page"):
                    item["page"] = candidate_found[0]["page"]
                if not item.get("quote"):
                    item["quote"] = candidate_found[0]["quote"]

        enriched.append(item)
    return enriched


def _text_pattern(value: object) -> re.Pattern | None:
    """Build a conservative text-layer pattern for a serialized field value."""
    text = str(value or "").strip()
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        text = text.split(".", 1)[0]
    if not text or len(_normalise(text)) < 3:
        return None
    if re.fullmatch(r"\d+", text):
        # Documents commonly group money with spaces/NBSP.
        return re.compile(r"(?<!\d)" + r"[\s\u00a0]*".join(map(re.escape, text)) + r"(?!\d)")
    # Permit formatting whitespace around punctuation, but do not use fuzzy
    # matching: a citation must still contain the actual textual value.
    pieces = []
    for char in text:
        if char.isspace():
            pieces.append(r"\s+")
        elif char in "/.-":
            pieces.append(r"\s*" + re.escape(char) + r"\s*")
        elif char in "«»\"“”":
            pieces.append(r"[«»\"“”]*")
        else:
            pieces.append(re.escape(char))
    return re.compile("".join(pieces), re.I)


_RU_MONTHS = {
    1: r"январ[ья]", 2: r"феврал[ья]", 3: r"март[а]?",
    4: r"апрел[ья]", 5: r"ма[йя]", 6: r"июн[ья]",
    7: r"июл[ья]", 8: r"август[а]?", 9: r"сентябр[ья]",
    10: r"октябр[ья]", 11: r"ноябр[ья]", 12: r"декабр[ья]",
}


def _fallback_text_patterns(item: dict) -> list[re.Pattern]:
    """Field-aware OCR fallbacks that still point to source evidence.

    These patterns do not pretend that a normalised value was printed
    verbatim.  They only recover the surrounding source quotation for common
    document formatting (dates in words, short quantities and OCR-confused
    legal names).
    """
    value = str(item.get("value") or "").strip()
    name = str(item.get("name") or "")
    patterns: list[re.Pattern] = []

    date_match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if date_match:
        day, month, year = map(int, date_match.groups())
        month_name = _RU_MONTHS.get(month)
        if month_name:
            patterns.append(re.compile(
                rf"[«\"“]?\s*0?{day}\s*[»\"”]?\s+{month_name}\s+{year}\s*(?:г(?:ода)?\.?|ж\.)?",
                re.I,
            ))

    if re.fullmatch(r"\d{1,2}", value) and (
        name.endswith("_quantity") or name.endswith("_months")
        or name in {"addendum_number", "changed_clause"}
    ):
        patterns.append(re.compile(rf"(?<!\d){re.escape(value)}(?!\d)"))

    if name in {
        "lessee_name", "lessor_name", "buyer_name", "seller_name",
        "gps_provider", "gps_customer", "guarantor_name",
    }:
        core = re.sub(r"^(?:ИП|ТОО|АО)\s*", "", value, flags=re.I)
        core = re.sub(r"[«»\"“”]", "", core).strip()
        tokens = [token for token in re.findall(r"[A-Za-zА-Яа-яЁё.-]+", core) if len(token) >= 3]
        if tokens:
            # OCR in the verified bilingual documents commonly reads
            # Cyrillic "Ким" as Latin-looking "Kum".
            token_patterns = []
            for token in tokens[:3]:
                escaped = re.escape(token)
                if token.casefold() == "ким":
                    escaped = r"[КK][иu][мm]"
                token_patterns.append(escaped)
            patterns.append(re.compile(r"\s+".join(token_patterns), re.I))

    if any(key in name for key in ("contract_number", "iban")) and len(value) >= 8:
        pieces = []
        for char in value:
            if char in "IІ1":
                pieces.append(r"[IІ1]")
            elif char in "OО0":
                pieces.append(r"[OО0]")
            elif char in "/.-":
                pieces.append(r"\s*" + re.escape(char) + r"\s*")
            else:
                pieces.append(re.escape(char))
        patterns.append(re.compile("".join(pieces), re.I))
    return patterns


def enrich_field_text_quotes(fields: list[dict], pages: list[object]) -> list[dict]:
    """Add citations from page text when layout-token matching is insufficient."""
    enriched = deepcopy(fields or [])
    for item in enriched:
        if item.get("quote") or item.get("value") in (None, "", []) or isinstance(item.get("value"), list):
            continue
        # Derived/calculated values intentionally have no direct quotation.
        if item.get("status") in {"candidate", "calculated"} or item.get("value_type") != "direct":
            continue
        patterns = [pattern for pattern in [_text_pattern(item.get("value"))] if pattern]
        patterns.extend(_fallback_text_patterns(item))
        if not patterns:
            continue
        for page in pages or []:
            text = str(getattr(page, "text", "") or "")
            match = next((pattern.search(text) for pattern in patterns if pattern.search(text)), None)
            if not match:
                continue
            item["page"] = int(getattr(page, "page_number", 1) or 1)
            item["quote"] = text[max(0, match.start() - 180):match.end() + 180]
            break
    return enriched


def unresolved_pages(page_methods: list[dict], document_type: str) -> list[dict]:
    items = []
    for page in page_methods or []:
        quality = float(page.get("quality") or 0)
        char_count = int(page.get("char_count") or 0)
        reasons = []
        if char_count < 80:
            reasons.append("очень мало распознанного текста")
        if quality < 0.55:
            reasons.append("низкое качество распознавания")
        if page.get("method") == "ocr" and not page.get("layout_word_count"):
            reasons.append("нет координат для подсветки")
        if reasons:
            items.append({
                "page": page.get("page"),
                "reason": ", ".join(reasons),
                "quality": quality,
                "char_count": char_count,
            })
    if document_type == "unknown" and not items:
        for page in (page_methods or [])[:3]:
            items.append({
                "page": page.get("page"),
                "reason": "тип документа не определён — проверьте заголовок и реквизиты",
                "quality": float(page.get("quality") or 0),
                "char_count": int(page.get("char_count") or 0),
            })
    return items[:20]
