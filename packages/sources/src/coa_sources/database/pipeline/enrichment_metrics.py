# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""EMF metric emitter for the enrichment pipeline.

Provides an ``EnrichmentContext`` to accumulate data during processing and
an ``EnrichmentMetricEmitter`` that knows how to turn that data into
CloudWatch EMF metrics via the base ``emit_metric`` helper.
"""

from __future__ import annotations

import json
import logging

from botocore.exceptions import ClientError
from coa_common.bedrock import DEFAULT_MODEL_ID, BedrockTruncationError
from coa_common.bedrock_metrics import cost_for

from coa_sources.database.metrics import emit_metric

logger = logging.getLogger(__name__)


_THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException"}
_VALIDATION_CODES = {"ValidationException", "ModelNotReadyException"}
_GUARDRAIL_CODES = {"AccessDeniedException", "ModelErrorException"}


class EnrichmentContext:
    """Accumulates data gathered during the enrichment process.

    Constructed from the DynamoDB source item; derives metric dimensions
    (engine) and accumulates token usage during processing.
    """

    def __init__(self, source_item: dict, namespace_id: str) -> None:
        """Build the context from a source DDB item.

        Args:
            source_item: The source's DynamoDB record; the engine dimension is
                derived from its configuration.
            namespace_id: Namespace the source belongs to, used as a dimension.
        """
        self.namespace_id = namespace_id
        self.engine = _resolve_engine(source_item)


def _resolve_engine(source_item: dict) -> str:
    """Derive engine label from a source DDB record.

    JDBC sources store an ``engine`` field in their configuration JSON
    (e.g. "postgresql", "mysql"). Glue sources have no engine field and
    default to "glue".
    """
    raw_config = source_item.get("configuration", "{}")
    try:
        parsed_config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    except json.JSONDecodeError:
        logger.warning("Failed to parse source configuration JSON, defaulting engine to 'glue'", exc_info=True)
        return "glue"
    engine = parsed_config.get("engine")
    return str(engine).lower() if engine else "glue"


class BedrockTokenUsageCounter:
    """Tracks cumulative Bedrock token usage for cost estimation."""

    def __init__(self, model_id: str = DEFAULT_MODEL_ID) -> None:
        """Initialize the running input/output token totals to zero.

        Args:
            model_id: Model the calls are billed against. Defaults to the
                enrichment model (``BEDROCK_CHAT_MODEL_ID``); priced through
                ``coa_common.bedrock_metrics.cost_for`` instead of fixed rates.
        """
        self.model_id = model_id
        self.input_tokens = 0
        self.output_tokens = 0

    def record_call(self, input_tokens: int, output_tokens: int) -> None:
        """Add one Bedrock call's token counts to the running totals.

        Args:
            input_tokens: Input (prompt) tokens consumed by the call.
            output_tokens: Output (completion) tokens produced by the call.
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    @property
    def estimated_calls_cost_usd(self) -> float:
        """Estimated USD cost of all recorded calls at the enrichment model's rates."""
        return cost_for(self.model_id, self.input_tokens, self.output_tokens)


class EnrichmentMetricEmitter:
    """Emits enrichment metrics using data from an EnrichmentContext."""

    def __init__(self, context: EnrichmentContext) -> None:
        """Bind the emitter to a context and start a fresh token counter.

        Args:
            context: Enrichment context supplying the Engine/NamespaceId
                dimensions attached to every emitted metric.
        """
        self._ctx = context
        self._token_counter = BedrockTokenUsageCounter()

    def emit_metric(self, name: str, value: float, unit: str = "None", **extra_dims: str) -> None:
        """Emit a metric with Engine and NamespaceId dimensions included."""
        emit_metric(name, value, unit, Engine=self._ctx.engine, NamespaceId=self._ctx.namespace_id, **extra_dims)

    def emit_bedrock_invocation_metrics(
        self, *, stage: str, latency_ms: float, input_tokens: int, output_tokens: int
    ) -> None:
        """Emit per-Bedrock-call metrics and accumulate tokens for cost."""
        self._token_counter.record_call(input_tokens, output_tokens)
        self.emit_metric("BedrockInvocationLatencyMs", latency_ms, "Milliseconds", Stage=stage)
        self.emit_metric("BedrockInputTokens", input_tokens, "Count", Stage=stage)
        self.emit_metric("BedrockOutputTokens", output_tokens, "Count", Stage=stage)

    def emit_bedrock_invocation_error(self, *, stage: str, exc: Exception) -> None:
        """Emit BedrockInvocationErrors with ErrorType dimension."""
        error_type = _classify_bedrock_error(exc)
        self.emit_metric("BedrockInvocationErrors", 1, "Count", Stage=stage, ErrorType=error_type)

    def emit_enrichment_estimated_cost(self) -> None:
        """Emit total estimated cost computed from accumulated token counts."""
        self.emit_metric("EnrichmentEstimatedCostUsd", self._token_counter.estimated_calls_cost_usd)


def _classify_bedrock_error(exc: Exception) -> str:
    """Classify a Bedrock error into a metric ErrorType dimension value."""
    if isinstance(exc, BedrockTruncationError):
        return "Truncated"
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _THROTTLE_CODES:
            return "Throttling"
        if code in _VALIDATION_CODES:
            return "Validation"
        if code in _GUARDRAIL_CODES:
            return "Guardrail"
    return "Other"
