# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKGClient verifies VKG's certificate behind the internal ALB (HTTPS).

With AWS Private CA the client verifies against the TLS context from
``coa_common.service_tls`` (public roots + the private CA). Without one it
keeps httpx's default verification.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_serve.clients import vkg as vkg_module
from coa_serve.clients.vkg import VKGClient

pytestmark = pytest.mark.unit

_ENDPOINT = "https://vkg.coa-test.internal:8080"


async def test_uses_the_service_tls_context_when_there_is_one():
    ctx = object()
    with (
        patch.object(vkg_module, "ssl_context", return_value=ctx),
        patch.object(vkg_module.httpx, "AsyncClient") as client_cls,
    ):
        await VKGClient(base_url=_ENDPOINT)._get_http()

    assert client_cls.call_args.kwargs["verify"] is ctx


async def test_default_verification_without_a_private_ca():
    with (
        patch.object(vkg_module, "ssl_context", return_value=None),
        patch.object(vkg_module.httpx, "AsyncClient") as client_cls,
    ):
        await VKGClient(base_url=_ENDPOINT)._get_http()

    assert client_cls.call_args.kwargs["verify"] is True
