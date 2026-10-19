"""TLS context for calls to COA's own services behind the internal ALB.

With ``service_discovery=alb`` and AWS Private CA (the default certificate
source), ontology-engine and VKG are reached over HTTPS through an internal ALB
whose certificate is signed by a private CA. CDK writes the CA's root
certificate to SSM and passes the parameter name in ``SERVICE_CA_CERT_PARAM``.
Callers pass :func:`ssl_context` to urllib (``context=``) or httpx
(``verify=``): it trusts the public roots plus that CA.

Without the variable (Cloud Map mode, or a public ACM certificate) the
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


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext | None:
    """Return a TLS context that trusts the private CA, or ``None`` if there is none.

    The CA root is read from SSM once per process.

    Returns:
        An ``ssl.SSLContext`` (default public roots plus the private CA), or
        ``None`` when ``SERVICE_CA_CERT_PARAM`` is not set.

    Raises:
        RuntimeError: If the parameter is set but cannot be read. Failing loudly
            beats a confusing certificate error on every call.
    """
    param = os.environ.get(CA_CERT_PARAM_ENV, "")
    if not param:
        return None
    try:
        resp = boto3.client("ssm", region_name=resolve_region()).get_parameter(Name=param)
    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"Cannot read the private CA root certificate from SSM {param}: {e}") from e
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=resp["Parameter"]["Value"])
    return ctx
