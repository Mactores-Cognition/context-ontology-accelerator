# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-model output-token ceilings for Bedrock text models.

Call sites size ``max_tokens`` for their task (up to 16k for enrichment and
document extraction, up to 64k for Tier-3 synthesis). Bedrock rejects a request
whose ``maxTokens`` exceeds the model's own output limit with a
``ValidationException``, so a budget sized for one model family fails outright on
another. :func:`clamp_max_tokens` lowers a request to the model's ceiling so the
same call sites work across families.

Clamping can still truncate a long answer; call sites keep detecting
``stopReason == "max_tokens"`` as before.
"""

from __future__ import annotations

import os

# Substring of the model id (bare id, geo profile or ARN) -> max output tokens,
# from the Bedrock model cards. Only models with a published ceiling below the
# budgets COA requests need an entry; unknown models are left unclamped.
_OUTPUT_TOKEN_CEILINGS: tuple[tuple[str, int], ...] = (
    ("amazon.nova-pro", 10_000),  # Nova Pro model card: "Max output tokens: 10K"
)

# Deployment override for any model, e.g. a newer model whose ceiling is not in
# the table above. Positive integer; anything else is ignored.
_OVERRIDE_ENV = "BEDROCK_MAX_OUTPUT_TOKENS"


def max_output_tokens(model_id: str | None) -> int | None:
    """Return the output-token ceiling for ``model_id``, or None when unknown."""
    raw = os.environ.get(_OVERRIDE_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    if not model_id:
        return None
    lowered = model_id.lower()
    for fragment, ceiling in _OUTPUT_TOKEN_CEILINGS:
        if fragment in lowered:
            return ceiling
    return None


def clamp_max_tokens(model_id: str | None, requested: int) -> int:
    """Lower ``requested`` to the model's output ceiling when one is known."""
    ceiling = max_output_tokens(model_id)
    if ceiling is None:
        return requested
    return min(requested, ceiling)
