# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""TLS context for calls to COA's own services behind the internal ALB.

With the internal ALB and AWS Private CA (service_cert=private-ca), the
ALB's certificate is signed by a private CA whose root certificate CDK writes
to SSM. Callers get its name in ``SERVICE_CA_CERT_PARAM`` and verify the ALB
against the public roots plus that CA. Without the variable (Cloud Map mode,
or a public ACM certificate) callers keep their default verification.
"""

from __future__ import annotations

import datetime
import os
import socket
import ssl

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
