# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata store abstraction and implementations."""

from .base import AssetResult, MetadataStoreClient, ProjectResult, SearchResult
from .catalog_reader import read_approved_catalog
from .dynamodb import DynamoDBMetadataStore
from .exceptions import MetadataStoreError
from .factory import get_metadata_store, is_dynamodb_backend, metadata_store_configured
from .reader import read_assets_for_datasource
from .smus import SMUSClient

__all__ = [
    "AssetResult",
    "DynamoDBMetadataStore",
    "MetadataStoreClient",
    "MetadataStoreError",
    "ProjectResult",
    "SMUSClient",
    "SearchResult",
    "get_metadata_store",
    "is_dynamodb_backend",
    "metadata_store_configured",
    "read_approved_catalog",
    "read_assets_for_datasource",
]
