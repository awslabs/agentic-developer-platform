"""E1 reads the existing release. It never provisions or starts another flow."""

from pathlib import Path

from .definitions import DEFINITION_HASH, fixture_test
from .delivery import Evidence
from .http import Client, Unsupported
from .manifest import load_manifest, verify_checkout
from .native_probe import execute_source
from .parity import observe
from .runtime import read_runtime

EVALUATION_CRITERIA = {"q2.deployed-code": "functional", "q2.ui-api": "api"}


def evaluate_release(*, config, inventory, context):
    if not __debug__:
        raise RuntimeError("qualification assertions require Python without -O")
    manifest, _ = load_manifest(config)
    verify_checkout(config)
    client = Client(config, manifest)
    spec = context.specification
    if {
        c.criterion_id: c.kind for c in spec.criteria
    } != EVALUATION_CRITERIA or not all(c.required for c in spec.criteria):
        raise Unsupported("Q2 E1 requires the fixed deployed-code and UI/API criteria")
    graph = client.get(f"/orchestration/flows/{context.flow_id}")
    plans = client.get(f"/orchestration/flows/{context.flow_id}/plans")
    current = [p for p in plans if p["superseded_at"] is None]
    if len(current) != 1 or current[0]["version"] != context.accepted_plan_version:
        raise Unsupported("current evaluation plan changed")
    plan = current[0]["plan_document"]
    if (
        plan.get("spec_revision") != DEFINITION_HASH
        or plan["execution_policy"]["policy_hash"] != context.policy_hash
    ):
        raise Unsupported("evaluation does not belong to the accepted Q2 policy")
    node = next(n for n in graph["nodes"] if n["id"] == context.node_id)
    if node["kind"] != "eval" or node["node_ref"] != "verify":
        raise Unsupported("evaluation is not the pinned predecessor release check")
    qualification_id = graph["slug"]
    import re

    if not re.fullmatch(r"q-[a-z0-9-]{8,50}", qualification_id):
        raise Unsupported("not a qualification fixture")
    target = manifest.runtime["engine"]
    runtime = read_runtime(client, target)
    actual_target = {
        "provider": "aws",
        "account_id": runtime["account_id"],
        "region": runtime["cluster"].split(":")[3],
        "resource_kind": "eks-namespace",
        "resource_id": target.namespace,
    }
    if (
        runtime["actual_revision"] != context.actual_revision
        or actual_target != spec.target.model_dump()
    ):
        raise Unsupported("current deployed release/target differs from E1 context")
    actor = client.get("/auth/me")
    roles = ["admin" if actor["is_admin"] else "member"]
    if actor["org_id"] != config.org_ref or actor["user_id"] != config.identity_ref:
        raise Unsupported("evaluation actor changed")
    fixture = {
        "fixture_set_id": "q2-delivery-v1",
        "definition_hash": DEFINITION_HASH,
        "roles": roles,
        "row_counts": {actor["org_id"]: len(graph["nodes"])},
    }
    if (
        spec.fixtures.fixture_set_id != fixture["fixture_set_id"]
        or spec.fixtures.definition_hash != DEFINITION_HASH
        or spec.fixtures.roles != roles
        or spec.fixtures.org_refs != [actor["org_id"]]
        or len(graph["nodes"]) < spec.fixtures.minimum_rows_per_org
    ):
        raise Unsupported(
            "observed fixture population/roles do not satisfy E1 specification"
        )
    evidence = Evidence(inventory)
    artifacts, criteria = [], []
    for criterion_id, kind in EVALUATION_CRITERIA.items():
        outcome, detail, paths = "pass", "Observed current release", []
        try:
            if criterion_id == "q2.deployed-code":
                value = execute_source(
                    client,
                    target,
                    Path(__file__).with_name("release_process.py").read_text(),
                    {
                        "qualification_id": qualification_id,
                        "tests": fixture_test(0, qualification_id),
                    },
                    runtime=runtime,
                )
                assert (
                    value["successful"]
                    and value["tests_run"] == 3
                    and value["skipped"] == 0
                )
            else:
                value = observe(client, context.flow_id)
            artifact = evidence.save(
                criterion_id,
                {"runtime": runtime, "observation": value},
                "current-release:EKS/API/browser",
            )
            path = str(
                (inventory.path.parent / artifact.path).relative_to(
                    config.artifact_directory
                )
            )
            artifacts.append({"path": path, "kind": kind})
            paths.append(path)
        except Unsupported as exc:
            outcome, detail = "not_run", str(exc)
        except (AssertionError, KeyError, ValueError):
            outcome, detail = (
                "fail",
                "Current release did not satisfy the fixed criterion",
            )
        criteria.append(
            {
                "criterion_id": criterion_id,
                "outcome": outcome,
                "artifact_paths": paths,
                "detail": detail,
            }
        )
    # A rollout during the observations invalidates the entire release proof.
    after = read_runtime(client, target)
    if (after["actual_revision"], after["digest"]) != (
        runtime["actual_revision"],
        runtime["digest"],
    ):
        raise Unsupported("runtime changed during evaluation")
    return {
        "live": True,
        "actual_revision": runtime["actual_revision"],
        "target": actual_target,
        "fixtures": fixture,
        "criteria": criteria,
        "artifacts": artifacts,
    }
