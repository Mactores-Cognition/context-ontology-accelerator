# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.metadata_store.factory.

``METADATA_STORE_BACKEND`` (set by the CDK flag ``metadata_store=dynamodb``)
selects the backend. Unset or ``smus`` keeps today's SMUSClient; ``dynamodb``
builds a DynamoDBMetadataStore on ``METADATA_STORE_TABLE``.
"""

from __future__ import annotations

import os
import socket
from unittest.mock import patch

import pytest
from coa_common.metadata_store.exceptions import MetadataStoreError

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


_FACTORY = "coa_common.metadata_store.factory"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("METADATA_STORE_BACKEND", raising=False)
    monkeypatch.delenv("METADATA_STORE_TABLE", raising=False)
    # Building a boto3 client resolves credentials: keep them fake and never read ~/.aws or instance metadata.
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _dynamodb_env(monkeypatch, table: str = "coa-dev-metadata-assets") -> None:
    monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("METADATA_STORE_TABLE", table)


class TestGetMetadataStore:
    # SMUSClient is patched in these tests: building a real one makes botocore rewrite the module-level
    # _DATAZONE_RETRY_CONFIG in place, which breaks test_smus_extra.py::TestDataZoneRetryConfig if it runs later.
    def test_default_backend_is_smus(self):
        from coa_common.metadata_store.factory import get_metadata_store

        with patch(f"{_FACTORY}.SMUSClient") as smus_cls:
            client = get_metadata_store(domain_id="dzd-1", region_name="us-east-1")
        smus_cls.assert_called_once()
        assert client is smus_cls.return_value

    def test_explicit_smus_backend(self, monkeypatch):
        from coa_common.metadata_store.factory import get_metadata_store

        monkeypatch.setenv("METADATA_STORE_BACKEND", "smus")
        with patch(f"{_FACTORY}.SMUSClient") as smus_cls:
            client = get_metadata_store(domain_id="dzd-1", region_name="us-east-1")
        smus_cls.assert_called_once()
        assert client is smus_cls.return_value

    def test_smus_receives_the_same_arguments_as_before(self):
        from coa_common.metadata_store.factory import get_metadata_store

        with patch(f"{_FACTORY}.SMUSClient") as smus_cls:
            client = get_metadata_store(
                domain_id="dzd-1",
                region_name="us-east-1",
                assume_role_arn="arn:aws:iam::123:role/access",
                session_name="sources-api",
            )

        smus_cls.assert_called_once_with(
            domain_id="dzd-1",
            region_name="us-east-1",
            assume_role_arn="arn:aws:iam::123:role/access",
            session_name="sources-api",
        )
        assert client is smus_cls.return_value

    def test_dynamodb_backend_builds_the_dynamodb_store(self, monkeypatch):
        from coa_common.metadata_store.dynamodb import DynamoDBMetadataStore
        from coa_common.metadata_store.factory import get_metadata_store

        _dynamodb_env(monkeypatch, "coa-dev-metadata-assets")
        store = get_metadata_store(region_name="us-east-1")

        assert isinstance(store, DynamoDBMetadataStore)
        assert store.table_name == "coa-dev-metadata-assets"

    def test_dynamodb_backend_ignores_datazone_arguments(self, monkeypatch):
        from coa_common.metadata_store.dynamodb import DynamoDBMetadataStore
        from coa_common.metadata_store.factory import get_metadata_store

        _dynamodb_env(monkeypatch)
        with patch(f"{_FACTORY}.SMUSClient") as smus_cls:
            store = get_metadata_store(
                domain_id="",
                region_name="us-east-1",
                assume_role_arn="arn:aws:iam::123:role/access",
                session_name="x",
            )

        smus_cls.assert_not_called()
        assert isinstance(store, DynamoDBMetadataStore)

    def test_dynamodb_backend_without_table_raises(self, monkeypatch):
        from coa_common.metadata_store.factory import get_metadata_store

        monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
        with pytest.raises(MetadataStoreError, match="METADATA_STORE_TABLE"):
            get_metadata_store(region_name="us-east-1")

    def test_unknown_backend_raises(self, monkeypatch):
        from coa_common.metadata_store.factory import get_metadata_store

        monkeypatch.setenv("METADATA_STORE_BACKEND", "glue")
        with pytest.raises(MetadataStoreError, match="METADATA_STORE_BACKEND"):
            get_metadata_store(domain_id="dzd-1", region_name="us-east-1")


class TestBackendChecks:
    def test_is_dynamodb_backend(self, monkeypatch):
        from coa_common.metadata_store.factory import is_dynamodb_backend

        assert is_dynamodb_backend() is False
        monkeypatch.setenv("METADATA_STORE_BACKEND", "smus")
        assert is_dynamodb_backend() is False
        monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
        assert is_dynamodb_backend() is True

    @pytest.mark.parametrize(
        ("backend", "table", "domain_id", "expected"),
        [
            (None, None, "dzd-1", True),  # today's DataZone deployment
            (None, None, "", False),  # DataZone mode without a domain
            ("dynamodb", "coa-dev-metadata-assets", "", True),  # DynamoDB mode needs no domain
            ("dynamodb", None, "", False),  # DynamoDB mode without a table
        ],
    )
    def test_metadata_store_configured(self, monkeypatch, backend, table, domain_id, expected):
        from coa_common.metadata_store.factory import metadata_store_configured

        if backend:
            monkeypatch.setenv("METADATA_STORE_BACKEND", backend)
        if table:
            monkeypatch.setenv("METADATA_STORE_TABLE", table)
        assert metadata_store_configured(domain_id) is expected


class TestExports:
    def test_metadata_store_package_exports(self):
        from coa_common.metadata_store import (  # noqa: F401
            DynamoDBMetadataStore,
            get_metadata_store,
            is_dynamodb_backend,
            metadata_store_configured,
        )

    def test_coa_common_package_exports(self):
        from coa_common import DynamoDBMetadataStore, get_metadata_store  # noqa: F401
