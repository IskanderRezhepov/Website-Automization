from bcc_downloader import Customer, Dossier, Document, dossier_matches, is_target_document, select_customers


def doc(title, category=""):
    return Document("1", "1", category, title, "", "", "")


def test_leasing_application_matches():
    assert is_target_document(doc("Заявление о присоединении (договор лизинга)"))
    assert is_target_document(doc("ЗАЯВЛЕНИЕ О ПРИСОЕДИНЕНИИ - ДОГОВОР ЛИЗИНГА"))


def test_sale_contract_matches():
    assert is_target_document(doc("Договор купли-продажи"))
    assert is_target_document(doc("Договор купли продажи автомобиля"))


def test_unrelated_document_does_not_match():
    assert not is_target_document(doc("Акт приема-передачи"))
    assert not is_target_document(doc("Заявление клиента"))


def test_dossier_filter_matches_label_or_id():
    d = Dossier("889900", "F-959805394", "Лизинг")
    assert dossier_matches(d, "F-959805394")
    assert dossier_matches(d, "889900")
    assert not dossier_matches(d, "F-000000000")


def test_customer_key_selects_exact_client():
    customers = [Customer("05815638", "Досанов Нургазы Кубеевич"), Customer("12806028", "Nur stroy")]
    selected, error = select_customers(customers, "12806028")
    assert error is None
    assert selected == [customers[1]]


def test_multiple_clients_without_key_is_ambiguous():
    customers = [Customer("05815638", "A"), Customer("12806028", "B")]
    selected, error = select_customers(customers, "")
    assert selected == []
    assert "multiple clients found" in error


def test_wrong_customer_key_lists_available_keys():
    customers = [Customer("05815638", "A"), Customer("12806028", "B")]
    selected, error = select_customers(customers, "99999999")
    assert selected == []
    assert "05815638" in error and "12806028" in error


def test_normalized_document_filename_keeps_number_and_date():
    from bcc_downloader import normalized_document_filename
    doc = Document(
        document_id="123",
        architecture_id="1",
        category="Договор купли-продажи",
        title="ДОГОВОР КУПЛИ-ПРОДАЖИ АВТОМОБИЛЯ",
        number="NU8/2026/U/S/043581",
        date="13.08.2026",
        status="",
    )
    name = normalized_document_filename(doc, ".pdf")
    assert name.startswith("02_Договор купли-продажи")
    assert "NU8_2026_U_S_043581" in name
    assert "13.08.2026" in name
    assert name.endswith(".pdf")
    assert "/" not in name


def test_normalized_leasing_filename():
    from bcc_downloader import normalized_document_filename
    doc = Document(
        document_id="124",
        architecture_id="1",
        category="Заявление о присоединении",
        title="Заявление о присоединении (Договор лизинга)",
        number="952505077704991",
        date="01.08.2026",
        status="",
    )
    name = normalized_document_filename(doc, ".pdf")
    assert name.startswith("01_Заявление о присоединении_Договор лизинга")
    assert "952505077704991" in name


def test_purchase_category_does_not_override_act_note():
    assert not is_target_document(doc(
        "Акт к договору купли-продажи автомобиля",
        category="Договор купли-продажи",
    ))


def test_leasing_category_does_not_override_act_note():
    assert not is_target_document(doc(
        "Акт к договору финансового лизинга",
        category="Договор финансового лизинга",
    ))


def test_direct_financial_leasing_contract_matches():
    assert is_target_document(doc("Договор финансового лизинга № 12345"))
    assert is_target_document(doc("Договор лизинга № 12345"))


def test_addendum_is_not_treated_as_contract():
    assert not is_target_document(doc("Дополнительное соглашение к договору лизинга"))
    assert not is_target_document(doc("Приложение к договору купли-продажи"))


def test_abbreviation_dkp_matches_when_category_confirms():
    from bcc_downloader import document_kind
    d = Document("10", "1", "Договор купли-продажи, акты", "ДКП", "643/BL/27-07", "27.07.2026", "")
    assert document_kind(d) == "purchase_contract"


def test_abbreviation_dfl_matches_when_category_confirms():
    from bcc_downloader import document_kind
    d = Document("11", "1", "Договор финансового лизинга, акты", "ДФЛ", "FQ8/2026/Л/S/017633", "27.07.2026", "")
    assert document_kind(d) == "leasing_contract"


def test_dopik_abbreviations_are_rejected():
    from bcc_downloader import document_kind
    assert document_kind(Document("12", "1", "Договор купли-продажи, акты", "Допик к ДКП", "1", "", "")) is None
    assert document_kind(Document("13", "1", "Договор финансового лизинга, акты", "Допик к ДФЛ", "1", "", "")) is None


def test_best_purchase_candidate_prefers_real_number():
    from bcc_downloader import select_best_contract_documents
    weak = Document("20", "1", "Договор купли-продажи, акты", "ДКП", "1", "05.08.2026", "")
    strong = Document("21", "1", "Договор купли-продажи, акты", "ДКП", "643/BL/27-07", "27.07.2026", "")
    chosen = select_best_contract_documents([weak, strong])
    assert chosen == [strong]


def test_full_name_beats_abbreviation_even_if_both_exist():
    from bcc_downloader import select_best_contract_documents
    abbr = Document("30", "1", "Договор финансового лизинга, акты", "ДФЛ", "FQ8/2026/ABC", "", "")
    full = Document("31", "1", "Договор финансового лизинга, акты", "Договор лизинга - ИП AES", "FQ8/2026/L/S/017633", "", "")
    chosen = select_best_contract_documents([abbr, full])
    assert chosen == [full]


def test_v19_screenshot_case_downloads_all_numbered_applications_and_dkps():
    from bcc_downloader import select_important_documents
    docs = [
        Document("101", "1", "Договор финансового лизинга, акты", "соглашение ФЛ", "-", "08.07.2026", ""),
        Document("102", "1", "Договор финансового лизинга, акты", "соглашение ТОО", "-", "08.07.2026", ""),
        Document("103", "1", "Договор финансового лизинга, акты", "Заявление о присоединении", "AM2/2026/U/S/039531/3", "07.07.2026", ""),
        Document("104", "1", "Договор финансового лизинга, акты", "Заявление о присоединении", "AM2/2026/U/S/039531/1", "02.07.2026", ""),
        Document("105", "1", "Договор финансового лизинга, акты", "Заявление о присоединении", "AM2/2026/U/S/039531/2", "02.07.2026", ""),
        Document("201", "2", "Договор купли-продажи, акты", "ДКП", "860_269628", "07.07.2026", ""),
        Document("202", "2", "Договор купли-продажи, акты", "ДКП", "628 /BL/02-07", "02.07.2026", ""),
        Document("203", "2", "Договор купли-продажи, акты", "ДКП", "629/BL/02-07", "02.07.2026", ""),
    ]
    chosen = select_important_documents(docs)
    assert [d.document_id for d in chosen] == ["103", "104", "105", "201", "202", "203"]


def test_v19_actual_numbered_dfl_suppresses_joining_application_fallback():
    from bcc_downloader import select_important_documents
    dfl = Document("301", "1", "Договор финансового лизинга, акты", "ДФЛ", "FQ8/2026/I/S/017633", "27.07.2026", "")
    app = Document("302", "1", "Договор финансового лизинга, акты", "Заявление о присоединении", "FQ8/2026/I/S/017633/1", "27.07.2026", "")
    assert select_important_documents([dfl, app]) == [dfl]


def test_v19_all_numbered_dkps_are_kept_but_acts_are_rejected():
    from bcc_downloader import select_important_documents
    a = Document("401", "2", "Договор купли-продажи, акты", "ДКП", "100/BL/01", "01.01.2026", "")
    b = Document("402", "2", "Договор купли-продажи, акты", "Договор купли-продажи", "101/BL/01", "01.01.2026", "")
    act = Document("403", "2", "Договор купли-продажи, акты", "Акт к ДКП", "ACT-101", "01.01.2026", "")
    assert select_important_documents([a, b, act]) == [a, b]
