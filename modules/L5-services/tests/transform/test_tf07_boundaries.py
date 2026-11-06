"""Synthetic boundary controls; installed denial and Pod execution remain live checks."""

import copy

import pytest

from l5_services.transform_boundary_probe import SURFACES, probe_boundaries


OWNER = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
FOREIGN = {"tenant_id": "tenant-foreign", "product_id": "product-foreign", "project_id": "project-foreign"}
REFERENCE = {"tenant_id": "tenant-example", "project_id": "project-example", "record_id": "result-example"}


class FixtureDriver:
    def __init__(self):
        self.calls = []
        self.overrides = {}

    async def read(self, surface, actor, reference):
        self.calls.append((surface, actor, reference))
        allowed = actor == OWNER
        trace = {"actor": actor, "reference": reference, "source": SURFACES[surface],
                 "evidence_ref": f"private/{surface}/{'owned' if allowed else 'foreign'}",
                 "status": 200 if allowed else 404, "effect_count": 0}
        if allowed:
            trace["resource_id"] = REFERENCE["record_id"]
        else:
            trace["body"] = {"detail": {"code": "not_found"}}
        return {**trace, **self.overrides.get((surface, allowed), {})}

    async def probe_executor(self, scope):
        self.calls.append(("executor", scope))
        observation = {"source": "kubernetes-api", "scope": scope,
                       "evidence_ref": "private/executor/uid-example",
                       "pod": {"metadata": {"uid": "uid-example"},
                               "spec": {"automountServiceAccountToken": False,
                                        "enableServiceLinks": False, "hostNetwork": False,
                                        "hostPID": False, "hostIPC": False,
                                        "containers": [{"env": [
                                            {"name": "HOME", "value": "/tmp"},
                                            {"name": "BG_CONFIG_DIR", "value": "/tmp/bg"},
                                            {"name": "TMPDIR", "value": "/tmp"}],
                                            "securityContext": {
                                            "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                            "capabilities": {"drop": ["ALL"]}}}],
                                        "volumes": [{"name": "source", "projected": {
                                            "sources": [{"configMap": {"name": "source-example"}}]}}]}},
                       "probes": {"allowed_source_read": True, "metadata_denied": True,
                                  "egress_denied": True, "credentials_denied": True,
                                  "source_escape_denied": True, "host_escape_denied": True}}
        changed = self.overrides.get("executor", {})
        if "pod_spec" in changed:
            observation["pod"]["spec"] = changed["pod_spec"]
            changed = {key: value for key, value in changed.items() if key != "pod_spec"}
        return {**observation, **changed}


@pytest.mark.asyncio
async def test_paired_boundary_probes_require_successful_owner_and_denied_foreign_reads():
    driver = FixtureDriver()
    result = await probe_boundaries(driver, owner=OWNER, foreign=FOREIGN, reference=REFERENCE)
    assert result["live_qualified"] is False
    assert len(result["evidence_refs"]) == 7
    assert driver.calls == [
        (surface, actor, REFERENCE) for surface in SURFACES for actor in (OWNER, FOREIGN)
    ] + [("executor", OWNER)]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,allowed,change", [
    ("client", True, {"status": 503}),
    ("broker", False, {"status": 200}),
    ("source", False, {"body": {"detail": {"secret": "leaked"}}}),
    ("client", False, {"effect_count": 1}),
    ("broker", False, {"evidence_ref": ""}),
])
async def test_missing_control_disclosure_or_side_effect_refuses_boundary_claim(surface, allowed, change):
    driver = FixtureDriver()
    driver.overrides[(surface, allowed)] = change
    with pytest.raises(ValueError):
        await probe_boundaries(driver, owner=OWNER, foreign=FOREIGN, reference=REFERENCE)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [
    {"pod_spec": {"automountServiceAccountToken": True, "enableServiceLinks": False,
              "containers": [{}], "volumes": []}},
    {"pod_spec": {"automountServiceAccountToken": False, "enableServiceLinks": False,
              "containers": [{}], "volumes": [{"hostPath": {"path": "/"}}]}},
    {"pod_spec": {"automountServiceAccountToken": False, "enableServiceLinks": False,
                  "hostNetwork": False, "hostPID": False, "hostIPC": False,
                  "containers": [{}], "volumes": [{"name": "source", "projected": {
                      "sources": [{"secret": {"name": "credentials"}}]}}]}},
    {"probes": {"metadata_denied": True}},
    {"source": "provider-prose"},
])
async def test_unqualified_pod_or_escape_refuses_boundary_claim(unsafe):
    driver = FixtureDriver()
    driver.overrides["executor"] = copy.deepcopy(unsafe)
    with pytest.raises(ValueError):
        await probe_boundaries(driver, owner=OWNER, foreign=FOREIGN, reference=REFERENCE)


@pytest.mark.asyncio
async def test_unscoped_or_same_actor_fixture_never_contacts_clients():
    driver = FixtureDriver()
    with pytest.raises(ValueError):
        await probe_boundaries(driver, owner=OWNER, foreign=OWNER, reference=REFERENCE)
    assert not driver.calls
