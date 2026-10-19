# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG facade over HTTPS behind the internal ALB (SERVICE_TLS=1, ALB mode only).

entrypoint.sh makes a self-signed certificate and exports SERVICE_TLS_CERT /
SERVICE_TLS_KEY before the facade starts; translate-server.py then serves TLS.
Without SERVICE_TLS nothing changes. The ALB does not validate target
certificates, so self-signed is the standard set-up.
"""

from __future__ import annotations

import datetime
import importlib.util
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

pytestmark = pytest.mark.unit

_VKG = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _VKG / "entrypoint.sh"
_DOCKERFILE = _VKG / "Dockerfile"

# Same shared-module rule as test_translate_internals.py.
if "translate_server" in sys.modules:
    translate_server = sys.modules["translate_server"]
else:
    _spec = importlib.util.spec_from_file_location("translate_server", _VKG / "translate-server.py")
    assert _spec is not None and _spec.loader is not None
    translate_server = importlib.util.module_from_spec(_spec)
    sys.modules["translate_server"] = translate_server
    _spec.loader.exec_module(translate_server)


def _write_self_signed(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vkg")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = directory / "cert.pem", directory / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return certfile, keyfile


class TestFacadeTls:
    def test_serves_tls_when_the_certificate_is_given(self, tmp_path, monkeypatch):
        certfile, keyfile = _write_self_signed(tmp_path)
        monkeypatch.setenv("SERVICE_TLS_CERT", str(certfile))
        monkeypatch.setenv("SERVICE_TLS_KEY", str(keyfile))

        server = translate_server._make_server(0, host="127.0.0.1")
        try:
            assert isinstance(server.socket, ssl.SSLSocket)
        finally:
            server.server_close()

    def test_plain_http_without_tls_settings(self, monkeypatch):
        monkeypatch.delenv("SERVICE_TLS_CERT", raising=False)
        monkeypatch.delenv("SERVICE_TLS_KEY", raising=False)

        server = translate_server._make_server(0, host="127.0.0.1")
        try:
            assert not isinstance(server.socket, ssl.SSLSocket)
        finally:
            server.server_close()


def _tls_block() -> str:
    text = _ENTRYPOINT.read_text()
    start = text.index("# --- TLS for the internal ALB")
    end = text.index("# --- end TLS ---")
    return text[start:end]


class TestEntrypointTls:
    def test_certificate_is_made_before_any_facade_start(self):
        text = _ENTRYPOINT.read_text()
        first_start = text.index("exec python3 /app/translate-server.py")
        assert text.index("# --- end TLS ---") < first_start
        block = _tls_block()
        assert 'if [ "${SERVICE_TLS:-}" = "1" ]' in block
        assert "openssl req -x509" in block
        assert "export SERVICE_TLS_CERT=" in block and "SERVICE_TLS_KEY=" in block

    @pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not installed")
    def test_block_makes_a_certificate_and_key(self, tmp_path):
        script = _tls_block() + '\necho "$SERVICE_TLS_CERT $SERVICE_TLS_KEY"\n'
        out = subprocess.run(
            ["bash", "-c", script],
            env={"SERVICE_TLS": "1", "TLS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        cert, key = Path(out[-2]), Path(out[-1])
        assert cert.parent == tmp_path and cert.exists() and key.exists()
        x509.load_pem_x509_certificate(cert.read_bytes())

    def test_block_does_nothing_without_service_tls(self, tmp_path):
        script = _tls_block() + '\necho "cert=${SERVICE_TLS_CERT:-none}"\n'
        out = subprocess.run(
            ["bash", "-c", script],
            env={"TLS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "cert=none" in out
        assert list(tmp_path.iterdir()) == []


def test_dockerfile_installs_openssl():
    install = next(line for line in _DOCKERFILE.read_text().splitlines() if line.startswith("RUN yum install"))
    assert " openssl" in install
