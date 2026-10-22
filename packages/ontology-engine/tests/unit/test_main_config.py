# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for main.py's app.state.config defaults.

Specifically regression-protects the URLs that are used for internal HTTP
self-calls (e.g. the validation routine fetches ontology turtle via the
public /ontologies/ endpoint). Pre-consolidation defaults pointed at
Docker-Compose hostnames (`http://ontology-catalog:8000`,
`http://embedding-generator:8000`) which produced DNS failures on every
demo run post-MR-!39.
"""

import importlib

import pytest

pytestmark = pytest.mark.unit


def _reload_main():
    """Re-import coa_ontology.main picking up current env vars.

    The module builds app.state.config at import time, so we must purge any
    cached import to let os.getenv() see the current environment.
    """
    import coa_ontology.main as mod

    return importlib.reload(mod)


@pytest.fixture
def clean_env(monkeypatch):
    """Unset every env var main.py consults so we see pure defaults."""
    for var in (
        "DATA_CATALOG_URL",
        "EMBEDDING_GENERATOR_URL",
        "ONTOLOGY_CATALOG_URL",
        "NEPTUNE_ANALYTICS_URL",
        "LLM_MODEL_ID",
        "LLM_REGION",
        "BEDROCK_REGION",
        "BEDROCK_EMBED_MODEL_ID",
        "DESCRIPTION_LLM_MODEL_ID",
        "GROUNDING_LLM_MODEL_ID",
        "BEDROCK_CHAT_MODEL_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


class TestConfigDefaults:
    def test_ontology_catalog_url_defaults_to_localhost_8001(self, clean_env):
        """Regression test — pre-fix default was 'http://ontology-catalog:8000'
        which caused DNS failures in the consolidated deployment. Must stay on
        a localhost default so internal HTTP self-calls resolve.
        """
        mod = _reload_main()
        assert mod.app.state.config["ontology_catalog_url"] == "http://localhost:8001"

    def test_embedding_generator_url_defaults_to_localhost_8001(self, clean_env):
        """Same consolidation-leftover issue — old default was
        'http://embedding-generator:8000'.
        """
        mod = _reload_main()
        assert mod.app.state.config["embedding_generator_url"] == "http://localhost:8001"

    def test_data_catalog_url_defaults_to_localhost_8003(self, clean_env):
        """data-catalog is legitimately a separate service (mock fixture
        service); keep default at :8003 which is what config-example.yaml
        and the demo runbook both use.
        """
        mod = _reload_main()
        assert mod.app.state.config["data_catalog_url"] == "http://localhost:8003"


class TestConfigOverrides:
    def test_ontology_catalog_url_override(self, clean_env):
        clean_env.setenv("ONTOLOGY_CATALOG_URL", "https://prod-catalog.example.com")
        mod = _reload_main()
        assert mod.app.state.config["ontology_catalog_url"] == "https://prod-catalog.example.com"

    def test_embedding_generator_url_override(self, clean_env):
        clean_env.setenv("EMBEDDING_GENERATOR_URL", "https://prod-embed.example.com")
        mod = _reload_main()
        assert mod.app.state.config["embedding_generator_url"] == "https://prod-embed.example.com"

    def test_llm_model_id_override(self, clean_env):
        clean_env.setenv("LLM_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
        mod = _reload_main()
        assert mod.app.state.config["llm_model_id"] == "us.anthropic.claude-sonnet-4-6"

    def test_llm_model_ids_default_to_shared_constant(self, clean_env):
        from coa_common.constants import DEFAULT_LLM_MODEL_ID

        mod = _reload_main()
        cfg = mod.app.state.config
        assert cfg["llm_model_id"] == DEFAULT_LLM_MODEL_ID
        assert cfg["description_llm_model_id"] == DEFAULT_LLM_MODEL_ID
        assert cfg["grounding_llm_model_id"] == DEFAULT_LLM_MODEL_ID

    def test_grounding_model_follows_chat_model_then_explicit_override(self, clean_env):
        clean_env.setenv("BEDROCK_CHAT_MODEL_ID", "amazon.nova-pro-v1:0")
        assert _reload_main().app.state.config["grounding_llm_model_id"] == "amazon.nova-pro-v1:0"
        clean_env.setenv("GROUNDING_LLM_MODEL_ID", "some.rerank-model")
        assert _reload_main().app.state.config["grounding_llm_model_id"] == "some.rerank-model"
