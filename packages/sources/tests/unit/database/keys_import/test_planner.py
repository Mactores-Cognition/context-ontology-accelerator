"""Keys import: checking rows against the source's tables and planning changes.

Rules: work/add-bulk-import.txt, section 2.4. Expected results: section 3
(P1-P18 for planning, B1-B8 for the request body sent to the existing save code).
"""

from __future__ import annotations

import pytest
from coa_common.domain_models import Column, ForeignKey, PrimaryKey, Table
from coa_sources.database.keys_import.csv_reader import KeyRow
from coa_sources.database.keys_import.planner import (
    PlannedFk,
    RowOutcome,
    TableChange,
    build_patch_body,
    plan_import,
)

pytestmark = pytest.mark.unit


def _table(
    name: str,
    columns: list[str],
    *,
    database: str = "sales",
    pk: list[str] | None = None,
    fks: list[ForeignKey] | None = None,
    review_status: str = "PENDING_REVIEW",
) -> Table:
    table = Table(
        name=name,
        database=database,
        columns=[Column(name=c, data_type="string") for c in columns],
        primary_key=PrimaryKey(columns=pk or [], source="AI_INFERRED" if pk else ""),
        foreign_keys=list(fks or []),
    )
    table.business_metadata.review_status = review_status
    return table


def _fk(line: int, table: str, column: str, target: str, target_column: str) -> KeyRow:
    return KeyRow(
        line=line, type="FK", source_table=table, columns=(column,), target_table=target, target_column=target_column
    )


def _pk(line: int, table: str, *columns: str) -> KeyRow:
    return KeyRow(line=line, type="PK", source_table=table, columns=tuple(columns))


def _ai_fk(column: str, target: str, target_column: str, review_status: str = "PENDING_REVIEW") -> ForeignKey:
    return ForeignKey(
        column=column,
        target_table=target,
        target_column=target_column,
        source="AI_INFERRED",
        confidence=0.9,
        review_status=review_status,
        provenance="naming pattern",
    )


def _source(**overrides: Table) -> list[Table]:
    tables = {
        "purchase_order": _table("purchase_order", ["po_number", "supplier_id", "parent_po"]),
        "supplier": _table("supplier", ["supplier_id", "name"]),
        "part": _table("part", ["part_number"]),
    }
    tables.update(overrides)
    return list(tables.values())


def _outcome(plan, line: int):
    return next(r for r in plan.results if r.line == line)


class TestForeignKeys:
    def test_p1_new_fk_is_added(self) -> None:
        plan = plan_import([_fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ADD
        change = plan.changes["sales.purchase_order"]
        assert change.add_fks == [PlannedFk(column="supplier_id", target_table="supplier", target_column="supplier_id")]
        assert change.primary_key is None

    def test_p2_identical_existing_fk_is_already_there(self) -> None:
        po = _table(
            "purchase_order",
            ["po_number", "supplier_id"],
            fks=[_ai_fk("supplier_id", "supplier", "supplier_id")],
        )
        plan = plan_import(
            [_fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id")], _source(purchase_order=po)
        )
        assert _outcome(plan, 2).outcome == RowOutcome.ALREADY_THERE
        assert plan.changes == {}

    def test_p3_existing_fk_stored_as_database_dot_table_matches_bare_name(self) -> None:
        po = _table(
            "purchase_order",
            ["po_number", "supplier_id"],
            fks=[_ai_fk("supplier_id", "sales.supplier", "supplier_id")],
        )
        plan = plan_import(
            [_fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id")], _source(purchase_order=po)
        )
        assert _outcome(plan, 2).outcome == RowOutcome.ALREADY_THERE

    def test_p4_different_fk_on_same_column_is_added_and_warned(self) -> None:
        # Decision 10: the AI key stays; the CSV key is added as a second key.
        po = _table(
            "purchase_order",
            ["po_number", "supplier_id"],
            fks=[_ai_fk("supplier_id", "part", "part_number")],
        )
        plan = plan_import(
            [_fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id")], _source(purchase_order=po)
        )
        result = _outcome(plan, 2)
        assert result.outcome == RowOutcome.ADD
        assert "part" in result.warning
        assert len(plan.changes["sales.purchase_order"].add_fks) == 1

    def test_p5_same_fk_twice_in_file_second_is_duplicate(self) -> None:
        rows = [
            _fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id"),
            _fk(3, "purchase_order", "supplier_id", "supplier", "supplier_id"),
        ]
        plan = plan_import(rows, _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ADD
        assert _outcome(plan, 3).outcome == RowOutcome.DUPLICATE
        assert len(plan.changes["sales.purchase_order"].add_fks) == 1

    def test_p18_self_reference_is_added(self) -> None:
        plan = plan_import([_fk(2, "purchase_order", "parent_po", "purchase_order", "po_number")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ADD


class TestRowChecks:
    def test_p6_unknown_table(self) -> None:
        plan = plan_import([_fk(2, "nope", "a", "supplier", "supplier_id")], _source())
        result = _outcome(plan, 2)
        assert result.outcome == RowOutcome.ERROR
        assert "nope" in result.message

    def test_p7_unknown_column(self) -> None:
        plan = plan_import([_fk(2, "purchase_order", "nope", "supplier", "supplier_id")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ERROR
        assert "nope" in _outcome(plan, 2).message

    def test_p8_unknown_target_table(self) -> None:
        plan = plan_import([_fk(2, "purchase_order", "supplier_id", "vendor", "id")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ERROR
        assert "vendor" in _outcome(plan, 2).message

    def test_p9_unknown_target_column(self) -> None:
        plan = plan_import([_fk(2, "purchase_order", "supplier_id", "supplier", "nope")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ERROR
        assert "nope" in _outcome(plan, 2).message

    def test_p10_rejected_table_is_skipped(self) -> None:
        po = _table("purchase_order", ["po_number", "supplier_id"], review_status="REJECTED")
        plan = plan_import(
            [_fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id")], _source(purchase_order=po)
        )
        result = _outcome(plan, 2)
        assert result.outcome == RowOutcome.SKIPPED
        assert "rejected" in result.message.lower()
        assert plan.changes == {}

    def test_p16_database_dot_table_and_ambiguous_names(self) -> None:
        crm_orders = _table("orders", ["id", "customer_id"], database="crm")
        sales_orders = _table("orders", ["id", "customer_id"], database="sales")
        customer = _table("customer", ["customer_id"], database="crm")
        tables = [crm_orders, sales_orders, customer]
        rows = [
            _fk(2, "orders", "customer_id", "customer", "customer_id"),
            _fk(3, "crm.orders", "customer_id", "customer", "customer_id"),
            _fk(4, "customer", "customer_id", "orders", "id"),
            _fk(5, "customer", "customer_id", "sales.orders", "id"),
        ]
        plan = plan_import(rows, tables)
        assert _outcome(plan, 2).outcome == RowOutcome.ERROR
        assert "ambiguous" in _outcome(plan, 2).message.lower()
        assert _outcome(plan, 3).outcome == RowOutcome.ADD
        assert _outcome(plan, 4).outcome == RowOutcome.ERROR
        assert _outcome(plan, 5).outcome == RowOutcome.ADD
        # A target whose bare name is not unique is stored as database.table.
        assert plan.changes["crm.customer"].add_fks == [
            PlannedFk(column="customer_id", target_table="sales.orders", target_column="id")
        ]


class TestPrimaryKeys:
    def test_p11_pk_set_when_table_has_none(self) -> None:
        plan = plan_import([_pk(2, "purchase_order", "po_number")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ADD
        assert plan.changes["sales.purchase_order"].primary_key == ("po_number",)

    def test_p12_same_pk_is_already_there(self) -> None:
        po = _table("purchase_order", ["po_number"], pk=["po_number"])
        plan = plan_import([_pk(2, "purchase_order", "po_number")], _source(purchase_order=po))
        assert _outcome(plan, 2).outcome == RowOutcome.ALREADY_THERE
        assert plan.changes == {}

    def test_p13_different_existing_pk_is_kept_and_row_skipped(self) -> None:
        po = _table("purchase_order", ["po_number", "supplier_id"], pk=["supplier_id"])
        plan = plan_import([_pk(2, "purchase_order", "po_number")], _source(purchase_order=po))
        result = _outcome(plan, 2)
        assert result.outcome == RowOutcome.SKIPPED
        assert "supplier_id" in result.message
        assert plan.changes == {}

    def test_p14_conflicting_and_duplicate_pk_rows(self) -> None:
        rows = [
            _pk(2, "purchase_order", "po_number"),
            _pk(3, "purchase_order", "po_number"),
            _pk(4, "purchase_order", "supplier_id"),
        ]
        plan = plan_import(rows, _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ADD
        assert _outcome(plan, 3).outcome == RowOutcome.DUPLICATE
        assert _outcome(plan, 4).outcome == RowOutcome.ERROR
        assert plan.changes["sales.purchase_order"].primary_key == ("po_number",)

    def test_p15_unknown_pk_column(self) -> None:
        plan = plan_import([_pk(2, "purchase_order", "po_number", "nope")], _source())
        assert _outcome(plan, 2).outcome == RowOutcome.ERROR
        assert "nope" in _outcome(plan, 2).message


class TestCounts:
    def test_p17_counts_and_tables_to_update(self) -> None:
        po = _table(
            "purchase_order",
            ["po_number", "supplier_id", "parent_po"],
            fks=[_ai_fk("supplier_id", "supplier", "supplier_id")],
        )
        rows = [
            _fk(2, "purchase_order", "supplier_id", "supplier", "supplier_id"),  # already there
            _fk(3, "purchase_order", "parent_po", "purchase_order", "po_number"),  # add
            _fk(4, "purchase_order", "parent_po", "purchase_order", "po_number"),  # duplicate
            _pk(5, "supplier", "supplier_id"),  # add
            _fk(6, "nope", "a", "supplier", "supplier_id"),  # error
        ]
        plan = plan_import(rows, _source(purchase_order=po))
        assert plan.to_add == 2
        assert plan.already_there == 1
        assert plan.duplicates == 1
        assert plan.skipped == 0
        assert plan.errors == 1
        assert plan.tables_to_update == 2
        assert [r.line for r in plan.results] == [2, 3, 4, 5, 6]


class TestPatchBody:
    def test_b1_fk_change_sends_existing_then_new_and_no_primary_key(self) -> None:
        existing = _ai_fk("supplier_id", "supplier", "supplier_id")
        po = _table("purchase_order", ["po_number", "supplier_id", "parent_po"], fks=[existing])
        plan = plan_import([_fk(2, "purchase_order", "parent_po", "purchase_order", "po_number")], [po])
        body = build_patch_body(po, plan.changes["sales.purchase_order"])
        assert "primaryKey" not in body
        assert [fk["column"] for fk in body["foreignKeys"]] == ["supplier_id", "parent_po"]

    def test_b2_existing_fk_fields_are_preserved(self) -> None:
        existing = _ai_fk("supplier_id", "supplier", "supplier_id")
        existing.target_datasource_id = ""
        po = _table("purchase_order", ["po_number", "supplier_id", "parent_po"], fks=[existing])
        plan = plan_import([_fk(2, "purchase_order", "parent_po", "purchase_order", "po_number")], [po])
        sent = build_patch_body(po, plan.changes["sales.purchase_order"])["foreignKeys"][0]
        assert sent["column"] == "supplier_id"
        assert sent["targetTable"] == "supplier"
        assert sent["targetColumn"] == "supplier_id"
        assert sent["source"] == "AI_INFERRED"
        assert sent["provenance"] == "naming pattern"
        assert "targetDatasourceId" not in sent

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("PENDING_REVIEW", None), ("", None), ("APPROVED", "APPROVED"), ("REJECTED", "REJECTED")],
    )
    def test_b3_review_status_sent_only_when_approved_or_rejected(self, status: str, expected: str | None) -> None:
        # The existing save code rejects PENDING_REVIEW as a sent value (400).
        existing = _ai_fk("supplier_id", "supplier", "supplier_id", review_status=status)
        po = _table("purchase_order", ["po_number", "supplier_id", "parent_po"], fks=[existing])
        plan = plan_import([_fk(2, "purchase_order", "parent_po", "purchase_order", "po_number")], [po])
        sent = build_patch_body(po, plan.changes["sales.purchase_order"])["foreignKeys"][0]
        assert sent.get("reviewStatus") == expected

    def test_b4_new_fk_carries_only_column_and_target(self) -> None:
        po = _table("purchase_order", ["po_number", "parent_po"])
        plan = plan_import([_fk(2, "purchase_order", "parent_po", "purchase_order", "po_number")], [po])
        body = build_patch_body(po, plan.changes["sales.purchase_order"])
        assert body["foreignKeys"] == [
            {"column": "parent_po", "targetTable": "purchase_order", "targetColumn": "po_number"}
        ]

    def test_b5_pk_change_sends_only_primary_key(self) -> None:
        po = _table("purchase_order", ["po_number", "line"])
        plan = plan_import([_pk(2, "purchase_order", "po_number", "line")], [po])
        body = build_patch_body(po, plan.changes["sales.purchase_order"])
        assert body == {"primaryKey": {"columns": ["po_number", "line"]}}

    def test_b6_pk_and_fk_change_sends_both(self) -> None:
        po = _table("purchase_order", ["po_number", "parent_po"])
        plan = plan_import(
            [
                _pk(2, "purchase_order", "po_number"),
                _fk(3, "purchase_order", "parent_po", "purchase_order", "po_number"),
            ],
            [po],
        )
        body = build_patch_body(po, plan.changes["sales.purchase_order"])
        assert body["primaryKey"] == {"columns": ["po_number"]}
        assert len(body["foreignKeys"]) == 1

    def test_b7_nothing_to_send_when_keys_appeared_since_preview(self) -> None:
        # The worker re-reads each table; keys added meanwhile are not sent twice.
        po = _table("purchase_order", ["po_number", "parent_po"])
        plan = plan_import([_fk(2, "purchase_order", "parent_po", "purchase_order", "po_number")], [po])
        po_now = _table(
            "purchase_order",
            ["po_number", "parent_po"],
            fks=[ForeignKey(column="parent_po", target_table="purchase_order", target_column="po_number")],
        )
        assert build_patch_body(po_now, plan.changes["sales.purchase_order"]) is None

    def test_b8_pk_not_sent_when_table_got_a_pk_since_preview(self) -> None:
        po = _table("purchase_order", ["po_number", "supplier_id"])
        plan = plan_import([_pk(2, "purchase_order", "po_number")], [po])
        po_now = _table("purchase_order", ["po_number", "supplier_id"], pk=["supplier_id"])
        assert build_patch_body(po_now, plan.changes["sales.purchase_order"]) is None

    @pytest.mark.parametrize(
        ("stored", "planned"),
        [("sales.purchase_order", "purchase_order"), ("purchase_order", "sales.purchase_order")],
    )
    def test_b9_key_added_since_preview_in_other_name_form_is_not_sent(self, stored: str, planned: str) -> None:
        po_now = _table(
            "purchase_order",
            ["po_number", "parent_po"],
            fks=[ForeignKey(column="parent_po", target_table=stored, target_column="po_number")],
        )
        change = TableChange(
            table_id="sales.purchase_order",
            add_fks=[PlannedFk(column="parent_po", target_table=planned, target_column="po_number")],
        )
        assert build_patch_body(po_now, change) is None

    @pytest.mark.parametrize("stored", ["crm.purchase_order", "purchase_order_line", "sales.part"])
    def test_b10_key_to_a_different_table_does_not_count_as_already_added(self, stored: str) -> None:
        po_now = _table(
            "purchase_order",
            ["po_number", "parent_po"],
            fks=[ForeignKey(column="parent_po", target_table=stored, target_column="po_number")],
        )
        change = TableChange(
            table_id="sales.purchase_order",
            add_fks=[PlannedFk(column="parent_po", target_table="sales.purchase_order", target_column="po_number")],
        )
        body = build_patch_body(po_now, change)
        assert body is not None
        assert len(body["foreignKeys"]) == 2
