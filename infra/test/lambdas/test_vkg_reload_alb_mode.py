# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG reload Lambda with the internal ALB instead of Cloud Map.

With ``SERVICE_DISCOVERY_BACKEND=alb`` a new namespace's VKG service is reached
through the shared internal ALB: the Lambda creates a target group and a
listener rule matching the ``X-Coa-Namespace`` header, then creates the ECS
service attached to that target group. Cloud Map is never called.

Loads the same ``index`` module as test_vkg_reload_handler.py (see conftest.py).
"""

import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda" / "vkg-reload"))

LISTENER_ARN = "arn:aws:elasticloadbalancing:us-west-2:111:listener/app/coa-svc/abc/def"

ENV_ALB = {
    "CLUSTER_ARN": "arn:aws:ecs:us-west-2:111:cluster/vkg",
    "RESOURCE_PREFIX": "coa-dev",
    "VKG_TASK_ROLE_ARN": "arn:aws:iam::111:role/task",
    "VKG_EXECUTION_ROLE_ARN": "arn:aws:iam::111:role/exec",
    "ONTOLOGY_BUCKET": "ontology-bucket",
    "PRIVATE_SUBNET_IDS": "subnet-a,subnet-b",
    "ECS_SECURITY_GROUP_ID": "sg-123",
    "AWS_REGION": "us-west-2",
    "VKG_IMAGE_PARAM_NAME": "/coa/vkg/container-image",
    "SERVICE_DISCOVERY_BACKEND": "alb",
    "VKG_LISTENER_ARN": LISTENER_ARN,
    "VPC_ID": "vpc-123",
}

NS = "550e8400-e29b-41d4-a716-446655440000"


def _expected_tg_name(prefix: str, ns: str) -> str:
    # Must match cleanup.py's lookup in packages/control-plane (namespace delete).
    return "vkg-" + hashlib.sha256(f"{prefix}:{ns}".encode()).hexdigest()[:28]


def _header_rule(ns: str, priority: str, tg_arn: str) -> dict:
    return {
        "RuleArn": f"arn:rule:{ns}",
        "Priority": priority,
        "Conditions": [
            {"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "X-Coa-Namespace", "Values": [ns]}}
        ],
        "Actions": [{"Type": "forward", "TargetGroupArn": tg_arn}],
    }


def _mocks():
    ecs = MagicMock()
    ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
    ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
    ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
    ecs.describe_services.return_value = {"services": []}
    ecs.update_service.side_effect = ecs.exceptions.ServiceNotFoundException("not found")
    ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}

    elbv2 = MagicMock()
    for name in ("PriorityInUseException", "TooManyTargetGroupsException", "TooManyRulesException"):
        setattr(elbv2.exceptions, name, type(name, (Exception,), {}))
    elbv2.describe_rules.return_value = {
        "Rules": [
            {"RuleArn": "arn:rule:default", "Priority": "default", "Conditions": [], "Actions": []},
            _header_rule("other-ns", "3", "arn:tg:other"),
        ]
    }
    elbv2.create_target_group.return_value = {"TargetGroups": [{"TargetGroupArn": "arn:tg:new"}]}
    elbv2.create_rule.return_value = {"Rules": [{"RuleArn": "arn:rule:new"}]}

    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
    sd = MagicMock()
    return ecs, elbv2, ssm, sd


def _load(ecs, elbv2, ssm, sd):
    import importlib

    import index

    importlib.reload(index)
    index.ecs = ecs
    index.elbv2 = elbv2
    index.ssm = ssm
    index.sd = sd
    index.autoscaling = MagicMock()
    return index


def _event(ns=NS):
    return {"detail": {"namespace": ns, "version": "v1"}}


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail fast if anything tries a real DNS lookup (all AWS clients are mocked).

    The handler builds real boto3 clients at import; fake credentials and no
    instance-metadata lookup keep that from reaching 169.254.169.254.
    """
    import socket

    import boto3

    # boto3's shared default session caches credentials. Start each test with a
    # fresh one and restore the previous one afterwards, so the fake credentials
    # below never reach later tests (where they would turn an unmocked client's
    # instant NoCredentialsError into a real network call).
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)

    def _blocked(host, *args, **kwargs):
        raise RuntimeError(f"unit test attempted a real network lookup: {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


class TestTargetGroupName:
    def test_fits_the_32_character_limit_for_a_uuid_namespace(self):
        index = _load(*_mocks())
        name = index._target_group_name("coa-dev", NS)
        assert len(name) <= 32
        assert name == _expected_tg_name("coa-dev", NS)

    def test_differs_by_namespace_and_by_prefix(self):
        index = _load(*_mocks())
        assert index._target_group_name("coa-dev", "ns-a") != index._target_group_name("coa-dev", "ns-b")
        assert index._target_group_name("coa-dev", "ns-a") != index._target_group_name("coa-prod", "ns-a")


class TestProvisionInAlbMode:
    @patch.dict(os.environ, ENV_ALB, clear=False)
    def test_creates_target_group_rule_and_load_balanced_service(self):
        os.environ.pop("CLOUD_MAP_NAMESPACE_ID", None)
        ecs, elbv2, ssm, sd = _mocks()
        index = _load(ecs, elbv2, ssm, sd)

        result = index.handler(_event(), None)

        assert result["status"] == "provisioned"
        tg_kwargs = elbv2.create_target_group.call_args.kwargs
        assert tg_kwargs["Name"] == _expected_tg_name("coa-dev", NS)
        assert tg_kwargs["Protocol"] == "HTTP"
        assert tg_kwargs["Port"] == 8080
        assert tg_kwargs["VpcId"] == "vpc-123"
        assert tg_kwargs["TargetType"] == "ip"
        assert tg_kwargs["HealthCheckPath"] == "/health"
        assert {"Key": "coa:namespace", "Value": NS} in tg_kwargs["Tags"]

        elbv2.modify_target_group_attributes.assert_called_once_with(
            TargetGroupArn="arn:tg:new",
            Attributes=[{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}],
        )

        rule_kwargs = elbv2.create_rule.call_args.kwargs
        assert rule_kwargs["ListenerArn"] == LISTENER_ARN
        assert rule_kwargs["Priority"] == 4  # highest existing (3) + 1
        assert rule_kwargs["Conditions"] == [
            {"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "X-Coa-Namespace", "Values": [NS]}}
        ]
        assert rule_kwargs["Actions"] == [{"Type": "forward", "TargetGroupArn": "arn:tg:new"}]

        svc_kwargs = ecs.create_service.call_args.kwargs
        assert svc_kwargs["loadBalancers"] == [
            {"targetGroupArn": "arn:tg:new", "containerName": "ontop", "containerPort": 8080}
        ]
        assert svc_kwargs["healthCheckGracePeriodSeconds"] >= 180
        assert "serviceRegistries" not in svc_kwargs
        sd.create_service.assert_not_called()

    @patch.dict(os.environ, ENV_ALB, clear=False)
    def test_reuses_the_existing_rule_for_the_namespace(self):
        ecs, elbv2, ssm, sd = _mocks()
        elbv2.describe_rules.return_value = {"Rules": [_header_rule(NS, "7", "arn:tg:existing")]}
        index = _load(ecs, elbv2, ssm, sd)

        result = index.handler(_event(), None)

        assert result["status"] == "provisioned"
        elbv2.create_rule.assert_not_called()
        elbv2.create_target_group.assert_not_called()
        assert ecs.create_service.call_args.kwargs["loadBalancers"][0]["targetGroupArn"] == "arn:tg:existing"

    @patch.dict(os.environ, ENV_ALB, clear=False)
    def test_retries_with_the_next_priority_when_one_is_taken(self):
        ecs, elbv2, ssm, sd = _mocks()
        elbv2.create_rule.side_effect = [
            elbv2.exceptions.PriorityInUseException("taken"),
            {"Rules": [{"RuleArn": "arn:rule:new"}]},
        ]
        index = _load(ecs, elbv2, ssm, sd)

        result = index.handler(_event(), None)

        assert result["status"] == "provisioned"
        priorities = [c.kwargs["Priority"] for c in elbv2.create_rule.call_args_list]
        assert priorities[0] < priorities[1]

    @patch.dict(os.environ, ENV_ALB, clear=False)
    def test_quota_hit_fails_with_a_clear_reason_and_creates_no_service(self):
        ecs, elbv2, ssm, sd = _mocks()
        elbv2.create_target_group.side_effect = elbv2.exceptions.TooManyTargetGroupsException("limit")
        index = _load(ecs, elbv2, ssm, sd)
        emitted = []
        index._emit_metric = lambda name, ns: emitted.append(name)

        result = index.handler(_event(), None)

        assert result["status"] == "failed"
        assert "quota" in result["reason"].lower()
        assert "ReloadFailed" in emitted
        ecs.create_service.assert_not_called()

    @patch.dict(os.environ, {**ENV_ALB, "VKG_LISTENER_ARN": ""}, clear=False)
    def test_provision_skipped_without_the_listener_arn(self):
        ecs, elbv2, ssm, sd = _mocks()
        index = _load(ecs, elbv2, ssm, sd)

        result = index.handler(_event(), None)

        assert result["status"] == "skipped"
        assert "env vars" in result["reason"]
        elbv2.create_target_group.assert_not_called()


class TestDefaultModeUnchanged:
    @patch.dict(
        os.environ,
        {**{k: v for k, v in ENV_ALB.items() if k != "SERVICE_DISCOVERY_BACKEND"}, "CLOUD_MAP_NAMESPACE_ID": "ns-1"},
        clear=False,
    )
    def test_without_the_flag_uses_cloud_map_and_no_alb(self):
        os.environ.pop("SERVICE_DISCOVERY_BACKEND", None)
        ecs, elbv2, ssm, sd = _mocks()
        sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})
        index = _load(ecs, elbv2, ssm, sd)

        result = index.handler(_event(), None)

        assert result["status"] == "provisioned"
        sd.create_service.assert_called_once()
        elbv2.create_target_group.assert_not_called()
        assert ecs.create_service.call_args.kwargs["serviceRegistries"] == [{"registryArn": "arn:sd:svc"}]
