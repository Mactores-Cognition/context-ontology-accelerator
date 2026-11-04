"""Read a keys-import CSV into rows, with per-row errors a steward can act on.

Format (one row per key, header required, any column order, extra columns ignored)::

    type,source_table,column,target_table,target_column
    PK,purchase_order_line,po_number|line_number,,
    FK,purchase_order,supplier_id,supplier,supplier_id

Line numbers are file line numbers (header = line 1), which is what a steward
sees in a spreadsheet.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

REQUIRED_COLUMNS: tuple[str, ...] = ("type", "source_table", "column", "target_table", "target_column")
MAX_ROWS = 50_000
_KEY_TYPES = ("PK", "FK")
_COLUMN_SEPARATOR = "|"


class CsvFormatError(ValueError):
    """The file as a whole cannot be used (empty, missing header columns, too many rows)."""


@dataclass(frozen=True)
class KeyRow:
    """One valid key row from the file."""

    line: int
    type: str
    source_table: str
    columns: tuple[str, ...]
    target_table: str = ""
    target_column: str = ""


@dataclass(frozen=True)
class RowError:
    """A row that could not be read, with its file line number."""

    line: int
    message: str


@dataclass
class ParseResult:
    """Valid rows and per-row errors, both in file order."""

    rows: list[KeyRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)


def parse_keys_csv(text: str) -> ParseResult:
    """Parse the CSV text of a keys import.

    Args:
        text: The whole file as text.

    Returns:
        The valid rows and the per-row errors. A bad row never stops the others.

    Raises:
        CsvFormatError: If the file is empty, has no key rows, lacks a required
            header column, or has more than MAX_ROWS rows.
    """
    text = text.lstrip("﻿")
    if not text.strip():
        raise CsvFormatError("The file is empty.")

    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    positions = {name.strip().lower(): i for i, name in enumerate(header)}
    missing = [name for name in REQUIRED_COLUMNS if name not in positions]
    if missing:
        raise CsvFormatError(f"The header row is missing required column(s): {', '.join(missing)}.")

    result = ParseResult()
    data_rows = 0
    for values in reader:
        # line_num counts physical lines, so a quoted value spanning lines keeps later numbers right.
        line = reader.line_num
        cells = {name: _cell(values, positions[name]) for name in REQUIRED_COLUMNS}
        if not any(cells.values()):
            continue  # blank line, or a row of empty cells
        data_rows += 1
        if data_rows > MAX_ROWS:
            raise CsvFormatError(f"The file has more than {MAX_ROWS} key rows; split it into smaller files.")
        row, error = _parse_row(line, cells)
        if error is not None:
            result.errors.append(RowError(line=line, message=error))
        elif row is not None:
            result.rows.append(row)

    if data_rows == 0:
        raise CsvFormatError("The file has a header row but no key rows.")
    return result


def _cell(values: list[str], index: int) -> str:
    return values[index].strip() if index < len(values) else ""


def _parse_row(line: int, cells: dict[str, str]) -> tuple[KeyRow | None, str | None]:
    key_type = cells["type"].upper()
    if key_type not in _KEY_TYPES:
        return None, f"type must be PK or FK, not {cells['type']!r}."
    if not cells["source_table"]:
        return None, "source_table is empty."
    if not cells["column"]:
        return None, "column is empty."

    columns = tuple(part.strip() for part in cells["column"].split(_COLUMN_SEPARATOR))
    if any(not part for part in columns):
        return None, f"column {cells['column']!r} has an empty part between '{_COLUMN_SEPARATOR}' separators."

    if key_type == "PK":
        if cells["target_table"] or cells["target_column"]:
            return None, "A PK row must leave target_table and target_column empty."
        return KeyRow(line=line, type="PK", source_table=cells["source_table"], columns=columns), None

    if len(columns) > 1 or _COLUMN_SEPARATOR in cells["target_column"]:
        return None, "Foreign keys are single-column only; two-column foreign keys are not supported."
    if not cells["target_table"] or not cells["target_column"]:
        return None, "An FK row needs both target_table and target_column."
    return (
        KeyRow(
            line=line,
            type="FK",
            source_table=cells["source_table"],
            columns=columns,
            target_table=cells["target_table"],
            target_column=cells["target_column"],
        ),
        None,
    )
