# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys-import worker Lambda: one SQS message per run (sent by StartKeysImport).

For each message: load the run, read the CSV from S3, plan against the source's
tables as they are now, add the keys through the existing per-table key code
(``worker.apply_changes``), write an error report to S3 when there is anything to
report, and record the results on the run.

Failures before any key is written (file or tables cannot be read) are retried by
SQS (partial batch response); after MAX_ATTEMPTS deliveries the run is marked
FAILED. A run that is not IN_PROGRESS (already finished) is never run again, so
a redelivered message is harmless.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from coa_sources.database.keys_import import api_handler
from coa_sources.database.keys_import.csv_reader import CsvFormatError, parse_keys_csv
from coa_sources.database.keys_import.job_store import STATUS_COMPLETED, STATUS_FAILED, STATUS_IN_PROGRESS
from coa_sources.database.keys_import.planner import plan_import
from coa_sources.database.keys_import.worker import ApplyResult, apply_changes, make_load_table, make_save_keys

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_WAIT_S = 10.0
STOP_MARGIN_MS = 60_000
MAX_STORED_FAILURES = 100
REPORT_COLUMNS = ("line", "table", "outcome", "message", "warning")
# Source states in which a run goes ahead: those where key edits are allowed, plus
# APPROVING (decision 9: approval is not blocked; refused tables are retried).
_EXTRA_RUNNABLE_STATES = frozenset({"APPROVING"})


class _RetryableError(Exception):
    """The file or the tables could not be read; SQS may retry the message."""


class _RunFailed(Exception):
    """The run cannot go ahead; the job is marked FAILED with this message."""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for the keys-import SQS queue.

    Args:
        event: SQS event; each record body is {"namespaceId", "sourceId", "jobId"}.
        context: Lambda context (its remaining time stops a run before the timeout).

    Returns:
        A partial batch response listing the messages SQS should deliver again.
    """
    retry: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message = _parse(record)
        if message is None:
            continue
        namespace_id, source_id, job_id = message
        try:
            _run(namespace_id, source_id, job_id, context)
        except _RetryableError as exc:
            attempts = int((record.get("attributes") or {}).get("ApproximateReceiveCount") or 1)
            if attempts < MAX_ATTEMPTS:
                logger.warning("keys_import_retry job=%s attempt=%d reason=%s", job_id, attempts, exc)
                retry.append({"itemIdentifier": record["messageId"]})
            else:
                _mark_failed(source_id, job_id, str(exc))
        except _RunFailed as exc:
            _mark_failed(source_id, job_id, str(exc))
    return {"batchItemFailures": retry}


def _parse(record: dict[str, Any]) -> tuple[str, str, str] | None:
    try:
        body = json.loads(record.get("body") or "")
    except json.JSONDecodeError:
        body = None
    fields = ("namespaceId", "sourceId", "jobId")
    if not isinstance(body, dict) or not all(isinstance(body.get(f), str) and body.get(f) for f in fields):
        logger.error("keys_import_bad_message message_id=%s", record.get("messageId"))
        return None
    return body["namespaceId"], body["sourceId"], body["jobId"]


def _run(namespace_id: str, source_id: str, job_id: str, context: Any) -> None:
    jobs = api_handler._aws().jobs
    job = jobs.get(source_id, job_id)
    if job is None or job.get("namespaceId") != namespace_id:
        logger.error("keys_import_unknown_job source=%s job=%s", source_id, job_id)
        return
    if job.get("status") != STATUS_IN_PROGRESS:
        logger.info("keys_import_job_already_finished job=%s status=%s", job_id, job.get("status"))
        return

    _check_source(namespace_id, source_id)
    try:
        text = api_handler._read_csv(job["s3Key"])
        parse = parse_keys_csv(text)
        tables = api_handler._load_source_tables(namespace_id, source_id)
    except CsvFormatError as exc:
        raise _RunFailed(str(exc)) from exc
    except api_handler._ApiError as exc:
        raise _RetryableError(_error_text(exc.response)) from exc
    except Exception as exc:
        raise _RetryableError(f"could not read the file or the source's tables: {exc}") from exc

    plan = plan_import(parse.rows, tables)
    result = apply_changes(
        plan.changes,
        load_table=make_load_table(namespace_id, source_id),
        save_keys=make_save_keys(namespace_id, source_id),
        sleep=time.sleep,
        retry_wait_s=RETRY_WAIT_S,
        should_stop=_stop_check(context),
    )

    report_rows = _report_rows(api_handler._Checked(parse=parse, plan=plan), parse, result)
    fields: dict[str, Any] = {
        "status": STATUS_COMPLETED,
        "completedAt": datetime.now(UTC).isoformat(),
        "tablesUpdated": result.tables_updated,
        "tablesUnchanged": result.tables_unchanged,
        "tablesFailed": result.tables_failed,
        "keysAdded": result.keys_added,
        "tableFailures": [{"tableId": f.table_id, "message": f.message} for f in result.failures][:MAX_STORED_FAILURES],
        "issuesTruncated": bool(job.get("issuesTruncated")) or len(result.failures) > MAX_STORED_FAILURES,
    }
    if report_rows:
        fields["reportKey"] = _write_report(namespace_id, source_id, job_id, report_rows)
    jobs.update(source_id, job_id, fields)
    logger.info(
        "keys_import_completed job=%s updated=%d failed=%d keys=%d",
        job_id,
        result.tables_updated,
        result.tables_failed,
        result.keys_added,
    )


def _check_source(namespace_id: str, source_id: str) -> None:
    routes = api_handler._routes()
    try:
        item = routes._get_dao().get({"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}, projection=["status"])
    except Exception as exc:
        raise _RetryableError(f"could not read the source: {exc}") from exc
    if not item:
        raise _RunFailed("The source no longer exists.")
    status = item.get("status")
    if status not in routes._REVIEWABLE_STATES and status not in _EXTRA_RUNNABLE_STATES:
        raise _RunFailed(f"The source is {status}; keys cannot be added to it now.")


def _stop_check(context: Any) -> Any:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if remaining is None:
        return None
    return lambda: remaining() < STOP_MARGIN_MS


def _error_text(response: dict[str, Any]) -> str:
    try:
        return str(json.loads(response.get("body") or "{}").get("error") or response.get("statusCode"))
    except json.JSONDecodeError:
        return str(response.get("statusCode"))


def _report_rows(checked: Any, parse: Any, result: ApplyResult) -> list[dict[str, str]]:
    table_by_line = {row.line: row.source_table for row in parse.rows}
    rows = [
        {
            "line": str(issue["line"]),
            "table": table_by_line.get(issue["line"], ""),
            "outcome": issue["outcome"],
            "message": issue.get("message", ""),
            "warning": issue.get("warning", ""),
        }
        for issue in checked.issues()
    ]
    rows += [
        {"line": "", "table": f.table_id, "outcome": "TABLE_FAILED", "message": f.message, "warning": ""}
        for f in result.failures
    ]
    return rows


def _write_report(namespace_id: str, source_id: str, job_id: str, rows: list[dict[str, str]]) -> str:
    key = f"keys-import/{namespace_id}/{source_id}/reports/{job_id}.csv"
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    api_handler._aws().s3.put_object(
        Bucket=api_handler._bucket(), Key=key, Body=out.getvalue().encode("utf-8"), ContentType="text/csv"
    )
    return key


def _mark_failed(source_id: str, job_id: str, message: str) -> None:
    logger.error("keys_import_failed job=%s reason=%s", job_id, message)
    api_handler._aws().jobs.update(
        source_id,
        job_id,
        {"status": STATUS_FAILED, "completedAt": datetime.now(UTC).isoformat(), "errorMessage": message},
    )
