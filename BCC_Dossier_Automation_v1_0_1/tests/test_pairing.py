from pathlib import Path

import fitz

from bcc_downloader import (
    Document,
    DownloadedDocument,
    group_downloaded_contracts,
    match_purchase_to_lease,
)


def _pdf(path: Path, text: str):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def _doc(doc_id: str, title: str, number: str):
    return Document(doc_id, "1", "category", title, number, "02.07.2026", "")


def test_dkp_is_paired_by_internal_lease_reference(tmp_path):
    p = tmp_path / "dkp.pdf"
    _pdf(p, "Договор купли-продажи. Заявление о присоединении (Договор лизинга) № AM2/2026/U/S/039531/2")
    got, reason = match_purchase_to_lease(
        p,
        ["AM2/2026/U/S/039531/1", "AM2/2026/U/S/039531/2", "AM2/2026/U/S/039531/3"],
    )
    assert got == "AM2/2026/U/S/039531/2"
    assert "reference" in reason


def test_unmatched_dkp_goes_to_review(tmp_path):
    lease_path = tmp_path / "lease.pdf"
    dkp_path = tmp_path / "dkp.pdf"
    _pdf(lease_path, "Заявление о присоединении № AM2/2026/U/S/039531/1")
    _pdf(dkp_path, "Договор купли-продажи без номера связанного договора лизинга")
    items = [
        DownloadedDocument(_doc("1", "Заявление о присоединении", "AM2/2026/U/S/039531/1"), "leasing_application", lease_path),
        DownloadedDocument(_doc("2", "ДКП", "628/BL/02-07"), "purchase_contract", dkp_path),
    ]
    records = group_downloaded_contracts(items, tmp_path, lambda _: None)
    assert any(r["status"] == "REVIEW" and r["document_number"] == "628/BL/02-07" for r in records)
    assert (tmp_path / "_REVIEW_UNMATCHED").exists()


def test_three_pairs_are_separated(tmp_path):
    nums = [
        "AM2/2026/U/S/039531/1",
        "AM2/2026/U/S/039531/2",
        "AM2/2026/U/S/039531/3",
    ]
    items = []
    for i, number in enumerate(nums, 1):
        lp = tmp_path / f"lease{i}.pdf"
        dp = tmp_path / f"dkp{i}.pdf"
        _pdf(lp, f"Заявление о присоединении № {number}")
        _pdf(dp, f"ДКП № {620+i}/BL/02-07 для передачи по Заявлению о присоединении (Договор лизинга) № {number}")
        items.extend([
            DownloadedDocument(_doc(str(i), "Заявление о присоединении", number), "leasing_application", lp),
            DownloadedDocument(_doc(str(10+i), "ДКП", f"{620+i}/BL/02-07"), "purchase_contract", dp),
        ])
    records = group_downloaded_contracts(items, tmp_path, lambda _: None)
    pair_dirs = sorted(p for p in tmp_path.iterdir() if p.is_dir() and "_LEASE_" in p.name)
    assert len(pair_dirs) == 3
    assert all(len(list(p.glob("*.pdf"))) == 2 for p in pair_dirs)
    assert sum(r["status"] == "PAIRED" for r in records) == 6
