"""Offline portability readbacks; illustrative Products are not live targets."""

from copy import deepcopy

import pytest

from portability import validate_projects, verify_portability
from test_context import manifest


@pytest.fixture
def portable():
    selected = manifest()
    selected["projects"] = [
        {"scope": {"tenant_id": "tenant-example", "product_id": f"product-{name}-example",
                   "project_id": f"project-{name}-example"},
         "repository_ref": f"repository-{name}-example", "team_ref": f"team-{name}-example",
         "policy_ref": f"policy-{name}-example", "environment_ref": f"environment-{name}-example",
         "account_ref": f"account-{name}-example", "role_ref": f"role-{name}-example",
         "is_adp": False, "source_sha": letter * 40,
         "workload_kind": kind, "selected_cross_account": name == "river",
         "capability": "investigation", "owned_assets": [f"asset-{name}-owned"],
         "retained_assets": [f"asset-{name}-retained"],
         "configuration_receipt": f"configuration-{name}-example",
         "operation_receipt": f"operation-{name}-example",
         "context_receipt": f"context-{name}-example",
         "cleanup_receipt": f"cleanup-{name}-example",
         "limits": {operation: {"max_spend_usd": "0.25", "max_requests": 4, "deadline_seconds": 120}
                    for operation in ("operation", "context")}}
        for name, letter, kind in (("river", "c", "aws_resource"), ("summit", "d", "eks"))]
    return selected


class FakePlatform:
    def __init__(self, manifest, fault=None):
        self.manifest, self.fault = manifest, fault
        self.platform = {**manifest["release"], "hosting_account_ref": "hosting-account-example",
                         "build_id": "build-once-example"}
        self.assets = {}
        self.actions = []

    def configure(self, project):
        self.actions.append(("configure", project["scope"]["project_id"]))
        scope = project["scope"]
        self.assets[scope["project_id"]] = {ref: {"owner_scope": scope} for ref in project["owned_assets"]}
        self.assets[scope["project_id"]].update({ref: {"owner_scope": scope}
                                                 for ref in project["retained_assets"]})
        if self.fault == "other_project_mutation" and scope["product_id"] == "product-summit-example":
            self.assets["project-river-example"]["asset-river-retained"] = {"owner_scope": scope}
        if self.fault == "redeploy":
            self.platform["build_id"] = "second-build-example"

    def run_operation(self, scope, capability, limits):
        self.actions.append(("operation", scope["project_id"]))
        return {"run_id": "run-" + scope["project_id"]}

    def run_context(self, scope, result_ref, limits):
        self.actions.append(("context", scope["project_id"]))
        return {"run_id": "later-" + scope["project_id"]}

    def probe_foreign(self, scope, other):
        self.actions.append(("foreign", scope["project_id"], other["project_id"]))
        return {"status": 404, "disclosed_resources": []}

    def cleanup_owned(self, scope, assets):
        self.actions.append(("cleanup", scope["project_id"]))
        if self.fault != "missing_cleanup":
            for asset in assets:
                self.assets[scope["project_id"]].pop(asset)
        if self.fault == "delete_retained":
            self.assets[scope["project_id"]].clear()


class FakeReader:
    def __init__(self, platform):
        self.platform = platform

    def project(self, scope):
        return next(item for item in self.platform.manifest["projects"] if item["scope"] == scope)

    def read_platform(self, target):
        return deepcopy(self.platform.platform)

    def read_product(self, scope):
        project = self.project(scope)
        return {"scope": scope, "is_adp": self.platform.fault == "adp_product",
                "team_ref": project["team_ref"]}

    def read_configuration(self, scope):
        project = self.project(scope)
        return {**{field: project[field] for field in (
                    "repository_ref", "team_ref", "policy_ref", "environment_ref", "workload_kind",
                    "account_ref", "role_ref")}, "scope": scope}

    def read_access(self, scope):
        project = self.project(scope)
        return {"scope": scope, "account_ref": "hosting-account-example"
                if self.platform.fault == "wrong_account" else project["account_ref"],
                "role_ref": project["role_ref"], "authorized": True}

    def read_native(self, scope, run_id):
        project = self.project(scope)
        return {"scope": scope, "run_id": run_id, "status": "completed",
                "capability": project["capability"], "native_job_id": "job-" + scope["project_id"],
                "result_ref": "result-" + scope["project_id"], "native_output": "observed",
                "source_sha": "f" * 40 if self.platform.fault == "wrong_source" else project["source_sha"],
                "deployment_id": self.platform.manifest["release"]["deployment_id"]}

    def read_context(self, scope, run_id):
        return {"scope": scope, "run_id": run_id, "status": "completed", "decision": "observed",
                "cited_result_ref": "result-" + scope["project_id"],
                "cited_native_job_id": "job-" + scope["project_id"]}

    def read_foreign_denial(self, scope, foreign):
        return {"status": 404, "disclosed_resources": [],
                "provider_session_calls": 1 if self.platform.fault == "foreign_federation" else 0}

    def read_usage(self, scope):
        return {kind: {"spend_usd": "0.01", "requests": 2, "elapsed_seconds": 12}
                for kind in ("operation", "context")}

    def read_assets(self, scope, refs):
        return {ref: deepcopy(self.platform.assets.get(scope["project_id"], {}).get(ref))
                for ref in refs if ref in self.platform.assets.get(scope["project_id"], {})}


def test_two_non_adp_projects_use_only_configuration_on_identical_artifacts(portable):
    platform = FakePlatform(portable)
    proof = verify_portability(portable, platform, FakeReader(platform))
    assert len(proof) == 2
    assert [action[0] for action in platform.actions].count("configure") == 2
    assert [action[0] for action in platform.actions].count("cleanup") == 2
    assert platform.platform["build_id"] == "build-once-example"
    assert {item["native"]["scope"]["product_id"] for item in proof} == {
        "product-river-example", "product-summit-example"}


@pytest.mark.parametrize("change", [
    lambda manifest: manifest["projects"][1].update(repository_ref=manifest["projects"][0]["repository_ref"]),
    lambda manifest: manifest["projects"][1].update(team_ref=manifest["projects"][0]["team_ref"]),
    lambda manifest: manifest["projects"][1].update(is_adp=True),
    lambda manifest: manifest["projects"][1].update(capability="unsupported"),
    lambda manifest: manifest["projects"][0].update(workload_kind="eks"),
    lambda manifest: manifest["projects"][0].update(selected_cross_account=False),
    lambda manifest: manifest["projects"].pop(),
    lambda manifest: manifest["projects"][0]["limits"]["context"].update(max_spend_usd="0"),
])
def test_portability_rejects_adp_only_shared_source_and_unbounded_manifest(portable, change):
    change(portable)
    with pytest.raises(ValueError):
        validate_projects(portable)


@pytest.mark.parametrize("fault", ["adp_product", "redeploy", "wrong_account", "wrong_source",
                                    "foreign_federation", "missing_cleanup", "delete_retained", "other_project_mutation"])
def test_portability_rejects_wrong_scope_source_redeploy_and_failed_cleanup(portable, fault):
    platform = FakePlatform(portable, fault)
    with pytest.raises(ValueError):
        verify_portability(portable, platform, FakeReader(platform))
