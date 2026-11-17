"""Cognito pre-token-generation trigger: make the MCP app client's access tokens usable by COA.

COA's token checks (AgentCore on the MCP server and Context Manager runtimes, and
the MCP server itself) accept a token only if ``aud`` is a COA app client ID, and
the grant lookup needs the caller's ``email``. Cognito ID tokens carry both;
Cognito access tokens carry neither. MCP clients that sign in with OAuth (for
example Amazon Quick) send the access token, so for sign-ins through the MCP app
client this trigger adds two claims to the access token:

    aud   = the MCP app client ID (Cognito allows aud only if it equals the
            session's app client ID)
    email = the user's email attribute

Tokens for every other app client, and all ID tokens, are returned unchanged.

The MCP app client ID is read once per container from the SSM parameter named in
``MCP_CLIENT_ID_PARAM``. It isn't passed as an environment variable because that
would make a CloudFormation dependency cycle (user pool -> trigger -> app client
-> user pool). If the lookup fails, the event is returned unchanged: sign-in still
works for every client, and COA rejects the MCP access token (fail closed).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_mcp_client_id: str | None = None


def _get_mcp_client_id() -> str | None:
    """Return the MCP app client ID, read from SSM once per container.

    Returns:
        The client ID, or None if the parameter can't be read.
    """
    global _mcp_client_id
    if _mcp_client_id is None:
        try:
            response = boto3.client("ssm").get_parameter(Name=os.environ["MCP_CLIENT_ID_PARAM"])
        except Exception:
            logger.exception("mcp_client_id_lookup_failed")
            return None
        _mcp_client_id = response["Parameter"]["Value"]
    return _mcp_client_id


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Add aud and email to access tokens issued through the MCP app client.

    Args:
        event: Cognito pre-token-generation event (version 2).
        context: Lambda context (unused).

    Returns:
        The event, with claim overrides for the MCP app client's access token.
    """
    client_id = event["callerContext"]["clientId"]
    if client_id != _get_mcp_client_id():
        return event
    claims = {"aud": client_id}
    email = event["request"]["userAttributes"].get("email")
    if email:
        claims["email"] = email
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {"claimsToAddOrOverride": claims},
    }
    logger.info("mcp_access_token_claims_added", extra={"has_email": bool(email)})
    return event
