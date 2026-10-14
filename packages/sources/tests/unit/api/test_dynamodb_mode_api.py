# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources API in DynamoDB mode (METADATA_STORE_BACKEND=dynamodb).

SourcesStack does not set SMUS_DOMAIN_ID in this mode, so the module-level
``_SMUS_DOMAIN_ID`` is empty. The table endpoints must not answer
"SMUS_DOMAIN_ID not configured" (HTTP 500), and source deletion must remove the
source's items instead of skipping cleanup.
"""

from __future__ import annotations

import os
import socket
import sys
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Import sources_handler FIRST to resolve the circular import, then database_routes.
import coa_sources.api.sources_handler as _sh  # noqa: E402, I001
import coa_sources.api.database_routes as _dr  # noqa: E402, I001

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


_DR = "coa_sources.api.database_routes"
_SH = "coa_sources.api.sources_handler"
_TABLE = "test-metadata-assets"
_REGION = "us-east-1"
_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_SOURCE_ID = "11111111-2222-4333-8444-555555555555"


def _current(module_name: str, fallback):
    return sys.modules.get(module_name, fallback)


@pytest.fixture()
def dynamodb_mode(monkeypatch):
    monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("METADATA_STORE_TABLE", _TABLE)
    with patch(f"{_DR}._SMUS_DOMAIN_ID", ""), patch(f"{_SH}._SMUS_DOMAIN_ID", ""):
        yield


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


def _put_asset(tbl, project_id: str, name: str) -> None:
    tbl.put_item(
        Item={
            "PK": f"PROJECT#{project_id}",
            "SK": f"ASSET#{name}",
            "assetId": f"aid-{name}",
            "name": name,
            "projectId": project_id,
            "formsOutput": [],
            "revision": 1,
        }
    )


class TestClientAndGuards:
    def test_get_smus_client_builds_the_dynamodb_store(self, dynamodb_mode):
        # By name, not isinstance: test_sources_handler.py purges coa_common* from sys.modules.
        store = _current(_DR, _dr)._get_smus_client()
        assert type(store).__name__ == "DynamoDBMetadataStore"
        assert store.table_name == _TABLE

    def test_smus_or_500_returns_a_client(self, dynamodb_mode):
        client, err = _current(_DR, _dr)._smus_or_500()
        assert err is None
        assert client is not None

    def test_list_tables_passes_the_config_check(self, dynamodb_mode):
        """Unknown namespace -> 404, which proves the 500 config check was passed."""
        with patch(f"{_DR}._resolve_project_id", return_value=None):
            resp = _current(_DR, _dr)._handle_list_tables({"queryStringParameters": None}, _NAMESPACE_ID, _SOURCE_ID)
        assert resp["statusCode"] == 404

    def test_get_table_passes_the_config_check(self, dynamodb_mode):
        with patch(f"{_DR}._resolve_project_id", return_value=None):
            resp = _current(_DR, _dr)._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "sales.orders")
        assert resp["statusCode"] == 404


class TestSourceDeleteCleanup:
    def test_deletes_the_sources_items(self, assets_table):
        _put_asset(assets_table, _NAMESPACE_ID, f"DS#{_SOURCE_ID}:sales.orders")
        _put_asset(assets_table, _NAMESPACE_ID, f"DS#{_SOURCE_ID}:sales.customers")
        _put_asset(assets_table, _NAMESPACE_ID, "DS#other-source:sales.keep")

        with patch(f"{_DR}._resolve_project_id", return_value=_NAMESPACE_ID):
            removed, complete = _current(_SH, _sh)._delete_source_datazone_assets(_NAMESPACE_ID, _SOURCE_ID)

        assert (removed, complete) == (2, True)
        assert [i["name"] for i in assets_table.scan()["Items"]] == ["DS#other-source:sales.keep"]
