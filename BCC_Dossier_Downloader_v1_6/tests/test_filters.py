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
