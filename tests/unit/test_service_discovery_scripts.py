# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deploy/destroy script behaviour for the internal ALB (service_discovery=alb).

With ``SCL_SERVICE_DISCOVERY=alb`` ontology-engine and the per-namespace VKG
services sit behind an internal ALB instead of Cloud Map:

- ``deploy.sh`` must pass the ``service_discovery`` CDK context.
- ``destroy.sh`` Step 4 must also remove what the VKG reload Lambda created at
  runtime in ALB mode (listener rules + target groups, which CloudFormation does
  not own), found through the ``/<prefix>/vkg/listener-arn`` parameter, while
  keeping the Cloud Map clean-up for default-mode deployments.

Text-level checks in the style of test_metadata_store_scripts.py: they read
the scripts and never run them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _destroy_step4() -> str:
    """Return the text of destroy.sh Step 4 (from its header to Step 5)."""
    text = (_SCRIPTS / "destroy.sh").read_text()
    match = re.search(r"# ── Step 4:.*?(?=# ── Step 5:)", text, re.DOTALL)
    assert match, "destroy.sh no longer has the expected Step 4 / Step 5 headers"
    return match.group(0)


def test_deploy_passes_the_service_discovery_context() -> None:
    text = (_SCRIPTS / "deploy.sh").read_text()
    assert "SCL_SERVICE_DISCOVERY" in text, "deploy.sh does not read SCL_SERVICE_DISCOVERY"
    assert "--context service_discovery=" in text, "deploy.sh does not pass the service_discovery CDK context"


def test_destroy_step4_removes_alb_rules_and_target_groups() -> None:
    step4 = _destroy_step4()
    assert "/vkg/listener-arn" in step4, "Step 4 does not read the VKG listener ARN from SSM"
    assert "aws elbv2 delete-rule" in step4
    assert "aws elbv2 delete-target-group" in step4
    # Rules first: a target group cannot be deleted while a rule forwards to it.
    assert step4.index("aws elbv2 delete-rule") < step4.index("aws elbv2 delete-target-group")


def test_destroy_step4_keeps_the_cloud_map_cleanup() -> None:
    step4 = _destroy_step4()
    assert "aws servicediscovery delete-service" in step4


def test_deploy_passes_the_https_certificate_settings() -> None:
    # HTTPS for the internal ALB (replace-cloud-map-notes.txt sections 14-15):
    # private CA by default, or a public ACM certificate for a delegated domain.
    text = (_SCRIPTS / "deploy.sh").read_text()
    for env_var, context_key in (
        ("SCL_SERVICE_CERT", "service_cert"),
        ("SCL_SERVICE_DOMAIN", "service_domain"),
        ("SCL_PUBLIC_ZONE_ID", "public_zone_id"),
    ):
        assert env_var in text, f"deploy.sh does not read {env_var}"
        assert f"--context {context_key}=" in text, f"deploy.sh does not pass the {context_key} CDK context"
