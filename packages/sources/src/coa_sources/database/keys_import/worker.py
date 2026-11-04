# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Apply a keys-import plan table by table through the existing per-table key code.

Decision 6 (work/add-bulk-import.txt, section 2.1): keys are saved by calling the
existing ``PATCH .../tables/{tableId}/keys`` handler (``_handle_update_table_keys``)
as it is, so its checks and saving rules apply unchanged. Decision 9: bulk approve
is not blocked; a table refused with 409 while the source is being approved is
retried after a wait. One table's failure never stops the others.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from coa_common.domain_models import Table

from coa_sources.database.keys_import.planner import TableChange, build_patch_body, pending_keys

logger = logging.getLogger(__name__)

LoadTable = Callable[[str], Table]
SaveKeys = Callable[[str, dict], tuple[int, dict]]

_HTTP_CONFLICT = 409
NOT_PROCESSED_MESSAGE = (
    "not processed: the run ran out of time; start the import again to add the rest (keys already added are skipped)."
)


class KeysImportError(RuntimeError):
    """A table could not be read through the existing sources code."""


@dataclass(frozen=True)
class TableFailure:
    """A table whose keys could not be saved."""

    table_id: str
    message: str


@dataclass
class ApplyResult:
    """Totals of one run."""

    tables_updated: int = 0
    tables_unchanged: int = 0
    tables_failed: int = 0
    keys_added: int = 0
    failures: list[TableFailure] = field(default_factory=list)


def apply_changes(
    changes: dict[str, TableChange],
    *,
    load_table: LoadTable,
    save_keys: SaveKeys,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = 5,
    retry_wait_s: float = 10.0,
    should_stop: Callable[[], bool] | None = None,
) -> ApplyResult:
    """Save the planned keys of every table.

    Each table is re-read first, so keys added since the preview are not sent twice.

    Args:
        changes: Planned changes by table id (``ImportPlan.changes``).
        load_table: Returns a table with its current keys.
        save_keys: Calls the existing keys handler; returns (HTTP status, response body).
        sleep: Wait function (injected for tests).
        max_retries: Retries for a table refused with 409 (source being approved).
        retry_wait_s: Seconds to wait before each retry.
        should_stop: Checked before each table; when it returns True (e.g. the Lambda
            is about to time out), the remaining tables are reported as not processed.

    Returns:
        Totals and the tables that failed.
    """
    result = ApplyResult()
    for table_id, change in changes.items():
        if should_stop is not None and should_stop():
            _fail(result, table_id, NOT_PROCESSED_MESSAGE)
            continue
        try:
            table = load_table(table_id)
        except Exception as exc:  # noqa: BLE001 - one table's failure must not stop the others
            _fail(result, table_id, f"could not read the table: {exc}")
            continue

        body = build_patch_body(table, change)
        if body is None:
            result.tables_unchanged += 1
            continue
        new_fks, primary_key = pending_keys(table, change)

        status, response = _save_with_retry(table_id, body, save_keys, sleep, max_retries, retry_wait_s)
        if status is None:
            _fail(result, table_id, f"could not save the keys: {response.get('error', '')}")
        elif 200 <= status < 300:
            result.tables_updated += 1
            result.keys_added += len(new_fks) + (1 if primary_key else 0)
        else:
            _fail(result, table_id, f"HTTP {status}: {response.get('error', '')}".rstrip(": "))
    return result


def _save_with_retry(
    table_id: str,
    body: dict,
    save_keys: SaveKeys,
    sleep: Callable[[float], None],
    max_retries: int,
    retry_wait_s: float,
) -> tuple[int | None, dict]:
    attempt = 0
    while True:
        try:
            status, response = save_keys(table_id, body)
        except Exception as exc:  # noqa: BLE001 - reported as this table's failure
            return None, {"error": str(exc)}
        if status != _HTTP_CONFLICT or attempt >= max_retries:
            return status, response
        attempt += 1
        logger.info("keys_import_retry_after_conflict table=%s attempt=%d", table_id, attempt)
        sleep(retry_wait_s)


def _fail(result: ApplyResult, table_id: str, message: str) -> None:
    logger.warning("keys_import_table_failed table=%s message=%s", table_id, message)
    result.tables_failed += 1
    result.failures.append(TableFailure(table_id=table_id, message=message))


def _routes() -> Any:
    """The existing sources API routes module (imported late; it needs sources_handler first)."""
    import coa_sources.api.sources_handler  # noqa: F401  (resolves the routes module's circular import)
    from coa_sources.api import database_routes

    return database_routes


def make_save_keys(namespace_id: str, source_id: str) -> SaveKeys:
    """Return a ``save_keys`` that calls the existing keys handler for this source."""

    def save_keys(table_id: str, body: dict) -> tuple[int, dict]:
        response = _routes()._handle_update_table_keys({"body": json.dumps(body)}, namespace_id, source_id, table_id)
        return int(response["statusCode"]), json.loads(response.get("body") or "{}")

    return save_keys


def make_load_table(namespace_id: str, source_id: str) -> LoadTable:
    """Return a ``load_table`` that reads a table through the existing sources helpers."""

    def load_table(table_id: str) -> Table:
        routes = _routes()
        client, err = routes._smus_or_500()
        if err is None:
            project_id, err = routes._project_or_404(namespace_id)
        if err is None:
            asset, err = routes._load_single_asset(client, project_id, source_id, table_id)
        if err is not None:
            raise KeysImportError(json.loads(err.get("body") or "{}").get("error", f"HTTP {err.get('statusCode')}"))
        return asset["table"]

    return load_table
