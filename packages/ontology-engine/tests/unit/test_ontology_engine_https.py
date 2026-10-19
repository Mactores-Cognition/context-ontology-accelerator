"""ontology-engine behind the internal ALB over HTTPS.

Two sides (replace-cloud-map-notes.txt sections 14-15):

* The api-proxy Lambda calls ontology-engine and passes the TLS context from
  ``coa_common.service_tls`` (private CA) to urllib.
* The container, when ``SERVICE_TLS_PORT`` is set (ALB mode only), serves the
  ALB over HTTPS on that port with a self-signed certificate made at start, and
  keeps plain HTTP on 127.0.0.1 for its own calls. Without the variable it starts
  exactly as before.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.unit

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "https://ontology-engine.coa-test.internal:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")


def _ok(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestApiProxyUsesServiceTls:
    def test_forwarded_request_uses_the_context(self):
        from coa_ontology import api_proxy_handler as mod

        ctx = object()
        event = {
            "httpMethod": "POST",
            "resource": "/namespaces/{namespaceId}/induce",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": None,
            "body": "{}",
        }
        with (
            patch.object(mod, "ssl_context", return_value=ctx),
            patch("urllib.request.urlopen", return_value=_ok(b"{}")) as urlopen,
        ):
            mod.handler(event, None)

        assert urlopen.call_args.kwargs["context"] is ctx

    def test_readiness_check_uses_the_context(self, monkeypatch):
        from coa_ontology import api_proxy_handler as mod

        for var in ("SOURCES_TABLE", "NEPTUNE_ENDPOINT", "OPENSEARCH_ENDPOINT"):
            monkeypatch.setenv(var, "")
        ctx = object()
        with (
            patch.object(mod, "ssl_context", return_value=ctx),
            patch("urllib.request.urlopen", return_value=_ok(json.dumps({"graph_store": "ok"}).encode())) as urlopen,
        ):
            mod.handler({"httpMethod": "GET", "resource": "/system-health"}, None)

        assert urlopen.call_args.kwargs["context"] is ctx


class TestServeTls:
    def test_self_signed_certificate_for_the_alb(self, tmp_path):
        from coa_ontology.serve_tls import make_self_signed

        certfile, keyfile = make_self_signed(tmp_path)

        assert certfile.exists() and keyfile.exists()
        cert = x509.load_pem_x509_certificate(certfile.read_bytes())
        assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "ontology-engine"
        assert cert.not_valid_after_utc - datetime.datetime.now(datetime.UTC) > datetime.timedelta(days=365)
        assert oct(keyfile.stat().st_mode & 0o777) == "0o600"

    def test_two_servers_https_for_the_alb_http_on_localhost(self, tmp_path):
        from coa_ontology.serve_tls import build_configs

        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        http, https = build_configs("coa_ontology.main:app", http_port=8001, tls_port=8443, certfile=cert, keyfile=key)

        assert (http.host, http.port, http.ssl_certfile) == ("127.0.0.1", 8001, None)
        assert (https.host, https.port) == ("0.0.0.0", 8443)
        assert (https.ssl_certfile, https.ssl_keyfile) == (str(cert), str(key))
        # App startup/shutdown runs once, on the HTTP server.
        assert (http.lifespan, https.lifespan) == ("on", "off")


class TestDockerfileStartCommand:
    def test_tls_entry_point_only_when_service_tls_port_is_set(self):
        cmd = next(line for line in _DOCKERFILE.read_text().splitlines() if line.startswith("CMD"))
        assert "SERVICE_TLS_PORT" in cmd
        assert "coa_ontology.serve_tls" in cmd
        # Without the variable: today's start command, unchanged.
        assert "uvicorn coa_ontology.main:app --host 0.0.0.0 --port ${PORT:-8001}" in cmd
