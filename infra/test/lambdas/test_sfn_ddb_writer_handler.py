"""Unit tests for the sfn-ddb-writer Lambda (moto; no real AWS).

The sources state machines invoke this Lambda instead of the built-in Step
Functions DynamoDB tasks. Each event below is what a converted state sends
after Step Functions resolves its ``.$`` paths: the former task parameters plus
``Operation``.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda" / "sfn-ddb-writer" / "index.py"

# Loaded under a unique module name, not as ``index`` (see conftest.py).
_MODULE_NAME = "sfn_ddb_writer_index_under_test"

TABLE = "coa-dev-sources"
KEY = {"PK": {"S": "NS#ns1"}, "SK": {"S": "SRC#s1"}}


@pytest.fixture
def env(monkeypatch):
    """Create a moto sources table with one item and import the handler against it."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName=TABLE,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
        )
        ddb.put_item(TableName=TABLE, Item={**KEY, "status": {"S": "SCANNING"}})

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _HANDLER_PATH)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, _MODULE_NAME, mod)
        spec.loader.exec_module(mod)
        yield mod, ddb


def _item(ddb):
    return ddb.get_item(TableName=TABLE, Key=KEY).get("Item")


def test_update_item_with_string_values(env):
    # DocUpdateStatusFailed.
    mod, ddb = env
    out = mod.handler(
        {
            "Operation": "UpdateItem",
            "TableName": TABLE,
            "Key": KEY,
            "UpdateExpression": "SET #s = :s, #u = :u, #e = :e",
            "ExpressionAttributeNames": {"#s": "status", "#u": "updatedAt", "#e": "errorMessage"},
            "ExpressionAttributeValues": {
                ":s": {"S": "SCAN_FAILED"},
                ":u": {"S": "2026-10-26T10:00:00Z"},
                ":e": {"S": "boom"},
            },
        },
        None,
    )
    assert out == {}
    item = _item(ddb)
    assert item["status"] == {"S": "SCAN_FAILED"}
    assert item["updatedAt"] == {"S": "2026-10-26T10:00:00Z"}
    assert item["errorMessage"] == {"S": "boom"}


def test_update_item_with_number_and_bool_values(env):
    # SourcesSavePreprocessingResults ("N.$" via States.Format, "BOOL.$").
    mod, ddb = env
    mod.handler(
        {
            "Operation": "UpdateItem",
            "TableName": TABLE,
            "Key": KEY,
            "UpdateExpression": "SET #ft = :ft, #pit = :pit",
            "ExpressionAttributeNames": {"#ft": "filesTotal", "#pit": "preprocessingIssuesTruncated"},
            "ExpressionAttributeValues": {":ft": {"N": "12"}, ":pit": {"BOOL": True}},
        },
        None,
    )
    item = _item(ddb)
    assert item["filesTotal"] == {"N": "12"}
    assert item["preprocessingIssuesTruncated"] == {"BOOL": True}


def test_delete_item(env):
    # SourcesDocDeleteDdbRecord.
    mod, ddb = env
    mod.handler({"Operation": "DeleteItem", "TableName": TABLE, "Key": KEY}, None)
    assert _item(ddb) is None


def test_event_is_not_modified(env):
    mod, _ = env
    event = {"Operation": "DeleteItem", "TableName": TABLE, "Key": KEY}
    mod.handler(event, None)
    assert event == {"Operation": "DeleteItem", "TableName": TABLE, "Key": KEY}


@pytest.mark.parametrize("operation", [None, "PutItem", "updateItem"])
def test_unsupported_operation_fails(env, operation):
    mod, ddb = env
    event = {"TableName": TABLE, "Key": KEY}
    if operation is not None:
        event["Operation"] = operation
    with pytest.raises(ValueError, match="Unsupported Operation"):
        mod.handler(event, None)
    assert _item(ddb) is not None


def test_dynamodb_error_is_raised_so_the_task_fails(env):
    mod, _ = env
    with pytest.raises(ClientError, match="ResourceNotFoundException"):
        mod.handler({"Operation": "DeleteItem", "TableName": "missing-table", "Key": KEY}, None)
