# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""build_data_source_lookup in DynamoDB mode (METADATA_STORE_BACKEND=dynamodb).

MetricServiceStack sets METADATA_STORE_BACKEND / METADATA_STORE_TABLE and no
SMUS_DOMAIN_ID in this mode. The lookup must still be built, otherwise metric
table checks are silently skipped.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_DYNAMODB_ENV = {
    "DATA_SOURCES_TABLE": "sources-tbl",
    "NAMESPACES_TABLE": "ns-tbl",
    "METADATA_STORE_BACKEND": "dynamodb",
    "METADATA_STORE_TABLE": "coa-dev-metadata-assets",
    "AWS_REGION": "us-east-1",
}


@patch("coa_metrics.data_source_lookup_factory._resolve_project_id", return_value="prj-9")
def test_builds_lookup_without_smus_domain_id(mock_resolve):
    from coa_metrics.data_source_lookup_factory import build_data_source_lookup
    from coa_metrics.lookups import SmusCatalogDataSourceLookup

    with patch.dict("os.environ", _DYNAMODB_ENV, clear=True):
        lookup = build_data_source_lookup("ns-1")

    assert isinstance(lookup, SmusCatalogDataSourceLookup)
    mock_resolve.assert_called_once_with("ns-1", "ns-tbl", "us-east-1")


def test_still_requires_the_tables():
    """Guard (passes before and after): DynamoDB mode does not relax the other settings."""
    from coa_metrics.data_source_lookup_factory import build_data_source_lookup

    env = {k: v for k, v in _DYNAMODB_ENV.items() if k != "DATA_SOURCES_TABLE"}
    with patch.dict("os.environ", env, clear=True):
        assert build_data_source_lookup("ns-1") is None


def test_datazone_mode_still_requires_the_domain():
    """Guard (passes before and after): without the DynamoDB flag, SMUS_DOMAIN_ID stays required."""
    from coa_metrics.data_source_lookup_factory import build_data_source_lookup

    env = {"DATA_SOURCES_TABLE": "sources-tbl", "NAMESPACES_TABLE": "ns-tbl", "AWS_REGION": "us-east-1"}
    with patch.dict("os.environ", env, clear=True):
        assert build_data_source_lookup("ns-1") is None
