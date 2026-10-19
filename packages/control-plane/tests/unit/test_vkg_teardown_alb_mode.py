"""VKG teardown on namespace delete with the internal ALB instead of Cloud Map.

With ``SERVICE_DISCOVERY_BACKEND=alb`` the reload Lambda routes a namespace's
VKG service through a listener rule (header ``X-Coa-Namespace``) and a target
group. ``teardown_vkg_service`` must remove both after deleting the ECS service,
and never call Cloud Map. Best-effort, like the Cloud Map path: it logs and
does not raise.
"""

from __future__ import annotations

import hashlib
import socket
from unittest.mock import MagicMock, patch

import pytest

LISTENER_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:listener/app/coa-svc/abc/def"

ENV_ALB = {
    "AWS_REGION": "us-east-1",
    "RESOURCE_PREFIX": "coa-dev-",
    "VKG_CLUSTER_ARN": "arn:aws:ecs:us-east-1:111:cluster/vkg",
    "SERVICE_DISCOVERY_BACKEND": "alb",
    "VKG_LISTENER_ARN": LISTENER_ARN,
    "NAMESPACES_TABLE": "test-ns-table",
}


def _expected_tg_name(prefix: str, ns: str) -> str:
    # Must match the reload Lambda (infra/lambda/vkg-reload/index.py).
    return "vkg-" + hashlib.sha256(f"{prefix}:{ns}".encode()).hexdigest()[:28]


def _rule(ns: str, tg_arn: str) -> dict:
    return {
        "RuleArn": f"arn:rule:{ns}",
        "Priority": "5",
        "Conditions": [
            {"Field": "http-header", "HttpHeaderConfig": {"HttpHeaderName": "X-Coa-Namespace", "Values": [ns]}}
        ],
        "Actions": [{"Type": "forward", "TargetGroupArn": tg_arn}],
    }


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Fail fast if anything tries a real DNS lookup (boto3 is mocked)."""

    def _blocked(host, *args, **kwargs):
        raise RuntimeError(f"unit test attempted a real network lookup: {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


def _clients():
    ecs = MagicMock()
    ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
    elbv2 = MagicMock()
    elbv2.exceptions.ResourceInUseException = type("ResourceInUseException", (Exception,), {})
    elbv2.exceptions.TargetGroupNotFoundException = type("TargetGroupNotFoundException", (Exception,), {})
    elbv2.describe_rules.return_value = {
        "Rules": [
            {"RuleArn": "arn:rule:default", "Priority": "default", "Conditions": [], "Actions": []},
            _rule("other-ns", "arn:tg:other"),
            _rule("test-ns", "arn:tg:test"),
        ]
    }
    requested: list[str] = []

    def client_fn(service, **kw):
        requested.append(service)
        return {"ecs": ecs, "elbv2": elbv2}.get(service, MagicMock())

    return ecs, elbv2, client_fn, requested


@pytest.mark.unit
class TestTeardownVkgServiceAlbMode:
    @patch.dict("os.environ", ENV_ALB)
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_deletes_service_then_rule_then_target_group(self, mock_boto3):
        ecs, elbv2, client_fn, requested = _clients()
        mock_boto3.client.side_effect = client_fn
        from coa_control_plane.namespace.cleanup import teardown_vkg_service

        teardown_vkg_service("test-ns")

        ecs.delete_service.assert_called_once_with(
            cluster=ENV_ALB["VKG_CLUSTER_ARN"], service="coa-dev-vkg-test-ns", force=True
        )
        elbv2.describe_rules.assert_called_with(ListenerArn=LISTENER_ARN)
        elbv2.delete_rule.assert_called_once_with(RuleArn="arn:rule:test-ns")
        elbv2.delete_target_group.assert_called_once_with(TargetGroupArn="arn:tg:test")
        assert "servicediscovery" not in requested

    @patch.dict("os.environ", ENV_ALB)
    @patch("coa_control_plane.namespace.cleanup.time")
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_retries_target_group_delete_while_in_use(self, mock_boto3, mock_time):
        ecs, elbv2, client_fn, _ = _clients()
        elbv2.delete_target_group.side_effect = [
            elbv2.exceptions.ResourceInUseException("in use"),
            elbv2.exceptions.ResourceInUseException("in use"),
            None,
        ]
        mock_boto3.client.side_effect = client_fn
        from coa_control_plane.namespace.cleanup import teardown_vkg_service

        teardown_vkg_service("test-ns")

        assert elbv2.delete_target_group.call_count == 3
        assert mock_time.sleep.call_count == 2

    @patch.dict("os.environ", ENV_ALB)
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_without_a_rule_finds_the_target_group_by_name(self, mock_boto3):
        ecs, elbv2, client_fn, _ = _clients()
        elbv2.describe_rules.return_value = {"Rules": []}
        elbv2.describe_target_groups.return_value = {"TargetGroups": [{"TargetGroupArn": "arn:tg:orphan"}]}
        mock_boto3.client.side_effect = client_fn
        from coa_control_plane.namespace.cleanup import teardown_vkg_service

        teardown_vkg_service("test-ns")

        elbv2.delete_rule.assert_not_called()
        elbv2.describe_target_groups.assert_called_once_with(Names=[_expected_tg_name("coa-dev", "test-ns")])
        elbv2.delete_target_group.assert_called_once_with(TargetGroupArn="arn:tg:orphan")

    @patch.dict("os.environ", ENV_ALB)
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_nothing_to_delete_does_not_raise(self, mock_boto3):
        ecs, elbv2, client_fn, _ = _clients()
        elbv2.describe_rules.return_value = {"Rules": []}
        elbv2.describe_target_groups.side_effect = elbv2.exceptions.TargetGroupNotFoundException("none")
        mock_boto3.client.side_effect = client_fn
        from coa_control_plane.namespace.cleanup import teardown_vkg_service

        teardown_vkg_service("test-ns")

        elbv2.delete_target_group.assert_not_called()

    @patch.dict("os.environ", ENV_ALB)
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_elb_errors_are_logged_not_raised(self, mock_boto3):
        ecs, elbv2, client_fn, _ = _clients()
        elbv2.describe_rules.side_effect = RuntimeError("throttled")
        mock_boto3.client.side_effect = client_fn
        from coa_control_plane.namespace.cleanup import teardown_vkg_service

        teardown_vkg_service("test-ns")  # must not raise
