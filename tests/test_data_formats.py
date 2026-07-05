"""Unit tests for the Data source block's multi-format loaders (``agent_runtime.data_formats``).

Stdlib / pyyaml formats run everywhere. The heavy-lib formats (pdf/parquet/xlsx, and html via
bs4) are skipped when their optional dependency is not importable in this environment — they are
exercised live in-container after the image rebuild."""

from __future__ import annotations

import importlib.util

import pytest

from agent_runtime import data_formats as D


def _have(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


# ---- inline (text) formats -------------------------------------------------- #
def test_object_formats_inline():
    assert D.load_inline("json", '{"a": 1}') == {"a": 1}
    assert D.load_inline("yaml", "a: 1\nb: two") == {"a": 1, "b": "two"}
    assert D.load_inline("toml", "a = 1") == {"a": 1}
    # an already-parsed dict passes through
    assert D.load_inline("json", {"a": 1}) == {"a": 1}


def test_tabular_text_formats_inline():
    assert D.load_inline("csv", "x,y\n1,2\n3,4") == [{"x": "1", "y": "2"}, {"x": "3", "y": "4"}]
    assert D.load_inline("tsv", "x\ty\n1\t2") == [{"x": "1", "y": "2"}]
    assert D.load_inline("jsonl", '{"a": 1}\n{"a": 2}') == [{"a": 1}, {"a": 2}]


def test_document_text_formats_inline():
    assert D.load_inline("markdown", "# Hi") == "# Hi"
    assert D.load_inline("text", "plain") == "plain"
    assert D.load_inline("xml", "<r><a>hello</a><b>world</b></r>") == "hello\nworld"


def test_empty_and_error_degrade_to_category_empty():
    assert D.load_inline("json", "") == {}          # object empty
    assert D.load_inline("csv", "") == []           # rows empty
    assert D.load_inline("markdown", "") == ""      # text empty
    assert D.load_inline("json", "{not json") == {}  # parse error → empty, no raise
    # binary format has no inline form
    assert D.load_inline("pdf", "x") == ""
    assert D.load_inline("parquet", "x") == []


def test_parse_object_strict_and_lenient():
    assert D.parse_object_strict("json", '{"a": 1}') == {"a": 1}
    assert D.parse_object_strict("yaml", "a: 1") == {"a": 1}
    with pytest.raises(ValueError):
        D.parse_object_strict("json", "[1, 2]")     # array ≠ object
    with pytest.raises(ValueError):
        D.parse_object_strict("csv", "x,y")         # not an object format
    # lenient never raises
    assert D.parse_object("json", "[1,2]") == {}
    assert D.parse_object("yaml", "a: 1") == {"a": 1}


def test_load_file_missing_path_degrades(tmp_path):
    assert D.load_file("json", str(tmp_path / "nope.json")) == {}
    assert D.load_file("csv", str(tmp_path / "nope.csv")) == []


def test_load_file_json_and_csv(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"topic": "x", "n": 2}', encoding="utf-8")
    assert D.load_file("json", str(p)) == {"topic": "x", "n": 2}
    c = tmp_path / "d.csv"
    c.write_text("a,b\n1,2\n", encoding="utf-8")
    assert D.load_file("csv", str(c)) == [{"a": "1", "b": "2"}]


def test_taxonomy_is_consistent():
    # every ALL_FORMATS entry has an output category; binary ⊂ all; inline = all − binary
    for f in D.ALL_FORMATS:
        assert f in D.OBJECT_FORMATS or f in D.TABULAR_FORMATS or f in D.TEXT_FORMATS
    assert set(D.BINARY_FORMATS).issubset(D.ALL_FORMATS)
    assert set(D.INLINE_FORMATS) == set(D.ALL_FORMATS) - set(D.BINARY_FORMATS)


# ---- heavy-lib formats (skip if the optional dep is absent locally) --------- #
@pytest.mark.skipif(not _have("bs4"), reason="beautifulsoup4 not installed here")
def test_html_readable_text():
    html = "<html><body><h1>Title</h1><script>x=1</script><p>Body</p></body></html>"
    out = D.load_inline("html", html)
    assert "Title" in out and "Body" in out and "x=1" not in out


@pytest.mark.skipif(not _have("pyarrow"), reason="pyarrow not installed here")
def test_parquet_rows(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    p = tmp_path / "t.parquet"
    pq.write_table(pa.table({"a": [1, 2], "b": ["x", "y"]}), p)
    assert D.load_file("parquet", str(p)) == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


@pytest.mark.skipif(not _have("openpyxl"), reason="openpyxl not installed here")
def test_xlsx_rows(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["a", "b"])
    ws.append([1, 2])
    p = tmp_path / "t.xlsx"
    wb.save(p)
    assert D.load_file("xlsx", str(p)) == [{"a": 1, "b": 2}]


@pytest.mark.skipif(not _have("pypdf"), reason="pypdf not installed here")
def test_pdf_text(tmp_path):
    # pypdf can read a minimal PDF; build one with a single blank page (text may be empty but
    # the loader must return a string and not crash).
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    p = tmp_path / "t.pdf"
    with open(p, "wb") as f:
        w.write(f)
    assert isinstance(D.load_file("pdf", str(p)), str)
