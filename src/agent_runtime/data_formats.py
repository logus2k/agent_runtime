"""Multi-format loading for the Data source block (composer kind ``data``).

One block, many formats. A ``format`` maps to a loader that turns inline text OR a file into
the value the block emits on its ``out`` port. Formats fall into three OUTPUT categories:

  * **OBJECT**  (json / yaml / toml)                    -> ``dict``       (foldable into Agent vars)
  * **TABULAR** (csv / tsv / jsonl / parquet / xlsx)    -> ``list[dict]`` (rows)
  * **TEXT**    (markdown / text / html / xml / pdf)    -> ``str``        (document text)

**Binary** formats (pdf / parquet / xlsx) are FILE-ONLY — they have no inline form. Loaders
degrade LOUDLY: a parse/read error logs a warning and returns the category's empty value
(``{}`` / ``[]`` / ``""``) so a bad file never crashes a run (the runtime's no-silent-failures
ethos). Heavy third-party imports (pyarrow / pypdf / bs4 / openpyxl) are done LAZILY inside the
loaders so importing this module stays cheap and a missing optional dep degrades one format,
not the process.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import tomllib
from typing import Any

log = logging.getLogger("agent_runtime.data_formats")

# ---- format taxonomy -------------------------------------------------------- #
OBJECT_FORMATS = ("json", "yaml", "toml")
TABULAR_FORMATS = ("csv", "tsv", "jsonl", "parquet", "xlsx")
TEXT_FORMATS = ("markdown", "text", "html", "xml", "pdf")
# Binary = no inline form (read as bytes from a file). NOTE: parquet/xlsx are tabular, pdf is
# text — "binary" is about the INPUT medium, orthogonal to the output category.
BINARY_FORMATS = ("pdf", "parquet", "xlsx")

# Display / selection order for the UI dropdown (grouped: object, tabular, document).
ALL_FORMATS = (
    "json", "yaml", "toml",
    "csv", "tsv", "jsonl", "parquet", "xlsx",
    "markdown", "text", "html", "xml", "pdf",
)
# Formats that accept an inline (typed-in) value — everything that isn't binary.
INLINE_FORMATS = tuple(f for f in ALL_FORMATS if f not in BINARY_FORMATS)


def empty_for(fmt: str) -> Any:
    """The empty value for a format's OUTPUT category ({} object / [] rows / "" text)."""
    if fmt in OBJECT_FORMATS:
        return {}
    if fmt in TABULAR_FORMATS:
        return []
    return ""


# ---- text (inline-capable) loaders: str -> value ---------------------------- #
def _load_json(t: str) -> Any:
    return json.loads(t)


def _load_yaml(t: str) -> Any:
    import yaml  # ships with the farm (agent records)
    return yaml.safe_load(t)


def _load_toml(t: str) -> Any:
    return tomllib.loads(t)


def _load_delimited(t: str, delimiter: str) -> list[dict]:
    return [dict(r) for r in csv.DictReader(io.StringIO(t), delimiter=delimiter)]


def _load_jsonl(t: str) -> list[Any]:
    return [json.loads(line) for line in t.splitlines() if line.strip()]


def _load_text(t: str) -> str:
    return t


def _load_html(t: str) -> str:
    """HTML -> readable text (tags stripped, scripts/styles dropped) via BeautifulSoup+lxml."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(t, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _load_xml(t: str) -> str:
    """XML -> readable text (concatenated element text). Structure-lossy but predictable."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(t)
    return "\n".join(s.strip() for s in root.itertext() if s and s.strip())


_TEXT_LOADERS = {
    "json": _load_json,
    "yaml": _load_yaml,
    "toml": _load_toml,
    "csv": lambda t: _load_delimited(t, ","),
    "tsv": lambda t: _load_delimited(t, "\t"),
    "jsonl": _load_jsonl,
    "markdown": _load_text,
    "text": _load_text,
    "html": _load_html,
    "xml": _load_xml,
}


# ---- binary loaders: bytes -> value ----------------------------------------- #
def _load_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _load_parquet(data: bytes) -> list[dict]:
    import pyarrow.parquet as pq
    return pq.read_table(io.BytesIO(data)).to_pylist()


def _load_xlsx(data: bytes) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h) if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
    return [dict(zip(header, r)) for r in rows[1:]]


_BYTES_LOADERS = {
    "pdf": _load_pdf,
    "parquet": _load_parquet,
    "xlsx": _load_xlsx,
}


# ---- public API ------------------------------------------------------------- #
def load_inline(fmt: str, value: Any) -> Any:
    """Parse an INLINE (typed-in) value per ``fmt``. Object formats accept an already-parsed
    dict/list verbatim. Binary formats have no inline form → empty + warning. Parse errors
    degrade to the category empty value + a loud warning."""
    if fmt in BINARY_FORMATS:
        log.warning("Data block: format %r is binary (file only) — inline ignored", fmt)
        return empty_for(fmt)
    if fmt in OBJECT_FORMATS and isinstance(value, (dict, list)):
        return value
    text = "" if value is None else str(value)
    if not text.strip():
        return empty_for(fmt)
    loader = _TEXT_LOADERS.get(fmt)
    if loader is None:
        log.warning("Data block: unknown format %r — using empty", fmt)
        return empty_for(fmt)
    try:
        return loader(text)
    except Exception as exc:  # noqa: BLE001 - surface loudly, never crash the run
        log.warning("Data block: could not parse inline %s (%s) — using empty", fmt, exc)
        return empty_for(fmt)


def load_file(fmt: str, path: str) -> Any:
    """Read a FILE at ``path`` on the runtime filesystem and parse it per ``fmt`` (binary
    formats are read as bytes; the rest as UTF-8 text). Missing/unreadable/unparseable →
    the category empty value + a loud warning. Same trust model as the File Initiator."""
    if not path:
        return empty_for(fmt)
    try:
        if fmt in BINARY_FORMATS:
            with open(path, "rb") as f:
                return _BYTES_LOADERS[fmt](f.read())
        loader = _TEXT_LOADERS.get(fmt)
        if loader is None:
            log.warning("Data block: unknown format %r — using empty", fmt)
            return empty_for(fmt)
        with open(path, "r", encoding="utf-8") as f:
            return loader(f.read())
    except Exception as exc:  # noqa: BLE001 - surface loudly, never crash the run
        log.warning("Data block: could not read %r as %s (%s) — using empty", path, fmt, exc)
        return empty_for(fmt)


def parse_object_strict(fmt: str, value: Any) -> dict:
    """Parse inline OBJECT content to a dict, RAISING ``ValueError`` on anything that isn't a
    valid object of that format. Used at authoring/validate time (loud). An already-parsed dict
    passes through; an empty/whitespace value is the caller's responsibility to skip."""
    if fmt not in OBJECT_FORMATS:
        raise ValueError(f"format '{fmt}' is not an object format")
    if isinstance(value, dict):
        return value
    text = "" if value is None else str(value)
    try:
        loaded = _TEXT_LOADERS[fmt](text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid {fmt}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"inline {fmt} must be an object, got {type(loaded).__name__}")
    return loaded


def parse_object(fmt: str, value: Any) -> dict:
    """Lenient object parse for the compile-time vars fold: a dict on success, ``{}`` otherwise
    (non-object format, empty, or unparseable). Never raises."""
    if isinstance(value, dict):
        return value
    if not str(value or "").strip():
        return {}
    try:
        return parse_object_strict(fmt, value)
    except ValueError:
        return {}
