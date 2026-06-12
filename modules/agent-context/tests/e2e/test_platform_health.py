"""
E2E tests for agent-context platform deployment health.

Tests 1-6 from issue #21:
1. All expected Deployments have >= 1 ready replica
2. All expected Services resolve in-cluster DNS
3. All PVCs bound and sized as expected
4. CronJobs scheduled (ingestion-refresh daily @ 6am UTC)
5. No pods in CrashLoopBackOff, Error, or ImagePullBackOff
6. ServiceAccount agent-context-sa has IRSA annotation
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Expected resources (single source of truth)
# ---------------------------------------------------------------------------

EXPECTED_DEPLOYMENTS = [
    "litellm-proxy",
    "openviking-server",
    "deepwiki",
    "codegraph",
]

EXPECTED_SERVICES = {
    "openviking": 1933,
    "deepwiki": 8001,
    "litellm-proxy": 4000,
    "context-mcp": 5100,
}

EXPECTED_PVCS = {
    "openviking-data": "200Gi",
    "platform-data": None,  # S3/EFS — size varies
}


# ---------------------------------------------------------------------------
# Test 1: All expected Deployments have >= 1 ready replica
# ---------------------------------------------------------------------------


class TestDeployments:
    """Verify every expected Deployment has at least one ready replica."""

    @pytest.mark.parametrize("deploy_name", EXPECTED_DEPLOYMENTS)
    def test_deployment_has_ready_replicas(self, kube_client, deploy_name):
        deploy = kube_client.get_deployment(deploy_name)
        assert deploy is not None, f"Deployment {deploy_name} not found"
        ready = deploy.get("status", {}).get("readyReplicas", 0)
        assert ready >= 1, (
            f"Deployment {deploy_name} has {ready} ready replicas (expected >= 1)"
        )


# ---------------------------------------------------------------------------
# Test 2: All expected Services resolve in-cluster DNS
# ---------------------------------------------------------------------------


class TestServices:
    """Verify expected Services exist and expose the correct port."""

    @pytest.mark.parametrize("svc_name,expected_port", list(EXPECTED_SERVICES.items()))
    def test_service_exists_with_port(self, kube_client, svc_name, expected_port):
        svc = kube_client.get_service(svc_name)
        assert svc is not None, f"Service {svc_name} not found"
        ports = svc.get("spec", {}).get("ports", [])
        port_numbers = [p.get("port") for p in ports]
        assert expected_port in port_numbers, (
            f"Service {svc_name} expected port {expected_port}, found {port_numbers}"
        )


# ---------------------------------------------------------------------------
# Test 3: All PVCs bound and sized as expected
# ---------------------------------------------------------------------------


class TestPVCs:
    """Verify PVCs are Bound with expected sizes."""

    @pytest.mark.parametrize("pvc_name,expected_size", list(EXPECTED_PVCS.items()))
    def test_pvc_bound(self, kube_client, pvc_name, expected_size):
        pvc = kube_client.get_pvc(pvc_name)
        assert pvc is not None, f"PVC {pvc_name} not found"
        phase = pvc.get("status", {}).get("phase", "Unknown")
        assert phase == "Bound", f"PVC {pvc_name} phase is {phase}, expected Bound"

        if expected_size is not None:
            actual_size = (
                pvc.get("spec", {})
                .get("resources", {})
                .get("requests", {})
                .get("storage", "")
            )
            assert actual_size == expected_size, (
                f"PVC {pvc_name} size is {actual_size}, expected {expected_size}"
            )


# ---------------------------------------------------------------------------
# Test 4: CronJobs scheduled
# ---------------------------------------------------------------------------


class TestCronJobs:
    """Verify the ingestion-refresh CronJob exists and runs at 6am UTC."""

    def test_ingestion_refresh_cronjob_exists(self, kube_client):
        cj = kube_client.get_cronjob("ingestion-refresh")
        assert cj is not None, "CronJob ingestion-refresh not found"

    def test_ingestion_refresh_schedule(self, kube_client):
        cj = kube_client.get_cronjob("ingestion-refresh")
        assert cj is not None, "CronJob ingestion-refresh not found"
        schedule = cj.get("spec", {}).get("schedule", "")
        assert schedule == "0 6 * * *", (
            f"CronJob schedule is '{schedule}', expected '0 6 * * *'"
        )


# ---------------------------------------------------------------------------
# Test 5: No pods in bad states
# ---------------------------------------------------------------------------


class TestPodHealth:
    """Verify no pods are in CrashLoopBackOff, Error, or ImagePullBackOff."""

    def test_no_pods_in_bad_state(self, kube_client):
        bad_pods = kube_client.get_pods_in_bad_state()
        if bad_pods:
            names = [p.get("metadata", {}).get("name", "?") for p in bad_pods]
            pytest.fail(
                f"Pods in bad state: {', '.join(names)}"
            )


# ---------------------------------------------------------------------------
# Test 6: ServiceAccount IRSA annotation
# ---------------------------------------------------------------------------


class TestServiceAccount:
    """Verify agent-context-sa exists with IRSA role annotation."""

    def test_service_account_exists(self, kube_client):
        sa = kube_client.get_service_account("agent-context-sa")
        assert sa is not None, "ServiceAccount agent-context-sa not found"

    def test_service_account_has_irsa_annotation(self, kube_client):
        sa = kube_client.get_service_account("agent-context-sa")
        assert sa is not None, "ServiceAccount agent-context-sa not found"
        annotations = sa.get("metadata", {}).get("annotations", {})
        role_arn = annotations.get("eks.amazonaws.com/role-arn", "")
        assert role_arn, (
            "ServiceAccount agent-context-sa missing eks.amazonaws.com/role-arn annotation"
        )
        assert "agent-context" in role_arn.lower() or "irsa" in role_arn.lower(), (
            f"IRSA role ARN doesn't look right: {role_arn}"
        )
