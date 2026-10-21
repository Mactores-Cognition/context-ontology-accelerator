"""Scan pipeline in DynamoDB mode (METADATA_STORE_BACKEND=dynamodb).

SourcesStack sets METADATA_STORE_BACKEND / METADATA_STORE_TABLE on the
discovery Lambda, the enrichment container and the bulk-review worker, and does
NOT set SMUS_DOMAIN_ID or PROJECT_ACCESS_ROLE_ARN in this mode.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from coa_common.domain_models import BusinessMetadata, Column, DiscoveredMetadata, Table
from moto import mock_aws

pytestmark = pytest.mark.unit


_LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail fast on DNS lookups too (the sources conftest blocks only connections)."""
    real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            raise RuntimeError(f"unit test attempted a real network lookup: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


_TABLE = "test-metadata-assets"
_REGION = "us-east-1"


@pytest.fixture()
def dynamodb_mode(monkeypatch):
    monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("METADATA_STORE_TABLE", _TABLE)
    monkeypatch.delenv("SMUS_DOMAIN_ID", raising=False)
    monkeypatch.delenv("PROJECT_ACCESS_ROLE_ARN", raising=False)


@pytest.fixture()
def assets_table(dynamodb_mode):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        yield ddb.create_table(
            TableName=_TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "assetId", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "AssetById",
                    "KeySchema": [{"AttributeName": "assetId", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )


def _table(name: str, description: str = "", ds: str = "DS#src-1") -> Table:
    return Table(
        name=name,
        database="sales",
        data_source_id=ds,
        business_metadata=BusinessMetadata(description=description),
        columns=[Column(name="id", data_type="int", nullable=False)],
    )


def _raw(tbl, name: str, project_id: str = "proj-1") -> dict | None:
    return tbl.get_item(Key={"PK": f"PROJECT#{project_id}", "SK": f"ASSET#{name}"}).get("Item")


def _form_content(item: dict) -> dict:
    return json.loads(item["formsOutput"][0]["content"])


class TestDiscoveryHandler:
    def test_module_imports_without_smus_domain_id(self):
        """discovery_handler reads SMUS_DOMAIN_ID at import; DynamoDB mode does not set it.

        Imported in a fresh interpreter so this test does not reload the module
        other tests already hold.
        """
        env = {k: v for k, v in os.environ.items() if k != "SMUS_DOMAIN_ID"}
        env.update(
            {
                "SOURCES_TABLE": "test-datasources",
                "SOURCE_SCAN_JOBS_TABLE": "test-scan-jobs",
                "NAMESPACES_TABLE": "test-namespaces",
                "METADATA_STORE_BACKEND": "dynamodb",
                "METADATA_STORE_TABLE": _TABLE,
            }
        )
        proc = subprocess.run(
            [sys.executable, "-c", "import coa_sources.database.pipeline.discovery_handler"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]


class TestEnrichmentHandler:
    @patch(
        "coa_sources.database.enrichment.cross_source_orchestrator.run_for_namespace",
        return_value={"sources": 0, "relationships_written": 0, "tables_updated": 0},
    )
    @patch(
        "coa_sources.database.enrichment.table_enricher.run",
        return_value={"tables_enriched": 1, "tables_failed": 0, "tables_skipped_unchanged": 0},
    )
    @patch("coa_common.dao.DynamoDBDAO")
    def test_runs_without_smus_domain_id(self, mock_dao_cls, mock_run, _xsrc, monkeypatch, dynamodb_mode):
        for var, value in {
            "DATASOURCE_ID": "DS#ds-123",
            "SCAN_JOB_ID": "SCAN#scan-456",
            "NAMESPACE_ID": "ns-test",
            "SCAN_TYPE": "full",
            "SOURCES_TABLE": "test-datasources",
            "SOURCE_SCAN_JOBS_TABLE": "test-scan-jobs",
            "NAMESPACES_TABLE": "test-namespaces",
        }.items():
            monkeypatch.setenv(var, value)

        def _get(key):
            if key.get("SK") == "METADATA":
                return {"dataZoneProjectId": "ns-test"}
            if key.get("SK", "").startswith("SRC#"):
                return {"sourceId": "ds-123"}
            return None

        dao = MagicMock()
        dao.get.side_effect = _get
        mock_dao_cls.return_value = dao

        from coa_sources.database.pipeline.enrichment_handler import handler

        handler()

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["domain_id"] == ""
        assert mock_run.call_args.kwargs["namespace_id"] == "ns-test"  # the project id


class TestMetadataWriter:
    def test_creates_then_revises_assets_in_the_table(self, assets_table):
        from coa_sources.database.metadata_writer import write_to_datazone

        metadata = DiscoveredMetadata(tables=[_table("orders"), _table("customers")])
        first = write_to_datazone(domain_id="", project_id="proj-1", metadata=metadata, data_source_id="DS#src-1")
        assert first == {"assets_created": 2, "assets_revised": 0}
        item = _raw(assets_table, "DS#src-1:sales.orders")
        assert _form_content(item)["tableName"] == "orders"

        second = write_to_datazone(domain_id="", project_id="proj-1", metadata=metadata, data_source_id="DS#src-1")
        assert second == {"assets_created": 0, "assets_revised": 2}
        assert int(_raw(assets_table, "DS#src-1:sales.orders")["revision"]) == 2


class TestTableEnricherWriteBack:
    def test_writes_enrichment_as_a_revision(self, assets_table):
        from coa_common.datazone_forms import build_forms_input
        from coa_sources.database.enrichment.table_enricher import _write_enriched_assets

        name = "DS#src-1:sales.orders"
        assets_table.put_item(
            Item={
                "PK": "PROJECT#proj-1",
                "SK": f"ASSET#{name}",
                "assetId": "a-orders",
                "name": name,
                "projectId": "proj-1",
                "formsOutput": build_forms_input(_table("orders")),
                "revision": 1,
            }
        )

        _write_enriched_assets([_table("orders", description="AI description")], "", "proj-1")

        item = _raw(assets_table, name)
        assert _form_content(item)["description"] == "AI description"
        assert int(item["revision"]) == 2


class TestBulkReviewWorker:
    def test_builds_the_dynamodb_store(self, dynamodb_mode):
        from coa_sources.database.bulk_review import worker

        # Checked by name, not isinstance: test_sources_handler.py purges coa_common* from sys.modules,
        # so the worker's factory and a fresh import can hold different copies of the class.
        store = worker._build_smus_client()
        assert type(store).__name__ == "DynamoDBMetadataStore"
        assert store.table_name == _TABLE
