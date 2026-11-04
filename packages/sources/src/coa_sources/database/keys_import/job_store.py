"""Keys-import job records, in the feature's own DynamoDB table (decision 8).

Item key: PK = ``SRC#{sourceId}``, SK = job id (``YYYYMMDDTHHMMSSZ-xxxxxxxx``), so a
source's runs sort by start time. Only this feature reads or writes the table.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

JOB_ID_FORMAT = "%Y%m%dT%H%M%SZ"
# Same pattern as the KeysImportJobId shape in keys-import.smithy.
JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

# A run still IN_PROGRESS after this long is treated as dead (the worker Lambda
# times out after 15 minutes and SQS retries it a few times), so it no longer
# blocks a new import of the same source.
STALE_AFTER = timedelta(minutes=90)


def new_job_id(now: datetime) -> str:
    """Return a job id: the UTC start time plus 8 random hex characters."""
    return f"{now.astimezone(UTC).strftime(JOB_ID_FORMAT)}-{secrets.token_hex(4)}"


def source_pk(source_id: str) -> str:
    """Partition key of a source's runs."""
    return f"SRC#{source_id}"


def plain(value: Any) -> Any:
    """Convert DynamoDB's Decimal numbers back to int/float, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


class JobStore:
    """Read and write keys-import runs."""

    def __init__(self, table: Any) -> None:
        """Wrap a boto3 DynamoDB Table resource."""
        self._table = table

    def put_new(self, item: dict[str, Any]) -> None:
        """Write a new run; fails if the id is already taken."""
        self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(SK)")

    def get(self, source_id: str, job_id: str) -> dict[str, Any] | None:
        """Return one run, or None."""
        item = self._table.get_item(Key={"PK": source_pk(source_id), "SK": job_id}).get("Item")
        return plain(item) if item else None

    def list(
        self,
        source_id: str,
        namespace_id: str,
        *,
        limit: int,
        start_key: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Return a page of a source's runs in a namespace, newest first, and the next page's key."""
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk",
            "FilterExpression": "namespaceId = :ns",
            "ExpressionAttributeValues": {":pk": source_pk(source_id), ":ns": namespace_id},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = self._table.query(**kwargs)
        return [plain(i) for i in response.get("Items", [])], response.get("LastEvaluatedKey")

    def find_running(self, source_id: str, now: datetime) -> dict[str, Any] | None:
        """Return a run of this source that is still IN_PROGRESS and not stale, or None."""
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk",
            "FilterExpression": "#s = :running",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":pk": source_pk(source_id), ":running": STATUS_IN_PROGRESS},
        }
        while True:
            response = self._table.query(**kwargs)
            for item in response.get("Items", []):
                started = datetime.fromisoformat(str(item["startedAt"]))
                if now - started < STALE_AFTER:
                    return plain(item)
            if "LastEvaluatedKey" not in response:
                return None
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def update(self, source_id: str, job_id: str, fields: dict[str, Any]) -> None:
        """Set these fields on a run."""
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        expression = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        self._table.update_item(
            Key={"PK": source_pk(source_id), "SK": job_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
