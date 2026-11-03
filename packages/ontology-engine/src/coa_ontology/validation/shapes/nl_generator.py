# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Natural language → ConstraintConfig generation via LLM.

Two modes:
  1. infer_constraints: LLM reads the ontology and proposes semantic constraints
  2. translate_rule: single NL rule → PropertyConstraint entry
"""

import logging
import os
import re

import boto3
from coa_common.bedrock import DEFAULT_MODEL_ID, BedrockClient
from coa_common.config import resolve_region
from coa_common.guardrail_metrics import COMPONENT_ONTOLOGY_SHAPES

from coa_ontology.validation.shapes.config import (
    ClassConstraints,
    ConstraintConfig,
    ConstraintSource,
    PropertyConstraint,
)

log = logging.getLogger(__name__)

_GUARDRAIL_SSM_PARAM = os.getenv("GUARDRAIL_SSM_PARAM", "")
_AWS_REGION = resolve_region()


def _default_bedrock_region() -> str:
    """Region for the Bedrock client and its guardrail decision metrics.

    The ontology ECS task sets BEDROCK_REGION/LLM_REGION but NOT AWS_REGION, so
    defaulting to ``resolve_region()`` alone would publish guardrail metrics to
    us-east-1 while the LLM call ran in the deployed region. Mirrors the
    precedence in ``main.py``'s ``bedrock_region`` config entry.
    """
    return os.getenv("BEDROCK_REGION") or os.getenv("LLM_REGION") or resolve_region()


def _resolve_guardrail_id() -> str | None:
    """Resolve the Bedrock guardrail ID from SSM at runtime; None if unavailable.

    Mirrors the enrichment path's resolver so the ontology inference call gets
    the same content-interception layer.
    """
    if not _GUARDRAIL_SSM_PARAM:
        return None
    try:
        ssm = boto3.client("ssm", region_name=_AWS_REGION)
        return ssm.get_parameter(Name=_GUARDRAIL_SSM_PARAM)["Parameter"]["Value"]
    except Exception:
        log.warning("Failed to resolve guardrail from SSM %s — proceeding without", _GUARDRAIL_SSM_PARAM, exc_info=True)
        return None


# Matches opening OR closing <document> / <document_content> tags,
# case-insensitively and tolerant of surrounding whitespace (e.g.
# "</DOCUMENT_CONTENT>", "</document_content >", "< document >"). Stripping both
# open and close prevents an injected tag from breaking out of — or forging a
# nested — untrusted data block.
_DOC_TAG_RE = re.compile(r"<\s*/?\s*document(?:_content)?\s*>", re.IGNORECASE)


def _defang(text: str) -> str:
    """Neutralize the XML content-block delimiters in untrusted text.

    Strips the delimiters so an injected tag cannot break out of the
    <document_content> data block.
    """
    return _DOC_TAG_RE.sub("", text)


_INFER_SYSTEM = """\
You are a SHACL domain constraint expert. Given an OWL ontology in Turtle, \
propose DOMAIN-SPECIFIC constraints that the database schema cannot express.

DO NOT propose:
- "required" / "unique" — already auto-generated from NOT NULL / UNIQUE / PK
- "reference" / referential integrity — already auto-generated from FK relationships
- "datatype" — already auto-generated from column types
These are handled by the system automatically. Your job is ONLY domain logic.

FOCUS ON these constraint types:
- positive: numeric values with business minimums (e.g., premium > 0, age >= 18)
- temporal: date ordering rules (e.g., effective_date < expiration_date)
- pattern: string format rules (e.g., policy_number matches "POL-[0-9]{6}")
- enum: value must be one of a known set (e.g., status in [active, expired, cancelled])
- cross_field: relationship between two properties on the same class
- custom: any domain rule not covered above (describe in natural language)

Output ONLY valid JSON — an array of objects:
[
  {
    "class_name": "Policy",
    "class_uri": "http://example.org/ind#Policy",
    "constraints": [
      {
        "property_path": "http://example.org/ind#policy_premiumAmount",
        "property_name": "premiumAmount",
        "constraint_type": "positive",
        "params": {"min_exclusive": 0},
        "description": "premium amount must be greater than zero"
      },
      {
        "property_path": "http://example.org/ind#policy_effectiveDate",
        "property_name": "effectiveDate",
        "constraint_type": "custom",
        "params": {},
        "description": "effective date must be before expiration date"
      }
    ]
  }
]

Rules:
- 2-5 constraints per class max
- Only propose what the SCHEMA CANNOT express (no NOT NULL, no FK, no type rules)
- Think like a domain expert: what business rules would a data steward enforce?
- Output valid JSON only, no markdown
"""


def infer_constraints_from_ontology(
    ontology_turtle: str,
    existing_config: ConstraintConfig | None = None,
    region: str | None = None,
    model_id: str = DEFAULT_MODEL_ID,
    client: BedrockClient | None = None,
) -> ConstraintConfig:
    """LLM reads the ontology and proposes semantic constraints beyond DB schema.

    Uses the shared ``BedrockClient`` (lib/common) for the LLM call — it parses
    the JSON response and strips markdown fences. ``client`` is injectable for
    testing.

    ``region`` defaults to the task's real Bedrock region (see
    :func:`_default_bedrock_region`) rather than a hardcoded us-east-1 — the sole
    production caller passes none, and the region also routes this call's
    guardrail decision metrics.
    """
    if len(ontology_turtle) > 8000:
        truncated = ontology_turtle[:8000]
        last_stmt = truncated.rfind(" .\n")
        context = truncated[: last_stmt + 3] if last_stmt > 0 else truncated
    else:
        context = ontology_turtle

    existing_note = ""
    if existing_config and existing_config.classes:
        existing_names = [
            f"{_defang(cls.class_name)}: {', '.join(_defang(c.description) for c in cls.constraints[:3])}"
            for cls in existing_config.classes[:5]
        ]
        existing_note = "\n\nAlready enforced (do NOT duplicate these):\n" + "\n".join(f"- {n}" for n in existing_names)

    # SECURITY: the ontology Turtle (and stored constraint descriptions) are
    # user-controlled data. Wrap them in a <document_content> block so the model
    # treats them as data, not instructions, and defang any embedded closing tag
    # so injected content cannot break out of the block into an instruction
    # position. The trailing directive stays outside the untrusted block.
    prompt = (
        "<document>\n"
        "<source>ontology_turtle</source>\n"
        "<document_content>\n"
        f"{_defang(context)}\n"
        "</document_content>\n"
        "</document>"
        f"{existing_note}\n\n"
        "Propose semantic constraints (JSON):"
    )

    bedrock = client or BedrockClient(
        region=region or _default_bedrock_region(),
        model_id=model_id,
        guardrail_id=_resolve_guardrail_id(),
        component=COMPONENT_ONTOLOGY_SHAPES,
    )
    try:
        # 8192 leaves room for reasoning models (GPT-5.6 Luna), which spend part
        # of maxTokens on reasoning before the answer.
        data = bedrock.invoke(_INFER_SYSTEM, prompt, max_tokens=8192)
        classes = []
        for cls_data in data.result:
            constraints = [
                PropertyConstraint(
                    property_path=c["property_path"],
                    property_name=c["property_name"],
                    constraint_type=c.get("constraint_type", "custom"),
                    params=c.get("params", {}),
                    source=ConstraintSource.LLM_INFERRED,
                    description=c["description"],
                    enabled=True,
                )
                for c in cls_data.get("constraints", [])
            ]
            classes.append(
                ClassConstraints(
                    class_uri=cls_data["class_uri"],
                    class_name=cls_data["class_name"],
                    constraints=constraints,
                )
            )
        return ConstraintConfig(classes=classes)

    except Exception as e:
        log.error("Constraint inference failed: %s", e)
        raise RuntimeError(f"Constraint inference failed: {e}") from e
