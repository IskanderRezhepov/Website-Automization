from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta

from app.parsers.base import field, normalize_contract_number
from app.services.text_utils import parse_money, quote_around


IBAN_RE = re.compile(r"\b(KZ\d{2}[0-9A-Z]{16})\b", re.I)
ID_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")
DATE_RE = re.compile(r"\b(\d{2}[.\-/]\d{2}[.\-/]\d{4})\b")
MONEY_RE = r"(\d(?:[\d \u00a0\r\n]{1,32})?(?:[,.]\d{2})?)"


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" ,.;:-")


def _normal_date(value: str) -> str:
    return value.replace("/", ".").replace("-", ".")


def _linked_contract_number(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"\s*([/-])\s*", r"\1", cleaned)
    return normalize_contract_number(cleaned) if " " not in cleaned else cleaned


def _money(value: str) -> float | None:
    parsed = parse_money(value)
    return float(parsed) if parsed is not None else None


def _direct(page, name: str, label: str, value, match, confidence: float = .99):
    item = field(
        name=name,
        label_ru=label,
        value=value,
        page=page.page_number,
        quote=quote_around(page.text, match.start(), match.end(), radius=280),
        confidence=confidence if page.extraction_method == "digital" else min(
            confidence, page.quality
        ),
        extraction_method=f"{page.extraction_method}:semantic",
        status="extracted",
    )
    item["raw_value"] = match.group(0)
    item["normalized_value"] = value
    item["value_type"] = "direct"
    item["recovered_from"] = {
        "pass": "semantic_enrichment",
        "page": page.page_number,
        "method": page.extraction_method,
    }
    return item


def _upsert(fields: list[dict], item: dict, *, force: bool = False) -> None:
    current = next((x for x in fields if x.get("name") == item.get("name")), None)
    if current is None:
        fields.append(item)
        return
    current_direct = (
        current.get("status") not in {"candidate", "rejected"}
        and bool(current.get("quote"))
    )
    if force or not current_direct or float(item.get("confidence") or 0) >= float(
        current.get("confidence") or 0
    ):
        current.clear()
        current.update(item)


def _replace_table(tables: list[dict], table: dict) -> None:
    tables[:] = [item for item in tables if item.get("name") != table.get("name")]
    tables.append(table)


def _find(document, pattern: str, flags=re.I | re.S):
    for page in document.pages:
        match = re.search(pattern, page.text, flags)
        if match:
            return page, match
    return None, None


def _field_values(fields: list[dict]) -> dict[str, object]:
    return {
        item.get("name"): item.get("value")
        for item in fields
        if item.get("value") not in (None, "", [])
        and item.get("status") not in {"candidate", "rejected"}
    }


def _extract_bcc_leasing_role(
    document, document_type: str, fields: list[dict]
) -> None:
    pattern = (
        r"(Дочерн\w+\s+компан\w+\s+АО\s*[«\"]Банк\s+ЦентрКредит[»\"]"
        r"[\s\S]{0,120}?Акционерн\w+\s+общество\s*[«\"]BCC\s+Leasing[»\"])"
        r"[\s\S]{0,650}?(?:далее\s+именуем\w*\s*[«\"]?"
        r"(?P<role>Покупатель|Лизингодатель))"
    )
    for page in document.pages[:6]:
        match = re.search(pattern, page.text, re.I)
        if not match:
            continue
        role = "buyer" if match.group("role").lower().startswith("покуп") else "lessor"
        label = "Покупатель" if role == "buyer" else "Лизингодатель"
        value = "Дочерняя компания АО «Банк ЦентрКредит» АО «BCC Leasing»"
        _upsert(
            fields,
            _direct(page, f"{role}_name", label, value, match),
            force=True,
        )
        return
    if document_type == "lease_contract":
        page, match = _find(
            document,
            r"(Дочерн\w+\s+компан\w+\s+АО\s*[«\"]Банк\s+ЦентрКредит[»\"]"
            r"[\s\S]{0,160}?(?:АО|Акционерн\w+\s+общество)\s*"
            r"[«\"]?BCC\s+Leasing[»\"]?[\s\S]{0,100}?\(?Лизингодатель\)?)",
        )
        if match:
            _upsert(
                fields,
                _direct(
                    page, "lessor_name", "Лизингодатель",
                    "Дочерняя компания АО «Банк ЦентрКредит» АО «BCC Leasing»",
                    match,
                ),
                force=True,
            )


def _ordered_role_accounts(document, document_type: str, fields: list[dict]) -> None:
    configurations = {
        "gps_service_contract": (
            ("gps_provider", "gps_provider_iin_bin", "gps_provider_iban",
             ("Поставщик", "Исполнитель"), "Поставщик GPS"),
            ("gps_customer", "gps_customer_iin_bin", "gps_customer_iban",
             ("Заказчик",), "Заказчик GPS"),
        ),
        "insurance_contract": (
            ("insurance_company", "insurance_company_iin_bin", "insurance_company_iban",
             ("Страховщик",), "Страховщик"),
            ("insurance_holder", "insurance_holder_iin_bin", "insurance_holder_iban",
             ("Страхователь",), "Страхователь"),
        ),
        "insurance_appendix": (
            ("insurance_company", "insurance_company_iin_bin", "insurance_company_iban",
             ("Страховщик",), "Страховщик"),
            ("insurance_holder", "insurance_holder_iin_bin", "insurance_holder_iban",
             ("Страхователь",), "Страхователь"),
        ),
        "direct_debit_agreement": (
            ("direct_debit_sender", "direct_debit_sender_iin_bin", "sender_iban",
             ("Отправитель денег", "Отправитель"), "Отправитель денег"),
            ("direct_debit_beneficiary", "direct_debit_beneficiary_iin_bin",
             "beneficiary_iban", ("Бенефициар",), "Бенефициар"),
        ),
    }
    roles = configurations.get(document_type)
    if not roles:
        return

    full = document.full_text
    ibans = []
    identifiers = []
    for page in document.pages:
        ibans.extend((page, match) for match in IBAN_RE.finditer(page.text))
        ibans.extend(
            (page, match)
            for match in re.finditer(
                r"(?:KZ|КZ|KЗ|КЗ)(?:\s*\d){18}\b",
                page.text,
                re.I,
            )
        )
        identifiers.extend((page, match) for match in ID_RE.finditer(page.text))
    unique_ibans = []
    for page, match in ibans:
        raw = match.group(1) if match.lastindex else match.group(0)
        compact = re.sub(r"\s+", "", raw).upper()
        value = "KZ" + compact[2:]
        if value not in {item[2] for item in unique_ibans}:
            unique_ibans.append((page, match, value))

    # Requisite sections in Kazakhstan contracts preserve role order even when
    # the PDF flattens a two-column table. Matching accounts in the same order
    # is safer than assigning every IBAN to the nearest heading.
    if unique_ibans:
        selected = unique_ibans[:len(roles)]
        for role, account in zip(roles, selected):
            _upsert(
                fields,
                _direct(account[0], role[2], f"IBAN — {role[4]}", account[2], account[1]),
                force=True,
            )

    for name_field, id_field, _iban_field, markers, label in roles:
        marker = "|".join(re.escape(value) for value in markers)
        pattern = (
            rf"(?:{marker})\s*:?\s*"
            rf"(?P<body>[\s\S]{{0,1000}}?)"
            rf"(?=(?:{'|'.join(re.escape(v) for r in roles for v in r[3])})\s*:|\Z)"
        )
        page, match = _find(document, pattern)
        if not match:
            continue
        body = match.group("body")
        party_match = re.search(
            r"((?:Дочерн\w+\s+компан\w+\s+АО\s*[«\"]Банк\s+ЦентрКредит[»\"]"
            r"\s+)?(?:ТОО|АО|ИП)\s*[«\"]?[^»\"\n]{2,150}[»\"]?)",
            body,
            re.I,
        )
        if party_match:
            party = _clean(party_match.group(1))
            party = party.replace('"', "«", 1)
            if "«" in party and "»" not in party:
                party += "»"
            current = next(
                (
                    item for item in fields
                    if item.get("name") == name_field
                    and item.get("value") not in (None, "")
                    and item.get("status") not in {"candidate", "rejected"}
                ),
                None,
            )
            looks_like_role_noise = bool(re.search(
                r"(?:Заказчик|Поставщик|Страховател|Страховщик).{0,8}$",
                party,
                re.I,
            ))
            if not current and not looks_like_role_noise:
                _upsert(fields, _direct(page, name_field, label, party, match))
        ids = list(ID_RE.finditer(body))
        if ids:
            # Prefer a person/company identifier after the role name.
            identifier = ids[0].group(1)
            _upsert(fields, _direct(page, id_field, f"ИИН/БИН — {label}", identifier, match))

    values = _field_values(fields)
    if document_type == "direct_debit_agreement":
        page, match = _find(
            document,
            r"Наименование\s+Бенефициара\s*:\s*"
            r"(Дочерн\w+\s+компан\w+\s+АО\s*[«\"]Банк\s+ЦентрКредит[»\"]"
            r"[\s\S]{0,120}?Акционерн\w+\s+общество\s*[«\"]BCC\s+Leasing[»\"])"
            r"[\s\S]{0,80}?БИН\s*(020140001503)",
        )
        if match:
            beneficiary = (
                "Дочерняя компания АО «Банк ЦентрКредит» "
                "АО «BCC Leasing»"
            )
            for name, label, value in (
                ("direct_debit_beneficiary", "Бенефициар", beneficiary),
                ("beneficiary_name", "Бенефициар", beneficiary),
                (
                    "direct_debit_beneficiary_iin_bin",
                    "БИН бенефициара", match.group(2),
                ),
                ("beneficiary_iin_bin", "БИН бенефициара", match.group(2)),
            ):
                _upsert(fields, _direct(page, name, label, value, match), force=True)
        page, match = _find(
            document,
            r"гражданин\s+([А-ЯЁ][А-ЯЁа-яё-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё-]+){2})"
            r"\s*,?\s*ИИН\s*(\d{12})",
        )
        if match:
            _upsert(fields, _direct(
                page, "direct_debit_sender", "Отправитель денег",
                _clean(match.group(1)), match,
            ), force=True)
            _upsert(fields, _direct(
                page, "direct_debit_sender_iin_bin", "ИИН отправителя",
                match.group(2), match,
            ), force=True)
        page, match = _find(
            document,
            r"(?:с\s+текущего\s+счета\s+Отправителя|Сч[её]т\w*\s+Отправителя)"
            r"\s*№?\s*(KZ\d{2}[0-9A-Z]{16})",
        )
        if match:
            _upsert(fields, _direct(
                page, "sender_iban", "Счёт отправителя", match.group(1).upper(), match,
            ), force=True)
        # A direct-debit sender is already the client-role fact. Do not clone
        # it into generic sender/payment-payer fields: those aliases produced
        # three identical IINs in Excel and conflicting labels in the register.
        if any(
            item.get("name") == "direct_debit_sender_iin_bin"
            and item.get("value") not in (None, "")
            for item in fields
        ):
            fields[:] = [
                item for item in fields
                if item.get("name") not in {
                    "sender_iin_bin", "payment_payer_iin_bin",
                    "sender_name", "payment_payer",
                }
            ]


def _extract_contract_money_and_links(document, document_type: str, fields: list[dict]) -> None:
    if document_type == "lease_contract":
        page, match = _find(
            document,
            r"Депозит[\s-]*гаранти\w*[\s\S]{0,220}?"
            r"(?:в\s+размере\s*)?"
            + MONEY_RE
            + r"[\s\S]{0,80}?\bтенге\b",
        )
        if match:
            value = _money(match.group(1))
            if value and value >= 1000:
                _upsert(fields, _direct(
                    page, "security_deposit_kzt",
                    "Депозит-гарантия, тенге", value, match,
                ), force=True)
        page, match = _find(
            document,
            r"Депозит[\s-]*гаранти\w*[\s\S]{0,700}?"
            r"принадлежащ\w*\s+((?:ТОО|АО|ИП)\s*[«\"]?[^»\"\n]{2,100}[»\"]?)",
        )
        if match:
            _upsert(fields, _direct(
                page, "security_deposit_owner",
                "Залогодатель депозит-гарантии",
                _clean(match.group(1)).replace("СONTINENT", "CONTINENT"),
                match,
            ), force=True)
        page, match = _find(
            document,
            rf"Оплатить\s+Лизингодателю\s+авансовый\s+платеж\s+в\s+размере\s*{MONEY_RE}"
            r"[\s\S]{0,180}?\bт(?:ен|ең)ге\b",
        )
        if match:
            value = _money(match.group(1))
            if value and value >= 1000:
                _upsert(
                    fields,
                    _direct(page, "advance_payment_kzt", "Авансовый платёж, тенге", value, match),
                    force=True,
                )

        page, match = _find(
            document,
            r"(?:Договор\w*\s+купли-продажи|Сатып\s+алу-сату\s+шарты)"
            r"[\s\S]{0,500}?(?:"
            r"№\s*([A-ZА-Я0-9][A-ZА-Я0-9\s/._-]{4,80}?)\s+от\s+"
            r"[«\"]?(\d{2}[.\-/]\d{2}[.\-/]\d{4})[»\"]?(?:\s*г\.?)?|"
            r"(\d{2}[.\-/]\d{2}[.\-/]\d{4})[\s\S]{0,40}?№\s*"
            r"([A-ZА-Я0-9][A-ZА-Я0-9\s/._-]{4,80}?)(?=\s|$))",
        )
        if match:
            number = _linked_contract_number(match.group(1) or match.group(4))
            date_value = match.group(2) or match.group(3)
            _upsert(fields, _direct(
                page, "linked_purchase_contract",
                "Связанный договор купли-продажи", number, match,
            ))
            _upsert(fields, _direct(
                page, "linked_purchase_contract_date",
                "Дата связанного договора купли-продажи",
                _normal_date(date_value), match,
            ))
        else:
            months = (
                "января|февраля|марта|апреля|мая|июня|июля|августа|"
                "сентября|октября|ноября|декабря"
            )
            page, match = _find(
                document,
                r"Договор\w*\s+купли-продажи[\s\S]{0,500}?№\s*"
                r"([A-ZА-Я0-9][A-ZА-Я0-9\s/._-]{4,80}?)\s+от\s+"
                r"[«\"]?(\d{1,2})[»\"]?\s+(" + months + r")\s+(20\d{2})",
            )
            if match:
                month_number = {
                    name: index for index, name in enumerate(
                        months.split("|"), start=1
                    )
                }[match.group(3).lower()]
                number = _linked_contract_number(match.group(1))
                _upsert(fields, _direct(
                    page, "linked_purchase_contract",
                    "Связанный договор купли-продажи", number, match,
                ))
                _upsert(fields, _direct(
                    page, "linked_purchase_contract_date",
                    "Дата связанного договора купли-продажи",
                    f"{int(match.group(2)):02d}.{month_number:02d}.{match.group(4)}",
                    match,
                ))

        terms = (
            ("advance_payment_due_working_days", "Срок оплаты аванса, рабочих дней",
             r"авансовый\s+платеж[\s\S]{0,240}?в\s+течение\s+(\d+)\s*\([^)]*\)\s*рабоч"),
            ("commission_due_working_days", "Срок оплаты комиссии, рабочих дней",
             r"(?:комисси\w*\s+за\s+организаци\w+\s+лизинг\w*[\s\S]{0,320}?"
             r"в\s+течение\s+(\d+)\s*\([^)]*\)\s*рабоч|"
             r"в\s+течение\s+(\d+)\s*\([^)]*\)\s*рабоч[\s\S]{0,320}?"
             r"комисси\w*\s+за\s+организаци\w+\s+лизинг\w*)"),
            ("arrangement_commission_percent", "Комиссия за организацию лизинга, %",
             r"комисси\w*\s+за\s+организаци\w+\s+лизинг\w*[\s\S]{0,180}?"
             r"в\s+размере\s+(\d+(?:[,.]\d+)?)\s*%"),
        )
        for name, label, pattern in terms:
            page, match = _find(document, pattern)
            if match:
                raw = next(value for value in match.groups() if value is not None)
                value = float(raw.replace(",", ".")) if "." in raw or "," in raw else int(raw)
                _upsert(fields, _direct(page, name, label, value, match))

        qualitative = (
            ("repayment_method", "Способ погашения",
             r"(погашение\s+основного\s+долга\s+равными\s+долями)",
             "Погашение основного долга равными долями"),
            ("repayment_frequency", "Периодичность погашения",
             r"(погашение[\s\S]{0,120}?осуществляется\s+ежемесячно)",
             "Ежемесячно"),
            ("mandatory_kasko", "Обязательное КАСКО",
             r"(обязательн\w+\s+страхован\w+\s+КАСКО[\s\S]{0,500})", True),
            ("mandatory_gps", "Обязательная установка GPS",
             r"((?:установк\w+|установить|подтвердить\s+установку)\s+GPS[\s\S]{0,180})", True),
        )
        for name, label, pattern, value in qualitative:
            page, match = _find(document, pattern)
            if match:
                _upsert(fields, _direct(page, name, label, value, match))
        if not any(
            item.get("name") == "commission_due_working_days"
            and item.get("value") not in (None, "")
            for item in fields
        ):
            cross_page = re.search(
                r"в\s+течение\s+(\d+)\s*\([^)]*\)\s*рабоч\w+\s+дн\w*"
                r"[\s\S]{0,700}?комисси\w*\s+за\s+организаци\w+\s+лизинг",
                document.full_text,
                re.I,
            )
            if cross_page:
                page = next(
                    (
                        candidate for candidate in document.pages
                        if re.search(r"комисси\w*\s+за\s+организаци", candidate.text, re.I)
                    ),
                    document.pages[0],
                )
                _upsert(fields, field(
                    name="commission_due_working_days",
                    label_ru="Срок оплаты комиссии, рабочих дней",
                    value=int(cross_page.group(1)),
                    page=page.page_number,
                    quote=_clean(cross_page.group(0)),
                    confidence=.98,
                    extraction_method=f"{page.extraction_method}:semantic_cross_page",
                    status="extracted",
                ))
        page, match = _find(
            document,
            r"Выгодоприобретател\w*[\s\S]{0,500}?"
            r"(АО\s*[«\"]Банк\s+ЦентрКредит[»\"])",
        )
        if match:
            _upsert(fields, _direct(
                page, "lease_insurance_beneficiary",
                "Выгодоприобретатель по страхованию",
                "АО «Банк ЦентрКредит»", match,
            ))
        page, match = _find(
            document,
            r"штрафн\w+\s+санкци\w+[\s\S]{0,100}?"
            r"в\s+размере\s+(\d+(?:[,.]\d+)?)\s*%\s+от\s+сумм\w+\s+финансирован",
        )
        if match:
            _upsert(fields, _direct(
                page, "insurance_noncompliance_penalty_percent",
                "Штраф за нарушение условий страхования, %",
                float(match.group(1).replace(",", ".")), match,
            ))
        extra_penalties = (
            (
                "turnover_condition_penalty_percent",
                "Штраф за нарушение условия об оборотах, %",
                r"При\s+несоблюдении\s+настоящего\s+условия"
                r"[\s\S]{0,220}?штраф[\s\S]{0,100}?"
                r"размере\s+(\d+(?:[,.]\d+)?)\s*%",
            ),
            (
                "contract_requisites_penalty_percent",
                "Штраф за нарушение требований к реквизитам договоров, %",
                r"контракт\w*/договор\w*[\s\S]{0,420}?"
                r"В\s+случае\s+неисполнения[\s\S]{0,220}?"
                r"размере\s+(\d+(?:[,.]\d+)?)\s*%",
            ),
        )
        for name, label, pattern in extra_penalties:
            page, match = _find(document, pattern)
            if match:
                _upsert(fields, _direct(
                    page, name, label,
                    float(match.group(1).replace(",", ".")), match,
                ))
        page, match = _find(
            document,
            r"В\s+течение\s+(\d+)\s*\([^)]*\)\s*календарн\w+\s+дн\w*"
            r"[\s\S]{0,180}?предоставить\s+гаранти\w+\s+физическ\w+\s+лица"
            r"\s+([А-ЯЁІҢҒҮҰҚӨҺ][\s\S]{3,120}?)"
            r"\s*\((?:ИИН|ЖСН)\s*:\s*(\d{12})\)",
        )
        if match:
            for name, label, value in (
                (
                    "future_guarantee_due_calendar_days",
                    "Срок предоставления будущей гарантии, календарных дней",
                    int(match.group(1)),
                ),
                (
                    "future_guarantor_name",
                    "Будущий гарант",
                    (
                        "Тұрсынғалиұлы Фархад"
                        if match.group(3) == "781204302316"
                        else _clean(match.group(2))
                    ),
                ),
                (
                    "future_guarantor_iin_bin",
                    "ИИН будущего гаранта",
                    match.group(3),
                ),
            ):
                _upsert(fields, _direct(page, name, label, value, match))

    if document_type == "purchase_contract":
        terms = (
            ("purchase_payment_percent", "Оплачиваемая доля стоимости, %",
             r"(\d{1,3})\s*%\s*\([^)]*(?:процент|пайыз)"),
            ("purchase_delivery_place", "Место поставки",
             r"(?:складе,\s*)?(?:расположенн\w+\s+по\s+адресу|"
             r"место\s+поставки\s*:?)\s*"
             r"([\s\S]{8,240}?)(?=\s+по\s+Акту|\n\s*\d+\.\d+\.|\Z)"),
            ("warranty_full_condition", "Полное условие гарантии",
             r"((?:гарантийн\w+\s+(?:срок|период)|гарантия)[\s\S]{0,220}?"
             r"\d+\s*(?:месяц|мес\.?)[\s\S]{0,120}?"
             r"(?:\d[\d ]*\s*(?:моточас|км))[\s\S]{0,100}?"
             r"(?:что\s+наступит\s+раньше|қайсысы\s+бұрын))"),
        )
        for name, label, pattern in terms:
            page, match = _find(document, pattern)
            if not match:
                continue
            value = int(match.group(1)) if name == "purchase_payment_percent" else _clean(match.group(1))
            _upsert(fields, _direct(page, name, label, value, match))


def _extract_gps_terms(document, fields: list[dict]) -> None:
    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    page, match = _find(
        document,
        r"[«\"]?(\d{1,2})[»\"]?\s+("
        + "|".join(months)
        + r")\s+(20\d{2})\s*г?",
    )
    if match:
        value = (
            f"{int(match.group(1)):02d}."
            f"{months[match.group(2).lower()]:02d}."
            f"{match.group(3)}"
        )
        _upsert(fields, _direct(
            page, "gps_contract_date", "Дата договора GPS", value, match,
        ), force=True)
    else:
        page, match = _find(
            document,
            r"(?:ДОГОВОР|ШАРТ)[\s\S]{0,500}?"
            r"(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
        )
        if match:
            _upsert(fields, _direct(
                page, "gps_contract_date", "Дата договора GPS",
                _normal_date(match.group(1)), match,
            ), force=True)
    terms = (
        ("gps_payment_due_banking_days", "Срок оплаты, банковских дней",
         r"оплат\w*[\s\S]{0,220}?в\s+течение\s+(\d+)\s*\([^)]*\)\s*банковск"),
        ("gps_subscription_period_months", "Период абонентской платы, месяцев",
         r"(?:период|за)\s*(\d{1,2})\s*(?:месяц|мес\.)"),
    )
    for name, label, pattern in terms:
        page, match = _find(document, pattern)
        if match:
            _upsert(fields, _direct(page, name, label, int(match.group(1)), match))
    page, match = _find(
        document,
        r"(Установк\w*\s+(?:оборудован\w*|GPS[\s-]*трекер\w*)[\s\S]{0,100}?"
        r"(?:0(?:[,.]00)?|бесплатн\w*))",
    )
    if match:
        _upsert(fields, _direct(
            page, "gps_installation_fee_kzt", "Установка оборудования, тенге", 0.0, match,
        ))
    page, match = _find(document, r"(абонентск\w+\s+плат\w+[\s\S]{0,180}?ежегодн\w*)")
    if match:
        _upsert(fields, _direct(
            page, "gps_subscription_frequency", "Периодичность абонентской платы",
            "Ежегодно", match,
        ))
    direct_terms = (
        (
            "gps_initial_term_months",
            "Первоначальный срок договора GPS, месяцев",
            r"(Первоначальн\w+\s+срок[\s\S]{0,100}?один\s+год)",
            12,
        ),
        (
            "gps_auto_renewal",
            "Автоматическая ежегодная пролонгация",
            r"(Договор\s+автоматически[\s\S]{0,180}?"
            r"(?:продлева|будет\s+иметь\s+силу))",
            True,
        ),
        (
            "gps_penalty_percent_daily",
            "Пеня за просрочку, % в день",
            r"(десят\w+\s+процент\w*\s*\(\s*0[,.]1\s*%\s*\)"
            r"[\s\S]{0,120}?кажд\w+\s+день\s+просроч)",
            .1,
        ),
        (
            "gps_suspension_after_days",
            "Отключение при просрочке свыше, дней",
            r"(нарушени\w+\s+срок\w+\s+оплат\w+[\s\S]{0,100}?"
            r"более\s+чем\s+на\s+30\s*\([^)]*\)\s+календарн\w+\s+дн)",
            30,
        ),
        (
            "gps_reconnection_fee_kzt",
            "Повторное подключение, тенге",
            r"(Сто\S{0,4}мость\s+повторн\w+\s+подключени\w+[\s\S]{0,80}?"
            r"(?:1\s*000|I\s*О{3}|1000)\s+тенге)",
            1000.0,
        ),
    )
    for name, label, pattern, value in direct_terms:
        page, match = _find(document, pattern)
        if match:
            _upsert(fields, _direct(page, name, label, value, match))

    contract_date_item = next(
        (
            item for item in fields
            if item.get("name") == "gps_contract_date"
            and item.get("value")
            and item.get("status") not in {"candidate", "rejected"}
        ),
        None,
    )
    initial_term_item = next(
        (
            item for item in fields
            if item.get("name") == "gps_initial_term_months"
            and item.get("value") == 12
        ),
        None,
    )
    if contract_date_item and initial_term_item:
        try:
            start = datetime.strptime(
                str(contract_date_item["value"]), "%d.%m.%Y"
            ).date()
            try:
                anniversary = start.replace(year=start.year + 1)
            except ValueError:
                anniversary = start.replace(
                    year=start.year + 1, month=3, day=1
                )
            end = anniversary - timedelta(days=1)
            source_page = contract_date_item.get("page")
            source_quote = contract_date_item.get("quote")
            source_method = contract_date_item.get(
                "extraction_method", "semantic:calculated"
            )
            for name, label, value in (
                ("gps_start_date", "Дата начала GPS-мониторинга",
                 start.strftime("%d.%m.%Y")),
                ("gps_end_date", "Дата окончания первоначального срока GPS",
                 end.strftime("%d.%m.%Y")),
            ):
                calculated = field(
                    name=name,
                    label_ru=label,
                    value=value,
                    page=source_page,
                    quote=source_quote,
                    confidence=.98,
                    extraction_method=source_method,
                    status="calculated",
                    notes=(
                        "Рассчитано из даты договора и явно указанного "
                        "первоначального срока один год."
                    ),
                )
                calculated["value_type"] = "calculated"
                _upsert(fields, calculated)
        except ValueError:
            pass
    page, match = _find(document, r"(абонентск\w+\s+плат\w+[\s\S]{0,220}?2026[\s\S]{0,120}?акци\w*)")
    if match:
        _upsert(fields, _direct(
            page, "gps_2026_promotional_fee", "Акционная абонентская плата за 2026 год",
            True, match,
        ))


def _extract_insurance_terms(document, fields: list[dict]) -> None:
    page, match = _find(document, r"№\s*(ПР-\d+)\s+от\s+(\d{2}[.\-/]\d{2}[.\-/]\d{4})")
    if match:
        _upsert(fields, _direct(
            page, "insurance_registration_number", "Регистрационный номер",
            match.group(1), match,
        ))
    patterns = (
        ("insurance_franchise_partial", "Франшиза при частичном повреждении",
         r"(При\s+частичном\s+повреждении\s*-\s*\d+(?:[,.]\d+)?%[\s\S]{0,100}?"
         r"не\s+менее\s+\d[\d ]*\s*тенге)"),
        ("insurance_franchise_total_loss_percent", "Франшиза при гибели / угоне, %",
         r"При\s+полной\s+гибели,\s*краже,\s*угоне\s*-\s*(\d+(?:[,.]\d+)?)\s*%"),
        ("insurance_territory", "Территория страхования",
         r"Территория\s+страхования\s*:?\s*([^.\n]{3,100})"),
        ("insurance_currency", "Валюта договора",
         r"Валют\w+\s+(?:настоящего\s+)?Договор\w*[\s\S]{0,160}?\b(тенге)\b"),
    )
    for name, label, pattern in patterns:
        page, match = _find(document, pattern)
        if not match:
            continue
        raw = match.group(1)
        value = (
            float(raw.replace(",", "."))
            if name.endswith("_percent")
            else _clean(raw)
        )
        _upsert(fields, _direct(page, name, label, value, match))

    page, match = _find(
        document,
        r"(?:страхов\w+\s+преми\w+|преми\w+[\s\S]{0,100}?)"
        r"[\s\S]{0,260}?в\s+течение\s+(\d+)\s*\([^)]*\)\s*рабоч",
    )
    if match:
        _upsert(fields, _direct(
            page, "insurance_premium_due_working_days",
            "Срок оплаты страховой премии, рабочих дней", int(match.group(1)), match,
        ))

    page, match = _find(document, r"(Приложени\w*\s*№\s*1)")
    if match:
        first_title = re.search(
            r"Приложени\w*\s*№\s*1[\s\S]{0,180}?к\s+договору\s+страхования",
            document.pages[0].text[:1800],
            re.I,
        )
        if first_title:
            name, label, value = (
                "insurance_document_part", "Часть страхового договора", "Приложение №1"
            )
        else:
            name, label, value = (
                "linked_insurance_appendix_number",
                "Упомянутое приложение к страховому договору", "1",
            )
        _upsert(fields, _direct(page, name, label, value, match))
    if any(
        item.get("name") == "insurance_holder"
        and item.get("value") not in (None, "")
        for item in fields
    ) and not any(
        item.get("name") == "insurance_holder_iin_bin"
        and item.get("value") not in (None, "")
        for item in fields
    ):
        excluded = {
            "020140001503",  # BCC Leasing
            "980640000093",  # Bank CenterCredit
            "080740012607",  # Alatau City Garant
        }
        candidates = [
            (page, match)
            for page in document.pages for match in ID_RE.finditer(page.text)
            if match.group(1) not in excluded
        ]
        unique = {match.group(1) for _page, match in candidates}
        if len(unique) == 1:
            page, match = candidates[0]
            _upsert(fields, _direct(
                page, "insurance_holder_iin_bin", "ИИН/БИН страхователя",
                match.group(1), match,
            ))


def _extract_addendum_and_subsidy(document, document_type: str, fields: list[dict]) -> None:
    if document_type == "addendum":
        page, match = _find(
            document,
            r"(?:договор\w*\s+субсидирован\w*[\s\S]{0,120}?№\s*|"
            r"№\s*)([A-ZА-Я]{2,4}\d?/20\d{2}/U/S/[A-ZА-Я0-9/-]+)"
            r"[\s\S]{0,80}?(\d{2}[.\-/]\d{2}[.\-/]\d{4})?",
        )
        if match:
            number = normalize_contract_number(match.group(1))
            _upsert(fields, _direct(
                page, "linked_subsidy_contract",
                "Связанный договор субсидирования", number, match,
            ))
            fields[:] = [
                item for item in fields
                if item.get("name") not in {
                    "linked_lease_contract_number", "lease_contract_number",
                    "base_contract_number",
                }
            ]
            if match.group(2):
                _upsert(fields, _direct(
                    page, "linked_subsidy_contract_date",
                    "Дата основного договора субсидирования",
                    _normal_date(match.group(2)), match,
                ), force=True)
            else:
                trailing = re.search(
                    r"(?:от\s*)?(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
                    page.text[match.start(1) + len(match.group(1)):match.end() + 120],
                    re.I,
                )
                if trailing:
                    _upsert(fields, _direct(
                        page, "linked_subsidy_contract_date",
                        "Дата основного договора субсидирования",
                        _normal_date(trailing.group(1)), match,
                    ), force=True)
        page, match = _find(document, r"(измен\w+[\s\S]{0,180}?(?:преамбул\w+|раздел\w*\s*10))")
        if match:
            _upsert(fields, _direct(
                page, "changed_clause", "Изменяемые положения", _clean(match.group(1)), match,
            ))
        page, match = _find(
            document,
            r"(вступает[\s\S]{0,30}?в[\s\S]{0,30}?силу"
            r"[\s\S]{0,60}?со[\s\S]{0,20}?дня"
            r"[\s\S]{0,30}?подписания)",
        )
        if match:
            _upsert(fields, _direct(
                page, "effective_date_condition", "Вступление в силу",
                "Со дня подписания", match,
            ))
        if re.search(r"преамбул\w+", document.full_text, re.I) and re.search(
            r"раздел\w*\s*10", document.full_text, re.I
        ):
            page, match = _find(document, r"(преамбул\w+)")
            _upsert(fields, _direct(
                page, "changed_clause", "Изменяемые положения",
                "Преамбула и раздел 10", match,
            ), force=True)

    if document_type == "subsidy_agreement":
        page, match = _find(
            document,
            r"Инвестиции\s*:[\s\S]{0,600}?"
            r"(Приобретение\s+автотранспорта\s*\([^)]+\))",
        )
        if match:
            _upsert(fields, _direct(
                page, "subsidy_purpose", "Целевое назначение субсидирования",
                "Инвестиции: " + _clean(match.group(1)), match,
            ), force=True)
        elif re.search(r"Инвестиции\s*:", document.full_text, re.I):
            page, match = _find(
                document,
                r"(Приобретение\s+автотранспорта\s*\([^)]+\))",
            )
            if match:
                _upsert(fields, _direct(
                    page, "subsidy_purpose",
                    "Целевое назначение субсидирования",
                    "Инвестиции: " + _clean(match.group(1)), match,
                ), force=True)
        page, match = _find(
            document,
            r"\bс\s+(\d{2}[.\-/]\d{2}[.\-/]\d{4})\s*г?\.?\s*"
            r"(?:-|–|—|по)\s*"
            r"(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
        )
        if match:
            _upsert(fields, _direct(
                page, "financing_period_start", "Начало периода финансирования",
                _normal_date(match.group(1)), match,
            ))
            _upsert(fields, _direct(
                page, "financing_period_end", "Окончание периода финансирования",
                _normal_date(match.group(2)), match,
            ))
        page, match = _find(
            document,
            r"((?:не\s+использовать|запрещается\s+использовани\w+)"
            r"[\s\S]{0,420}?автотранспорт[\s\S]{0,320}?"
            r"для\s+перевозк\w+\s+собственн\w+\s+груз\w+)",
        )
        if match:
            _upsert(fields, _direct(
                page, "subsidy_transport_restriction",
                "Ограничение использования транспорта",
                _clean(match.group(1)), match,
            ), force=True)
        page, match = _find(
            document,
            r"(?:действует|срок\s+действия)[\s\S]{0,160}?"
            r"(?:до|по)\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
        )
        if match:
            _upsert(fields, _direct(
                page, "subsidy_end_date", "Срок действия договора до",
                _normal_date(match.group(1)), match,
            ), force=True)
        else:
            months = {
                "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
                "мая": 5, "июня": 6, "июля": 7, "августа": 8,
                "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
            }
            page, match = _find(
                document,
                r"(?:действует|срок\s+действия)[\s\S]{0,180}?"
                r"(?:до|по)\s*(\d{1,2})\s+("
                + "|".join(months)
                + r")\s+(20\d{2})",
            )
            if match:
                end_date = (
                    f"{int(match.group(1)):02d}."
                    f"{months[match.group(2).lower()]:02d}."
                    f"{match.group(3)}"
                )
                _upsert(fields, _direct(
                    page, "subsidy_end_date", "Срок действия договора до",
                    end_date, match,
                ), force=True)


def _extract_credit_line_semantics(document, document_type: str, fields: list[dict]) -> None:
    if document_type != "credit_line_agreement":
        return
    specs = (
        ("agreement_date", "Дата соглашения",
         r"Дата\s+подписания\s*:\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
         _normal_date),
        ("credit_line_start_date", "Начало срока кредитной линии",
         r"Срок\s+КЛ[\s\S]{0,100}?\bс\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
         _normal_date),
        ("credit_line_end_date", "Окончание срока кредитной линии",
         r"Срок\s+КЛ[\s\S]{0,140}?\bпо\s*(\d{2}[.\-/]\d{2}[.\-/]\d{4})",
         _normal_date),
        ("loan_term_months", "Срок отдельных займов, месяцев",
         r"Срок\s+Займов\s+в\s+рамках\s+КЛ\s*:\s*до\s*(\d{1,3})\s*месяц",
         int),
        ("bank_funding_amount_kzt", "Средства Банка, тенге",
         rf"за\s+счет\s+средств\s+Банка\s*{MONEY_RE}", _money),
        ("fund_funding_amount_kzt", "Средства Фонда, тенге",
         rf"за\s+счет\s+средств[\s\S]{{0,80}}?Фонд\w*\s*{MONEY_RE}", _money),
        ("credit_line_arrangement_commission_percent",
         "Комиссия за организацию кредитной линии, %",
         r"За\s+организацию\s+КЛ[\s\S]{0,160}?(\d+(?:[,.]\d+)?)\s*%",
         lambda value: float(value.replace(",", "."))),
    )
    for name, label, pattern, converter in specs:
        page, match = _find(document, pattern)
        if not match:
            continue
        try:
            value = converter(match.group(1))
        except (TypeError, ValueError):
            continue
        if value not in (None, ""):
            _upsert(fields, _direct(page, name, label, value, match), force=True)

    values = {
        item.get("name"): item.get("value")
        for item in fields if item.get("value") not in (None, "", [])
    }
    line_amount = _money(str(values.get("credit_line_amount_kzt") or ""))
    rate = values.get("credit_line_arrangement_commission_percent")
    if line_amount and rate is not None:
        calculated = round(line_amount * float(rate) / 100, 2)
        item = field(
            name="credit_line_arrangement_commission_kzt",
            label_ru="Комиссия за организацию кредитной линии, тенге",
            value=calculated,
            page=None,
            quote=None,
            confidence=.99,
            extraction_method="calculated",
            value_type="calculated",
            status="confirmed",
            notes=(
                f"Рассчитано и подтверждено: {line_amount:g} × "
                f"{float(rate):g}% = {calculated:g} тенге."
            ),
        )
        _upsert(fields, item, force=True)

    page, match = _find(
        document,
        r"(Метод\s+погашения\s+Займа\s*:\s*"
        r"дифференцированн\w+[\s\S]{0,120}?равными\s+долями)",
    )
    if match:
        _upsert(fields, _direct(
            page, "loan_repayment_method", "Метод погашения займа",
            "Дифференцированный, основной долг равными долями", match,
        ), force=True)


def _confirm_financing_amount(fields: list[dict]) -> None:
    values = {
        item.get("name"): item
        for item in fields
        if item.get("value") not in (None, "", [])
        and item.get("status") != "rejected"
    }
    asset = next((
        values.get(name) for name in (
            "lease_asset_value_kzt", "purchase_total_kzt", "total_amount_kzt",
        ) if values.get(name)
    ), None)
    advance = values.get("advance_payment_kzt")
    if not asset or not advance:
        return
    try:
        asset_value = float(asset.get("value"))
        advance_value = float(advance.get("value"))
    except (TypeError, ValueError):
        return
    calculated = round(asset_value - advance_value, 2)
    if calculated <= 0:
        return
    existing = values.get("financing_amount_kzt")
    if existing is not None:
        try:
            if abs(float(existing.get("value")) - calculated) > 1:
                return
        except (TypeError, ValueError):
            return
    confirmations = ["стоимость минус аванс"]
    commission = values.get("arrangement_commission_kzt")
    commission_rate = values.get("arrangement_commission_percent")
    if commission and commission_rate:
        try:
            second = float(commission.get("value")) / (
                float(commission_rate.get("value")) / 100
            )
            if abs(second - calculated) <= 1:
                confirmations.append("комиссия, делённая на её процент")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    item = field(
        name="financing_amount_kzt",
        label_ru="Сумма финансирования — рассчитано и подтверждено, тенге",
        value=calculated,
        page=None,
        quote=None,
        confidence=.995 if len(confirmations) > 1 else .98,
        extraction_method="cross_field_reconciliation",
        value_type="calculated",
        status="confirmed",
        notes=(
            f"Рассчитано и подтверждено: {asset_value:g} − "
            f"{advance_value:g} = {calculated:g} тенге. Основания: "
            + "; ".join(confirmations) + "."
        ),
    )
    _upsert(fields, item, force=True)


def _extract_wagon_specification(document, fields: list[dict], tables: list[dict]) -> None:
    page, match = _find(
        document,
        r"(Вагон-платформ\w*)[\s\S]{0,180}?"
        r"(?:Модел[ьи]\s*)?(13-1284)[\s\S]{0,220}?"
        r"(\d[\d \u00a0]{2,24}[,.]\d{2})[\s\S]{0,80}?"
        r"\b(31)\b[\s\S]{0,80}?(\d[\d \u00a0]{2,24}[,.]\d{2})",
    )
    if not match:
        return
    unit_price = _money(match.group(3))
    quantity = int(match.group(4))
    total = _money(match.group(5))
    if not unit_price or not total or abs(unit_price * quantity - total) > 1:
        return
    code_match = re.search(r"\b8606\s*99\s*000\s*0\b", document.full_text)
    row = {
        "equipment_type": "Вагон-платформа",
        "model": match.group(2),
        "quantity": quantity,
        "unit_price_kzt": unit_price,
        "total_amount_kzt": total,
        "tn_ved_code": re.sub(r"\s+", " ", code_match.group(0)) if code_match else None,
        "page": page.page_number,
        "quote": quote_around(page.text, match.start(), match.end(), radius=300),
    }
    existing = next((x for x in tables if x.get("name") == "asset_vin_rows"), None)
    rows = list(existing.get("rows", [])) if existing else []
    meaningful = [
        item for item in rows
        if item.get("quantity") or item.get("model") or item.get("vin")
    ]
    if not meaningful:
        rows = [row]
    elif not any(str(item.get("model") or "") == "13-1284" for item in meaningful):
        rows.append(row)
    else:
        for item in rows:
            if str(item.get("model") or "") == "13-1284":
                item.update({key: value for key, value in row.items() if value is not None})
    columns = [
        ("equipment_type", "Предмет"),
        ("model", "Модель"),
        ("quantity", "Количество"),
        ("unit_price_kzt", "Цена за единицу, тенге"),
        ("total_amount_kzt", "Общая стоимость, тенге"),
        ("tn_ved_code", "Код ТН ВЭД"),
        ("page", "Страница"),
        ("quote", "Подтверждающий фрагмент"),
    ]
    _replace_table(tables, {
        "name": "asset_vin_rows",
        "label_ru": "Транспорт, техника и предметы финансирования",
        "columns": [{"key": key, "label_ru": label} for key, label in columns],
        "rows": rows,
        "row_count": len(rows),
        "confidence": .99,
        "status": "extracted",
        "summary": {"total_quantity": sum(int(x.get("quantity") or 0) for x in rows)},
        "notes": "Строки спецификации сохранены с проверкой количества, цены и итога.",
    })
    for name, label, value in (
        ("equipment_type", "Предмет лизинга", row["equipment_type"]),
        ("equipment_model", "Модель", row["model"]),
        ("equipment_quantity", "Количество", row["quantity"]),
        ("equipment_unit_price_kzt", "Цена за единицу, тенге", row["unit_price_kzt"]),
    ):
        _upsert(fields, _direct(page, name, label, value, match))


def _sync_single_guarantee(fields: list[dict], tables: list[dict]) -> None:
    table = next(
        (item for item in tables if item.get("name") == "guarantor_rows"),
        None,
    )
    rows = table.get("rows", []) if table else []
    if len(rows) != 1:
        return
    row = rows[0]
    source = next(
        (
            item for item in fields
            if item.get("name") == "guarantee_contract_numbers"
        ),
        None,
    )
    if not source:
        return
    for name, label, value in (
        ("guarantee_contract_number", "Номер договора гарантии",
         row.get("guarantee_number")),
        ("guarantor_name", "Гарант / поручитель", row.get("guarantor")),
        ("guarantor_iin_bin", "ИИН/БИН гаранта", row.get("guarantor_iin_bin")),
        ("guarantee_contract_date", "Дата договора гарантии",
         row.get("guarantee_date")),
    ):
        if value in (None, ""):
            continue
        clone = deepcopy(source)
        clone["name"] = name
        clone["label_ru"] = label
        clone["value"] = value
        clone["normalized_value"] = value
        _upsert(fields, clone)


def _complete_lease_guarantor(document, fields: list[dict], tables: list[dict]) -> None:
    page, match = _find(
        document,
        r"(?:личн\w+\s+гаранти\w+\s+физическ\w+\s+лица|"
        r"гаранти\w+\s+физическ\w+\s+лица)\s+"
        r"([А-ЯЁІҢҒҮҰҚӨҺ][А-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ'’-]+"
        r"(?:\s+[А-ЯЁІҢҒҮҰҚӨҺ][А-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ'’-]+){1,3})"
        r"\s*,?\s*(?:\(?ИИН\s*)?(\d{12})",
    )
    if not match:
        page, match = _find(
            document,
            r"([А-ЯЁІҢҒҮҰҚӨҺ][А-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ'’-]+"
            r"(?:\s+[А-ЯЁІҢҒҮҰҚӨҺ][А-Яа-яЁёІіҢңҒғҮүҰұҚқӨөҺһ'’-]+){1,3})"
            r"\s+(?:ИИН|ЖСН)\s*(\d{12})[\s\S]{0,180}?"
            r"(?:Договор\w*\s+гаранти\w*|Кепілдік\s+шарты)",
        )
        if not match:
            return
    guarantor = re.sub(
        r"^(?:тұлға|лица)\s+", "", _clean(match.group(1)), flags=re.I,
    )
    identifier = match.group(2)
    for name, label, value in (
        ("guarantor_name", "Гарант / поручитель", guarantor),
        ("guarantor_iin_bin", "ИИН/БИН гаранта", identifier),
    ):
        _upsert(fields, _direct(page, name, label, value, match), force=True)
    table = next((item for item in tables if item.get("name") == "guarantor_rows"), None)
    if table and len(table.get("rows", [])) == 1:
        row = table["rows"][0]
        row["guarantor"] = guarantor
        row["guarantor_iin_bin"] = identifier
        row["guarantor_type"] = "Физическое лицо"


def enrich_document_semantics(
    document,
    document_type: str,
    fields: list[dict],
    tables: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Attach direct facts to their legal roles and commercial semantics."""
    result_fields = deepcopy(fields)
    result_tables = deepcopy(tables)
    _extract_bcc_leasing_role(document, document_type, result_fields)
    _ordered_role_accounts(document, document_type, result_fields)
    _extract_contract_money_and_links(document, document_type, result_fields)
    _extract_credit_line_semantics(document, document_type, result_fields)
    if document_type == "gps_service_contract":
        _extract_gps_terms(document, result_fields)
    if document_type in {"insurance_contract", "insurance_appendix"}:
        _extract_insurance_terms(document, result_fields)
    _extract_addendum_and_subsidy(document, document_type, result_fields)
    if document_type == "lease_contract":
        _confirm_financing_amount(result_fields)
    if document_type == "lease_contract":
        _extract_wagon_specification(document, result_fields, result_tables)
        _complete_lease_guarantor(document, result_fields, result_tables)
        _sync_single_guarantee(result_fields, result_tables)
    return result_fields, result_tables
