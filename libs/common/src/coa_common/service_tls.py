"""TLS context for calls to COA's own services behind the internal ALB.

With ``service_discovery=alb``, ontology-engine and VKG are reached over HTTPS
through an internal ALB. Where its certificate is not publicly trusted, CDK
tells the callers what to trust:

* AWS Private CA (the default): the CA's root certificate is in SSM, named by
  ``SERVICE_CA_CERT_PARAM``.
* A certificate imported into ACM (e.g. the client's own): its ACM ARN is in
  ``SERVICE_CERT_ARN``, and the CA chain imported with it is read from ACM.

Callers pass :func:`ssl_context` to urllib (``context=``) or httpx
(``verify=``): it trusts the public roots plus those CA certificates.

Without either variable (Cloud Map mode, or a public ACM certificate) the
function returns ``None`` and callers keep their default verification.
"""

from __future__ import annotations

import functools
import os
import ssl

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import resolve_region

CA_CERT_PARAM_ENV = "SERVICE_CA_CERT_PARAM"
CERT_ARN_ENV = "SERVICE_CERT_ARN"


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext | None:
    """Return a TLS context that trusts the services' CA, or ``None`` if there is none.

    The CA certificates are read from SSM or ACM once per process.

    Returns:
        An ``ssl.SSLContext`` (default public roots plus the CA certificates),
        or ``None`` when neither ``SERVICE_CA_CERT_PARAM`` nor
        ``SERVICE_CERT_ARN`` is set.

    Raises:
        RuntimeError: If the CA certificates are configured but cannot be read.
            Failing loudly beats a confusing certificate error on every call.
    """
    param = os.environ.get(CA_CERT_PARAM_ENV, "")
    if param:
        try:
            resp = boto3.client("ssm", region_name=resolve_region()).get_parameter(Name=param)
        except (BotoCoreError, ClientError) as e:
            raise RuntimeError(f"Cannot read the private CA root certificate from SSM {param}: {e}") from e
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=resp["Parameter"]["Value"])
        return ctx
    arn = os.environ.get(CERT_ARN_ENV, "")
    if arn:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=_imported_chain(arn))
        # The chain may stop at an issuing CA without its root; trust it as is.
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return ctx
    return None


def _imported_chain(arn: str) -> str:
    """Return the CA chain imported into ACM with the certificate ``arn`` (PEM).

    Args:
        arn: ACM certificate ARN. ACM is called in the ARN's region.

    Returns:
        The PEM CA certificates.

    Raises:
        RuntimeError: If ACM cannot be read or the certificate has no chain.
    """
    parts = arn.split(":")
    region = parts[3] if len(parts) > 3 and parts[3] else resolve_region()
    try:
        resp = boto3.client("acm", region_name=region).get_certificate(CertificateArn=arn)
    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"Cannot read the certificate {arn} from ACM: {e}") from e
    chain = resp.get("CertificateChain", "")
    if not chain.strip():
        raise RuntimeError(
            f"Certificate {arn} has no certificate chain in ACM; "
            "re-import it with --certificate-chain (the CA certificates that signed it)"
        )
    return chain
