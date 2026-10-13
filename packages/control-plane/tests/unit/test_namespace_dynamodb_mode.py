# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace create/delete in DynamoDB mode (METADATA_STORE_BACKEND=dynamodb).

In this mode the infra sets METADATA_STORE_BACKEND and METADATA_STORE_TABLE
and does NOT set DATAZONE_DOMAIN_ID, DATAZONE_PROJECT_PROFILE_ID or
PROJECT_ACCESS_ROLE_ARN(_SSM): there is no DataZone domain, no project-access
role to register, and project deletion removes the namespace's table items.
"""

from __future__ import annotations

import os
import socket
from unittest.mock import MagicMock, patch

import boto3
import pytest
from coa_common.dao import PaginatedResult
from coa_common.metadata_store.base import ProjectResult
from coa_control_plane_server.models.create_namespace_request_content import CreateNamespaceRequestContent
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
def dynamodb_mode(monkeypatch):
    """DynamoDB-mode environment exactly as NamespaceStack sets it (no DataZone variables)."""
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
        "NAMESPACES_TABLE": "test-namespaces",
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-role-mappings",
        "METADATA_STORE_BACKEND": "dynamodb",
        "METADATA_STORE_TABLE": _TABLE,
    }.items():
        monkeypatch.setenv(var, value)
    for var in (
        "AWS_PROFILE",
        "DATAZONE_DOMAIN_ID",
        "DATAZONE_PROJECT_PROFILE_ID",
        "PROJECT_ACCESS_ROLE_ARN_SSM",
        "PROJECT_ACCESS_ROLE_ARN",
        "ATHENA_RESULTS_BUCKET_SSM",
    ):
        monkeypatch.delenv(var, raising=False)


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
            "assetId": f"aid-{project_id}-{name}",
            "name": name,
            "projectId": project_id,
            "formsOutput": [],
            "revision": 1,
        }
    )


class TestCreateHandler:
    def test_builds_the_dynamodb_store_without_datazone_variables(self, dynamodb_mode):
        from coa_common.metadata_store.dynamodb import DynamoDBMetadataStore
        from coa_control_plane.namespace import create_handler

        create_handler._service = None
        try:
            service = create_handler._get_service()
            assert isinstance(service._metadata_store, DynamoDBMetadataStore)
            assert service._metadata_store.table_name == _TABLE
        finally:
            create_handler._service = None


class TestNamespaceServiceCreate:
    @patch("coa_control_plane.namespace.service.boto3")
    def test_create_skips_project_membership(self, mock_boto3, dynamodb_mode):
        from coa_control_plane.namespace.service import NamespaceService

        metadata = MagicMock()
        metadata.create_project.side_effect = lambda **kw: ProjectResult(project_id=kw["name"], name=kw["name"])
        ns_dao = MagicMock(spec=["query", "put", "delete", "update", "_table_name"])
        ns_dao.query.return_value = PaginatedResult(items=[])
        roles_dao = MagicMock(spec=["query", "put", "_table_name"])
        roles_dao.query.return_value = PaginatedResult(
            items=[{"PK": "NAMESPACE_TEMPLATE", "SK": "ROLE#namespace-owner", "name": "Namespace owner"}]
        )
        mappings_dao = MagicMock(spec=["put", "_table_name"])
        svc = NamespaceService(
            namespaces_dao=ns_dao, roles_dao=roles_dao, mappings_dao=mappings_dao, metadata_store=metadata
        )

        result = svc.create(CreateNamespaceRequestContent(name="sales", owner="alice@co.com"))

        metadata.create_project_membership.assert_not_called()
        metadata.delete_project.assert_not_called()  # no rollback
        assert result.data_zone_project_id == result.namespace_id


class TestDeleteProjectCleanup:
    def test_deletes_the_namespace_items_and_never_calls_datazone(self, assets_table):
        from coa_control_plane.namespace import cleanup

        _put_asset(assets_table, "proj-1", "DS#src-1:db.a")
        _put_asset(assets_table, "proj-1", "DS#src-1:db.b")
        _put_asset(assets_table, "proj-2", "DS#src-9:db.keep")

        with patch.object(cleanup, "_datazone_client") as dz:
            cleanup.delete_smu_project("ns-1", {"dataZoneProjectId": "proj-1"})

        dz.assert_not_called()
        remaining = [i["name"] for i in assets_table.scan()["Items"]]
        assert remaining == ["DS#src-9:db.keep"]

    def test_no_project_id_is_a_no_op(self, assets_table):
        from coa_control_plane.namespace import cleanup

        _put_asset(assets_table, "proj-1", "DS#src-1:db.a")
        cleanup.delete_smu_project("ns-1", {})
        assert assets_table.scan()["Count"] == 1
