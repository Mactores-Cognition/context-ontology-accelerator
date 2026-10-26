"""DynamoDB writes for Step Functions tasks, made from inside the VPC.

Step Functions' built-in DynamoDB tasks (arn:aws:states:::dynamodb:*) are
called by the Step Functions service itself, outside the VPC, so they never
use the VPC's DynamoDB endpoint. The sources pipelines invoke this Lambda
instead (arn:aws:states:::lambda:invoke). It runs in the VPC, so its
DynamoDB calls go through the endpoint.

The payload is the task's former DynamoDB parameters plus the operation:

    {"Operation": "UpdateItem", "TableName": ..., "Key": {...},
     "UpdateExpression": ..., "ExpressionAttributeNames": {...},
     "ExpressionAttributeValues": {...}}
    {"Operation": "DeleteItem", "TableName": ..., "Key": {...}}

Values keep DynamoDB's typed form ({"S": ...}, {"N": ...}, {"BOOL": ...}) and
are passed to the low-level client unchanged. DynamoDB errors are raised, so
the task fails exactly as the built-in task did. Which tables can be written
is limited by this function's IAM role.
"""

from __future__ import annotations

import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb")

_OPERATIONS = {
    "UpdateItem": dynamodb.update_item,
    "DeleteItem": dynamodb.delete_item,
}


def handler(event: dict, context: object) -> dict:
    """Lambda entry-point: run one UpdateItem or DeleteItem from a Step Functions task."""
    params = dict(event)
    operation = params.pop("Operation", None)
    call = _OPERATIONS.get(operation)
    if call is None:
        raise ValueError(f"Unsupported Operation {operation!r}; expected one of {sorted(_OPERATIONS)}")
    logger.info("%s on %s", operation, params.get("TableName"))
    call(**params)
    return {}
