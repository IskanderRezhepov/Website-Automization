from bcc_downloader import Dossier, Document, dossier_matches, is_target_document


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
