# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys import: the worker that applies a plan table by table.

Decisions 6 and 9 (work/add-bulk-import.txt, section 2.1): the worker saves
through the existing, unchanged per-table key code and does not block bulk
approve; a table refused with 409 while the source is being approved is
retried. Expected results: section 3 (W1-W7).
"""

from __future__ import annotations

import pytest
from coa_common.domain_models import Column, ForeignKey, Table
from coa_sources.database.keys_import.planner import PlannedFk, TableChange
from coa_sources.database.keys_import.worker import apply_changes

pytestmark = pytest.mark.unit


def _table(name: str, columns: list[str], fks: list[ForeignKey] | None = None) -> Table:
    return Table(
        name=name,
        database="sales",
        columns=[Column(name=c, data_type="string") for c in columns],
        foreign_keys=list(fks or []),
    )


def _change(table_id: str, *fks: PlannedFk, pk: tuple[str, ...] | None = None) -> TableChange:
    return TableChange(table_id=table_id, primary_key=pk, add_fks=list(fks))


FK_PO = PlannedFk(column="supplier_id", target_table="supplier", target_column="supplier_id")
FK_LINE = PlannedFk(column="po_number", target_table="purchase_order", target_column="po_number")


class _Fakes:
    """In-memory load/save pair recording every call."""

    def __init__(self, tables: dict[str, Table], responses: dict[str, list[int]] | None = None) -> None:
        self.tables = tables
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.saved: list[tuple[str, dict]] = []
        self.sleeps: list[float] = []

    def load_table(self, table_id: str) -> Table:
        if table_id not in self.tables:
            raise LookupError(f"table {table_id} not found")
        return self.tables[table_id]

    def save_keys(self, table_id: str, body: dict) -> tuple[int, dict]:
        self.saved.append((table_id, body))
        queue = self.responses.get(table_id)
        status = queue.pop(0) if queue else 200
        return status, ({"tableId": table_id} if status == 200 else {"error": f"status {status}"})

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _run(fakes: _Fakes, changes: dict[str, TableChange], **kwargs):
    return apply_changes(changes, load_table=fakes.load_table, save_keys=fakes.save_keys, sleep=fakes.sleep, **kwargs)


class TestApply:
    def test_w1_each_table_saved_once_with_its_new_keys(self) -> None:
        fakes = _Fakes(
            {
                "sales.purchase_order": _table("purchase_order", ["po_number", "supplier_id"]),
                "sales.purchase_order_line": _table("purchase_order_line", ["po_number", "line"]),
            }
        )
        changes = {
            "sales.purchase_order": _change("sales.purchase_order", FK_PO, pk=("po_number",)),
            "sales.purchase_order_line": _change("sales.purchase_order_line", FK_LINE),
        }
        result = _run(fakes, changes)
        assert [t for t, _ in fakes.saved] == ["sales.purchase_order", "sales.purchase_order_line"]
        assert result.tables_updated == 2
        assert result.keys_added == 3
        assert result.tables_failed == 0
        assert result.failures == []

    def test_w2_table_with_nothing_left_to_add_is_not_saved(self) -> None:
        already = ForeignKey(column="supplier_id", target_table="supplier", target_column="supplier_id")
        fakes = _Fakes({"sales.purchase_order": _table("purchase_order", ["supplier_id"], fks=[already])})
        result = _run(fakes, {"sales.purchase_order": _change("sales.purchase_order", FK_PO)})
        assert fakes.saved == []
        assert result.tables_unchanged == 1
        assert result.tables_updated == 0
        assert result.keys_added == 0

    def test_w3_409_while_source_is_approving_is_retried(self) -> None:
        fakes = _Fakes(
            {"sales.purchase_order": _table("purchase_order", ["supplier_id"])},
            responses={"sales.purchase_order": [409, 409, 200]},
        )
        result = _run(fakes, {"sales.purchase_order": _change("sales.purchase_order", FK_PO)}, retry_wait_s=7.0)
        assert len(fakes.saved) == 3
        assert fakes.sleeps == [7.0, 7.0]
        assert result.tables_updated == 1

    def test_w4_409_every_time_fails_that_table_only(self) -> None:
        fakes = _Fakes(
            {
                "sales.purchase_order": _table("purchase_order", ["supplier_id"]),
                "sales.purchase_order_line": _table("purchase_order_line", ["po_number"]),
            },
            responses={"sales.purchase_order": [409] * 10},
        )
        changes = {
            "sales.purchase_order": _change("sales.purchase_order", FK_PO),
            "sales.purchase_order_line": _change("sales.purchase_order_line", FK_LINE),
        }
        result = _run(fakes, changes, max_retries=3)
        assert sum(1 for t, _ in fakes.saved if t == "sales.purchase_order") == 4  # first try + 3 retries
        assert result.tables_failed == 1
        assert result.tables_updated == 1
        assert result.failures[0].table_id == "sales.purchase_order"
        assert "409" in result.failures[0].message

    @pytest.mark.parametrize("status", [400, 404, 500])
    def test_w5_other_errors_fail_that_table_without_retry(self, status: int) -> None:
        fakes = _Fakes(
            {
                "sales.purchase_order": _table("purchase_order", ["supplier_id"]),
                "sales.purchase_order_line": _table("purchase_order_line", ["po_number"]),
            },
            responses={"sales.purchase_order": [status]},
        )
        changes = {
            "sales.purchase_order": _change("sales.purchase_order", FK_PO),
            "sales.purchase_order_line": _change("sales.purchase_order_line", FK_LINE),
        }
        result = _run(fakes, changes)
        assert sum(1 for t, _ in fakes.saved if t == "sales.purchase_order") == 1
        assert fakes.sleeps == []
        assert result.tables_failed == 1
        assert result.tables_updated == 1
        assert str(status) in result.failures[0].message

    def test_w6_table_that_cannot_be_loaded_fails_and_others_continue(self) -> None:
        fakes = _Fakes({"sales.purchase_order_line": _table("purchase_order_line", ["po_number"])})
        changes = {
            "sales.purchase_order": _change("sales.purchase_order", FK_PO),
            "sales.purchase_order_line": _change("sales.purchase_order_line", FK_LINE),
        }
        result = _run(fakes, changes)
        assert result.tables_failed == 1
        assert result.tables_updated == 1
        assert result.failures[0].table_id == "sales.purchase_order"

    def test_w7_save_that_raises_fails_that_table_and_others_continue(self) -> None:
        fakes = _Fakes(
            {
                "sales.purchase_order": _table("purchase_order", ["supplier_id"]),
                "sales.purchase_order_line": _table("purchase_order_line", ["po_number"]),
            }
        )

        def save_keys(table_id: str, body: dict) -> tuple[int, dict]:
            if table_id == "sales.purchase_order":
                raise RuntimeError("DataZone unavailable")
            return fakes.save_keys(table_id, body)

        changes = {
            "sales.purchase_order": _change("sales.purchase_order", FK_PO),
            "sales.purchase_order_line": _change("sales.purchase_order_line", FK_LINE),
        }
        result = apply_changes(changes, load_table=fakes.load_table, save_keys=save_keys, sleep=fakes.sleep)
        assert result.tables_failed == 1
        assert result.tables_updated == 1
        assert "DataZone unavailable" in result.failures[0].message

    def test_w8_tables_left_when_time_runs_out_are_reported_not_processed(self) -> None:
        fakes = _Fakes(
            {
                "sales.purchase_order": _table("purchase_order", ["supplier_id"]),
                "sales.purchase_order_line": _table("purchase_order_line", ["po_number"]),
            }
        )
        changes = {
            "sales.purchase_order": _change("sales.purchase_order", FK_PO),
            "sales.purchase_order_line": _change("sales.purchase_order_line", FK_LINE),
        }
        checks = iter([False, True])
        result = _run(fakes, changes, should_stop=lambda: next(checks))
        assert [t for t, _ in fakes.saved] == ["sales.purchase_order"]
        assert result.tables_updated == 1
        assert result.tables_failed == 1
        assert result.failures[0].table_id == "sales.purchase_order_line"
        assert "start the import again" in result.failures[0].message
