# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for per-model output-token ceilings."""

from __future__ import annotations

import pytest
from coa_common.model_limits import clamp_max_tokens, max_output_tokens


@pytest.mark.unit
class TestModelLimits:
    @pytest.mark.parametrize(
        "model_id",
        [
            "amazon.nova-pro-v1:0",
            "us.amazon.nova-pro-v1:0",
            "arn:aws-us-gov:bedrock:us-gov-west-1::foundation-model/amazon.nova-pro-v1:0",
        ],
    )
    def test_nova_pro_ceiling_in_every_id_form(self, model_id, monkeypatch):
        monkeypatch.delenv("BEDROCK_MAX_OUTPUT_TOKENS", raising=False)
        assert max_output_tokens(model_id) == 10_000
        assert clamp_max_tokens(model_id, 16384) == 10_000
        assert clamp_max_tokens(model_id, 4096) == 4096

    def test_unknown_model_is_not_clamped(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MAX_OUTPUT_TOKENS", raising=False)
        assert max_output_tokens("some.other-model") is None
        assert clamp_max_tokens("some.other-model", 64000) == 64000
        assert clamp_max_tokens(None, 512) == 512

    def test_env_override_applies_to_any_model(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_OUTPUT_TOKENS", "2048")
        assert clamp_max_tokens("some.other-model", 8192) == 2048

    @pytest.mark.parametrize("raw", ["", "0", "-5", "abc"])
    def test_invalid_env_override_is_ignored(self, raw, monkeypatch):
        monkeypatch.setenv("BEDROCK_MAX_OUTPUT_TOKENS", raw)
        assert clamp_max_tokens("amazon.nova-pro-v1:0", 16384) == 10_000
