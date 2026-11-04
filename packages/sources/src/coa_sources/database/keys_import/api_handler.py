# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys-import API Lambda (its own function; routes in keys-import.smithy).

Routes (all under /namespaces/{namespaceId}/sources/{sourceId}/keys-import):

- POST /upload-url  pre-signed S3 PUT URL for the CSV
- POST /preview     check the CSV against the source's tables; writes nothing
- POST              start a run: check again, record the job, queue it (202)
- GET  /{jobId}     one run
- GET               a source's runs, newest first

Source state and namespace lookups go through the existing sources helpers, which
are called, never changed. Environment: KEYS_IMPORT_BUCKET, KEYS_IMPORT_JOBS_TABLE,
KEYS_IMPORT_QUEUE_URL, SMUS_DOMAIN_ID, plus the sources API settings those helpers read.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from coa_common.metadata_store.reader import read_assets_for_datasource
from coa_common.response import api_response, get_caller_identity, iso_to_epoch
from coa_control_plane_server.models.get_keys_import_job_response_content import GetKeysImportJobResponseContent
from coa_control_plane_server.models.get_keys_import_upload_url_request_content import (
    GetKeysImportUploadUrlRequestContent,
)
from coa_control_plane_server.models.list_keys_import_jobs_response_content import ListKeysImportJobsResponseContent
from coa_control_plane_server.models.preview_keys_import_request_content import PreviewKeysImportRequestContent
from coa_control_plane_server.models.preview_keys_import_response_content import PreviewKeysImportResponseContent
from coa_control_plane_server.models.start_keys_import_request_content import StartKeysImportRequestContent
from pydantic import ValidationError as PydanticValidationError

from coa_sources.database.keys_import.csv_reader import CsvFormatError, ParseResult, parse_keys_csv
from coa_sources.database.keys_import.job_store import (
    JOB_ID_RE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    JobStore,
    new_job_id,
)
from coa_sources.database.keys_import.planner import ImportPlan, RowOutcome, plan_import

logger = logging.getLogger(__name__)

BASE = "/namespaces/{namespaceId}/sources/{sourceId}/keys-import"
UPLOAD_URL_EXPIRES_S = 900
REPORT_URL_EXPIRES_S = 900
MAX_FILE_BYTES = 20 * 1024 * 1024
PREVIEW_MAX_ISSUES = 1000
JOB_MAX_ISSUES = 100
LIST_DEFAULT_PAGE = 20
LIST_MAX_PAGE = 100


class _ApiError(Exception):
    """An error response to return as is."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(response.get("body", ""))
        self.response = response


def _error(status: int, message: str) -> _ApiError:
    return _ApiError(api_response(status, {"error": message}))


# ── AWS clients (created on first use; reset_clients() for tests) ────────────


@dataclass
class _Clients:
    s3: Any
    sqs: Any
    jobs: JobStore


_clients: _Clients | None = None


def reset_clients() -> None:
    """Forget cached AWS clients (tests create new mocked ones)."""
    global _clients
    _clients = None


def _aws() -> _Clients:
    global _clients
    if _clients is None:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        table = boto3.resource("dynamodb", region_name=region).Table(os.environ["KEYS_IMPORT_JOBS_TABLE"])
        _clients = _Clients(
            s3=boto3.client("s3", region_name=region),
            sqs=boto3.client("sqs", region_name=region),
            jobs=JobStore(table),
        )
    return _clients


def _bucket() -> str:
    return os.environ["KEYS_IMPORT_BUCKET"]


# ── Existing sources helpers (called, not changed) ──────────────────────────


def _routes() -> Any:
    import coa_sources.api.sources_handler  # noqa: F401  (resolves the routes module's circular import)
    from coa_sources.api import database_routes

    return database_routes


def _require_editable_source(namespace_id: str, source_id: str) -> None:
    """404 / 400 / 409 unless the source exists, is a DATABASE source and allows key edits."""
    _, err = _routes()._reviewable_source_or_error(namespace_id, source_id)
    if err is not None:
        raise _ApiError(err)


def _load_source_tables(namespace_id: str, source_id: str) -> list:
    project_id, err = _routes()._project_or_404(namespace_id)
    if err is not None:
        raise _ApiError(err)
    return read_assets_for_datasource(os.environ.get("SMUS_DOMAIN_ID", ""), project_id, source_id)


# ── Request helpers ─────────────────────────────────────────────────────────


def _body(event: dict[str, Any], model: Any) -> Any:
    try:
        raw = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        raise _error(400, "Invalid JSON body") from None
    if not isinstance(raw, dict):
        raise _error(400, "Request body is required")
    try:
        return model.model_validate(raw)
    except PydanticValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise _error(400, f"Invalid request: {details}") from None


def _upload_prefix(namespace_id: str, source_id: str) -> str:
    return f"keys-import/{namespace_id}/{source_id}/"


def _check_key(namespace_id: str, source_id: str, key: str) -> None:
    """Accept only keys this API issues for this namespace and source: <prefix><8 hex>/<name>.csv."""
    prefix = _upload_prefix(namespace_id, source_id)
    parts = key[len(prefix) :].split("/") if key.startswith(prefix) else []
    if (
        len(parts) != 2
        or len(parts[0]) != 8
        or any(c not in "0123456789abcdef" for c in parts[0])
        or not parts[1].lower().endswith(".csv")
        or parts[1] in ("", ".", "..")
    ):
        raise _error(400, "s3Key is not an upload of this source; get an upload URL and upload the file again.")


def _read_csv(key: str) -> str:
    try:
        obj = _aws().s3.get_object(Bucket=_bucket(), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            raise _error(400, "The uploaded file was not found; upload it again.") from None
        raise
    if int(obj.get("ContentLength") or 0) > MAX_FILE_BYTES:
        raise _error(400, f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        return obj["Body"].read().decode("utf-8")
    except UnicodeDecodeError:
        raise _error(400, "The file is not UTF-8 text; save it as CSV UTF-8.") from None


@dataclass
class _Checked:
    parse: ParseResult
    plan: ImportPlan

    def counts(self) -> dict[str, int]:
        return {
            "rowsRead": len(self.parse.rows) + len(self.parse.errors),
            "toAdd": self.plan.to_add,
            "alreadyThere": self.plan.already_there,
            "duplicates": self.plan.duplicates,
            "skipped": self.plan.skipped,
            "errors": self.plan.errors + len(self.parse.errors),
            "tablesToUpdate": self.plan.tables_to_update,
        }

    def issues(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = [
            {"line": e.line, "outcome": RowOutcome.ERROR.value, "message": e.message} for e in self.parse.errors
        ]
        for r in self.plan.results:
            if r.outcome == RowOutcome.ADD and not r.warning:
                continue
            issue: dict[str, Any] = {"line": r.line, "outcome": r.outcome.value}
            if r.message:
                issue["message"] = r.message
            if r.warning:
                issue["warning"] = r.warning
            issues.append(issue)
        return sorted(issues, key=lambda i: i["line"])


def _check_file(namespace_id: str, source_id: str, key: str) -> _Checked:
    _check_key(namespace_id, source_id, key)
    _require_editable_source(namespace_id, source_id)
    text = _read_csv(key)
    try:
        parse = parse_keys_csv(text)
    except CsvFormatError as exc:
        raise _error(400, str(exc)) from None
    return _Checked(parse=parse, plan=plan_import(parse.rows, _load_source_tables(namespace_id, source_id)))


# ── Routes ──────────────────────────────────────────────────────────────────


def _upload_url(event: dict[str, Any], namespace_id: str, source_id: str) -> dict[str, Any]:
    body = _body(event, GetKeysImportUploadUrlRequestContent)
    _require_editable_source(namespace_id, source_id)
    key = f"{_upload_prefix(namespace_id, source_id)}{secrets.token_hex(4)}/{body.file_name}"
    url = _aws().s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": _bucket(), "Key": key, "ContentType": "text/csv"},
        ExpiresIn=UPLOAD_URL_EXPIRES_S,
    )
    return api_response(200, {"uploadUrl": url, "s3Key": key, "expiresIn": UPLOAD_URL_EXPIRES_S})


def _preview(event: dict[str, Any], namespace_id: str, source_id: str) -> dict[str, Any]:
    body = _body(event, PreviewKeysImportRequestContent)
    checked = _check_file(namespace_id, source_id, body.s3_key)
    issues = checked.issues()
    out = {
        "counts": checked.counts(),
        "rowIssues": issues[:PREVIEW_MAX_ISSUES],
        "rowIssuesTruncated": len(issues) > PREVIEW_MAX_ISSUES,
    }
    PreviewKeysImportResponseContent.model_validate(out)
    return api_response(200, out)


def _send_job_message(namespace_id: str, source_id: str, job_id: str) -> None:
    _aws().sqs.send_message(
        QueueUrl=os.environ["KEYS_IMPORT_QUEUE_URL"],
        MessageBody=json.dumps({"namespaceId": namespace_id, "sourceId": source_id, "jobId": job_id}),
    )


def _start(event: dict[str, Any], namespace_id: str, source_id: str) -> dict[str, Any]:
    body = _body(event, StartKeysImportRequestContent)
    checked = _check_file(namespace_id, source_id, body.s3_key)
    if checked.plan.to_add == 0:
        raise _error(400, "The file adds no keys: every row is already there, skipped, a duplicate or an error.")

    jobs = _aws().jobs
    now = datetime.now(UTC)
    running = jobs.find_running(source_id, now)
    if running is not None:
        raise _error(409, f"Another keys import is running for this source ({running['SK']}); wait for it to finish.")

    job_id = new_job_id(now)
    issues = checked.issues()
    jobs.put_new(
        {
            "PK": f"SRC#{source_id}",
            "SK": job_id,
            "namespaceId": namespace_id,
            "sourceId": source_id,
            "status": STATUS_IN_PROGRESS,
            "fileName": posixpath.basename(body.s3_key),
            "s3Key": body.s3_key,
            "startedAt": now.isoformat(),
            "startedBy": get_caller_identity(event),
            "counts": checked.counts(),
            "rowIssues": issues[:JOB_MAX_ISSUES],
            "issuesTruncated": len(issues) > JOB_MAX_ISSUES,
        }
    )
    try:
        _send_job_message(namespace_id, source_id, job_id)
    except Exception as exc:
        logger.exception("keys_import_queue_failed source=%s job=%s", source_id, job_id)
        jobs.update(
            source_id,
            job_id,
            {
                "status": STATUS_FAILED,
                "completedAt": datetime.now(UTC).isoformat(),
                "errorMessage": f"Could not queue the run: {exc}",
            },
        )
        return api_response(500, {"error": "Could not start the keys import; try again."})
    return api_response(202, {"jobId": job_id, "status": STATUS_IN_PROGRESS})


_JOB_FIELDS = (
    "status",
    "fileName",
    "startedBy",
    "counts",
    "tablesUpdated",
    "tablesUnchanged",
    "tablesFailed",
    "keysAdded",
    "tableFailures",
    "rowIssues",
    "issuesTruncated",
    "errorMessage",
)


def _job_view(item: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {"jobId": item["SK"], "startedAt": iso_to_epoch(item["startedAt"])}
    view.update({k: item[k] for k in _JOB_FIELDS if item.get(k) is not None})
    if item.get("completedAt"):
        view["completedAt"] = iso_to_epoch(item["completedAt"])
    if item.get("reportKey"):
        view["errorReportUrl"] = _aws().s3.generate_presigned_url(
            "get_object", Params={"Bucket": _bucket(), "Key": item["reportKey"]}, ExpiresIn=REPORT_URL_EXPIRES_S
        )
    return view


def _job_id_from(event: dict[str, Any]) -> str:
    job_id = (event.get("pathParameters") or {}).get("jobId") or ""
    if not JOB_ID_RE.match(job_id):
        raise _error(400, "Invalid jobId")
    return job_id


def _get_job(event: dict[str, Any], namespace_id: str, source_id: str) -> dict[str, Any]:
    job_id = _job_id_from(event)
    item = _aws().jobs.get(source_id, job_id)
    if item is None or item.get("namespaceId") != namespace_id:
        raise _error(404, f"Keys import {job_id} not found")
    out = {"job": _job_view(item)}
    GetKeysImportJobResponseContent.model_validate(out)
    return api_response(200, out)


def _list_jobs(event: dict[str, Any], namespace_id: str, source_id: str) -> dict[str, Any]:
    query = event.get("queryStringParameters") or {}
    try:
        limit = max(1, min(LIST_MAX_PAGE, int(query.get("maxResults") or LIST_DEFAULT_PAGE)))
    except ValueError:
        raise _error(400, "maxResults must be a number") from None
    start_key = None
    if query.get("nextToken"):
        try:
            start_key = json.loads(base64.b64decode(query["nextToken"]).decode())
        except Exception:
            raise _error(400, "Invalid nextToken") from None
        if not isinstance(start_key, dict) or start_key.get("PK") != f"SRC#{source_id}":
            raise _error(400, "Invalid nextToken")
    items, last_key = _aws().jobs.list(source_id, namespace_id, limit=limit, start_key=start_key)
    out: dict[str, Any] = {"items": [_job_view(i) for i in items]}
    if last_key:
        out["nextToken"] = base64.b64encode(json.dumps(last_key).encode()).decode()
    ListKeysImportJobsResponseContent.model_validate(out)
    return api_response(200, out)


_ROUTES = {
    (BASE + "/upload-url", "POST"): _upload_url,
    (BASE + "/preview", "POST"): _preview,
    (BASE, "POST"): _start,
    (BASE, "GET"): _list_jobs,
    (BASE + "/{jobId}", "GET"): _get_job,
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point: dispatch an API Gateway proxy request to its route.

    Args:
        event: API Gateway proxy integration event.
        context: Lambda runtime context (unused).

    Returns:
        The proxy response. Unexpected errors become a 500 without internal details.
    """
    if os.environ.get("KEYS_IMPORT_ENABLED", "true").lower() == "false":
        # The KeysImportStack keeps the routes wired when the feature is switched off.
        return api_response(404, {"error": "Keys import is not enabled in this deployment."})
    route = _ROUTES.get((event.get("resource", ""), event.get("httpMethod", "")))
    if route is None:
        return api_response(404, {"error": "Unknown route"})
    params = event.get("pathParameters") or {}
    try:
        return route(event, params.get("namespaceId", ""), params.get("sourceId", ""))
    except _ApiError as exc:
        return exc.response
    except Exception:
        logger.exception("keys_import_api_failed resource=%s", event.get("resource"))
        return api_response(500, {"error": "Internal server error"})
