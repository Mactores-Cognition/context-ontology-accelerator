# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""coa_common.metadata_store.reader in DynamoDB mode (METADATA_STORE_BACKEND=dynamodb).

The readers are shared by enrichment, rescans, metric validation and ontology
induction. In DynamoDB mode they must read from the metadata-assets table with
an empty domain id instead of calling DataZone. Assets are seeded directly in
the documented item layout so these tests do not depend on the store's writer.
"""

from __future__ import annotations

import json
import socket

import boto3
import pytest
from coa_common.datazone_forms import build_forms_input
from coa_common.domain_models import Column, Table
from coa_common.metadata_store.reader import (
    read_asset_names_for_datasource,
    read_assets_for_datasource,
    read_table_for_asset,
)
from moto import mock_aws

pytestmark = pytest.mark.unit


_LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail fast if a test leaves the machine (moto lets unmocked services, e.g. DataZone, through)."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def _getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            raise RuntimeError(f"unit test attempted a real network lookup: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _connect(self, address):
        if isinstance(address, tuple) and address[0] not in _LOOPBACK:
            raise RuntimeError(f"unit test attempted a real network connection: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", _connect)


_TABLE = "test-metadata-assets"
_REGION = "us-east-1"


@pytest.fixture()
def table(monkeypatch):
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
        "METADATA_STORE_BACKEND": "dynamodb",
        "METADATA_STORE_TABLE": _TABLE,
    }.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("PROJECT_ACCESS_ROLE_ARN", raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        tbl = ddb.create_table(
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
        _seed(tbl, "proj-1", "a-orders", Table(name="orders", database="sales", data_source_id="ds-1"))
        _seed(tbl, "proj-1", "a-customers", Table(name="customers", database="sales", data_source_id="ds-1"))
        _seed(tbl, "proj-1", "a-other", Table(name="widgets", database="ops", data_source_id="ds-2"))
        yield tbl


def _seed(tbl, project_id: str, asset_id: str, t: Table) -> None:
    t.columns = [Column(name="id", data_type="int", nullable=False)]
    name = f"DS#{t.data_source_id}:{t.table_id}"
    tbl.put_item(
        Item={
            "PK": f"PROJECT#{project_id}",
            "SK": f"ASSET#{name}",
            "assetId": asset_id,
            "name": name,
            "projectId": project_id,
            "typeIdentifier": "CoaRelationalTable",
            "description": "",
            "formsOutput": build_forms_input(t),
            "revision": 1,
        }
    )


def test_read_assets_for_datasource(table):
    tables = read_assets_for_datasource("", "proj-1", "ds-1")
    assert sorted(t.name for t in tables) == ["customers", "orders"]
    assert all(t.columns[0].name == "id" for t in tables)


def test_read_asset_names_for_datasource(table):
    names = read_asset_names_for_datasource("", "proj-1", "DS#ds-1")
    assert names == {"sales.orders": "a-orders", "sales.customers": "a-customers"}


def test_read_table_for_asset(table):
    t = read_table_for_asset("", "a-orders", "DS#ds-1:sales.orders", "ds-1")
    assert t is not None
    assert t.name == "orders"
    assert t.database == "sales"


def test_seeded_form_is_the_datazone_form_format(table):
    """Guard: the seeded item holds exactly what DataZone would return (formName + JSON content)."""
    item = table.get_item(Key={"PK": "PROJECT#proj-1", "SK": "ASSET#DS#ds-1:sales.orders"})["Item"]
    assert json.loads(item["formsOutput"][0]["content"])["tableName"] == "orders"
