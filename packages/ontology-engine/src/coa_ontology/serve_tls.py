# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run ontology-engine with HTTPS for the internal ALB (``service_discovery=alb``).

Started by the Dockerfile only when ``SERVICE_TLS_PORT`` is set. One process,
one app, two servers:

* HTTPS on ``0.0.0.0:$SERVICE_TLS_PORT`` for the ALB, with a self-signed
  certificate made at start (the ALB does not validate target certificates);
* plain HTTP on ``127.0.0.1:$PORT`` for the engine's own calls to
  ``http://localhost:8001`` and the container health check. It never leaves
  the container.

App startup and shutdown (lifespan) run once, on the HTTP server.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

APP = "coa_ontology.main:app"
_CERT_DAYS = 3650


def make_self_signed(directory: Path, common_name: str = "ontology-engine") -> tuple[Path, Path]:
    """Write a self-signed certificate and its private key for the ALB leg.

    Args:
        directory: Where to write ``cert.pem`` and ``key.pem``.
        common_name: Certificate subject CN.

    Returns:
        The certificate and key paths. The key file is readable by the owner only.
    """
    directory.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(common_name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = directory / "cert.pem", directory / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.touch(mode=0o600)
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    keyfile.chmod(0o600)
    return certfile, keyfile


def build_configs(app: str, *, http_port: int, tls_port: int, certfile: Path, keyfile: Path) -> list[uvicorn.Config]:
    """Build the two server configs: HTTP on localhost, HTTPS for the ALB.

    Args:
        app: ASGI app import string.
        http_port: Port for plain HTTP on 127.0.0.1.
        tls_port: Port for HTTPS on all interfaces.
        certfile: Certificate for the HTTPS server.
        keyfile: Private key for the HTTPS server.

    Returns:
        ``[http_config, https_config]``.
    """
    http = uvicorn.Config(app, host="127.0.0.1", port=http_port, lifespan="on")
    https = uvicorn.Config(
        app,
        host="0.0.0.0",  # noqa: S104 — container port, reached only through the ALB security group
        port=tls_port,
        ssl_certfile=str(certfile),
        ssl_keyfile=str(keyfile),
        lifespan="off",
    )
    return [http, https]


async def _serve(configs: list[uvicorn.Config]) -> None:
    await asyncio.gather(*(uvicorn.Server(config).serve() for config in configs))


def main() -> None:
    """Start both servers from ``PORT`` and ``SERVICE_TLS_PORT``."""
    certfile, keyfile = make_self_signed(Path(os.environ.get("SERVICE_TLS_DIR", "/tmp/tls")))  # noqa: S108
    configs = build_configs(
        APP,
        http_port=int(os.environ.get("PORT", "8001")),
        tls_port=int(os.environ["SERVICE_TLS_PORT"]),
        certfile=certfile,
        keyfile=keyfile,
    )
    asyncio.run(_serve(configs))


if __name__ == "__main__":
    main()
