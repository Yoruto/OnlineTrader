"""Import the first DSV-compatible XLSX sheet as a validated symbol preview."""

import csv
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from online_trader.configuration.dsv import DsvProfile, MAX_SYMBOLS
from online_trader.configuration.files import publish_new_bytes
from online_trader.configuration.symbol_csv import FIELDS, HEADERS, import_symbols
from online_trader.domain.values import decimal_text
from online_trader.errors import TraderError
from online_trader.reconciliation_export import tabular_workbook


MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_WORKBOOK_BYTES = 2_097_152
MAX_PART_BYTES = 4_194_304
MAX_COLUMNS = 32


def _part(archive, path):
    part = archive.getinfo(path)
    if part.file_size > MAX_PART_BYTES:
        raise ValueError("Worksheet part exceeds capacity")
    text = archive.read(part).decode("utf-8-sig")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("Worksheet XML cannot declare entities")
    return ET.fromstring(text)


def _inline_text(parent):
    return "".join(node.text or "" for node in parent.iter(MAIN + "t"))


def _cell_text(cell, strings):
    if cell.find(MAIN + "f") is not None:
        raise ValueError("Enter symbol values as literals")
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return _inline_text(cell)
    text = cell.findtext(MAIN + "v", "")
    if not text or kind == "str":
        return text
    if kind == "s" and re.fullmatch(r"0|[1-9][0-9]{0,8}", text):
        return strings[int(text)]
    if kind != "n" or len(text) > 48:
        raise ValueError("Unsupported symbol cell type")
    value = Decimal(text)
    if not value.is_finite() or not -24 <= value.adjusted() <= 20:
        raise ValueError("Numeric symbol cell is outside the supported range")
    return decimal_text(value)


def _worksheet_rows(archive):
    names = archive.namelist()
    if len(names) > 128 or len(names) != len(set(names)):
        raise ValueError("Ambiguous or oversized workbook")
    workbook = _part(archive, "xl/workbook.xml")
    sheet = workbook.find(f"{MAIN}sheets/{MAIN}sheet")
    relations = _part(archive, "xl/_rels/workbook.xml.rels")
    relation = next(item for item in relations if item.get("Id") == sheet.get(OFFICE_REL + "id"))
    target = relation.get("Target", "")
    if (relation.get("TargetMode") == "External"
            or not relation.get("Type", "").endswith("/worksheet")
            or not re.fullmatch(r"(?:/xl/)?worksheets/[A-Za-z0-9_.-]+\.xml", target)):
        raise ValueError("Worksheet is outside the workbook")
    path = target.lstrip("/") if target.startswith("/") else "xl/" + target
    strings = []
    if "xl/sharedStrings.xml" in names:
        strings = [_inline_text(item) for item in _part(archive, "xl/sharedStrings.xml").findall(MAIN + "si")]
    data = _part(archive, path).find(MAIN + "sheetData")
    previous_row = 0
    for row in data.findall(MAIN + "row"):
        row_number = int(row.get("r", "0"))
        if not previous_row < row_number <= MAX_SYMBOLS + 1:
            raise ValueError("Unexpected worksheet row number")
        previous_row = row_number
        cells = {}
        for cell in row.findall(MAIN + "c"):
            ref = re.fullmatch(r"([A-Z]{1,2})([1-9][0-9]{0,4})", cell.get("r", ""))
            if ref is None or int(ref[2]) != row_number:
                raise ValueError("Invalid worksheet cell address")
            column = 0
            for letter in ref[1]:
                column = 26 * column + ord(letter) - 64
            if column > MAX_COLUMNS or column in cells:
                raise ValueError("Unexpected worksheet column")
            cells[column] = _cell_text(cell, strings)
        if any(cells.values()):
            yield row_number, [cells.get(column, "") for column in range(1, max(cells) + 1)]


def load_symbol_workbook(path, profile, *, mode="replace"):
    try:
        with Path(path).open("rb") as source:
            if source.seek(0, 2) > MAX_WORKBOOK_BYTES:
                raise ValueError("Workbook exceeds capacity")
            source.seek(0)
            with ZipFile(source) as archive:
                rows = list(_worksheet_rows(archive))
        if not rows or rows[0][0] != 1:
            raise ValueError("Workbook must start with symbol headings")
        width = len(rows[0][1])
        stream = StringIO(newline="")
        writer = csv.writer(stream)
        previous = 0
        for row_number, row in rows:
            writer.writerows([[]] * (row_number - previous - 1))
            writer.writerow(row + [""] * (width - len(row)))
            previous = row_number
    except (OSError, BadZipFile, ET.ParseError, UnicodeError, InvalidOperation, ValueError,
            KeyError, IndexError, StopIteration, AttributeError, RuntimeError) as error:
        raise TraderError("E_SYMBOL_XLSX", "无法读取 XLSX 品种表，请检查首个工作表、列名和单元格内容") from error
    return import_symbols(stream.getvalue(), profile, mode=mode, escaped_names=False)


def export_symbol_workbook(profile, path):
    if not isinstance(profile, DsvProfile):
        raise TraderError("E_SYMBOL_XLSX", "Expected a validated parameter profile")
    rows = [HEADERS[:8]]
    rows.extend([symbol[key] for key in FIELDS[:8]] for symbol in profile.to_record()["settings"]["symbols"])
    workbook = tabular_workbook([("品种列表", rows, [14, 16, 11, 11, 11, 11, 11, 11])])
    return publish_new_bytes(workbook, path)
