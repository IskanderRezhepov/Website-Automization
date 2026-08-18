
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Classification:
    key: str
    label_ru: str
    confidence: float
    matched_keywords: list[str]
    alternatives: list[dict]


DOCUMENT_TYPES = {
    "real_estate_registration_notice": {
        "label": "Уведомление о государственной регистрации недвижимости",
        "title": [
            "уведомление о государственной регистрации",
            "мемлекеттік тіркеу туралы хабарлама",
        ],
        "strong": [
            "управление регистрации недвижимости",
            "объект недвижимости",
            "кадастровым номером",
            "зарегистрировано право",
        ],
        "support": [
            "правительство для граждан",
            "регистрационного дела",
            "недвижимое имущество",
        ],
    },
    "purchase_contract": {
        "label": "Договор купли-продажи для последующей передачи в финансовый лизинг",
        "title": [
            "договор купли продажи",
            "договор купли продажи товара",
        ],
        "strong": [
            "для последующей передачи в финансовый лизинг",
            "продавец продает",
            "покупатель покупает",
        ],
        "support": [
            "продавец",
            "покупатель",
            "товар",
            "оборудование",
            "условия поставки",
        ],
    },
    "lease_contract": {
        "label": "Договор финансового лизинга / заявление о присоединении",
        "title": [
            "договор финансового лизинга",
            "заявление о присоединении",
            "договор лизинга",
        ],
        "strong": [
            "лизингодатель",
            "лизингополучатель",
            "предмет лизинга",
        ],
        "support": [
            "авансовый платеж",
            "срок лизинга",
            "ставка вознаграждения",
        ],
    },
    "acceptance_act": {
        "label": "Акт приёма-передачи",
        "title": [
            "акт приема передачи",
            "акт приёма передачи",
            "қабылдау өткізу актісі",
            "приемо сдаточный акт",
        ],
        "strong": [
            "продавец передает",
            "покупатель принимает",
            "передал а покупатель принял",
        ],
        "support": [
            "итого",
            "vin",
            "наименование товара",
        ],
    },
    "payment_schedule": {
        "label": "График погашения / график платежей",
        "title": [
            "график погашения",
            "график платежей",
            "график погашения основного долга",
        ],
        "strong": [
            "остаток основного долга",
            "дата погашения",
            "сумма погашения процентов",
        ],
        "support": [
            "сумма займа",
            "сумма транша",
            "итого процентов",
        ],
    },
    "addendum": {
        "label": "Дополнительное соглашение",
        "title": [
            "дополнительное соглашение",
            "қосымша келісім",
        ],
        "strong": [
            "остаются в неизменном виде",
            "дополнить",
            "к договору финансового лизинга",
        ],
        "support": [
            "сумма транша",
            "номер транша",
        ],
    },
    "credit_line_agreement": {
        "label": "Соглашение об открытии кредитной линии",
        "title": [
            "соглашение об открытии кл",
            "соглашение об открытии кредитной линии",
            "кж ашу туралы келісім",
        ],
        "strong": [
            "сумма кл",
            "срок кл",
            "период доступности кл",
            "основные условия займов в рамках кл",
        ],
        "support": [
            "заемщик",
            "ставка вознаграждения по займу",
            "гарантия фонда",
        ],
    },
    "cash_pledge_agreement": {
        "label": "Договор залога денег на счёте",
        "title": [
            "договор залога денег на счете",
            "договор залога денег на счёте",
            "шоттағы ақшаны кепілге беру шарты",
        ],
        "strong": [
            "предмет залога",
            "депозитный счет",
            "счет вклада",
            "залогодатель",
        ],
        "support": [
            "залогодержатель",
            "договор вклада",
            "банковский счет",
        ],
    },
    "subsidy_agreement": {
        "label": "Договор субсидирования ставки вознаграждения",
        "title": [
            "договор субсидирования части ставки вознаграждения",
            "договор субсидирования",
        ],
        "strong": [
            "финансовое агентство",
            "субсидированию подлежит",
            "субсидируемой части ставки",
        ],
        "support": [
            "фонд развития предпринимательства даму",
            "получатель",
            "лизинговая компания",
        ],
    },
    "bank_guarantee_application": {
        "label": "Заявление о присоединении / банковская гарантия",
        "title": [
            "заявление о присоединении",
            "о предоставлении банковской гарантии",
            "банктік кепілдік беру туралы өтініш",
        ],
        "strong": [
            "сумма гарантии",
            "бенефициар",
            "принципал",
            "вид гарантии",
        ],
        "support": [
            "срок действия гарантии",
            "комиссионное вознаграждение",
            "способ выпуска гарантии",
        ],
    },
    "direct_debit_agreement": {
        "label": "Соглашение о прямом дебетовании банковского счёта",
        "title": [
            "соглашение о прямом дебетовании банковского счета",
            "соглашение о прямом дебетовании банковского счёта",
            "банктік шотты тікелей дебеттеу туралы келісім",
        ],
        "strong": [
            "отправитель",
            "прямого дебетования счета",
            "текущего счета отправителя",
            "бенефициар",
        ],
        "support": [
            "банк вправе осуществлять безналичный перевод",
            "договор гарантии",
            "счет отправителя",
        ],
    },
    "guarantee_contract": {
        "label": "Договор гарантии / поручительства",
        "title": [
            "договор гарантии",
            "договор поручительства",
        ],
        "strong": [
            "личная гарантия",
            "гарант",
            "поручитель",
        ],
        "support": [
            "обязательства по договору",
        ],
    },
    "insurance_contract": {
        "label": "Договор / полис страхования",
        "title": ["договор страхования", "страховой полис", "полис каско", "сертификат страхования"],
        "strong": ["страховщик", "страхователь", "страховая сумма", "страховая премия", "выгодоприобретатель"],
        "support": ["каско", "срок страхования", "страховой случай", "застрахованное имущество"],
    },
    "insurance_appendix": {
        "label": "Приложение к договору страхования",
        "title": [
            "приложение 1 к договору страхования",
            "приложение №1 к договору страхования",
            "приложение к договору страхования",
        ],
        "strong": [
            "страховая сумма",
            "страховая премия",
            "застрахованное имущество",
        ],
        "support": ["каско", "серия", "тариф"],
    },
    "gps_service_contract": {
        "label": "Договор GPS / спутникового мониторинга",
        "title": ["договор gps", "договор глонасс", "договор спутникового мониторинга", "акт установки gps"],
        "strong": ["мониторинг транспорта", "gps трекер", "навигационное оборудование", "абонентская плата"],
        "support": ["gps", "глонасс", "трекер", "местоположение транспорта"],
    },
    "payment_order": {
        "label": "Платёжное поручение",
        "title": ["платежное поручение", "төлем тапсырма"],
        "strong": ["отправитель денег", "бенефициар", "назначение платежа", "сумма прописью"],
        "support": ["код назначения платежа", "дата валютирования", "иик"],
    },
    "invoice": {
        "label": "Счёт / счёт на оплату / счёт-фактура",
        "title": [
            "счет на оплату",
            "счёт на оплату",
            "счет фактура",
            "счёт фактура",
        ],
        "strong": [
            "итого к оплате",
            "поставщик",
            "получатель",
        ],
        "support": [
            "бин",
            "банковские реквизиты",
        ],
    },
    "signature_receipt": {
        "label": "Квитанция о подписании",
        "title": [
            "квитанция о подписании",
        ],
        "strong": [
            "тип эцп",
            "дата подписания",
            "подписал",
        ],
        "support": [
            "doc id",
        ],
    },
}


FILENAME_SIGNALS = {
    "real_estate_registration_notice": [
        "уведомление о регистр",
        "государственной регистрации",
    ],
    "acceptance_act": ["акт приема", "акт приёма", "приема передачи"],
    "payment_schedule": ["график", "ag2"],
    "addendum": ["допик", "дополнитель"],
    "purchase_contract": ["дкп", "купли продажи"],
    "lease_contract": ["дфл", "дл ип", "лизинг"],
    "credit_line_agreement": ["открытии кл", "кредитной линии", "инд форма соглашения"],
    "cash_pledge_agreement": ["залог", "кепіл", "scan дфл 1"],
    "subsidy_agreement": ["субсид", "даму"],
    "direct_debit_agreement": ["прямого дебетования", "соглашение фл"],
    "guarantee_contract": ["гарант", "поруч"],
    "insurance_contract": ["страхов", "каско", "полис"],
    "insurance_appendix": ["прил", "каско"],
    "gps_service_contract": ["gps", "джпс", "глонасс", "мониторинг"],
    "payment_order": ["платежное поручение", "төлем тапсырма", "пп gps", "пп каско"],
    "invoice": ["счет", "счёт", "invoice"],
}


def _normalize(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-яәіңғүұқөһ0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _contains(haystack: str, needle: str) -> bool:
    return _normalize(needle) in haystack


def classify(text: str, filename: str = "") -> Classification:
    normalized_text = _normalize(text[:50000])
    first_page_text = _normalize(text[:8000])
    normalized_filename = _normalize(filename)

    # Explicit self-identification on page one outranks references in payment
    # purpose text and misleading filenames.
    if any(
        _contains(first_page_text, title)
        for title in DOCUMENT_TYPES["payment_order"]["title"]
    ):
        return Classification(
            key="payment_order",
            label_ru=DOCUMENT_TYPES["payment_order"]["label"],
            confidence=0.99,
            matched_keywords=["платежное поручение"],
            alternatives=[],
        )

    # Strong own-title discrimination for leasing vs purchase contracts.
    # Restrict to the top of page 1 so cross-references later in the document
    # cannot flip the schema.
    top_title_zone = first_page_text[:2200]
    if re.search(r"\bдоговор\s+купли\s+продажи(?:\s+автомобиля|\s+товара)?\b", top_title_zone):
        return Classification(
            key="purchase_contract",
            label_ru=DOCUMENT_TYPES["purchase_contract"]["label"],
            confidence=0.995,
            matched_keywords=["собственный заголовок: договор купли-продажи"],
            alternatives=[],
        )
    if re.search(r"\bдоговор\s+финансового\s+лизинга\b", top_title_zone):
        return Classification(
            key="lease_contract",
            label_ru=DOCUMENT_TYPES["lease_contract"]["label"],
            confidence=0.995,
            matched_keywords=["собственный заголовок: договор финансового лизинга"],
            alternatives=[],
        )

    # A leasing accession/application can mention an acceptance act many times
    # in its conditions.  The document title on page one must outrank those
    # internal references; otherwise a lease application can be misclassified
    # as an acceptance act.  Keep bank-guarantee applications separate.
    lease_application_title = bool(
        re.search(r"\bзаявлени[ея]\b.{0,100}?(?:о\s+присоединении|к\s+договору\s+присоединения)", first_page_text)
        or re.search(r"\bзаявление\s+о\s+присоединении\b", first_page_text)
    )
    lease_context = any(
        token in first_page_text
        for token in ("лизингополучатель", "лизингодатель", "финансового лизинга", "договор лизинга", "предмет лизинга")
    )
    bank_guarantee_context = any(
        token in first_page_text
        for token in ("банковской гарантии", "сумма гарантии", "принципал", "бенефициар")
    )
    if lease_application_title and lease_context and not bank_guarantee_context:
        return Classification(
            key="lease_contract",
            label_ru=DOCUMENT_TYPES["lease_contract"]["label"],
            confidence=0.99,
            matched_keywords=["заявление о присоединении", "финансовый лизинг"],
            alternatives=[],
        )

    # An appendix can repeat most policy terminology, but it is not evidence
    # that the underlying insurance contract itself was uploaded.
    if (
        re.search(
            r"\bприложение\s*(?:№|n)?\s*1\b.{0,260}?"
            r"\bк\s+договору\s+страхован",
            first_page_text,
        )
        or (
            "прил" in normalized_filename
            and "каско" in normalized_filename
            and "приложение" in first_page_text
        )
    ):
        return Classification(
            key="insurance_appendix",
            label_ru=DOCUMENT_TYPES["insurance_appendix"]["label"],
            confidence=0.99,
            matched_keywords=["приложение к договору страхования"],
            alternatives=[],
        )

    # "ДС [№] к ДФЛ/ДГ/договору" is the standard document-name abbreviation
    # for an addendum.  Requiring both "ДС" and the base-contract marker keeps
    # this safe from unrelated two-letter filename fragments.
    if re.search(
        r"(?:^| )дс(?: \d{1,4})? к (?:дфл|дг|договор)",
        normalized_filename,
    ):
        return Classification(
            key="addendum",
            label_ru=DOCUMENT_TYPES["addendum"]["label"],
            confidence=0.97,
            matched_keywords=["дс к основному договору"],
            alternatives=[],
        )

    scored: list[tuple[str, float, list[str]]] = []

    for key, config in DOCUMENT_TYPES.items():
        score = 0.0
        matches: list[str] = []

        # A title on the first page is the strongest signal.
        for keyword in config["title"]:
            if _contains(first_page_text, keyword):
                score += 8.0
                matches.append(keyword)
            elif _contains(normalized_text, keyword):
                # A title appearing deep in the document may merely be a reference.
                score += 2.0
                matches.append(keyword)

        for keyword in config["strong"]:
            if _contains(first_page_text, keyword):
                score += 3.0
                matches.append(keyword)
            elif _contains(normalized_text, keyword):
                score += 1.5
                matches.append(keyword)

        for keyword in config["support"]:
            if _contains(normalized_text, keyword):
                score += 0.6
                matches.append(keyword)

        filename_hits = [
            signal
            for signal in FILENAME_SIGNALS.get(key, [])
            if _contains(normalized_filename, signal)
        ]
        if filename_hits:
            score += 5.0
            matches.extend(filename_hits)

        # Protect purchase contracts from being misclassified as acts merely
        # because later clauses mention an acceptance act.
        if key == "acceptance_act" and any(
            _contains(first_page_text, title)
            for title in DOCUMENT_TYPES["purchase_contract"]["title"]
        ):
            score -= 7.0

        if key == "purchase_contract" and "дкп" in normalized_filename:
            score += 3.0
            if _contains(first_page_text, "уведомление о государственной регистрации"):
                score -= 12.0
        if key == "gps_service_contract" and any(
            signal in normalized_filename for signal in ("джпс", "gps")
        ):
            score += 3.0

        if key == "guarantee_contract" and any(
            _contains(first_page_text, title)
            for title in DOCUMENT_TYPES["direct_debit_agreement"]["title"]
        ):
            score -= 10.0

        if key == "direct_debit_agreement" and any(
            _contains(first_page_text, title)
            for title in DOCUMENT_TYPES["direct_debit_agreement"]["title"]
        ):
            score += 8.0

        scored.append((key, score, sorted(set(matches))))

    scored.sort(key=lambda item: item[1], reverse=True)
    best_key, best_score, best_matches = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0

    alternatives = [
        {
            "key": key,
            "label_ru": DOCUMENT_TYPES[key]["label"],
            "score": round(score, 2),
        }
        for key, score, _ in scored[1:4]
        if score > 0
    ]

    if best_score < 6.0:
        return Classification(
            key="unknown",
            label_ru="Неизвестный тип документа",
            confidence=0.0,
            matched_keywords=best_matches,
            alternatives=alternatives,
        )

    margin = best_score - second_score
    confidence = min(0.99, 0.55 + best_score * 0.025 + max(0.0, margin) * 0.025)

    return Classification(
        key=best_key,
        label_ru=DOCUMENT_TYPES[best_key]["label"],
        confidence=round(confidence, 2),
        matched_keywords=best_matches,
        alternatives=alternatives,
    )
