import zipfile

import pytest

from anywhere_computer.documents import DRAW, PRESENT, REL, SHEET, WORD, read_document
from anywhere_computer.models import ReadDocument

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def package(path, main, parts, extra_rel=""):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{REL}"><Relationship Id="main" '
            f'Type="{R}/officeDocument" Target="{main}"/>{extra_rel}</Relationships>',
        )
        for name, value in parts.items():
            archive.writestr(name, value)
    return path


def test_docx_paragraph_pagination_tab_table_and_unchanged_input(tmp_path):
    path = package(
        tmp_path / "sample.docx",
        "word/document.xml",
        {
            "word/document.xml": f'<w:document xmlns:w="{WORD[1:-1]}"><w:body>'
            "<w:p><w:r><w:t>日本語</w:t><w:tab/><w:t>text</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>",
        },
    )
    before = path.read_bytes()
    first = read_document(ReadDocument(path=str(path), limit=1))
    assert first["entries"][0]["text"] == "日本語\ttext"
    assert first["truncated"] and first["total_entries"] == 2
    second = read_document(ReadDocument(path=str(path), offset=first["next_offset"]))
    assert second["entries"][0]["text"] == "cell"
    assert path.read_bytes() == before


def test_xlsx_shared_inline_formula_and_sheet_selection(tmp_path):
    path = package(
        tmp_path / "book.xlsx",
        "xl/workbook.xml",
        {
            "xl/workbook.xml": f'<workbook xmlns="{SHEET[1:-1]}" xmlns:r="{R}">'
            '<sheets><sheet name="Data" sheetId="1" r:id="sheet"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}">'
            f'<Relationship Id="sheet" Type="{R}/worksheet" Target="worksheets/data.xml"/>'
            f'<Relationship Id="strings" Type="{R}/sharedStrings" Target="sharedStrings.xml"/>'
            "</Relationships>",
            "xl/sharedStrings.xml": f'<sst xmlns="{SHEET[1:-1]}"><si><t>shared</t></si></sst>',
            "xl/worksheets/data.xml": f'<worksheet xmlns="{SHEET[1:-1]}"><sheetData><row r="1">'
            '<c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>inline</t></is></c>'
            '<c r="C1"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>',
        },
    )
    result = read_document(ReadDocument(path=str(path), section="Data"))
    assert [entry["value"] for entry in result["entries"]] == ["shared", "inline", "3"]
    assert result["entries"][2]["formula"] == "1+2"
    assert result["formulas_evaluated"] is False
    original = path.read_bytes()
    selected = read_document(ReadDocument(path=str(path), section="Data", cell_range="$B$1:C1"))
    assert [entry["cell"] for entry in selected["entries"]] == ["B1", "C1"]
    assert selected["total_entries"] == 2
    page = read_document(ReadDocument(path=str(path), cell_range="B1:C1", offset=1, limit=1))
    assert page["entries"][0]["formula"] == "1+2"
    assert read_document(ReadDocument(path=str(path), cell_range="A2:C2"))["entries"] == []
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="not found"):
        read_document(ReadDocument(path=str(path), section="Missing"))


@pytest.mark.parametrize("value", ["A0", "B2:A1", "A1:B2:C3", "Sheet!A1", "XFE1"])
def test_invalid_cell_ranges(value):
    from anywhere_computer.documents import cell_bounds

    with pytest.raises(ValueError):
        cell_bounds(value)


def test_pptx_follows_presentation_order_not_filenames(tmp_path):
    path = package(
        tmp_path / "deck.pptx",
        "ppt/presentation.xml",
        {
            "ppt/presentation.xml": f'<p:presentation xmlns:p="{PRESENT[1:-1]}" xmlns:r="{R}">'
            '<p:sldIdLst><p:sldId id="256" r:id="second"/><p:sldId id="257" r:id="first"/>'
            "</p:sldIdLst></p:presentation>",
            "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL}">'
            f'<Relationship Id="first" Type="{R}/slide" Target="slides/slide1.xml"/>'
            f'<Relationship Id="second" Type="{R}/slide" Target="slides/slide2.xml"/>'
            "</Relationships>",
            "ppt/slides/slide1.xml": f'<a:p xmlns:a="{DRAW[1:-1]}">'
            "<a:r><a:t>later</a:t></a:r></a:p>",
            "ppt/slides/slide2.xml": f'<a:p xmlns:a="{DRAW[1:-1]}">'
            "<a:r><a:t>first</a:t></a:r></a:p>",
        },
    )
    result = read_document(ReadDocument(path=str(path)))
    assert [entry["text"] for entry in result["entries"]] == ["first", "later"]
    selected = read_document(ReadDocument(path=str(path), section="2"))
    assert selected["entries"] == [{"slide": 2, "text": "later"}]


def test_rejects_dtd_and_traversal_relationship(tmp_path):
    path = package(
        tmp_path / "entities.docx",
        "word/document.xml",
        {
            "word/document.xml": '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///private">]><x/>',
        },
    )
    with pytest.raises(ValueError, match="declarations"):
        read_document(ReadDocument(path=str(path)))
    traversal = package(tmp_path / "traversal.docx", "../../outside", {})
    with pytest.raises(ValueError, match="leaves"):
        read_document(ReadDocument(path=str(traversal)))


def test_xml_size_limit_is_checked_before_parsing(tmp_path, monkeypatch):
    import anywhere_computer.documents as documents

    path = package(
        tmp_path / "large.docx",
        "word/document.xml",
        {
            "word/document.xml": " " * 4096,
        },
    )
    monkeypatch.setattr(documents, "XML_LIMIT", 2048)
    with pytest.raises(ValueError, match="exceeds"):
        read_document(ReadDocument(path=str(path)))
