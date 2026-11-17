"""Unit tests for the Cognito pre-token-generation Lambda (moto; no real AWS).

The trigger adds ``aud`` (the MCP app client ID) and ``email`` to access tokens
issued through the MCP app client, so MCP clients that send the access token
(for example Amazon Quick) pass COA's token checks. Tokens for other app
clients, and all ID tokens, must come back unchanged.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda" / "pre-token-generation" / "index.py"

# Loaded under a unique module name, not as ``index`` (see conftest.py).
_MODULE_NAME = "pre_token_generation_index_under_test"

PARAM = "/coa/mcp-client-id"
MCP_CLIENT_ID = "mcpclient123"
WEB_CLIENT_ID = "webclient456"


@pytest.fixture
def handler_module(monkeypatch):
    """Put the MCP client ID in a moto SSM parameter and import the handler."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "MCP_CLIENT_ID_PARAM": PARAM,
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("ssm").put_parameter(Name=PARAM, Value=MCP_CLIENT_ID, Type="String")
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _HANDLER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _event(client_id, user_attributes):
    """Build a V2_0 pre-token-generation event as Cognito sends it."""
    return {
        "version": "2",
        "triggerSource": "TokenGeneration_HostedAuth",
        "region": "us-east-1",
        "userPoolId": "us-east-1_example",
        "userName": "user-sub-1",
        "callerContext": {"awsSdkVersion": "aws-sdk-unknown-unknown", "clientId": client_id},
        "request": {
            "userAttributes": user_attributes,
            "groupConfiguration": {"groupsToOverride": ["analysts"], "iamRolesToOverride": [], "preferredRole": None},
            "scopes": ["openid", "email", "profile"],
        },
        "response": {"claimsAndScopeOverrideDetails": None},
    }


def test_mcp_client_access_token_gets_aud_and_email(handler_module):
    event = _event(MCP_CLIENT_ID, {"sub": "user-sub-1", "email": "user@example.com"})

    result = handler_module.handler(event, None)

    assert result["response"]["claimsAndScopeOverrideDetails"] == {
        "accessTokenGeneration": {"claimsToAddOrOverride": {"aud": MCP_CLIENT_ID, "email": "user@example.com"}},
    }


def test_id_token_is_not_changed(handler_module):
    event = _event(MCP_CLIENT_ID, {"sub": "user-sub-1", "email": "user@example.com"})

    result = handler_module.handler(event, None)

    assert "idTokenGeneration" not in result["response"]["claimsAndScopeOverrideDetails"]


def test_web_client_tokens_are_unchanged(handler_module):
    event = _event(WEB_CLIENT_ID, {"sub": "user-sub-1", "email": "user@example.com"})

    result = handler_module.handler(event, None)

    assert result["response"]["claimsAndScopeOverrideDetails"] is None


def test_user_without_email_gets_aud_only(handler_module):
    event = _event(MCP_CLIENT_ID, {"sub": "user-sub-1"})

    result = handler_module.handler(event, None)

    assert result["response"]["claimsAndScopeOverrideDetails"] == {
        "accessTokenGeneration": {"claimsToAddOrOverride": {"aud": MCP_CLIENT_ID}},
    }


def test_mcp_client_id_is_read_once_per_container(handler_module):
    handler_module.handler(_event(MCP_CLIENT_ID, {"email": "user@example.com"}), None)
    boto3.client("ssm").delete_parameter(Name=PARAM)

    result = handler_module.handler(_event(MCP_CLIENT_ID, {"email": "user@example.com"}), None)

    assert (
        result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"]["aud"]
        == MCP_CLIENT_ID
    )


def test_tokens_unchanged_when_parameter_lookup_fails(handler_module):
    boto3.client("ssm").delete_parameter(Name=PARAM)
    event = _event(MCP_CLIENT_ID, {"email": "user@example.com"})

    result = handler_module.handler(event, None)

    assert result["response"]["claimsAndScopeOverrideDetails"] is None
