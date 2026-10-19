"""Deploy/destroy script behaviour for the DynamoDB metadata store.

With ``SCL_METADATA_STORE=dynamodb`` the platform deploys no DataZone domain:
NamespaceStack publishes the placeholder ``NO_DATAZONE_DOMAIN`` to
``/<prefix>/smus/domain-id`` instead of a real domain id. Two scripts must
follow suit:

- ``destroy.sh`` Step 5 force-deletes the DataZone domain whose id it reads from
  that parameter. It must skip the placeholder rather than call
  ``delete-domain`` on it, while still deleting a real domain.
- ``deploy.sh`` must pass the ``metadata_store`` CDK context and skip
  ``check-smus-admin-principal.sh``, which validates the IAM role that only the
  DataZone (SMUS) login role trusts.

Text-level checks in the style of test_destroy_sg_fallback_scope.py: they read
the scripts and never run them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_PLACEHOLDER = "NO_DATAZONE_DOMAIN"


def _destroy_step5() -> str:
    """Return the text of destroy.sh Step 5 (from its header to Step 6)."""
    text = (_SCRIPTS / "destroy.sh").read_text()
    match = re.search(r"# ── Step 5:.*?(?=# ── Step 6:)", text, re.DOTALL)
    assert match, "destroy.sh no longer has the expected Step 5 / Step 6 headers"
    return match.group(0)


def test_destroy_step5_skips_the_placeholder_domain_id() -> None:
    step5 = _destroy_step5()
    assert _PLACEHOLDER in step5, "Step 5 does not recognise the NO_DATAZONE_DOMAIN placeholder"
    # The placeholder check must come before the delete call, so it can skip it.
    assert step5.index(_PLACEHOLDER) < step5.index("aws datazone delete-domain")


def test_destroy_step5_still_deletes_a_real_domain() -> None:
    step5 = _destroy_step5()
    assert 'aws datazone delete-domain --identifier "$DOMAIN_ID"' in step5
    assert "--skip-deletion-check" in step5


def test_deploy_passes_metadata_store_context_and_skips_the_smus_admin_check() -> None:
    text = (_SCRIPTS / "deploy.sh").read_text()
    assert "SCL_METADATA_STORE" in text, "deploy.sh does not read SCL_METADATA_STORE"
    assert "--context metadata_store=" in text, "deploy.sh does not pass the metadata_store CDK context"

    lines = text.splitlines()
    calls = [
        i
        for i, line in enumerate(lines)
        if "check-smus-admin-principal.sh" in line and not line.lstrip().startswith("#")
    ]
    call = calls[0] if calls else None
    assert call is not None, "deploy.sh no longer calls check-smus-admin-principal.sh"
    # The call must be conditional on the metadata store: on its own line or in
    # the guard just above it.
    guard = "\n".join(lines[max(0, call - 5) : call + 1])
    assert "SCL_METADATA_STORE" in guard, "check-smus-admin-principal.sh is not skipped in DynamoDB mode"
