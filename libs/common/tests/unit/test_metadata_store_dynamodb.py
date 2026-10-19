# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.metadata_store.dynamodb.DynamoDBMetadataStore.

The store replaces DataZone with one DynamoDB table (created by NamespaceStack
when ``metadata_store=dynamodb``): PK ``PROJECT#{projectId}``, SK
``ASSET#{assetName}``, GSI ``AssetById`` on ``assetId``. Forms are stored as
the caller passed them and returned as ``formsOutput``; forms over the
compression threshold are gzip-compressed and marked ``formsEncoding=gzip``.
"""

from __future__ import annotations

import base64
import json
import os
import random
import socket

import boto3
import pytest
from coa_common.metadata_store.base import AssetResult, MetadataStoreClient, ProjectResult
from coa_common.metadata_store.exceptions import MetadataStoreError
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
_FORM = "CoaTableMetadata"


@pytest.fixture()
def table(monkeypatch):
    """Moto table matching NamespaceStack's MetadataAssetsTable."""
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
    }.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
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


@pytest.fixture()
def store(table):
    from coa_common.metadata_store.dynamodb import DynamoDBMetadataStore

    return DynamoDBMetadataStore(table_name=_TABLE, region_name=_REGION)


def _forms(content: str = '{"tableName": "orders"}') -> list[dict[str, str]]:
    return [{"formName": _FORM, "content": content, "typeIdentifier": _FORM}]


def _create(store, name: str, project_id: str = "proj-1", content: str = '{"tableName": "t"}') -> AssetResult:
    return store.create_asset(
        project_id=project_id,
        name=name,
        type_identifier="CoaRelationalTable",
        description="desc",
        forms_input=_forms(content),
    )


def _raw(table, name: str, project_id: str = "proj-1") -> dict | None:
    return table.get_item(Key={"PK": f"PROJECT#{project_id}", "SK": f"ASSET#{name}"}).get("Item")


def _incompressible(n_bytes: int) -> str:
    """Base64 of random bytes: gzip barely shrinks it."""
    return base64.b64encode(random.Random(0).randbytes(n_bytes)).decode()


class TestInterface:
    def test_find_asset_by_name_is_part_of_the_interface(self):
        assert "find_asset_by_name" in MetadataStoreClient.__abstractmethods__

    def test_store_implements_the_interface(self, store):
        assert isinstance(store, MetadataStoreClient)

    def test_exposes_table_name(self, store):
        assert store.table_name == _TABLE


class TestProjects:
    def test_create_project_uses_the_name_as_id(self, store):
        result = store.create_project(name="ns-1", description="d", project_profile_id="ignored")
        assert result == ProjectResult(project_id="ns-1", name="ns-1")

    def test_get_project_returns_the_id(self, store):
        assert store.get_project(project_id="ns-1") == ProjectResult(project_id="ns-1", name="ns-1")

    def test_create_project_membership_is_a_no_op(self, store, table):
        store.create_project_membership(project_id="ns-1", user_identifier="arn:aws:iam::1:role/x")
        assert table.scan()["Count"] == 0

    def test_delete_project_removes_only_that_projects_assets(self, store, table):
        for i in range(30):  # more than one BatchWriteItem (25)
            _create(store, f"DS#src-1:db.t{i}", project_id="proj-1")
        _create(store, "DS#src-1:db.keep", project_id="proj-2")

        store.delete_project(project_id="proj-1")

        items = table.scan()["Items"]
        assert [i["name"] for i in items] == ["DS#src-1:db.keep"]


class TestCreateAndRead:
    def test_create_asset_writes_the_documented_item_layout(self, store, table):
        result = _create(store, "DS#src-1:sales.orders")

        item = _raw(table, "DS#src-1:sales.orders")
        assert item is not None
        assert item["assetId"] == result.asset_id
        assert item["name"] == "DS#src-1:sales.orders"
        assert item["projectId"] == "proj-1"
        assert item["formsOutput"] == _forms('{"tableName": "t"}')
        assert int(item["revision"]) == 1
        assert "formsEncoding" not in item

    def test_create_asset_returns_ids(self, store):
        result = _create(store, "DS#src-1:sales.orders")
        assert result.asset_id
        assert result.name == "DS#src-1:sales.orders"
        assert result.project_id == "proj-1"

    def test_create_asset_rejects_a_duplicate_name(self, store):
        _create(store, "DS#src-1:sales.orders")
        with pytest.raises(MetadataStoreError):
            _create(store, "DS#src-1:sales.orders")

    def test_get_asset_by_id(self, store):
        created = _create(store, "DS#src-1:sales.orders")
        got = store.get_asset(asset_id=created.asset_id)
        assert got == AssetResult(asset_id=created.asset_id, name="DS#src-1:sales.orders", project_id="proj-1")

    def test_get_asset_missing_raises(self, store):
        with pytest.raises(MetadataStoreError):
            store.get_asset(asset_id="no-such-asset")

    def test_get_asset_forms_returns_forms_output(self, store):
        created = _create(store, "DS#src-1:sales.orders", content='{"tableName": "orders"}')
        detail = store.get_asset_forms(asset_id=created.asset_id)
        assert detail["id"] == created.asset_id
        assert detail["name"] == "DS#src-1:sales.orders"
        assert detail["owningProjectId"] == "proj-1"
        assert detail["formsOutput"][0]["formName"] == _FORM
        assert json.loads(detail["formsOutput"][0]["content"]) == {"tableName": "orders"}

    def test_get_asset_forms_missing_raises(self, store):
        with pytest.raises(MetadataStoreError):
            store.get_asset_forms(asset_id="no-such-asset")


class TestRevisionsAndDelete:
    def test_revision_replaces_forms_and_bumps_revision(self, store, table):
        created = _create(store, "DS#src-1:sales.orders", content='{"v": 1}')

        result = store.create_asset_revision(
            asset_id=created.asset_id, name="DS#src-1:sales.orders", description="d2", forms_input=_forms('{"v": 2}')
        )

        assert result.asset_id == created.asset_id
        assert result.project_id == "proj-1"
        item = _raw(table, "DS#src-1:sales.orders")
        assert item["formsOutput"] == _forms('{"v": 2}')
        assert int(item["revision"]) == 2

    def test_revision_of_missing_asset_raises(self, store):
        with pytest.raises(MetadataStoreError):
            store.create_asset_revision(asset_id="no-such-asset", name="x", forms_input=_forms())

    def test_delete_asset(self, store, table):
        created = _create(store, "DS#src-1:sales.orders")
        store.delete_asset(asset_id=created.asset_id)
        assert _raw(table, "DS#src-1:sales.orders") is None

    def test_delete_missing_asset_raises(self, store):
        with pytest.raises(MetadataStoreError):
            store.delete_asset(asset_id="no-such-asset")


class TestSearchAndFind:
    def test_search_by_source_prefix_is_scoped_to_source_and_project(self, store):
        _create(store, "DS#src-1:sales.orders")
        _create(store, "DS#src-1:sales.customers")
        _create(store, "DS#src-2:sales.orders")
        _create(store, "DS#src-1:sales.other_project", project_id="proj-2")

        result = store.search_assets(project_id="proj-1", search_text="DS#src-1")

        assert sorted(a.name for a in result.items) == ["DS#src-1:sales.customers", "DS#src-1:sales.orders"]
        assert result.next_token is None
        assert all(a.forms_output is None for a in result.items)

    def test_search_paginates_with_a_string_token(self, store):
        for name in ("a", "b", "c"):
            _create(store, f"DS#src-1:db.{name}")

        first = store.search_assets(project_id="proj-1", search_text="DS#src-1", max_results=2)
        assert len(first.items) == 2
        assert isinstance(first.next_token, str)
        second = store.search_assets(
            project_id="proj-1", search_text="DS#src-1", max_results=2, next_token=first.next_token
        )

        assert len(second.items) == 1
        assert second.next_token is None
        assert {a.name for a in first.items + second.items} == {"DS#src-1:db.a", "DS#src-1:db.b", "DS#src-1:db.c"}

    def test_search_include_forms_inlines_forms(self, store):
        _create(store, "DS#src-1:sales.orders", content='{"tableName": "orders"}')
        result = store.search_assets(project_id="proj-1", search_text="DS#src-1", include_forms=True)
        assert result.items[0].forms_output == _forms('{"tableName": "orders"}')

    def test_search_text_without_source_prefix_matches_by_substring(self, store):
        _create(store, "DS#src-1:sales.orders")
        _create(store, "DS#src-1:sales.customers")
        result = store.search_assets(project_id="proj-1", search_text="orders")
        assert [a.name for a in result.items] == ["DS#src-1:sales.orders"]

    def test_find_asset_by_name_is_exact(self, store):
        _create(store, "DS#src-1:sales.orders_archive")
        wanted = _create(store, "DS#src-1:sales.orders")
        found = store.find_asset_by_name(project_id="proj-1", name="DS#src-1:sales.orders")
        assert found is not None
        assert found.asset_id == wanted.asset_id

    def test_find_asset_by_name_missing_returns_none(self, store):
        assert store.find_asset_by_name(project_id="proj-1", name="DS#src-1:sales.nope") is None


class TestCompression:
    def test_small_forms_are_stored_plain(self, store, table):
        _create(store, "DS#src-1:db.small", content="x" * 1000)
        assert "formsEncoding" not in _raw(table, "DS#src-1:db.small")

    def test_large_forms_are_compressed_and_read_back_unchanged(self, store, table):
        big = "x" * 350_000  # over the 300 KB threshold, compresses well
        created = _create(store, "DS#src-1:db.wide", content=big)

        item = _raw(table, "DS#src-1:db.wide")
        assert item["formsEncoding"] == "gzip"
        assert "formsOutput" not in item
        assert store.get_asset_forms(asset_id=created.asset_id)["formsOutput"][0]["content"] == big

    def test_search_include_forms_decompresses(self, store):
        big = "y" * 350_000
        _create(store, "DS#src-1:db.wide", content=big)
        result = store.search_assets(project_id="proj-1", search_text="DS#src-1", include_forms=True)
        assert result.items[0].forms_output[0]["content"] == big

    def test_plain_and_compressed_items_coexist(self, store):
        small = _create(store, "DS#src-1:db.small", content="s" * 100)
        wide = _create(store, "DS#src-1:db.wide", content="w" * 350_000)
        assert store.get_asset_forms(asset_id=small.asset_id)["formsOutput"][0]["content"] == "s" * 100
        assert store.get_asset_forms(asset_id=wide.asset_id)["formsOutput"][0]["content"] == "w" * 350_000

    def test_revision_can_switch_an_item_to_compressed(self, store, table):
        created = _create(store, "DS#src-1:db.grows", content="small")
        store.create_asset_revision(
            asset_id=created.asset_id, name="DS#src-1:db.grows", forms_input=_forms("z" * 350_000)
        )
        assert _raw(table, "DS#src-1:db.grows")["formsEncoding"] == "gzip"
        assert store.get_asset_forms(asset_id=created.asset_id)["formsOutput"][0]["content"] == "z" * 350_000

    def test_too_large_even_after_compression_raises_a_clear_error(self, store, table):
        with pytest.raises(MetadataStoreError) as exc:
            _create(store, "DS#src-1:db.huge", content=_incompressible(450_000))
        message = str(exc.value)
        assert "DS#src-1:db.huge" in message
        assert "400 KB" in message
        assert _raw(table, "DS#src-1:db.huge") is None

    def test_revision_too_large_raises_and_keeps_the_old_item(self, store, table):
        created = _create(store, "DS#src-1:db.huge", content="old")
        with pytest.raises(MetadataStoreError) as exc:
            store.create_asset_revision(
                asset_id=created.asset_id, name="DS#src-1:db.huge", forms_input=_forms(_incompressible(450_000))
            )
        assert "DS#src-1:db.huge" in str(exc.value)
        assert _raw(table, "DS#src-1:db.huge")["formsOutput"] == _forms("old")
