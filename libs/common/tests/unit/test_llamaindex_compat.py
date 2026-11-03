# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for registering new Bedrock models with LlamaIndex BedrockConverse."""

from __future__ import annotations

import sys
import types

import pytest
from coa_common.llamaindex_compat import base_model_name, register_bedrock_converse_model


@pytest.fixture
def fake_llamaindex(monkeypatch):
    """Stand-in for llama_index.llms.bedrock_converse.{utils,base} with its model tables."""
    utils = types.ModuleType("llama_index.llms.bedrock_converse.utils")
    utils.BEDROCK_MODELS = {"amazon.nova-pro-v1:0": 300000}
    utils.BEDROCK_INFERENCE_PROFILE_SUPPORTED_MODELS = ("amazon.nova-pro-v1:0",)
    utils.BEDROCK_FUNCTION_CALLING_MODELS = ("amazon.nova-pro-v1:0",)
    utils.BEDROCK_NO_TEMP_MODELS = ("anthropic.claude-sonnet-5",)
    base = types.ModuleType("llama_index.llms.bedrock_converse.base")
    base.BEDROCK_NO_TEMP_MODELS = utils.BEDROCK_NO_TEMP_MODELS
    pkg = types.ModuleType("llama_index.llms.bedrock_converse")
    pkg.utils, pkg.base = utils, base
    for name, module in (
        ("llama_index", types.ModuleType("llama_index")),
        ("llama_index.llms", types.ModuleType("llama_index.llms")),
        ("llama_index.llms.bedrock_converse", pkg),
        ("llama_index.llms.bedrock_converse.utils", utils),
        ("llama_index.llms.bedrock_converse.base", base),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return utils, base


@pytest.mark.unit
class TestBaseModelName:
    @pytest.mark.parametrize(
        "model_id",
        [
            "openai.gpt-5.6-luna",
            "us.openai.gpt-5.6-luna",
            "global.openai.gpt-5.6-luna",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.openai.gpt-5.6-luna",
            "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-5.6-luna",
        ],
    )
    def test_every_id_form_resolves_to_the_bare_name(self, model_id):
        assert base_model_name(model_id) == "openai.gpt-5.6-luna"


@pytest.mark.unit
class TestRegisterBedrockConverseModel:
    def test_registers_unknown_openai_model_without_temperature(self, fake_llamaindex):
        utils, base = fake_llamaindex
        assert register_bedrock_converse_model("us.openai.gpt-5.6-terra") is True
        assert "openai.gpt-5.6-terra" in utils.BEDROCK_MODELS
        assert "openai.gpt-5.6-terra" in utils.BEDROCK_INFERENCE_PROFILE_SUPPORTED_MODELS
        assert "openai.gpt-5.6-terra" in utils.BEDROCK_FUNCTION_CALLING_MODELS
        # base.py reads its own import-time binding of the tuple.
        assert "openai.gpt-5.6-terra" in utils.BEDROCK_NO_TEMP_MODELS
        assert "openai.gpt-5.6-terra" in base.BEDROCK_NO_TEMP_MODELS

    def test_known_model_is_left_untouched(self, fake_llamaindex):
        utils, base = fake_llamaindex
        before = dict(utils.BEDROCK_MODELS), base.BEDROCK_NO_TEMP_MODELS
        assert register_bedrock_converse_model("us.amazon.nova-pro-v1:0") is False
        assert (dict(utils.BEDROCK_MODELS), base.BEDROCK_NO_TEMP_MODELS) == before

    def test_unknown_non_openai_model_keeps_temperature(self, fake_llamaindex):
        utils, base = fake_llamaindex
        assert register_bedrock_converse_model("us.example.new-model-v1:0") is True
        assert "example.new-model-v1:0" in utils.BEDROCK_MODELS
        assert "example.new-model-v1:0" not in base.BEDROCK_NO_TEMP_MODELS

    def test_registering_twice_is_a_no_op(self, fake_llamaindex):
        utils, _ = fake_llamaindex
        assert register_bedrock_converse_model("us.openai.gpt-5.6-luna") is True
        assert register_bedrock_converse_model("us.openai.gpt-5.6-luna") is False
        assert utils.BEDROCK_INFERENCE_PROFILE_SUPPORTED_MODELS.count("openai.gpt-5.6-luna") == 1

    @pytest.mark.parametrize("model_id", [None, ""])
    def test_empty_model_id_is_ignored(self, model_id):
        assert register_bedrock_converse_model(model_id) is False

    def test_missing_llamaindex_is_a_no_op(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "llama_index.llms.bedrock_converse", None)
        assert register_bedrock_converse_model("us.openai.gpt-5.6-luna") is False
