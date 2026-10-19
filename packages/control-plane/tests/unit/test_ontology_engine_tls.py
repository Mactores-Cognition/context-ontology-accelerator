"""Control-plane calls to ontology-engine verify its certificate.

Behind the internal ALB (HTTPS, AWS Private CA) the namespace API's
active-jobs check and the deletion pipeline's ``DELETE /ontologies`` call pass
the TLS context from ``coa_common.service_tls`` to urllib. With no private CA
the context is ``None`` and urllib keeps its default verification.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_ENDPOINT = "https://ontology-engine.coa-test.internal:8001"


def _response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = lambda self: io.BytesIO(json.dumps(body).encode())
    resp.__exit__ = lambda self, *a: False
    return resp


@patch.dict("os.environ", {"ONTOLOGY_ENGINE_ENDPOINT": _ENDPOINT})
def test_active_jobs_check_uses_the_service_tls_context():
    from coa_control_plane.namespace import delete_handler

    ctx = object()
    with (
        patch.object(delete_handler, "ssl_context", return_value=ctx),
        patch.object(delete_handler.urllib_request, "urlopen", return_value=_response({"activeJobs": 0})) as urlopen,
    ):
        delete_handler._check_active_induction_jobs("ns-1")

    assert urlopen.call_args.kwargs["context"] is ctx


@patch.dict("os.environ", {"ONTOLOGY_ENGINE_ENDPOINT": _ENDPOINT})
def test_delete_all_ontologies_uses_the_service_tls_context():
    from coa_control_plane.namespace import cleanup

    ctx = object()
    with (
        patch.object(cleanup, "ssl_context", return_value=ctx),
        patch.object(cleanup.urllib_request, "urlopen", return_value=_response({"graphsDropped": 0})) as urlopen,
    ):
        cleanup.delete_all_ontologies("ns-1")

    assert urlopen.call_args.kwargs["context"] is ctx
