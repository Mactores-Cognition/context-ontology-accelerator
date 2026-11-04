# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys import: the API Lambda (upload URL, preview, start, get job, list jobs).

S3, DynamoDB and SQS are simulated with moto; the existing sources lookups
(source record, namespace project) and the table reader are mocked. Expected
results: work/add-bulk-import.txt, section 3.6 (A1-A4, A6).
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime
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

from coa_sources.database.keys_import import api_handler  # noqa: E402
from tests.unit.conftest import dao_double  # noqa: E402

pytestmark = pytest.mark.unit

_DR = "coa_sources.api.database_routes"
_API = "coa_sources.database.keys_import.api_handler"
NS = "550e8400-e29b-41d4-a716-446655440000"
OTHER_NS = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
SRC = "src-db-001"
BUCKET = "test-keys-import"
JOBS_TABLE = "test-keys-import-jobs"
BASE = "/namespaces/{namespaceId}/sources/{sourceId}/keys-import"
HEADER = "type,source_table,column,target_table,target_column\n"
JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


def _table(name: str, columns: list[str], fks: list[ForeignKey] | None = None) -> Table:
    return Table(
        name=name,
        database="sales",
        data_source_id=SRC,
        columns=[Column(name=c, data_type="string") for c in columns],
        foreign_keys=list(fks or []),
    )


SOURCE_TABLES = [
    _table(
        "purchase_order",
        ["po_number", "supplier_id", "parent_po"],
        fks=[
            ForeignKey(column="supplier_id", target_table="supplier", target_column="supplier_id", source="AI_INFERRED")
        ],
    ),
    _table("supplier", ["supplier_id", "name"]),
]


@pytest.fixture
def aws(monkeypatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=JOBS_TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(QueueName="keys-import")["QueueUrl"]
        monkeypatch.setenv("KEYS_IMPORT_BUCKET", BUCKET)
        monkeypatch.setenv("KEYS_IMPORT_JOBS_TABLE", JOBS_TABLE)
        monkeypatch.setenv("KEYS_IMPORT_QUEUE_URL", queue_url)
        api_handler.reset_clients()

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
        with (
            patch(f"{_DR}._get_dao", return_value=sources_dao),
            patch(f"{_DR}._get_ns_dao", return_value=ns_dao),
            patch(f"{_API}.read_assets_for_datasource", return_value=SOURCE_TABLES) as reader,
        ):
            yield {
                "s3": s3,
                "ddb": boto3.resource("dynamodb", region_name="us-east-1").Table(JOBS_TABLE),
                "sqs": sqs,
                "queue_url": queue_url,
                "sources_dao": sources_dao,
                "reader": reader,
            }
        api_handler.reset_clients()


def _event(method: str, resource: str, body: Any = None, *, ns: str = NS, extra: dict | None = None) -> dict:
    params = {"namespaceId": ns, "sourceId": SRC, **(extra or {})}
    return {
        "resource": resource,
        "httpMethod": method,
        "pathParameters": params,
        "queryStringParameters": None,
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "requestContext": {"authorizer": {"email": "steward@example.com"}},
    }


def _call(event: dict) -> tuple[int, dict]:
    response = api_handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"] or "{}")


def _upload(aws: dict, text: str | bytes, name: str = "erp-keys.csv") -> str:
    key = f"keys-import/{NS}/{SRC}/abcd1234/{name}"
    aws["s3"].put_object(Bucket=BUCKET, Key=key, Body=text.encode() if isinstance(text, str) else text)
    return key


def _messages(aws: dict) -> list[dict]:
    out = aws["sqs"].receive_message(QueueUrl=aws["queue_url"], MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in out.get("Messages", [])]


def _jobs(aws: dict) -> list[dict]:
    return aws["ddb"].scan()["Items"]


GOOD_CSV = (
    HEADER
    + "FK,purchase_order,supplier_id,supplier,supplier_id\n"  # line 2: already there
    + "FK,purchase_order,parent_po,purchase_order,po_number\n"  # line 3: add
    + "PK,supplier,supplier_id,,\n"  # line 4: add
    + "FK,vendor,id,supplier,supplier_id\n"  # line 5: unknown table
    + "XX,supplier,name,,\n"  # line 6: bad type (CSV reader)
)


class TestUploadUrl:
    def test_a1_returns_presigned_url_and_key_under_namespace_and_source(self, aws) -> None:
        status, body = _call(_event("POST", BASE + "/upload-url", {"fileName": "erp-keys.csv"}))
        assert status == 200
        assert body["s3Key"].startswith(f"keys-import/{NS}/{SRC}/")
        assert body["s3Key"].endswith("/erp-keys.csv")
        assert BUCKET in body["uploadUrl"]
        assert body["expiresIn"] == 900

    @pytest.mark.parametrize("name", ["../x.csv", "a/b.csv", "a\\b.csv", "keys.txt", "", ".csv"])
    def test_a1_bad_file_name_is_400(self, aws, name: str) -> None:
        status, _ = _call(_event("POST", BASE + "/upload-url", {"fileName": name}))
        assert status == 400

    @pytest.mark.parametrize("body", ["not-json", None, {}])
    def test_a1_bad_body_is_400(self, aws, body: Any) -> None:
        status, _ = _call(_event("POST", BASE + "/upload-url", body))
        assert status == 400

    def test_a1_unknown_source_is_404(self, aws) -> None:
        aws["sources_dao"].get.return_value = None
        status, _ = _call(_event("POST", BASE + "/upload-url", {"fileName": "k.csv"}))
        assert status == 404

    def test_a1_source_being_scanned_is_409(self, aws) -> None:
        aws["sources_dao"].get.return_value = {**aws["sources_dao"].get.return_value, "status": "SCANNING"}
        status, _ = _call(_event("POST", BASE + "/upload-url", {"fileName": "k.csv"}))
        assert status == 409


class TestPreview:
    def test_a2_counts_and_row_issues_in_file_order(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        status, body = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 200
        assert body["counts"] == {
            "rowsRead": 5,
            "toAdd": 2,
            "alreadyThere": 1,
            "duplicates": 0,
            "skipped": 0,
            "errors": 2,
            "tablesToUpdate": 2,
        }
        assert [(i["line"], i["outcome"]) for i in body["rowIssues"]] == [
            (2, "ALREADY_THERE"),
            (5, "ERROR"),
            (6, "ERROR"),
        ]
        assert body["rowIssuesTruncated"] is False

    def test_a2_added_row_with_warning_is_listed(self, aws) -> None:
        key = _upload(aws, HEADER + "FK,purchase_order,supplier_id,purchase_order,po_number\n")
        status, body = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 200
        assert body["rowIssues"][0]["outcome"] == "ADD"
        assert "both are kept" in body["rowIssues"][0]["warning"]

    def test_a2_writes_nothing(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert _jobs(aws) == []
        assert _messages(aws) == []

    @pytest.mark.parametrize(
        "key",
        [
            f"keys-import/{NS}/src-other/abcd1234/k.csv",
            f"keys-import/{OTHER_NS}/{SRC}/abcd1234/k.csv",
            f"keys-import/{NS}/{SRC}/../../x/k.csv",
            "somewhere/else.csv",
        ],
    )
    def test_a2_key_outside_this_source_is_400(self, aws, key: str) -> None:
        status, _ = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 400

    def test_a2_missing_file_is_400(self, aws) -> None:
        status, body = _call(_event("POST", BASE + "/preview", {"s3Key": f"keys-import/{NS}/{SRC}/abcd1234/k.csv"}))
        assert status == 400
        assert "upload" in body["error"].lower()

    def test_a2_file_level_csv_error_is_400_with_reason(self, aws) -> None:
        key = _upload(aws, "type,source_table\nPK,t\n")
        status, body = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 400
        assert "target_column" in body["error"]

    def test_a2_file_not_utf8_is_400(self, aws) -> None:
        key = _upload(aws, b"\xff\xfe\x00bad")
        status, _ = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 400

    def test_a2_issues_cut_to_1000(self, aws) -> None:
        key = _upload(aws, HEADER + "FK,nope,a,supplier,supplier_id\n" * 1200)
        status, body = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 200
        assert len(body["rowIssues"]) == 1000
        assert body["rowIssuesTruncated"] is True
        assert body["counts"]["errors"] == 1200

    def test_a2_source_being_approved_is_409(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        aws["sources_dao"].get.return_value = {**aws["sources_dao"].get.return_value, "status": "APPROVING"}
        status, _ = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 409

    def test_a2_reads_tables_of_this_source(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        aws["reader"].assert_called_once_with("test-domain-id", "proj-123", SRC)


class TestStart:
    def test_a3_returns_202_records_job_and_queues_one_message(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        status, body = _call(_event("POST", BASE, {"s3Key": key}))
        assert status == 202
        assert JOB_ID_RE.match(body["jobId"])
        assert body["status"] == "IN_PROGRESS"

        [job] = _jobs(aws)
        assert job["PK"] == f"SRC#{SRC}"
        assert job["SK"] == body["jobId"]
        assert job["namespaceId"] == NS
        assert job["status"] == "IN_PROGRESS"
        assert job["fileName"] == "erp-keys.csv"
        assert job["s3Key"] == key
        assert job["startedBy"] == "steward@example.com"
        assert job["counts"]["toAdd"] == 2

        assert _messages(aws) == [{"namespaceId": NS, "sourceId": SRC, "jobId": body["jobId"]}]

    def test_a3_file_that_adds_nothing_is_400_and_starts_nothing(self, aws) -> None:
        key = _upload(aws, HEADER + "FK,purchase_order,supplier_id,supplier,supplier_id\n")
        status, body = _call(_event("POST", BASE, {"s3Key": key}))
        assert status == 400
        assert "no keys" in body["error"].lower()
        assert _jobs(aws) == []
        assert _messages(aws) == []

    def test_a3_second_import_while_one_runs_is_409(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        assert _call(_event("POST", BASE, {"s3Key": key}))[0] == 202
        status, _ = _call(_event("POST", BASE, {"s3Key": key}))
        assert status == 409
        assert len(_jobs(aws)) == 1

    def test_a3_queue_failure_marks_job_failed_and_is_500(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        with patch.object(api_handler, "_send_job_message", side_effect=RuntimeError("SQS down")):
            status, _ = _call(_event("POST", BASE, {"s3Key": key}))
        assert status == 500
        [job] = _jobs(aws)
        assert job["status"] == "FAILED"
        assert "SQS down" in job["errorMessage"]

    def test_a3_row_issues_stored_cut_to_100(self, aws) -> None:
        key = _upload(aws, HEADER + "PK,supplier,supplier_id,,\n" + "FK,nope,a,supplier,supplier_id\n" * 150)
        status, _ = _call(_event("POST", BASE, {"s3Key": key}))
        assert status == 202
        [job] = _jobs(aws)
        assert len(job["rowIssues"]) == 100
        assert job["issuesTruncated"] is True


class TestGetAndList:
    def _start(self, aws: dict) -> str:
        key = _upload(aws, GOOD_CSV)
        return _call(_event("POST", BASE, {"s3Key": key}))[1]["jobId"]

    def test_a4_get_job(self, aws) -> None:
        job_id = self._start(aws)
        status, body = _call(_event("GET", BASE + "/{jobId}", extra={"jobId": job_id}))
        assert status == 200
        job = body["job"]
        assert job["jobId"] == job_id
        assert job["status"] == "IN_PROGRESS"
        assert job["fileName"] == "erp-keys.csv"
        assert isinstance(job["startedAt"], int)
        assert job["counts"]["rowsRead"] == 5
        assert "errorReportUrl" not in job

    def test_a4_get_job_with_report_returns_download_url(self, aws) -> None:
        job_id = self._start(aws)
        aws["ddb"].update_item(
            Key={"PK": f"SRC#{SRC}", "SK": job_id},
            UpdateExpression="SET reportKey = :r",
            ExpressionAttributeValues={":r": f"keys-import/{NS}/{SRC}/reports/{job_id}.csv"},
        )
        _, body = _call(_event("GET", BASE + "/{jobId}", extra={"jobId": job_id}))
        assert f"reports/{job_id}.csv" in body["job"]["errorReportUrl"]

    def test_a4_unknown_job_is_404(self, aws) -> None:
        status, _ = _call(_event("GET", BASE + "/{jobId}", extra={"jobId": "20261104T153012Z-1a2b3c4d"}))
        assert status == 404

    def test_a4_job_of_another_namespace_is_404(self, aws) -> None:
        job_id = self._start(aws)
        status, _ = _call(_event("GET", BASE + "/{jobId}", ns=OTHER_NS, extra={"jobId": job_id}))
        assert status == 404

    def test_a4_malformed_job_id_is_400(self, aws) -> None:
        status, _ = _call(_event("GET", BASE + "/{jobId}", extra={"jobId": "not-a-job"}))
        assert status == 400

    def test_a4_list_newest_first_with_paging(self, aws) -> None:
        for job_id in ("20261104T100000Z-00000001", "20261104T110000Z-00000002"):
            aws["ddb"].put_item(
                Item={
                    "PK": f"SRC#{SRC}",
                    "SK": job_id,
                    "namespaceId": NS,
                    "sourceId": SRC,
                    "status": "COMPLETED",
                    "fileName": "k.csv",
                    "startedAt": "2026-11-04T10:00:00+00:00",
                    "counts": {
                        "rowsRead": 1,
                        "toAdd": 1,
                        "alreadyThere": 0,
                        "duplicates": 0,
                        "skipped": 0,
                        "errors": 0,
                        "tablesToUpdate": 1,
                    },
                }
            )
        event = _event("GET", BASE)
        event["queryStringParameters"] = {"maxResults": "1"}
        status, page1 = _call(event)
        assert status == 200
        assert [j["jobId"] for j in page1["items"]] == ["20261104T110000Z-00000002"]
        assert page1["nextToken"]

        event["queryStringParameters"] = {"maxResults": "1", "nextToken": page1["nextToken"]}
        _, page2 = _call(event)
        assert [j["jobId"] for j in page2["items"]] == ["20261104T100000Z-00000001"]

    def test_a4_list_ignores_runs_of_another_namespace(self, aws) -> None:
        self._start(aws)
        status, body = _call(_event("GET", BASE, ns=OTHER_NS))
        assert status == 200
        assert body["items"] == []

    def test_a4_bad_next_token_is_400(self, aws) -> None:
        event = _event("GET", BASE)
        event["queryStringParameters"] = {"nextToken": base64.b64encode(b"not json").decode()}
        status, _ = _call(event)
        assert status == 400


class TestRouting:
    def test_a6_unknown_route_is_404(self, aws) -> None:
        status, _ = _call(_event("DELETE", BASE))
        assert status == 404


class TestEdgeCases:
    def test_a2_file_over_size_limit_is_400(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        with patch.object(api_handler, "MAX_FILE_BYTES", 10):
            status, body = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 400
        assert "larger than" in body["error"]

    def test_a2_unknown_namespace_is_404(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        with patch(f"{_DR}._resolve_project_id", return_value=None):
            status, _ = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 404

    def test_a3_stale_running_job_does_not_block_a_new_import(self, aws) -> None:
        aws["ddb"].put_item(
            Item={
                "PK": f"SRC#{SRC}",
                "SK": "20261101T100000Z-00000001",
                "namespaceId": NS,
                "status": "IN_PROGRESS",
                "startedAt": "2026-11-01T10:00:00+00:00",
            }
        )
        key = _upload(aws, GOOD_CSV)
        status, _ = _call(_event("POST", BASE, {"s3Key": key}))
        assert status == 202

    def test_a4_finished_job_shows_completion_time(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        job_id = _call(_event("POST", BASE, {"s3Key": key}))[1]["jobId"]
        aws["ddb"].update_item(
            Key={"PK": f"SRC#{SRC}", "SK": job_id},
            UpdateExpression="SET #s = :s, completedAt = :c, keysAdded = :k",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "COMPLETED", ":c": "2026-11-04T15:31:40+00:00", ":k": 2},
        )
        _, body = _call(_event("GET", BASE + "/{jobId}", extra={"jobId": job_id}))
        assert body["job"]["status"] == "COMPLETED"
        assert body["job"]["completedAt"] == int(datetime.fromisoformat("2026-11-04T15:31:40+00:00").timestamp())
        assert body["job"]["keysAdded"] == 2

    def test_a4_max_results_not_a_number_is_400(self, aws) -> None:
        event = _event("GET", BASE)
        event["queryStringParameters"] = {"maxResults": "lots"}
        status, _ = _call(event)
        assert status == 400

    def test_a4_next_token_of_another_source_is_400(self, aws) -> None:
        event = _event("GET", BASE)
        token = base64.b64encode(json.dumps({"PK": "SRC#src-other", "SK": "x"}).encode()).decode()
        event["queryStringParameters"] = {"nextToken": token}
        status, _ = _call(event)
        assert status == 400

    def test_a6_unexpected_s3_error_is_500_without_details(self, aws) -> None:
        key = _upload(aws, GOOD_CSV)
        with patch.object(api_handler, "_bucket", return_value="no-such-bucket"):
            status, body = _call(_event("POST", BASE + "/preview", {"s3Key": key}))
        assert status == 500
        assert body == {"error": "Internal server error"}


class TestSwitchedOff:
    @pytest.mark.parametrize(
        ("method", "resource"),
        [("POST", BASE + "/upload-url"), ("POST", BASE + "/preview"), ("POST", BASE), ("GET", BASE)],
    )
    def test_a7_every_route_answers_not_enabled(self, monkeypatch, method: str, resource: str) -> None:
        monkeypatch.setenv("KEYS_IMPORT_ENABLED", "false")
        status, body = _call(_event(method, resource, {"fileName": "k.csv"}))
        assert status == 404
        assert "not enabled" in body["error"]
