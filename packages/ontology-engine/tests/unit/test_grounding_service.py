# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the centralized grounding service."""

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.grounding import (
    GroundingCandidate,
    GroundingRerankError,
    GroundingResult,
    GroundingService,
    _local_name,
    _normalize,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_catalog():
    oc = MagicMock()
    oc.search_embeddings.return_value = [
        {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo-agreements", "vector": [0.5] * 10},
        {"entity_uri": "http://fibo/CreditAgreement", "ontology_id": "fibo-agreements", "vector": [0.4] * 10},
        {"entity_uri": "http://fibo/ContractDocument", "ontology_id": "fibo-contracts", "vector": [0.3] * 10},
    ]
    return oc


@pytest.fixture
def mock_eg():
    return MagicMock()


@pytest.fixture
def service(mock_catalog, mock_eg):
    return GroundingService(
        ontology_catalog=mock_catalog,
        embedding_generator=mock_eg,
        llm_region="us-east-1",
        llm_model_id="us.anthropic.claude-haiku-4-5-20251001",
    )


class TestNormalize:
    def test_basic(self):
        assert _normalize("Agreement") == "agreement"
        assert _normalize("credit_agreement") == "creditagreement"
        assert _normalize("CreditAgreement") == "creditagreement"
        assert _normalize("party_role") == "partyrole"

    def test_strips_special_chars(self):
        assert _normalize("hello-world_123") == "helloworld123"


class TestLocalName:
    def test_hash_fragment(self):
        assert _local_name("http://ex.org/ont#Foo") == "Foo"

    def test_slash_path(self):
        assert _local_name("http://fibo/Agreements/Agreement") == "Agreement"


class TestGroundingModeNone:
    def test_returns_novel_immediately(self, service):
        result = service.ground_table(
            table_name="agreement",
            table_description="test",
            columns=[],
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="NONE",
        )
        assert result.match_type == "novel"
        assert result.mode == "NONE"
        assert result.candidates == []


class TestExactNameMatch:
    def test_exact_match_auto_grounds_in_standard_mode(self, service, mock_catalog):
        # In STANDARD mode (no LLM), exact-name match auto-grounds
        result = service.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo-agreements"],
        )
        assert result.match_type == "exact"
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.confidence == 1.0
        assert result.relationship == "exactMatch"
        assert result.reason.startswith("Exact name match")

    def test_exact_match_defers_to_llm_in_enhanced_mode(self, service, mock_catalog):
        # In ENHANCED mode, exact-name matches still go through LLM for domain verification
        llm_response = {
            "choice": "Agreement",
            "relationship": "exactMatch",
            "confidence": 0.95,
            "reason": "Table represents agreements matching FIBO Agreement",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response):
            result = service.ground_table(
                table_name="agreement",
                table_description="insurance agreements",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.mode == "ENHANCED"

    def test_no_exact_match_when_names_differ(self, service, mock_catalog):
        # "policy" doesn't match "Agreement" / "CreditAgreement" / "ContractDocument"
        with patch.object(
            service, "_llm_rerank", return_value={"choice": "NONE", "confidence": 0, "reason": "no match"}
        ):
            result = service.ground_table(
                table_name="policy",
                table_description="insurance policies",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.match_type == "novel"


class TestStandardMode:
    def test_uses_embedding_score_no_llm(self, service, mock_catalog):
        with patch.object(service, "_llm_rerank") as mock_llm:
            result = service.ground_table(
                table_name="policy",
                table_description="test",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="STANDARD",
                ontology_ids=["fibo-agreements"],
            )
            mock_llm.assert_not_called()
        # Cosine of [0.3]*10 vs [0.5]*10 is < 0.95, check it classified
        assert result.mode == "STANDARD"
        assert result.match_type in ("exact", "high_confidence", "ambiguous", "novel")


class TestEnhancedMode:
    def test_llm_picks_correct_candidate(self, service):
        llm_response = {
            "choice": "Agreement",
            "relationship": "exactMatch",
            "confidence": 0.92,
            "reason": "Table represents agreements matching FIBO Agreement",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response):
            # Use a name that won't trigger exact-name-match (so LLM runs)
            result = service.ground_table(
                table_name="agr_records",
                table_description="insurance agreement records",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.relationship == "exactMatch"
        assert result.confidence == 0.92
        assert result.mode == "ENHANCED"

    def test_llm_abstains(self, service):
        llm_response = {
            "choice": "NONE",
            "relationship": "none",
            "confidence": 0.0,
            "reason": "No candidate matches insurance claims",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response):
            result = service.ground_table(
                table_name="claim",
                table_description="insurance claims",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.match_type == "novel"
        assert result.chosen is None
        assert "No candidate matches" in result.reason

    def test_llm_rerank_failure_propagates_not_novel(self, service):
        # A rerank INFRASTRUCTURE failure raises GroundingRerankError; it must
        # propagate so the induction job fails loud, NOT degrade to a silent
        # novel result (the enhanced-grounding defect, issue #59). Contrast
        # test_llm_abstains above: a genuine {"choice": "NONE"} abstention still
        # yields novel.
        with (
            patch.object(service, "_llm_rerank", side_effect=GroundingRerankError("bedrock exploded")),
            pytest.raises(GroundingRerankError),
        ):
            service.ground_table(
                table_name="occurrence",
                table_description="events",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )


class TestToConceptMatch:
    def test_conversion(self, service):
        candidate = GroundingCandidate(
            entity_uri="http://fibo/Agreement",
            ontology_id="fibo-agreements",
            label="Agreement",
            definition="a negotiated understanding",
            lexical_sim=0.51,
            rerank_score=0.92,
        )
        result = GroundingResult(
            source_table="agreement",
            source_column="",
            candidates=[candidate],
            chosen=candidate,
            match_type="exact",
            relationship="exactMatch",
            confidence=0.92,
            reason="direct match",
            mode="ENHANCED",
        )
        cm = service.to_concept_match(result)
        assert cm.source_table == "agreement"
        assert cm.matched_class_uri == "http://fibo/Agreement"
        assert cm.match_type == "exact"
        assert cm.similarity == 0.92
        assert cm.scoring_strategy == "grounding_enhanced"
        assert len(cm.candidates) == 1


class TestNoCandidates:
    def test_empty_search_returns_novel(self, service, mock_catalog):
        mock_catalog.search_embeddings.return_value = []
        result = service.ground_table(
            table_name="xyz",
            table_description="",
            columns=[],
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
        )
        assert result.match_type == "novel"
        assert "No candidates" in result.reason


class TestEmptyGroundingScope:
    """An empty ``ontology_ids`` pool (``[]`` or ``None``) means the caller
    resolved a grounding pool and it is empty (user picked no ontologies and the
    namespace has no accepted induced ontologies). Recall must return no
    candidates — NOT fall back to a namespace-wide search that would match a
    loaded-but-unselected ontology. There is no unscoped/global recall.
    """

    def test_empty_list_returns_novel_without_searching(self, service, mock_catalog):
        result = service.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=[],  # explicit empty scope
        )
        assert result.match_type == "novel"
        # The empty-scope guard short-circuits before any embedding search.
        mock_catalog.search_embeddings.assert_not_called()

    def test_empty_list_on_ground_class_returns_novel(self, service, mock_catalog):
        result = service.ground_class(
            class_label="agreement",
            class_description="a mutual understanding",
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=[],
        )
        assert result.match_type == "novel"
        mock_catalog.search_embeddings.assert_not_called()

    def test_none_treated_as_empty_scope_returns_novel(self, service, mock_catalog):
        # ``None`` is an empty grounding pool, identical to ``[]``: recall
        # returns no candidates and the subject is novel. There is no
        # unscoped/global recall to fall back to.
        result = service.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=None,
        )
        assert result.match_type == "novel"
        mock_catalog.search_embeddings.assert_not_called()


class TestGroundClass:
    """ground_class() shares the ground_table() pipeline but frames the LLM
    reranker as class-vs-class (used by the unstructured induction path)."""

    def test_grounds_a_class_via_shared_pipeline(self, service, mock_catalog):
        llm_response = {
            "choice": "Agreement",
            "relationship": "exactMatch",
            "confidence": 0.95,
            "reason": "The class IS a FIBO Agreement",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response) as mock_rerank:
            result = service.ground_class(
                class_label="agreement",
                class_description="a mutual understanding between parties",
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        # ground_class must pass the class-flavored system prompt + subject kind.
        _, kwargs = mock_rerank.call_args
        assert kwargs["subject_kind"] == "class"
        assert "source ontology class" in kwargs["rerank_system"].lower()
        # And it always calls with empty columns (a class has none).
        called_columns = mock_rerank.call_args[0][2]
        assert called_columns == []

    def test_none_mode_short_circuits(self, service):
        result = service.ground_class(
            class_label="agreement",
            class_description="",
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="NONE",
        )
        assert result.match_type == "novel"

    def test_class_rerank_prompt_omits_columns_section(self, service, mock_catalog):
        # Drive the REAL _llm_rerank (patching only bedrock.converse) to verify
        # the class prompt has no "Columns:" block and labels the source a CLASS.
        captured = {}

        def fake_converse(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"][0]["text"]
            captured["system"] = kwargs["system"][0]["text"]
            return {
                "output": {
                    "message": {
                        "content": [
                            {"text": '{"choice": "NONE", "relationship": "none", "confidence": 0.0, "reason": "x"}'}
                        ]
                    }
                }
            }

        with patch.object(type(service), "bedrock", property(lambda self: MagicMock(converse=fake_converse))):
            service.ground_class(
                class_label="agreement",
                class_description="a mutual understanding",
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert "SOURCE CLASS:" in captured["prompt"]
        assert "Columns:" not in captured["prompt"]
        assert "source ontology class" in captured["system"].lower()


def _fake_bedrock_converse(usage: dict, choice: str = "NONE"):
    """A fake bedrock client whose converse returns a valid JSON body + usage."""
    body = f'{{"choice": "{choice}", "relationship": "none", "confidence": 0.0, "reason": "x"}}'

    def converse(**_kwargs):
        return {"output": {"message": {"content": [{"text": body}]}}, "usage": usage}

    return MagicMock(converse=converse)


class TestCostTrackerIntegration:
    """The rerank converse call records token usage when a CostTracker is
    injected, and is a no-op (no error) when the tracker is None."""

    def test_rerank_records_tokens_when_tracker_present(self, mock_catalog, mock_eg):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        service = GroundingService(
            ontology_catalog=mock_catalog,
            embedding_generator=mock_eg,
            llm_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cost_tracker=tracker,
        )
        fake = _fake_bedrock_converse({"inputTokens": 42, "outputTokens": 13})
        with patch.object(type(service), "bedrock", property(lambda self: fake)):
            # Scope grounding to a non-empty pool so recall returns candidates, and
            # use a name that does NOT exact-match a candidate so the LLM rerank runs.
            service.ground_table(
                table_name="agr_records",
                table_description="insurance agreement records",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert tracker.input_tokens_by_stage == {"rerank": 42}
        assert tracker.output_tokens_by_stage == {"rerank": 13}
        assert tracker.rerank_invocations == 1
        # rerank latency is captured (a StatisticSet with one sample).
        stats = tracker.rerank_latency_stats
        assert stats is not None and stats["SampleCount"] == 1.0

    def test_no_tracker_records_nothing_and_does_not_error(self, mock_catalog, mock_eg):
        service = GroundingService(
            ontology_catalog=mock_catalog,
            embedding_generator=mock_eg,
            cost_tracker=None,
        )
        fake = _fake_bedrock_converse({"inputTokens": 42, "outputTokens": 13})
        with patch.object(type(service), "bedrock", property(lambda self: fake)):
            result = service.ground_table(
                table_name="agr_records",
                table_description="insurance agreement records",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        # LLM abstained (choice NONE) → novel, and no crash without a tracker.
        assert result.match_type == "novel"


class TestModelResolution:
    """The rerank model and region come from deployment config, not a hard-coded
    Claude model in us-east-1 (Claude is not available in every target region)."""

    @pytest.fixture
    def env(self, monkeypatch):
        for var in ("GROUNDING_LLM_MODEL_ID", "BEDROCK_CHAT_MODEL_ID", "LLM_REGION"):
            monkeypatch.delenv(var, raising=False)
        return monkeypatch

    def test_defaults_to_shared_constant(self, env, mock_catalog, mock_eg):
        from coa_common.constants import DEFAULT_LLM_MODEL_ID

        svc = GroundingService(ontology_catalog=mock_catalog, embedding_generator=mock_eg)
        assert svc._llm_model_id == DEFAULT_LLM_MODEL_ID

    def test_env_resolution_order(self, env, mock_catalog, mock_eg):
        env.setenv("BEDROCK_CHAT_MODEL_ID", "amazon.nova-pro-v1:0")
        env.setenv("LLM_REGION", "us-gov-west-1")
        svc = GroundingService(ontology_catalog=mock_catalog, embedding_generator=mock_eg)
        assert svc._llm_model_id == "amazon.nova-pro-v1:0"
        assert svc._llm_region == "us-gov-west-1"
        env.setenv("GROUNDING_LLM_MODEL_ID", "some.rerank-model")
        svc = GroundingService(ontology_catalog=mock_catalog, embedding_generator=mock_eg)
        assert svc._llm_model_id == "some.rerank-model"

    def test_explicit_args_win(self, env, mock_catalog, mock_eg):
        env.setenv("GROUNDING_LLM_MODEL_ID", "some.rerank-model")
        svc = GroundingService(
            ontology_catalog=mock_catalog,
            embedding_generator=mock_eg,
            llm_model_id="explicit.model",
            llm_region="eu-west-1",
        )
        assert svc._llm_model_id == "explicit.model"
        assert svc._llm_region == "eu-west-1"
