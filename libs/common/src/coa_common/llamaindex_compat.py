# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Teach LlamaIndex's BedrockConverse about Bedrock models it does not list yet.

graphrag-toolkit builds every LLM it uses (document extraction in kg-build, the
serve lexical-baseline retriever) as ``llama_index.llms.bedrock_converse.
BedrockConverse``, which validates the model id against hard-coded tables:

* a geo-prefixed id (``us.openai.gpt-5.6-luna``, or an inference-profile ARN
  ending in one) whose base model is not in
  ``BEDROCK_INFERENCE_PROFILE_SUPPORTED_MODELS`` raises ``ValueError`` before
  any request is made;
* an id missing from ``BEDROCK_MODELS`` raises ``ValueError`` from the
  ``metadata`` property (context window lookup);
* ``temperature`` is always sent unless the id matches
  ``BEDROCK_NO_TEMP_MODELS`` — and the toolkit hard-codes ``temperature=0.0``.

OpenAI GPT-5.6 Luna/Terra are absent from llama-index-llms-bedrock-converse
0.15.x and reject ``temperature`` with a ValidationException, so without this
every extraction and lexical-baseline call fails. :func:`register_bedrock_converse_model`
adds a model to those tables in place. It is a no-op for models LlamaIndex
already knows (their upstream entries stay authoritative) and when LlamaIndex
is not installed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Geo prefixes LlamaIndex strips from inference-profile ids (utils.get_model_name).
_GEO_PREFIXES = ("us.", "us-gov.", "eu.", "apac.", "jp.", "global.", "ca.", "au.")

# Base-model prefixes known to reject ``temperature`` on Bedrock Converse
# ("This model doesn't support the temperature field"), verified 2026-11-03.
_NO_TEMPERATURE_PREFIXES = ("openai.gpt-5.", "openai.gpt-6")

# Context window registered for an unknown model. Only feeds LlamaIndex's
# prompt-sizing metadata; GPT-5.6 Luna/Terra both accept 131,072 output tokens.
_DEFAULT_CONTEXT_WINDOW = 128_000


def base_model_name(model_id: str) -> str:
    """Return the bare Bedrock model name for an id, geo profile or ARN."""
    name = model_id.rsplit("/", 1)[-1]
    for prefix in _GEO_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def register_bedrock_converse_model(model_id: str | None) -> bool:
    """Register ``model_id`` with LlamaIndex BedrockConverse if it is unknown.

    Returns True when the model was added, False when it was already known,
    empty, or LlamaIndex is not importable.
    """
    if not model_id:
        return False
    try:
        from llama_index.llms.bedrock_converse import base as bc_base
        from llama_index.llms.bedrock_converse import utils as bc_utils
    except ImportError:
        return False

    name = base_model_name(model_id)
    if name in bc_utils.BEDROCK_MODELS:
        return False

    bc_utils.BEDROCK_MODELS[name] = _DEFAULT_CONTEXT_WINDOW
    # setattr: the tables are fixed-length tuple literals, so a plain assignment of a
    # longer tuple does not type-check.
    for table in ("BEDROCK_INFERENCE_PROFILE_SUPPORTED_MODELS", "BEDROCK_FUNCTION_CALLING_MODELS"):
        setattr(bc_utils, table, (*getattr(bc_utils, table), name))
    no_temperature = name.startswith(_NO_TEMPERATURE_PREFIXES)
    if no_temperature:
        # base.py binds the tuple at import time, so both module attributes need it.
        extended = (*bc_utils.BEDROCK_NO_TEMP_MODELS, name)
        for module in (bc_utils, bc_base):
            module.BEDROCK_NO_TEMP_MODELS = extended  # type: ignore[attr-defined]
    logger.info(
        "llamaindex_bedrock_model_registered model=%s base=%s no_temperature=%s",
        model_id,
        name,
        no_temperature,
    )
    return True
