"""Keys import: the worker Lambda entry point (SQS message -> run -> job results).

S3, DynamoDB and SQS are simulated with moto; reading and saving tables use
fakes (the real save code is covered by test_contract_existing_save.py).
Expected results: work/add-bulk-import.txt, section 3.6 (H1-H10).
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.api.sources_handler  # noqa: E402, F401, I001
from coa_common.domain_models import Column, ForeignKey, Table  # noqa: E402

from coa_sources.database.keys_import import api_handler, worker_handler  # noqa: E402
from tests.unit.conftest import dao_double  # noqa: E402

pytestmark = pytest.mark.unit

_DR = "coa_sources.api.database_routes"
_API = "coa_sources.database.keys_import.api_handler"
_WH = "coa_sources.database.keys_import.worker_handler"
NS = "550e8400-e29b-41d4-a716-446655440000"
SRC = "src-db-001"
BUCKET = "test-keys-import"
JOBS_TABLE = "test-keys-import-jobs"
JOB_ID = "20261104T153012Z-1a2b3c4d"
KEY = f"keys-import/{NS}/{SRC}/abcd1234/erp-keys.csv"
HEADER = "type,source_table,column,target_table,target_column\n"
CSV_TEXT = (
    HEADER
    + "FK,purchase_order,supplier_id,supplier,supplier_id\n"  # line 2: already there
    + "FK,purchase_order,parent_po,purchase_order,po_number\n"  # line 3: add
    + "PK,supplier,supplier_id,,\n"  # line 4: add
    + "FK,vendor,id,supplier,supplier_id\n"  # line 5: unknown table
    + "XX,supplier,name,,\n"  # line 6: bad type
)
CLEAN_CSV = HEADER + "FK,purchase_order,parent_po,purchase_order,po_number\n"


def _tables() -> dict[str, Table]:
    def table(name: str, columns: list[str], fks: list[ForeignKey] | None = None) -> Table:
        return Table(
            name=name,
            database="sales",
            data_source_id=SRC,
            columns=[Column(name=c, data_type="string") for c in columns],
            foreign_keys=list(fks or []),
        )

    existing = ForeignKey(column="supplier_id", target_table="supplier", target_column="supplier_id")
    return {
        "sales.purchase_order": table("purchase_order", ["po_number", "supplier_id", "parent_po"], [existing]),
        "sales.supplier": table("supplier", ["supplier_id", "name"]),
    }


class _Context:
    def __init__(self, remaining_ms: int = 600_000) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


class _TableFakes:
    def __init__(self, tables: dict[str, Table], statuses: dict[str, int] | None = None) -> None:
        self.tables = tables
        self.statuses = statuses or {}
        self.saved: list[tuple[str, dict]] = []

    def make_load_table(self, namespace_id: str, source_id: str):
        assert (namespace_id, source_id) == (NS, SRC)
        return lambda table_id: self.tables[table_id]

    def make_save_keys(self, namespace_id: str, source_id: str):
        assert (namespace_id, source_id) == (NS, SRC)

        def save(table_id: str, body: dict) -> tuple[int, dict]:
            self.saved.append((table_id, body))
            status = self.statuses.get(table_id, 200)
            return status, ({} if status == 200 else {"error": "refused by the save code"})

        return save


@pytest.fixture
def aws(monkeypatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=JOBS_TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("KEYS_IMPORT_BUCKET", BUCKET)
        monkeypatch.setenv("KEYS_IMPORT_JOBS_TABLE", JOBS_TABLE)
        monkeypatch.setenv("KEYS_IMPORT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/keys-import")
        api_handler.reset_clients()
        ddb = boto3.resource("dynamodb", region_name="us-east-1").Table(JOBS_TABLE)
        ddb.put_item(
            Item={
                "PK": f"SRC#{SRC}",
                "SK": JOB_ID,
                "namespaceId": NS,
                "sourceId": SRC,
                "status": "IN_PROGRESS",
                "fileName": "erp-keys.csv",
                "s3Key": KEY,
                "startedAt": "2026-11-04T15:30:12+00:00",
                "counts": {"rowsRead": 5, "toAdd": 2, "alreadyThere": 1, "duplicates": 0, "skipped": 0, "errors": 2},
                "rowIssues": [],
                "issuesTruncated": False,
            }
        )
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=CSV_TEXT.encode())

        sources_dao = dao_double()
        sources_dao.get.return_value = {
            "PK": f"NS#{NS}",
            "SK": f"SRC#{SRC}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
            "discoveredSchemas": ["sales"],
        }
        ns_dao = dao_double()
        ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        fakes = _TableFakes(_tables())
        with (
            patch(f"{_DR}._get_dao", return_value=sources_dao),
            patch(f"{_DR}._get_ns_dao", return_value=ns_dao),
            patch(f"{_API}.read_assets_for_datasource", side_effect=lambda *_: list(fakes.tables.values())),
            patch(f"{_WH}.make_load_table", side_effect=fakes.make_load_table),
            patch(f"{_WH}.make_save_keys", side_effect=fakes.make_save_keys),
            patch(f"{_WH}.RETRY_WAIT_S", 0),
        ):
            yield {"s3": s3, "ddb": ddb, "sources_dao": sources_dao, "fakes": fakes}
        api_handler.reset_clients()


def _record(body: Any = None, *, receive_count: int = 1, message_id: str = "m-1") -> dict:
    body = {"namespaceId": NS, "sourceId": SRC, "jobId": JOB_ID} if body is None else body
    return {
        "messageId": message_id,
        "body": body if isinstance(body, str) else json.dumps(body),
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
    }


def _run(*records: dict, context: _Context | None = None) -> dict:
    return worker_handler.handler({"Records": list(records) or [_record()]}, context or _Context())


def _job(aws: dict) -> dict:
    return aws["ddb"].get_item(Key={"PK": f"SRC#{SRC}", "SK": JOB_ID})["Item"]


def _report_rows(aws: dict, key: str) -> list[dict[str, str]]:
    text = aws["s3"].get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    return list(csv.DictReader(io.StringIO(text)))


class TestRun:
    def test_h1_run_adds_keys_and_completes_the_job(self, aws) -> None:
        assert _run() == {"batchItemFailures": []}
        assert [t for t, _ in aws["fakes"].saved] == ["sales.purchase_order", "sales.supplier"]
        job = _job(aws)
        assert job["status"] == "COMPLETED"
        assert job["completedAt"]
        assert job["tablesUpdated"] == 2
        assert job["tablesUnchanged"] == 0
        assert job["tablesFailed"] == 0
        assert job["keysAdded"] == 2
        assert job["tableFailures"] == []

    def test_h2_report_lists_every_row_issue_with_its_table(self, aws) -> None:
        _run()
        job = _job(aws)
        assert job["reportKey"] == f"keys-import/{NS}/{SRC}/reports/{JOB_ID}.csv"
        rows = _report_rows(aws, job["reportKey"])
        assert [(r["line"], r["table"], r["outcome"]) for r in rows] == [
            ("2", "purchase_order", "ALREADY_THERE"),
            ("5", "vendor", "ERROR"),
            ("6", "", "ERROR"),
        ]

    def test_h3_no_report_when_there_is_nothing_to_report(self, aws) -> None:
        aws["s3"].put_object(Bucket=BUCKET, Key=KEY, Body=CLEAN_CSV.encode())
        _run()
        job = _job(aws)
        assert job["status"] == "COMPLETED"
        assert "reportKey" not in job

    def test_h4_table_that_cannot_be_saved_is_recorded_and_reported(self, aws) -> None:
        aws["fakes"].statuses["sales.supplier"] = 400
        _run()
        job = _job(aws)
        assert job["status"] == "COMPLETED"
        assert job["tablesUpdated"] == 1
        assert job["tablesFailed"] == 1
        assert job["tableFailures"][0]["tableId"] == "sales.supplier"
        assert "400" in job["tableFailures"][0]["message"]
        rows = _report_rows(aws, job["reportKey"])
        assert ("", "sales.supplier", "TABLE_FAILED") in [(r["line"], r["table"], r["outcome"]) for r in rows]

    def test_h9_tables_left_when_time_runs_out_are_reported(self, aws) -> None:
        _run(context=_Context(remaining_ms=1_000))
        job = _job(aws)
        assert job["status"] == "COMPLETED"
        assert job["tablesFailed"] == 2
        assert aws["fakes"].saved == []
        assert "start the import again" in job["tableFailures"][0]["message"]


class TestRedeliveryAndBadMessages:
    def test_h5_finished_job_is_not_run_again(self, aws) -> None:
        aws["ddb"].update_item(
            Key={"PK": f"SRC#{SRC}", "SK": JOB_ID},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "COMPLETED"},
        )
        assert _run() == {"batchItemFailures": []}
        assert aws["fakes"].saved == []

    @pytest.mark.parametrize(
        "body",
        [
            {"namespaceId": NS, "sourceId": SRC, "jobId": "20261104T000000Z-00000000"},
            {"namespaceId": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "sourceId": SRC, "jobId": JOB_ID},
        ],
    )
    def test_h6_unknown_job_or_other_namespace_is_ignored(self, aws, body: dict) -> None:
        assert _run(_record(body)) == {"batchItemFailures": []}
        assert aws["fakes"].saved == []
        assert _job(aws)["status"] == "IN_PROGRESS"

    @pytest.mark.parametrize("body", ["not-json", {"jobId": JOB_ID}, ["x"]])
    def test_h10_malformed_message_is_dropped(self, aws, body: Any) -> None:
        assert _run(_record(body)) == {"batchItemFailures": []}
        assert aws["fakes"].saved == []


class TestFailures:
    def test_h7_missing_file_is_retried_then_fails_the_job(self, aws) -> None:
        aws["s3"].delete_object(Bucket=BUCKET, Key=KEY)

        assert _run(_record(receive_count=1)) == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
        assert _job(aws)["status"] == "IN_PROGRESS"

        assert _run(_record(receive_count=3)) == {"batchItemFailures": []}
        job = _job(aws)
        assert job["status"] == "FAILED"
        assert "upload" in job["errorMessage"].lower()
        assert job["completedAt"]

    def test_h8_source_that_no_longer_allows_key_edits_fails_the_job(self, aws) -> None:
        aws["sources_dao"].get.return_value = {**aws["sources_dao"].get.return_value, "status": "REJECTED"}
        assert _run() == {"batchItemFailures": []}
        job = _job(aws)
        assert job["status"] == "FAILED"
        assert "REJECTED" in job["errorMessage"]
        assert aws["fakes"].saved == []

    def test_h8_source_being_approved_still_runs(self, aws) -> None:
        # Decision 9: approval is not blocked; the worker retries tables refused with 409.
        aws["sources_dao"].get.return_value = {**aws["sources_dao"].get.return_value, "status": "APPROVING"}
        _run()
        assert _job(aws)["status"] == "COMPLETED"
        assert len(aws["fakes"].saved) == 2


class TestMoreFailures:
    def test_h11_file_no_longer_valid_csv_fails_without_retry(self, aws) -> None:
        aws["s3"].put_object(Bucket=BUCKET, Key=KEY, Body=b"type,source_table\nPK,t\n")
        assert _run() == {"batchItemFailures": []}
        job = _job(aws)
        assert job["status"] == "FAILED"
        assert "target_column" in job["errorMessage"]

    def test_h12_tables_cannot_be_read_is_retried(self, aws) -> None:
        with patch(f"{_API}.read_assets_for_datasource", side_effect=RuntimeError("DataZone throttled")):
            assert _run() == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
        assert _job(aws)["status"] == "IN_PROGRESS"
        assert aws["fakes"].saved == []

    def test_h13_source_record_cannot_be_read_is_retried(self, aws) -> None:
        aws["sources_dao"].get.side_effect = RuntimeError("DynamoDB unavailable")
        assert _run() == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
        assert _job(aws)["status"] == "IN_PROGRESS"

    def test_h14_deleted_source_fails_the_job(self, aws) -> None:
        aws["sources_dao"].get.return_value = None
        assert _run() == {"batchItemFailures": []}
        job = _job(aws)
        assert job["status"] == "FAILED"
        assert "no longer exists" in job["errorMessage"]

    def test_h15_runs_without_a_lambda_context(self, aws) -> None:
        assert worker_handler.handler({"Records": [_record()]}, None) == {"batchItemFailures": []}
        assert _job(aws)["status"] == "COMPLETED"

    def test_h15_error_text_falls_back_to_status_code(self) -> None:
        assert worker_handler._error_text({"statusCode": 502, "body": "<html>"}) == "502"
        assert worker_handler._error_text({"statusCode": 404, "body": json.dumps({"error": "gone"})}) == "gone"
