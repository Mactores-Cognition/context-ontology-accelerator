"""TLS context for calls to COA's own services behind the internal ALB.

With the internal ALB and AWS Private CA (service_cert=private-ca), the
ALB's certificate is signed by a private CA whose root certificate CDK writes
to SSM. Callers get its name in ``SERVICE_CA_CERT_PARAM`` and verify the ALB
against the public roots plus that CA. With the client's own certificate
(service_cert=imported) callers get its ACM ARN in ``SERVICE_CERT_ARN`` and
trust the CA chain imported with it. Without either variable (Cloud Map mode,
or a public ACM certificate) callers keep their default verification.
"""

from __future__ import annotations

import datetime
import os
import socket
import ssl
import threading

import boto3
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from moto import mock_aws

pytestmark = pytest.mark.unit

_PARAM = "/coa-test/services/ca-cert"
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture(autouse=True)
def _isolated_aws(monkeypatch):
    """Fake credentials, no real network lookups, and a fresh cache per test."""
    real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            raise RuntimeError(f"unit test attempted a real network lookup: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    }.items():
        monkeypatch.setenv(key, value)
    from coa_common import service_tls

    service_tls.ssl_context.cache_clear()
    yield
    service_tls.ssl_context.cache_clear()


def _ca_pem(common_name: str = "coa-test root CA") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_no_parameter_means_default_verification(monkeypatch):
    monkeypatch.delenv("SERVICE_CA_CERT_PARAM", raising=False)
    from coa_common.service_tls import ssl_context

    assert ssl_context() is None


def test_context_trusts_the_private_ca(monkeypatch):
    monkeypatch.setenv("SERVICE_CA_CERT_PARAM", _PARAM)
    with mock_aws():
        boto3.client("ssm").put_parameter(Name=_PARAM, Value=_ca_pem(), Type="String")
        from coa_common.service_tls import ssl_context

        ctx = ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
    assert {"commonName": "coa-test root CA"} in subjects


def test_parameter_is_read_once(monkeypatch):
    monkeypatch.setenv("SERVICE_CA_CERT_PARAM", _PARAM)
    with mock_aws():
        ssm = boto3.client("ssm")
        ssm.put_parameter(Name=_PARAM, Value=_ca_pem(), Type="String")
        from coa_common.service_tls import ssl_context

        first = ssl_context()
        ssm.delete_parameter(Name=_PARAM)  # a second read would now fail
        second = ssl_context()

    assert first is second


def test_missing_parameter_fails_loudly(monkeypatch):
    monkeypatch.setenv("SERVICE_CA_CERT_PARAM", _PARAM)
    with mock_aws():
        from coa_common.service_tls import ssl_context

        with pytest.raises(RuntimeError, match="services/ca-cert"):
            ssl_context()


# ── The client's own certificate, by ACM ARN (service_cert=imported) ─────────

_HOST = "ontology-engine.coa.example.com"


def _issue(common_name, *, issuer=None, ca, san=None):
    """Return (key, certificate); self-signed when no issuer is given."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer_key, issuer_name = (key, name) if issuer is None else (issuer[0], issuer[1].subject)
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
    return key, builder.sign(issuer_key, hashes.SHA256())


def _pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _client_pki():
    """Root -> issuing CA -> leaf for _HOST, like a client's internal tool."""
    root = _issue("client root CA", ca=True)
    issuing = _issue("client issuing CA", issuer=root, ca=True)
    leaf = _issue(_HOST, issuer=issuing, ca=False, san=_HOST)
    return root, issuing, leaf


def _import(leaf, chain_pem, region="us-east-1") -> str:
    kwargs = {"Certificate": _pem(leaf[1]), "PrivateKey": _key_pem(leaf[0])}
    if chain_pem:
        kwargs["CertificateChain"] = chain_pem
    return boto3.client("acm", region_name=region).import_certificate(**kwargs)["CertificateArn"]


def test_certificate_arn_trusts_the_chain_imported_with_it(monkeypatch):
    monkeypatch.delenv("SERVICE_CA_CERT_PARAM", raising=False)
    root, issuing, leaf = _client_pki()
    with mock_aws():
        monkeypatch.setenv("SERVICE_CERT_ARN", _import(leaf, _pem(issuing[1]) + _pem(root[1])))
        from coa_common.service_tls import ssl_context

        ctx = ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
    assert {"commonName": "client root CA"} in subjects
    assert {"commonName": "client issuing CA"} in subjects
    assert {"commonName": _HOST} not in subjects  # the leaf itself is never a trust anchor


def test_certificate_arn_reads_acm_in_the_certificate_region(monkeypatch):
    # AWS_REGION is us-east-1 (fixture); the certificate lives in eu-west-1.
    monkeypatch.delenv("SERVICE_CA_CERT_PARAM", raising=False)
    root, issuing, leaf = _client_pki()
    with mock_aws():
        monkeypatch.setenv("SERVICE_CERT_ARN", _import(leaf, _pem(issuing[1]), region="eu-west-1"))
        from coa_common.service_tls import ssl_context

        assert ssl_context() is not None


def test_certificate_arn_without_a_chain_fails_loudly(monkeypatch):
    # ACM returns no CertificateChain for a certificate imported without one.
    # moto returns a built-in Amazon root instead, so fake that one response.
    from coa_common import service_tls

    _root, _issuing, leaf = _client_pki()

    class _Acm:
        def get_certificate(self, CertificateArn):  # noqa: N803 - boto3 argument name
            return {"Certificate": _pem(leaf[1])}

    monkeypatch.delenv("SERVICE_CA_CERT_PARAM", raising=False)
    monkeypatch.setenv(
        "SERVICE_CERT_ARN", "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
    )
    monkeypatch.setattr(service_tls.boto3, "client", lambda service, region_name=None: _Acm())

    with pytest.raises(RuntimeError, match="certificate chain"):
        service_tls.ssl_context()


def test_unknown_certificate_arn_fails_loudly(monkeypatch):
    monkeypatch.delenv("SERVICE_CA_CERT_PARAM", raising=False)
    arn = "arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555"
    monkeypatch.setenv("SERVICE_CERT_ARN", arn)
    with mock_aws():
        from coa_common.service_tls import ssl_context

        with pytest.raises(RuntimeError, match=arn):
            ssl_context()


def test_certificate_arn_handshake_with_only_the_issuing_ca_in_the_chain(monkeypatch, tmp_path):
    # Chain without the root: the issuing CA alone is enough to verify the
    # server (the context accepts an intermediate as the trust anchor).
    monkeypatch.delenv("SERVICE_CA_CERT_PARAM", raising=False)
    _root, issuing, leaf = _client_pki()
    with mock_aws():
        monkeypatch.setenv("SERVICE_CERT_ARN", _import(leaf, _pem(issuing[1])))
        from coa_common.service_tls import ssl_context

        client_ctx = ssl_context()

    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_text(_pem(leaf[1]) + _pem(issuing[1]))
    keyfile.write_text(_key_pem(leaf[0]))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile, keyfile)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)

    def serve() -> None:
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as tls:
            tls.sendall(b"x")  # the client reads it before closing

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    with (
        socket.create_connection(listener.getsockname(), timeout=10) as raw,
        client_ctx.wrap_socket(raw, server_hostname=_HOST) as tls,
    ):
        peer = dict(x[0] for x in tls.getpeercert()["subject"])
        assert tls.recv(1) == b"x"
    thread.join(timeout=10)
    listener.close()

    assert peer == {"commonName": _HOST}
