"""DynamoDB implementation of MetadataStoreClient.

Used instead of SageMaker Unified Studio (DataZone) when the stack is deployed
with ``metadata_store=dynamodb``. One table, created by NamespaceStack:

* PK ``PROJECT#{projectId}``, SK ``ASSET#{assetName}`` — one item per scanned
  table. The project id is the namespace id; no project resource exists.
* GSI ``AssetById`` on ``assetId`` — callers hand asset ids around (revise,
  delete, fetch forms), exactly as with DataZone.

Forms are stored as the caller passed them and returned as ``formsOutput``, so
``datazone_forms`` and every reader work unchanged. A revision overwrites the
item and bumps ``revision`` (no history). Forms larger than
``COMPRESS_THRESHOLD_BYTES`` are gzip-compressed into ``formsGzip`` with
``formsEncoding=gzip``; plain and compressed items coexist.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from coa_common.aws_config import async_boto_config

from .base import AssetResult, MetadataStoreClient, ProjectResult, SearchResult
from .exceptions import MetadataStoreError

logger = logging.getLogger(__name__)

ASSET_ID_INDEX = "AssetById"
# Forms JSON above this size is gzip-compressed before it is written.
COMPRESS_THRESHOLD_BYTES = 300 * 1024
# DynamoDB's hard per-item limit (attribute names + values).
MAX_ITEM_BYTES = 400 * 1024
# DataZone truncates asset descriptions to this length; keep items comparable.
_MAX_DESCRIPTION = 2048


def _pk(project_id: str) -> str:
    return f"PROJECT#{project_id}"


def _sk(name: str) -> str:
    return f"ASSET#{name}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _value_size(value: Any) -> int:
    """Approximate DynamoDB size of one attribute value, in bytes."""
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, bytes | bytearray):
        return len(value)
    if isinstance(value, int | float):
        return 8
    return len(json.dumps(value).encode())


def _item_size(item: dict[str, Any]) -> int:
    return sum(len(key.encode()) + _value_size(value) for key, value in item.items())


def _encode_token(last_key: dict[str, Any] | None) -> str | None:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (ValueError, TypeError) as exc:
        raise MetadataStoreError("Invalid next_token") from exc
    if not isinstance(decoded, dict):
        raise MetadataStoreError("Invalid next_token")
    return decoded


class DynamoDBMetadataStore(MetadataStoreClient):
    """MetadataStoreClient backed by the metadata-assets DynamoDB table.

    Args:
        table_name: Name of the metadata-assets table (``METADATA_STORE_TABLE``).
        region_name: AWS region. Defaults to the SDK default chain.
    """

    def __init__(self, table_name: str, region_name: str | None = None) -> None:
        """Bind the store to its table; no AWS call is made here.

        Args:
            table_name: Name of the metadata-assets table.
            region_name: AWS region; defaults to the SDK default chain.
        """
        self._table_name = table_name
        resource = boto3.resource("dynamodb", region_name=region_name, config=async_boto_config())
        self._table = resource.Table(table_name)

    @property
    def table_name(self) -> str:
        """Name of the backing DynamoDB table."""
        return self._table_name

    # ── Projects ──────────────────────────────────────────────────

    def create_project(self, *, name: str, description: str = "", project_profile_id: str = "") -> ProjectResult:
        """Return a project whose id is ``name`` (the namespace id); nothing is created.

        Args:
            name: Project name; NamespaceService passes the namespace id.
            description: Ignored.
            project_profile_id: Ignored (DataZone only).

        Returns:
            The project's id and name, both ``name``.
        """
        return ProjectResult(project_id=name, name=name)

    def delete_project(self, *, project_id: str) -> None:
        """Delete every asset item of a project.

        Args:
            project_id: Project (namespace) whose assets are deleted.

        Raises:
            MetadataStoreError: If a query or batch delete fails.
        """
        try:
            start_key: dict[str, Any] | None = None
            deleted = 0
            while True:
                kwargs: dict[str, Any] = {
                    "KeyConditionExpression": Key("PK").eq(_pk(project_id)),
                    "ProjectionExpression": "PK, SK",
                }
                if start_key:
                    kwargs["ExclusiveStartKey"] = start_key
                resp = self._table.query(**kwargs)
                items = resp.get("Items", [])
                with self._table.batch_writer() as batch:
                    for item in items:
                        batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                deleted += len(items)
                start_key = resp.get("LastEvaluatedKey")
                if not start_key:
                    break
        except ClientError as exc:
            raise MetadataStoreError(f"Failed to delete project: {exc}") from exc
        logger.info("Deleted %d asset item(s) of project %s", deleted, project_id)

    def get_project(self, *, project_id: str) -> ProjectResult:
        """Return the project; projects have no stored record.

        Args:
            project_id: Project id.

        Returns:
            The project's id, also used as its name.
        """
        return ProjectResult(project_id=project_id, name=project_id)

    # ── Assets ────────────────────────────────────────────────────

    def create_asset(
        self,
        *,
        project_id: str,
        name: str,
        type_identifier: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Create an asset item; the name must be new in the project.

        Args:
            project_id: Owning project identifier.
            name: Asset name (``DS#{sourceId}:{db}.{table}``).
            type_identifier: Asset type, stored for reference.
            description: Optional description (truncated to 2048 chars).
            forms_input: Metadata forms, stored and later returned as ``formsOutput``.

        Returns:
            The new asset's id, name, and owning project.

        Raises:
            MetadataStoreError: If the name exists, the item is too large, or the write fails.
        """
        now = _now()
        item: dict[str, Any] = {
            "PK": _pk(project_id),
            "SK": _sk(name),
            "assetId": str(uuid.uuid4()),
            "name": name,
            "projectId": project_id,
            "typeIdentifier": type_identifier,
            "description": (description or "")[:_MAX_DESCRIPTION],
            "revision": 1,
            "createdAt": now,
            "updatedAt": now,
            **self._encode_forms(name, forms_input or []),
        }
        self._check_size(name, item)
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise MetadataStoreError(f"Failed to create asset: {name} already exists") from exc
            raise MetadataStoreError(f"Failed to create asset {name}: {exc}") from exc
        return AssetResult(asset_id=item["assetId"], name=name, project_id=project_id)

    def create_asset_revision(
        self,
        *,
        asset_id: str,
        name: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Overwrite an asset's description and forms, bumping ``revision``.

        The asset keeps its id, project and name (the name is part of the key;
        callers always pass the existing name).

        Args:
            asset_id: Identifier of the asset to revise.
            name: Asset name.
            description: Optional description (truncated to 2048 chars).
            forms_input: Metadata forms replacing the current ones.

        Returns:
            The revised asset's id, name, and owning project.

        Raises:
            MetadataStoreError: If the asset is missing, the item is too large, or the write fails.
        """
        current = self._item_by_id(asset_id)
        if name != current["name"]:
            logger.warning("Ignoring rename of asset %s from %s to %s", asset_id, current["name"], name)
        item: dict[str, Any] = {
            key: current[key]
            for key in ("PK", "SK", "assetId", "name", "projectId", "typeIdentifier", "createdAt")
            if key in current
        }
        item.update(
            {
                "description": (description or "")[:_MAX_DESCRIPTION],
                "revision": int(current.get("revision", 1)) + 1,
                "updatedAt": _now(),
                **self._encode_forms(current["name"], forms_input or []),
            }
        )
        self._check_size(current["name"], item)
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_exists(PK)")
        except ClientError as exc:
            raise MetadataStoreError(f"Failed to create asset revision for {current['name']}: {exc}") from exc
        return AssetResult(asset_id=asset_id, name=current["name"], project_id=current["projectId"])

    def delete_asset(self, *, asset_id: str) -> None:
        """Delete an asset by id.

        Args:
            asset_id: Identifier of the asset to delete.

        Raises:
            MetadataStoreError: If the asset is missing or the delete fails.
        """
        current = self._item_by_id(asset_id)
        try:
            self._table.delete_item(Key={"PK": current["PK"], "SK": current["SK"]})
        except ClientError as exc:
            raise MetadataStoreError(f"Failed to delete asset: {exc}") from exc

    def get_asset(self, *, asset_id: str) -> AssetResult:
        """Fetch an asset by id.

        Args:
            asset_id: Identifier of the asset to fetch.

        Returns:
            The asset's id, name, and owning project.

        Raises:
            MetadataStoreError: If the asset is missing or the read fails.
        """
        return self._to_result(self._item_by_id(asset_id))

    def search_assets(
        self,
        *,
        project_id: str,
        search_text: str,
        max_results: int = 50,
        next_token: str | None = None,
        include_forms: bool = False,
        search_in_attributes: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> SearchResult:
        """List a project's assets matching ``search_text``, one page at a time.

        Every caller searches by data source (``DS#{sourceId}``), which is a
        key-prefix query. Any other text matches names case-insensitively,
        filtered after the query, so such pages can be shorter than
        ``max_results``. ``search_in_attributes`` and ``filters`` (DataZone
        search options) are accepted and ignored; exact-name lookups use
        :meth:`find_asset_by_name`.

        Args:
            project_id: Owning project to scope the search to.
            search_text: ``DS#…`` prefix, other text, or empty for all assets.
            max_results: Maximum assets read per page.
            next_token: Pagination cursor from a prior call.
            include_forms: When True, populate ``forms_output`` on each result.
            search_in_attributes: Ignored.
            filters: Ignored.

        Returns:
            Matching assets plus the next pagination token, if any.

        Raises:
            MetadataStoreError: If the token is invalid or the query fails.
        """
        prefix_search = search_text.startswith("DS#")
        condition = Key("PK").eq(_pk(project_id)) & Key("SK").begins_with(_sk(search_text if prefix_search else ""))
        kwargs: dict[str, Any] = {"KeyConditionExpression": condition, "Limit": max_results}
        start_key = _decode_token(next_token)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        try:
            resp = self._table.query(**kwargs)
        except ClientError as exc:
            raise MetadataStoreError(f"Failed to search assets: {exc}") from exc

        needle = search_text.lower()
        items = [
            AssetResult(
                asset_id=item["assetId"],
                name=item["name"],
                project_id=item["projectId"],
                forms_output=self._decode_forms(item) if include_forms else None,
            )
            for item in resp.get("Items", [])
            if prefix_search or needle in item["name"].lower()
        ]
        return SearchResult(items=items, next_token=_encode_token(resp.get("LastEvaluatedKey")))

    def find_asset_by_name(self, *, project_id: str, name: str) -> AssetResult | None:
        """Look an asset up by its exact name with one key read.

        Args:
            project_id: Owning project.
            name: Full asset name.

        Returns:
            The asset, or ``None`` when it does not exist.

        Raises:
            MetadataStoreError: If the read fails.
        """
        try:
            item = self._table.get_item(Key={"PK": _pk(project_id), "SK": _sk(name)}).get("Item")
        except ClientError as exc:
            raise MetadataStoreError(f"Failed to look up asset {name}: {exc}") from exc
        return self._to_result(item) if item else None

    def get_asset_forms(self, *, asset_id: str) -> dict[str, Any]:
        """Return the asset in DataZone's ``GetAsset`` shape, including ``formsOutput``.

        Args:
            asset_id: Identifier of the asset to fetch.

        Returns:
            ``id``, ``name``, ``owningProjectId``, ``description`` and ``formsOutput``.

        Raises:
            MetadataStoreError: If the asset is missing or the read fails.
        """
        item = self._item_by_id(asset_id)
        return {
            "id": item["assetId"],
            "name": item["name"],
            "owningProjectId": item["projectId"],
            "description": item.get("description", ""),
            "formsOutput": self._decode_forms(item),
        }

    # ── User Profiles & Memberships ───────────────────────────────

    def create_project_membership(
        self, *, project_id: str, user_identifier: str, designation: str = "PROJECT_CONTRIBUTOR"
    ) -> None:
        """No-op: access to the table is granted by IAM, not project membership.

        Args:
            project_id: Ignored.
            user_identifier: Ignored.
            designation: Ignored.
        """
        logger.debug("Project membership is not used with the DynamoDB store (project=%s)", project_id)

    # ── Helpers ───────────────────────────────────────────────────

    def _item_by_id(self, asset_id: str) -> dict[str, Any]:
        try:
            resp = self._table.query(
                IndexName=ASSET_ID_INDEX,
                KeyConditionExpression=Key("assetId").eq(asset_id),
                Limit=1,
            )
        except ClientError as exc:
            raise MetadataStoreError(f"Failed to get asset {asset_id}: {exc}") from exc
        items = resp.get("Items", [])
        if not items:
            raise MetadataStoreError(f"Asset not found: {asset_id}")
        return items[0]

    @staticmethod
    def _to_result(item: dict[str, Any]) -> AssetResult:
        return AssetResult(asset_id=item["assetId"], name=item["name"], project_id=item["projectId"])

    @staticmethod
    def _encode_forms(name: str, forms: list[dict[str, str]]) -> dict[str, Any]:
        """Item attributes holding ``forms``: plain, or gzip-compressed when large."""
        raw = json.dumps(forms).encode()
        if len(raw) <= COMPRESS_THRESHOLD_BYTES:
            return {"formsOutput": forms}
        compressed = gzip.compress(raw)
        logger.info("Compressed forms of asset %s from %d to %d bytes", name, len(raw), len(compressed))
        return {"formsGzip": compressed, "formsEncoding": "gzip"}

    @staticmethod
    def _decode_forms(item: dict[str, Any]) -> list[dict[str, Any]]:
        if item.get("formsEncoding") == "gzip":
            data = item["formsGzip"]
            raw = data.value if hasattr(data, "value") else data
            return json.loads(gzip.decompress(bytes(raw)))
        return list(item.get("formsOutput", []))

    @staticmethod
    def _check_size(name: str, item: dict[str, Any]) -> None:
        size = _item_size(item)
        if size > MAX_ITEM_BYTES:
            raise MetadataStoreError(
                f"Metadata for asset {name} is {size // 1024} KB even after compression, over DynamoDB's 400 KB "
                "item limit. The table has too many columns to store in one item."
            )
