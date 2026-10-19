# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG routing through the internal ALB (SERVICE_DISCOVERY_BACKEND=alb).

With Cloud Map each namespace's VKG service has its own hostname, so the
translator rewrites ``vkg.`` to ``vkg-{namespace}.``. Behind the internal ALB
every namespace shares the ALB's address, so the endpoint is used as is and
each request carries the namespace in the ``X-Coa-Namespace`` header, which the
ALB's listener rules match on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.clients.vkg import VKG_NAMESPACE_HEADER, VKGClient
from coa_serve.tier2.ontop.vkg_translator import VKGTranslator
from coa_serve.tier2.sql_firewall import SQLFirewall

_ALB_ENDPOINT = "http://internal-coa-dev-svc-123.us-east-1.elb.amazonaws.com:8080"


def _translator(endpoint: str = _ALB_ENDPOINT) -> VKGTranslator:
    return VKGTranslator(
        vkg_endpoint=endpoint,
        firewall=MagicMock(spec=SQLFirewall),
        query_executor=AsyncMock(),
    )


@pytest.mark.unit
class TestVKGClientHeaders:
    def test_header_name(self):
        assert VKG_NAMESPACE_HEADER == "X-Coa-Namespace"

    async def test_extra_headers_are_sent_on_every_request(self):
        client = VKGClient(base_url=_ALB_ENDPOINT, headers={VKG_NAMESPACE_HEADER: "ns-a"})
        http = await client._get_http()
        try:
            assert http.headers[VKG_NAMESPACE_HEADER] == "ns-a"
        finally:
            await client.close()

    async def test_no_namespace_header_by_default(self):
        client = VKGClient(base_url="http://vkg.local:8080")
        http = await client._get_http()
        try:
            assert VKG_NAMESPACE_HEADER not in http.headers
        finally:
            await client.close()


@pytest.mark.unit
class TestVKGTranslatorAlbMode:
    @pytest.fixture(autouse=True)
    def _alb_mode(self, monkeypatch):
        monkeypatch.setenv("SERVICE_DISCOVERY_BACKEND", "alb")

    def test_keeps_the_endpoint_and_sends_the_namespace_header(self):
        client = _translator()._resolve_client("ns-abc")
        assert client._base_url == _ALB_ENDPOINT
        assert client._headers == {VKG_NAMESPACE_HEADER: "ns-abc"}

    def test_each_namespace_gets_its_own_client_and_header(self):
        translator = _translator()
        client_a = translator._resolve_client("ns-a")
        client_b = translator._resolve_client("ns-b")
        assert client_a is not client_b
        assert client_a._headers[VKG_NAMESPACE_HEADER] == "ns-a"
        assert client_b._headers[VKG_NAMESPACE_HEADER] == "ns-b"
        assert translator._resolve_client("ns-a") is client_a

    def test_an_endpoint_without_vkg_dot_is_fine(self):
        # The Cloud Map rewrite needs "://vkg." in the URL; the ALB DNS name has none.
        client = _translator("http://localhost:8080")._resolve_client("ns-abc")
        assert client._base_url == "http://localhost:8080"


@pytest.mark.unit
class TestVKGTranslatorDefaultModeUnchanged:
    def test_without_the_flag_rewrites_the_hostname_and_sends_no_header(self, monkeypatch):
        monkeypatch.delenv("SERVICE_DISCOVERY_BACKEND", raising=False)
        client = _translator("http://vkg.coa-dev-services.local:8080")._resolve_client("ns-abc")
        assert client._base_url == "http://vkg-ns-abc.coa-dev-services.local:8080"
        assert not client._headers
