from bcc_downloader import (
    Architecture, filename_from_content_disposition, parse_architectures,
    parse_customers, parse_documents, parse_dossiers
)


def test_customer():
    html = '<tr><td><input name="r_pcust_shortname" value="123"></td><td>Client A</td><td>123</td></tr>'
    assert parse_customers(html)[0].key == "123"


def test_dossier():
    html = '<tr><td><input name="r_pdossier" value="88"></td><td>Досье №: ABC-1</td><td>Тип: Общие документы</td></tr>'
    d = parse_dossiers(html)[0]
    assert d.dossier_id == "88"


def test_architecture():
    html = '<ul class="parent" section="6" parchitecture="2819"><b>*Договор</b></ul>'
    a = parse_architectures(html)[0]
    assert (a.section, a.architecture_id) == ("6", "2819")


def test_document():
    html = '''<tr><td>x</td><td>x</td><td>x</td><td>Category</td><td>Title</td><td><img id="273" name="document_file"></td><td>0</td><td>N1</td><td></td><td>01.01.2026</td><td></td><td></td><td></td><td></td><td></td><td>Action</td><td>Status</td></tr>'''
    docs = parse_documents(html, Architecture("6", "2819", "A"))
    assert docs[0].document_id == "273"
    assert docs[0].number == "N1"


def test_filename():
    assert filename_from_content_disposition('attachment; filename="F-1/test.pdf"') == "test.pdf"
