# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pick the metadata store backend from the environment.

The CDK flag ``metadata_store=dynamodb`` sets ``METADATA_STORE_BACKEND=dynamodb``
and ``METADATA_STORE_TABLE`` on every function and container that uses the
store. Without it the backend is SMUS (DataZone), exactly as before.
"""

from __future__ import annotations

import os

from .base import MetadataStoreClient
from .dynamodb import DynamoDBMetadataStore
from .exceptions import MetadataStoreError
from .smus import SMUSClient

BACKEND_ENV = "METADATA_STORE_BACKEND"
TABLE_ENV = "METADATA_STORE_TABLE"
BACKEND_SMUS = "smus"
BACKEND_DYNAMODB = "dynamodb"


def _backend() -> str:
    return os.environ.get(BACKEND_ENV, "").strip() or BACKEND_SMUS


def is_dynamodb_backend() -> bool:
    """Whether ``METADATA_STORE_BACKEND`` selects the DynamoDB store."""
    return _backend() == BACKEND_DYNAMODB


def metadata_store_configured(domain_id: str = "") -> bool:
    """Whether the selected backend has what it needs to be built.

    Args:
        domain_id: The SMUS domain id the caller has (needed only for SMUS).

    Returns:
        True for the DynamoDB backend with a table name, or for SMUS with a domain id.
    """
    if is_dynamodb_backend():
        return bool(os.environ.get(TABLE_ENV))
    return bool(domain_id)


def get_metadata_store(
    *,
    domain_id: str = "",
    region_name: str | None = None,
    assume_role_arn: str | None = None,
    session_name: str | None = None,
) -> MetadataStoreClient:
    """Build the metadata store selected by ``METADATA_STORE_BACKEND``.

    Takes the same arguments as ``SMUSClient``; the DynamoDB store ignores the
    DataZone-only ones (it is reached with the caller's own IAM role).

    Args:
        domain_id: SMUS domain identifier (SMUS only).
        region_name: AWS region.
        assume_role_arn: Role to assume before DataZone calls (SMUS only).
        session_name: STS session name (SMUS only).

    Returns:
        A ``DynamoDBMetadataStore`` or an ``SMUSClient``.

    Raises:
        MetadataStoreError: If the backend is unknown, or DynamoDB is selected without a table.
    """
    backend = _backend()
    if backend == BACKEND_DYNAMODB:
        table_name = os.environ.get(TABLE_ENV, "")
        if not table_name:
            raise MetadataStoreError(f"{BACKEND_ENV}={BACKEND_DYNAMODB} requires {TABLE_ENV} to be set")
        return DynamoDBMetadataStore(table_name=table_name, region_name=region_name)
    if backend == BACKEND_SMUS:
        return SMUSClient(
            domain_id=domain_id,
            region_name=region_name,
            assume_role_arn=assume_role_arn,
            session_name=session_name,
        )
    raise MetadataStoreError(f"Unknown {BACKEND_ENV}: {backend!r} (expected {BACKEND_SMUS} or {BACKEND_DYNAMODB})")
