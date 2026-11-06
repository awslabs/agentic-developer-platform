"""Synthetic AC-02 configuration controls; live provider evidence is separate."""

from copy import deepcopy

import pytest

from l5_services.wa_portability import verify_portable_configurations


def reference(project, record_type, record_id):
    return {"tenant_id": "tenant-example", "project_id": project,
            "source_connection_id": f"source-{project}", "record_type": record_type,
            "record_id": record_id, "record_revision": f"revision-{project}"}


def cases():
    release = {name: f"{index:x}" * 64 for index, name in enumerate(
        ("wheel", "worker", "schema", "gateway", "frontend", "cli"), 1)}
    projects = []
    for index, iac_format in enumerate(("terraform", "cloudformation"), 1):
        project = f"external-project-{index}"
        architecture = reference(project, "repository_commit", f"iac-{index}")
        goal = reference(project, "document", f"goal-{index}")
        projects.append({
            "project_kind": "non_adp", "iac_format": iac_format,
            "architecture": architecture, "goal": goal,
            "run_input": {"service_id": "well_architected", "configuration_revision": index,
                          "binding_id": f"binding-{index}", "capability": "architecture_review",
                          "sources": [deepcopy(architecture)]},
            "settings": {"review_mode": "source_only", "pillars": ["SECURITY"],
                         "goal_refs": [deepcopy(goal)],
                         "execution_role_ref": reference(project, "provider_role", f"role-{index}"),
                         "source_store_ref": reference(project, "source_store", f"store-{index}")},
            "release_digests": deepcopy(release),
            "changes_since_release": ["configuration", "binding", "source"],
        })
    return projects


def test_two_distinct_projects_use_pinned_goals_and_one_release():
    pair = cases()
    assert verify_portable_configurations(pair) == pair[0]["release_digests"]
    assert pair[0]["architecture"] != pair[1]["architecture"]
    assert pair[0]["goal"] != pair[1]["goal"]


@pytest.mark.parametrize("change,error", [
    (lambda pair: pair[1].update(project_kind="adp"), "non-ADP"),
    (lambda pair: pair[1].update(iac_format="terraform"), "IaC formats"),
    (lambda pair: pair[1]["release_digests"].update(worker="f" * 64), "release artifacts changed"),
    (lambda pair: pair[1]["changes_since_release"].append("deploy"), "only configuration"),
    (lambda pair: pair[1]["settings"].update(goal_refs=[]), "pinned architecture"),
    (lambda pair: pair[1]["settings"].pop("execution_role_ref"), "pinned architecture"),
    (lambda pair: pair[1]["settings"]["goal_refs"][0].update(project_id="external-project-1"), "share one project"),
    (lambda pair: pair[1]["run_input"]["sources"][0].update(record_revision="latest"), "invalid architecture"),
    (lambda pair: pair[1]["architecture"].update(record_id="other"), "match pinned configuration"),
])
def test_portability_refuses_reuse_cross_scope_and_redeployment(change, error):
    pair = cases()
    change(pair)
    with pytest.raises(ValueError, match=error):
        verify_portable_configurations(pair)
