# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check keys-import rows against a source's tables and plan what to add.

Rules (work/add-bulk-import.txt, section 2.4):

- Add only. Existing keys (AI, deterministic or steward) stay as they are; a CSV
  foreign key that already exists is "already there". A CSV foreign key on a
  column that already has a different foreign key is added as a second key.
- A primary key is set only if the table has none; a different existing primary
  key is kept and the row is skipped.
- Targets must be tables in the same source.

``build_patch_body`` turns a planned change into the request body of the existing
``PATCH .../tables/{tableId}/keys`` handler, sending every existing foreign key
back unchanged so that handler keeps it as it is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum

from coa_common.domain_models import ForeignKey, ReviewStatus, Table

from coa_sources.database.keys_import.csv_reader import KeyRow

# Review statuses the existing handler accepts on a sent key; PENDING_REVIEW is
# rejected there (it is set by inference, not by a steward).
_SENDABLE_REVIEW_STATUSES = (ReviewStatus.APPROVED, ReviewStatus.REJECTED)


class RowOutcome(StrEnum):
    """What happens to one CSV row."""

    ADD = "ADD"
    ALREADY_THERE = "ALREADY_THERE"
    DUPLICATE = "DUPLICATE"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class RowResult:
    """The outcome of one CSV row, with its file line number."""

    line: int
    outcome: RowOutcome
    message: str = ""
    warning: str = ""


@dataclass(frozen=True)
class PlannedFk:
    """A foreign key to add. ``target_table`` is written as the existing handler expects it."""

    column: str
    target_table: str
    target_column: str


@dataclass
class TableChange:
    """Keys to add to one table."""

    table_id: str
    primary_key: tuple[str, ...] | None = None
    add_fks: list[PlannedFk] = field(default_factory=list)


@dataclass
class ImportPlan:
    """Per-table changes and per-row results (in file order)."""

    changes: dict[str, TableChange] = field(default_factory=dict)
    results: list[RowResult] = field(default_factory=list)

    def _count(self, outcome: RowOutcome) -> int:
        return sum(1 for r in self.results if r.outcome == outcome)

    @property
    def to_add(self) -> int:
        """Rows that add a key."""
        return self._count(RowOutcome.ADD)

    @property
    def already_there(self) -> int:
        """Rows whose key the table already has."""
        return self._count(RowOutcome.ALREADY_THERE)

    @property
    def duplicates(self) -> int:
        """Rows repeating an earlier row of the file."""
        return self._count(RowOutcome.DUPLICATE)

    @property
    def skipped(self) -> int:
        """Valid rows not applied (rejected table, different primary key already set)."""
        return self._count(RowOutcome.SKIPPED)

    @property
    def errors(self) -> int:
        """Rows that failed a check."""
        return self._count(RowOutcome.ERROR)

    @property
    def tables_to_update(self) -> int:
        """Tables that will get at least one key."""
        return len(self.changes)


class _SourceTables:
    """Resolves table names the way a steward writes them: bare name or database.table."""

    def __init__(self, tables: list[Table]) -> None:
        self._by_id = {t.table_id: t for t in tables}
        self._by_name: dict[str, list[Table]] = defaultdict(list)
        for t in tables:
            self._by_name[t.name].append(t)

    def resolve(self, name: str) -> tuple[Table | None, str]:
        """Return (table, "") or (None, reason)."""
        if name in self._by_id:
            return self._by_id[name], ""
        matches = self._by_name.get(name, [])
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            options = ", ".join(sorted(t.table_id for t in matches))
            return None, f"table {name!r} is ambiguous in this source; use database.table ({options})."
        return None, f"table {name!r} was not found in this source."

    def written_name(self, table: Table) -> str:
        """Bare name when unique in the source, else database.table (as Pass 2 writes targets)."""
        return table.name if len(self._by_name.get(table.name, [])) == 1 else table.table_id

    def target_id(self, fk: ForeignKey) -> str:
        """Table id an existing foreign key points to, or its raw target if it does not resolve."""
        table, _ = self.resolve(fk.target_table)
        return table.table_id if table is not None else fk.target_table


def plan_import(rows: list[KeyRow], tables: list[Table]) -> ImportPlan:
    """Check every row against the source's tables and plan the keys to add.

    Args:
        rows: Valid rows from ``parse_keys_csv``.
        tables: All tables of the source, with their current keys.

    Returns:
        The plan: per-table changes and one result per row, in file order.
    """
    source = _SourceTables(tables)
    plan = ImportPlan()
    seen_fks: set[tuple[str, str, str, str]] = set()
    seen_pks: dict[str, tuple[tuple[str, ...], int]] = {}

    for row in rows:
        table, reason = source.resolve(row.source_table)
        if table is None:
            plan.results.append(RowResult(row.line, RowOutcome.ERROR, reason))
            continue
        if table.business_metadata.review_status == ReviewStatus.REJECTED:
            plan.results.append(
                RowResult(row.line, RowOutcome.SKIPPED, f"table {table.table_id} was rejected; keys cannot be added.")
            )
            continue
        column_names = {c.name for c in table.columns}
        unknown = [c for c in row.columns if c not in column_names]
        if unknown:
            plan.results.append(
                RowResult(
                    row.line,
                    RowOutcome.ERROR,
                    f"column(s) {', '.join(unknown)} not found in table {table.table_id}.",
                )
            )
            continue
        if row.type == "PK":
            plan.results.append(_plan_primary_key(row, table, plan, seen_pks))
        else:
            plan.results.append(_plan_foreign_key(row, table, source, plan, seen_fks))
    return plan


def _change_for(plan: ImportPlan, table: Table) -> TableChange:
    return plan.changes.setdefault(table.table_id, TableChange(table_id=table.table_id))


def _plan_primary_key(
    row: KeyRow,
    table: Table,
    plan: ImportPlan,
    seen_pks: dict[str, tuple[tuple[str, ...], int]],
) -> RowResult:
    if table.table_id in seen_pks:
        earlier_columns, earlier_line = seen_pks[table.table_id]
        if earlier_columns == row.columns:
            return RowResult(row.line, RowOutcome.DUPLICATE, f"same primary key as line {earlier_line}.")
        return RowResult(
            row.line,
            RowOutcome.ERROR,
            f"conflicting primary key: line {earlier_line} sets ({', '.join(earlier_columns)}) for {table.table_id}.",
        )
    seen_pks[table.table_id] = (row.columns, row.line)

    existing = tuple(table.primary_key.columns)
    if existing == row.columns:
        return RowResult(row.line, RowOutcome.ALREADY_THERE)
    if existing:
        return RowResult(
            row.line,
            RowOutcome.SKIPPED,
            f"table {table.table_id} already has a different primary key ({', '.join(existing)}); it is kept.",
        )
    _change_for(plan, table).primary_key = row.columns
    return RowResult(row.line, RowOutcome.ADD)


def _plan_foreign_key(
    row: KeyRow,
    table: Table,
    source: _SourceTables,
    plan: ImportPlan,
    seen_fks: set[tuple[str, str, str, str]],
) -> RowResult:
    column = row.columns[0]
    target, reason = source.resolve(row.target_table)
    if target is None:
        return RowResult(row.line, RowOutcome.ERROR, f"target {reason}")
    if row.target_column not in {c.name for c in target.columns}:
        return RowResult(
            row.line,
            RowOutcome.ERROR,
            f"target column {row.target_column!r} not found in table {target.table_id}.",
        )

    key = (table.table_id, column, target.table_id, row.target_column)
    if key in seen_fks:
        return RowResult(row.line, RowOutcome.DUPLICATE, "same foreign key as an earlier line.")
    seen_fks.add(key)

    existing_on_column = [fk for fk in table.foreign_keys if fk.column == column]
    if any(
        source.target_id(fk) == target.table_id and (fk.target_column or "") == row.target_column
        for fk in existing_on_column
    ):
        return RowResult(row.line, RowOutcome.ALREADY_THERE)

    _change_for(plan, table).add_fks.append(
        PlannedFk(column=column, target_table=source.written_name(target), target_column=row.target_column)
    )
    warning = ""
    if existing_on_column:
        others = ", ".join(sorted({f"{fk.target_table}.{fk.target_column}" for fk in existing_on_column}))
        warning = f"column {column} already has a foreign key to {others}; both are kept."
    return RowResult(row.line, RowOutcome.ADD, warning=warning)


def _same_target(a: str, b: str) -> bool:
    """Compare target names written as a bare name or as database.table."""
    if a == b:
        return True
    if "." in a and "." not in b:
        return a.rsplit(".", 1)[1] == b
    if "." in b and "." not in a:
        return b.rsplit(".", 1)[1] == a
    return False


def pending_keys(table: Table, change: TableChange) -> tuple[list[PlannedFk], tuple[str, ...] | None]:
    """Keys of ``change`` the table (as it is now) does not have yet.

    The worker re-reads each table before saving, so keys added since the
    preview are not sent twice, and a primary key that appeared meanwhile is kept.
    """
    new_fks = [
        planned
        for planned in change.add_fks
        if not any(
            fk.column == planned.column
            and _same_target(fk.target_table, planned.target_table)
            and (fk.target_column or "") == planned.target_column
            for fk in table.foreign_keys
        )
    ]
    primary_key = change.primary_key if change.primary_key and not table.primary_key.columns else None
    return new_fks, primary_key


def build_patch_body(table: Table, change: TableChange) -> dict | None:
    """Build the existing keys handler's request body, or None when there is nothing to add.

    Existing foreign keys are sent back unchanged (the handler replaces the whole
    list and keeps unchanged entries as they are); new ones carry only column and
    target, so the handler saves them as STEWARD_SPECIFIED.
    """
    new_fks, primary_key = pending_keys(table, change)
    if not new_fks and primary_key is None:
        return None
    body: dict = {}
    if primary_key is not None:
        body["primaryKey"] = {"columns": list(primary_key)}
    if new_fks:
        body["foreignKeys"] = [_existing_fk_body(fk) for fk in table.foreign_keys] + [
            {"column": fk.column, "targetTable": fk.target_table, "targetColumn": fk.target_column} for fk in new_fks
        ]
    return body


def _existing_fk_body(fk: ForeignKey) -> dict:
    body = {"column": fk.column, "targetTable": fk.target_table}
    optional = {
        "targetColumn": fk.target_column,
        "source": fk.source,
        "reviewStatus": fk.review_status if fk.review_status in _SENDABLE_REVIEW_STATUSES else "",
        "targetDatasourceId": fk.target_datasource_id,
        "provenance": fk.provenance,
    }
    body.update({k: v for k, v in optional.items() if v})
    return body
