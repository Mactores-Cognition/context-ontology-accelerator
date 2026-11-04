# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys import: contract with the existing, unchanged per-table key code.

Decision 6 (work/add-bulk-import.txt, section 2.1): the worker saves keys by
calling the existing PATCH .../keys handler (_handle_update_table_keys) as it
is. These tests run what the feature sends through that real handler, with
only DataZone and DynamoDB mocked, so a change to the handler upstream shows
up here. Expected results: section 3 (C1-C6).
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, create_autospec, patch

import pytest

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Import sources_handler first to resolve the circular import, then database_routes.
import coa_sources.api.sources_handler  # noqa: E402, F401, I001
import coa_sources.api.database_routes as _dr  # noqa: E402, I001
from coa_common.datazone_forms import deserialize_form, serialize_form  # noqa: E402
from coa_common.domain_models import Column, ForeignKey, PrimaryKey, Table  # noqa: E402

from coa_sources.database.keys_import.csv_reader import KeyRow  # noqa: E402
from coa_sources.database.keys_import.planner import build_patch_body, plan_import  # noqa: E402
from coa_sources.database.keys_import.worker import (  # noqa: E402
    KeysImportError,
    apply_changes,
    make_load_table,
    make_save_keys,
)
from tests.unit.conftest import dao_double  # noqa: E402

pytestmark = pytest.mark.unit

_DR = "coa_sources.api.database_routes"
NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
SOURCE_ID = "src-db-001"


def _table(name: str, columns: list[str], *, fks: list[ForeignKey] | None = None) -> Table:
    return Table(
        name=name,
        database="sales",
        data_source_id=SOURCE_ID,
        namespace_id=NAMESPACE_ID,
        columns=[Column(name=c, data_type="string") for c in columns],
        primary_key=PrimaryKey(),
        foreign_keys=list(fks or []),
    )


def _install_tables(smus: MagicMock, tables: list[Table]) -> None:
    """Make the mocked DataZone client serve these tables as source assets."""
    catalog: dict[str, tuple[MagicMock, str]] = {}
    for table in tables:
        name = f"DS#{SOURCE_ID}:{table.table_id}"
        asset = MagicMock()
        asset.name = name
        asset.asset_id = f"asset-{table.table_id}"
        catalog[name] = (asset, json.dumps(serialize_form(table)))
    smus.find_asset_by_name.side_effect = lambda *, name, **_: catalog[name][0] if name in catalog else None
    smus.search_assets.return_value = MagicMock(items=[a for a, _ in catalog.values()], next_token=None)
    smus.get_asset_forms.side_effect = lambda *, asset_id: {
        "formsOutput": [
            {
                "formName": "CoaTableMetadata",
                "content": next(c for a, c in catalog.values() if a.asset_id == asset_id),
            }
        ]
    }
    smus.create_asset_revision.return_value = MagicMock(asset_id="asset-written")


@pytest.fixture
def env():
    ns_dao = dao_double()
    ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
    smus = create_autospec(_dr.MetadataStoreClient, instance=True)
    dao = dao_double()
    dao.get.return_value = {
        "PK": f"NS#{NAMESPACE_ID}",
        "SK": f"SRC#{SOURCE_ID}",
        "sourceType": "DATABASE",
        "status": "PENDING_REVIEW",
        "discoveredSchemas": ["sales"],
    }
    with (
        patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
        patch(f"{_DR}._get_ns_dao", return_value=ns_dao),
        patch(f"{_DR}._get_smus_client", return_value=smus),
        patch(f"{_DR}._get_dao", return_value=dao),
    ):
        yield {"smus": smus, "dao": dao}


def _written(smus: MagicMock) -> Table:
    forms_input = smus.create_asset_revision.call_args.kwargs["forms_input"]
    return deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=SOURCE_ID)


def _body_for(table: Table, rows: list[KeyRow], all_tables: list[Table]) -> dict:
    plan = plan_import(rows, all_tables)
    body = build_patch_body(table, plan.changes[table.table_id])
    assert body is not None
    return body


AI_FK = ForeignKey(
    column="supplier_id",
    target_table="supplier",
    target_column="supplier_id",
    source="AI_INFERRED",
    confidence=0.9,
    review_status="PENDING_REVIEW",
    provenance="naming pattern",
)
NEW_FK_ROW = KeyRow(
    line=2,
    type="FK",
    source_table="purchase_order",
    columns=("parent_po",),
    target_table="purchase_order",
    target_column="po_number",
)


def test_c1_existing_ai_fk_kept_unchanged_and_new_fk_saved_as_steward(env) -> None:
    po = _table("purchase_order", ["po_number", "supplier_id", "parent_po"], fks=[AI_FK])
    supplier = _table("supplier", ["supplier_id"])
    _install_tables(env["smus"], [po, supplier])

    status, _ = make_save_keys(NAMESPACE_ID, SOURCE_ID)(po.table_id, _body_for(po, [NEW_FK_ROW], [po, supplier]))

    assert status == 200
    written = {fk.column: fk for fk in _written(env["smus"]).foreign_keys}
    assert set(written) == {"supplier_id", "parent_po"}
    assert written["supplier_id"].source == "AI_INFERRED"
    assert written["supplier_id"].review_status == "PENDING_REVIEW"
    assert written["supplier_id"].provenance == "naming pattern"
    assert written["parent_po"].source == "STEWARD_SPECIFIED"
    assert written["parent_po"].target_table == "purchase_order"


def test_c2_existing_approved_fk_stays_approved(env) -> None:
    approved = ForeignKey(**{**AI_FK.__dict__, "review_status": "APPROVED"})
    po = _table("purchase_order", ["po_number", "supplier_id", "parent_po"], fks=[approved])
    supplier = _table("supplier", ["supplier_id"])
    _install_tables(env["smus"], [po, supplier])

    status, _ = make_save_keys(NAMESPACE_ID, SOURCE_ID)(po.table_id, _body_for(po, [NEW_FK_ROW], [po, supplier]))

    assert status == 200
    written = {fk.column: fk for fk in _written(env["smus"]).foreign_keys}
    assert written["supplier_id"].review_status == "APPROVED"
    assert written["supplier_id"].source == "AI_INFERRED"


def test_c3_primary_key_set_as_steward(env) -> None:
    po = _table("purchase_order", ["po_number", "line"])
    _install_tables(env["smus"], [po])
    row = KeyRow(line=2, type="PK", source_table="purchase_order", columns=("po_number", "line"))

    status, _ = make_save_keys(NAMESPACE_ID, SOURCE_ID)(po.table_id, _body_for(po, [row], [po]))

    assert status == 200
    written = _written(env["smus"])
    assert written.primary_key.columns == ["po_number", "line"]
    assert written.primary_key.source == "STEWARD_SPECIFIED"


def test_c4_source_being_approved_returns_409_and_writes_nothing(env) -> None:
    po = _table("purchase_order", ["po_number", "parent_po"])
    _install_tables(env["smus"], [po])
    env["dao"].get.return_value = {**env["dao"].get.return_value, "status": "APPROVING"}

    status, _ = make_save_keys(NAMESPACE_ID, SOURCE_ID)(po.table_id, _body_for(po, [NEW_FK_ROW], [po]))

    assert status == 409
    env["smus"].create_asset_revision.assert_not_called()


def test_c5_load_table_returns_current_keys(env) -> None:
    po = _table("purchase_order", ["po_number", "supplier_id"], fks=[AI_FK])
    _install_tables(env["smus"], [po])

    loaded = make_load_table(NAMESPACE_ID, SOURCE_ID)(po.table_id)

    assert loaded.table_id == "sales.purchase_order"
    assert [(fk.column, fk.target_table, fk.source) for fk in loaded.foreign_keys] == [
        ("supplier_id", "supplier", "AI_INFERRED")
    ]


def test_c6_apply_changes_with_real_adapters_saves_every_table(env) -> None:
    po = _table("purchase_order", ["po_number", "supplier_id", "parent_po"], fks=[AI_FK])
    supplier = _table("supplier", ["supplier_id"])
    _install_tables(env["smus"], [po, supplier])
    rows = [NEW_FK_ROW, KeyRow(line=3, type="PK", source_table="supplier", columns=("supplier_id",))]
    plan = plan_import(rows, [po, supplier])

    result = apply_changes(
        plan.changes,
        load_table=make_load_table(NAMESPACE_ID, SOURCE_ID),
        save_keys=make_save_keys(NAMESPACE_ID, SOURCE_ID),
        sleep=lambda _s: None,
    )

    assert result.tables_updated == 2
    assert result.keys_added == 2
    assert result.tables_failed == 0
    assert env["smus"].create_asset_revision.call_count == 2


def test_c7_load_table_reports_a_missing_table(env) -> None:
    _install_tables(env["smus"], [_table("purchase_order", ["po_number"])])

    with pytest.raises(KeysImportError):
        make_load_table(NAMESPACE_ID, SOURCE_ID)("sales.nope")
