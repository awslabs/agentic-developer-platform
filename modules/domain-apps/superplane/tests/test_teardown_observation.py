"""Offline regressions for U1-L1 teardown evidence — #5288, EPIC #4910.

Every case here runs the REAL verifier with injected transports. The injected transports
are what make the suite offline; they are not a relaxed verifier. Each produces an
`offline-fixture` record that `run_live` refuses to publish, so nothing in this file can
establish live acceptance — that is asserted directly in the stubbed-positive test.

The suite is weighted towards refusals on purpose. A cleanup checker's failure modes are
all of the shape "could not see it, so it must be gone", and a test suite that mostly
checked the happy path would not catch any of them.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from superplane_acceptance import teardown as t
from superplane_acceptance.cli_delivery import EvidenceError

ACCOUNT = "879318057152"
REGION = "us-east-1"
CLUSTER = "adp-dev"
ENDPOINT = "https://ABCDEF.gr7.us-east-1.eks.amazonaws.com"
CLUSTER_ARN = f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}"

# One revision per lane, all distinct, so a check that confuses two of them fails visibly.
DEPLOY_SHA = "a" * 40
UNDEPLOY_SHA = "b" * 40
ROLLOUT_SHA = "4" * 40
K8S_SHA = "3" * 40
RECORDER_SHA = "5" * 40

DEPLOY_RUN_ID = 101
UNDEPLOY_RUN_ID = 202
K8S_RUN_ID = 303
ROLLOUT_RUN_ID = 404
RECORDER_RUN_ID = 505

# The real timeline the ordering rules require: apply, then rollout, then the pre-teardown
# observation, then the Kubernetes teardown, then the Terraform destroy.
DEPLOYED_AT = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
ROLLED_OUT_AT = DEPLOYED_AT + timedelta(minutes=5)
OBSERVED_AT = DEPLOYED_AT + timedelta(minutes=10)
K8S_TORN_DOWN_AT = DEPLOYED_AT + timedelta(minutes=50)
UNDEPLOYED_AT = DEPLOYED_AT + timedelta(hours=1)

MODULE_PATH = t.MODULE_PATH
MODULE_ROOT = Path(t.MODULE_ROOT)
REPOSITORY_ROOT = MODULE_ROOT.parents[2]
VERIFIER_PATH = f"{MODULE_PATH}/superplane_acceptance/teardown.py"

# The state object `environments/dev/modules/superplane-backend.tfvars` names, with the
# repository-wide ACCOUNT_ID placeholder resolved the way the deploy scripts resolve it.
STATE_BUCKET = f"adp-terraform-state-{ACCOUNT}"
STATE_KEY = "dev/modules/superplane/terraform.tfstate"

# The repository files the revision-pinned derivation reads. Served from the local tree by the
# fake GitHub contents transport below, which is what keeps the suite offline while still
# exercising the real fetch-and-hash path.
SOURCE_FILES = (
    f"{MODULE_PATH}/infra/control-plane/main.tf",
    f"{MODULE_PATH}/infra/control-plane/config.tf",
    f"{MODULE_PATH}/infra/control-plane/irsa.tf",
    f"{MODULE_PATH}/releases/superplane.lock.yaml",
    "environments/dev/modules/superplane.tfvars",
    # U3's lifecycle contract: which rendered objects a teardown deletes and which it keeps.
    f"{MODULE_PATH}/k8s/rollback.sh",
    # Where the Terraform lanes' state object is named, so an attestation's claimed state key is
    # compared against the deploy's own backend configuration (F1).
    "environments/dev/modules/superplane-backend.tfvars",
)


# ---------------------------------------------------------------------------
# Fixtures: a complete, internally consistent PASSING world, so each test can
# break exactly one thing and prove that one thing is what refuses.
# ---------------------------------------------------------------------------
def _contents(path: str, sha: str):
    """Serve one repository path from the local tree as a GitHub contents response."""
    target = REPOSITORY_ROOT / path
    if target.is_dir():
        return [
            {"name": child.name, "type": "file"}
            for child in sorted(target.iterdir())
            if child.is_file()
        ]
    if not target.is_file():
        raise EvidenceError("BLOCKED: GitHub metadata/source request failed")
    return {
        "encoding": "base64",
        "content": base64.b64encode(target.read_bytes()).decode("ascii"),
    }


def source_hashes(sha: str = DEPLOY_SHA, overrides: dict | None = None) -> dict:
    """The hashes the derivation will compute, for building a consistent receipt."""
    hashes = {}
    for path in SOURCE_FILES:
        hashes[path] = hashlib.sha256((REPOSITORY_ROOT / path).read_bytes()).hexdigest()
    manifests = REPOSITORY_ROOT / MODULE_PATH / "k8s"
    for child in sorted(manifests.iterdir()):
        if child.is_file() and child.name.endswith((".yaml", ".yml")):
            key = f"{MODULE_PATH}/k8s/{child.name}"
            hashes[key] = hashlib.sha256(child.read_bytes()).hexdigest()
    # Only the manifests the derivation actually reads are hashed, so drop any it skips.
    hashes.update(overrides or {})
    return hashes


def verifier_sha256() -> str:
    """This module's own bytes, which a genuine receipt's `recorder_sha256` must equal."""
    return hashlib.sha256((REPOSITORY_ROOT / VERIFIER_PATH).read_bytes()).hexdigest()


_DERIVED: dict[str, list[dict]] = {}


def derived_for(sha: str = DEPLOY_SHA) -> list[dict]:
    """The real derivation at `sha`, via the offline contents transport."""
    if sha not in _DERIVED:
        config = {"tf_environment": "dev", "account": ACCOUNT, "region": REGION}
        sources = t.RevisionSources(github_runs(), sha)
        _DERIVED[sha] = t.derived_inventory(config, sources)
    return [dict(entry) for entry in _DERIVED[sha]]


def label(entry: dict) -> str:
    """The identity string the verifier's messages and records use for one resource."""
    namespace = entry.get("namespace")
    return f"{entry['type']} {entry['name']}" + (
        f" in namespace {namespace}" if namespace else ""
    )


def aws_scope(inventory: list[dict] | None = None) -> list[dict]:
    inventory = derived_for() if inventory is None else inventory
    return [entry for entry in inventory if entry["type"].startswith("aws_")]


def k8s_scope(lifecycle=None, inventory: list[dict] | None = None) -> list[dict]:
    inventory = derived_for() if inventory is None else inventory
    return t.k8s_entries(inventory, lifecycle)


def inventory_document(resources: list[dict] | None = None, **overrides) -> dict:
    """A well-formed pre-teardown OBSERVATION RECEIPT (the F2 contract).

    Built to pass by default so each test can invalidate exactly one field. Note every entry
    carries `observation: present` — a receipt is evidence that the resources were really
    there, not a list of names — and `evidence_kind: live`, because the live path refuses a
    receipt the recorder itself classified as fixture output.
    """
    if resources is None:
        resources = [
            {**entry, "observation": t.PRESENT}
            for entry in derived_for(overrides.get("_sha", DEPLOY_SHA))
        ]
    overrides.pop("_sha", None)
    document = {
        "schema": t.RECEIPT_SCHEMA,
        "evidence_kind": "live",
        "complete": True,
        "environment": "dev",
        "account": ACCOUNT,
        "region": REGION,
        "cluster": CLUSTER,
        "cluster_arn": CLUSTER_ARN,
        "deploy": {
            "run_id": DEPLOY_RUN_ID,
            "run_attempt": 1,
            "revision": DEPLOY_SHA,
            "run_url": f"https://github.com/aws-e/adp/actions/runs/{DEPLOY_RUN_ID}",
        },
        "observed_at": OBSERVED_AT.isoformat(),
        "source_sha256": source_hashes(),
        "resources": resources,
        "recorder_sha256": verifier_sha256(),
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# F1/F2: the execution attestations, and GitHub's own record of them.
#
# The authority is never the document. It is the artifact record GitHub holds for a verified run:
# a unique name within the run, an unexpired binding to that run and attempt, a digest GitHub
# computed over the bytes it received, and a creation time GitHub stamped. So the fixtures here
# build a real zip, hash it, and publish THAT hash as GitHub's digest — which means a test that
# edits an attestation without republishing it fails on the digest, exactly as a tampered archive
# would in production.
# ---------------------------------------------------------------------------
ATTESTED_EXECUTION = {
    "deploy": (DEPLOY_RUN_ID, DEPLOY_SHA, t.APPLY_WORKFLOW),
    "rollout": (ROLLOUT_RUN_ID, ROLLOUT_SHA, t.ROLLOUT_WORKFLOW),
    "undeploy": (UNDEPLOY_RUN_ID, UNDEPLOY_SHA, t.DESTROY_WORKFLOW),
    "k8s_teardown": (K8S_RUN_ID, K8S_SHA, None),  # workflow filled in from K8S_LANE
    "recorder": (RECORDER_RUN_ID, RECORDER_SHA, None),
}

ARTIFACT_CREATED = {
    "deploy": DEPLOYED_AT,
    "rollout": ROLLED_OUT_AT,
    "undeploy": UNDEPLOYED_AT,
    "k8s_teardown": K8S_TORN_DOWN_AT,
    # Inside the closed window between the deploy finishing and the teardown starting.
    "recorder": OBSERVED_AT,
}

# `aws-e/adp`'s numeric id, as the artifacts API reports it under `workflow_run`.
REPOSITORY_ID = 1186991269

# The COMPLETE set of keys the GitHub Actions artifacts API returns under `workflow_run`, read
# from live metadata for this repository on 2026-09-20:
#
#   "workflow_run": {"id": 35498757255, "repository_id": 1186991269,
#                    "head_repository_id": 1186991269, "head_branch": "main",
#                    "head_sha": "aba3ed24c8e6bb57a59498ac808641e448902d3a"}
#
# `run_attempt` is NOT among them. The previous fixtures invented it and the verifier required
# it, so real artifacts were refused while fabricated ones passed (U1-201). This tuple is
# asserted against the fixtures by `test_artifact_fixtures_match_the_real_api_shape`, so the same
# class of drift cannot silently return: a fixture that models a field GitHub does not send
# cannot validate a check that depends on it.
REAL_ARTIFACT_WORKFLOW_RUN_KEYS = (
    "head_branch",
    "head_repository_id",
    "head_sha",
    "id",
    "repository_id",
)

ARTIFACT_ID = {
    "deploy": 9001,
    "rollout": 9002,
    "undeploy": 9003,
    "k8s_teardown": 9004,
    "recorder": 9005,
}

# What each role's plan must be shown to have contained, per `require_attested_coverage`.
ATTESTED_SCOPE = {
    "deploy": aws_scope,
    "undeploy": aws_scope,
    "rollout": k8s_scope,
    "k8s_teardown": lambda: k8s_scope(t.DELETED),
}

# GitHub's artifact records for the current test, rebuilt by the autouse fixture below.
ARTIFACTS: list[dict] = []


def attestation_document(role: str, resources=None, **overrides) -> dict:
    """An execution attestation a producing lane would upload for `role`."""
    _name, _step, action = t.EXECUTION_ATTESTATION[role]
    run_id, revision, workflow = ATTESTED_EXECUTION[role]
    if resources is None:
        resources = [
            {
                "type": entry["type"],
                "name": entry["name"],
                **({"namespace": entry["namespace"]} if entry.get("namespace") else {}),
            }
            for entry in ATTESTED_SCOPE[role]()
        ]
    document = {
        "schema": t.ATTESTATION_SCHEMA,
        "repository": "aws-e/adp",
        "run_id": run_id,
        "run_attempt": 1,
        "revision": revision,
        "workflow": K8S_LANE if workflow is None else workflow,
        "account": ACCOUNT,
        "environment": "dev",
        "region": REGION,
        "action": action,
        "resources": resources,
    }
    if t.ATTESTATION_BINDING[role] is t.STATE_ATTESTATION_FIELDS:
        document.update({"state_bucket": STATE_BUCKET, "state_key": STATE_KEY})
    else:
        document.update({"cluster": CLUSTER, "cluster_arn": CLUSTER_ARN})
    document.update(overrides)
    return document


def _zip(members: dict[str, bytes]) -> bytes:
    """A byte-deterministic zip, so the digest a test publishes is reproducible."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, payload in members.items():
            bundle.writestr(
                zipfile.ZipInfo(name, date_time=(2026, 9, 18, 10, 0, 0)), payload
            )
    return buffer.getvalue()


def _archive(name: str, document) -> bytes:
    """One artifact archive holding one JSON document, as a producing lane would upload."""
    return _zip({f"{name}.json": json.dumps(document, indent=2, sort_keys=True)})


def publish_bytes(directory: Path, role: str, raw: bytes, **artifact) -> dict:
    """Write arbitrary archive bytes and register GitHub's record of exactly those bytes.

    Separate from `publish_artifact` so a test can publish a malformed archive — not a zip, two
    JSON members, oversized — without the helper quietly repairing it into a well-formed one.
    """
    name = t.EXECUTION_ATTESTATION[role][0]
    (Path(directory) / f"{name}.zip").write_bytes(raw)
    run_id, sha, _workflow = ATTESTED_EXECUTION[role]
    record = {
        "id": ARTIFACT_ID[role],
        "name": name,
        "expired": False,
        # GitHub's digest, over the bytes GitHub received. Recomputed here rather than asserted,
        # so an edited document without a republish fails authentication.
        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "created_at": ARTIFACT_CREATED[role].isoformat().replace("+00:00", "Z"),
        "size_in_bytes": len(raw),
        # The REAL `workflow_run` shape. Sanitized from live metadata read from this repository
        # on 2026-09-20; `run_attempt` is deliberately absent because the artifacts API does not
        # return it (U1-201). `REAL_ARTIFACT_WORKFLOW_RUN_KEYS` pins this against drift.
        "workflow_run": {
            "id": run_id,
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
            "head_branch": "main",
            "head_sha": sha,
        },
    }
    record.update(artifact)
    ARTIFACTS[:] = [item for item in ARTIFACTS if item["name"] != name]
    ARTIFACTS.append(record)
    return record


def publish_artifact(directory: Path, role: str, document, **artifact) -> dict:
    """Write a role's artifact archive and register GitHub's record of it."""
    name = t.EXECUTION_ATTESTATION[role][0]
    return publish_bytes(directory, role, _archive(name, document), **artifact)


def drop_artifact(role: str) -> None:
    name = t.EXECUTION_ATTESTATION[role][0]
    ARTIFACTS[:] = [item for item in ARTIFACTS if item["name"] != name]


def duplicate_artifact(role: str) -> None:
    """Two artifacts of one name in a run: which document the run produced is then unknown."""
    name = t.EXECUTION_ATTESTATION[role][0]
    original = next(item for item in ARTIFACTS if item["name"] == name)
    ARTIFACTS.append({**original, "id": original["id"] + 1})


@pytest.fixture
def attestations(tmp_path):
    """The default consistent attestation world: one authenticated archive per lane."""
    directory = tmp_path / "attestations"
    directory.mkdir(exist_ok=True)
    ARTIFACTS.clear()
    for role in ("deploy", "rollout", "undeploy", "k8s_teardown"):
        publish_artifact(directory, role, attestation_document(role))
    # The recorder's "attestation" IS the receipt: `authenticate_recorder` requires the receipt the
    # verifier reads to be exactly the document GitHub holds a digest for.
    publish_artifact(directory, "recorder", inventory_document())
    yield directory
    ARTIFACTS.clear()


@pytest.fixture
def inventory_file(attestations):
    """Write a receipt AND republish it as the recorder's artifact.

    The two are the same document by construction, so a test that alters the receipt still
    exercises the field it meant to alter instead of tripping the recorder digest check first.
    """

    def write(document) -> str:
        path = Path(attestations).parent / "pre-teardown-inventory.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        if type(document) is dict:
            publish_artifact(attestations, "recorder", document)
        return str(path)

    return write


@pytest.fixture
def environment(tmp_path, attestations, inventory_file):
    def build(**overrides) -> dict:
        values = {
            "SUPERPLANE_LIVE_ENVIRONMENT": "embark1/dev",
            "SUPERPLANE_LIVE_TF_ENVIRONMENT": "dev",
            "SUPERPLANE_LIVE_CLUSTER": CLUSTER,
            "SUPERPLANE_LIVE_DEPLOY_RUN_ID": str(DEPLOY_RUN_ID),
            "SUPERPLANE_LIVE_DEPLOY_SHA": DEPLOY_SHA,
            "SUPERPLANE_LIVE_ROLLOUT_RUN_ID": str(ROLLOUT_RUN_ID),
            "SUPERPLANE_LIVE_ROLLOUT_SHA": ROLLOUT_SHA,
            "SUPERPLANE_LIVE_UNDEPLOY_RUN_ID": str(UNDEPLOY_RUN_ID),
            "SUPERPLANE_LIVE_UNDEPLOY_SHA": UNDEPLOY_SHA,
            "SUPERPLANE_LIVE_INVENTORY_FILE": inventory_file(inventory_document()),
            "SUPERPLANE_LIVE_ATTESTATION_DIR": str(attestations),
            "SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE": str(tmp_path / "evidence.json"),
            # Supplied here so `settings` parses them on the real path; the lanes they refer to
            # are the hypothetical ones registered by the autouse fixtures.
            "SUPERPLANE_LIVE_K8S_TEARDOWN_RUN_ID": str(K8S_RUN_ID),
            "SUPERPLANE_LIVE_K8S_TEARDOWN_ATTEMPT": "1",
            "SUPERPLANE_LIVE_RECORDER_RUN_ID": str(RECORDER_RUN_ID),
            "SUPERPLANE_LIVE_RECORDER_ATTEMPT": "1",
        }
        values.update(overrides)
        return {k: v for k, v in values.items() if v is not None}

    return build


@pytest.fixture
def config(environment):
    return t.settings(environment())


def _steps(role: str, **conclusions) -> list[dict]:
    """Step records for a lane, every required step successful unless overridden."""
    _, _, required = t.LANE_EXECUTION[role]
    return [
        {"name": name, "conclusion": conclusions.get(name, "success")}
        for name in required
    ]


def _run_record(run_id, path, sha, started, completed):
    return {
        "id": run_id,
        "repository": {"full_name": "aws-e/adp"},
        "path": path,
        "head_sha": sha,
        "event": t.DISPATCH_EVENT,
        "status": "completed",
        "conclusion": "success",
        "run_started_at": started.isoformat().replace("+00:00", "Z"),
        "updated_at": completed.isoformat().replace("+00:00", "Z"),
        "run_attempt": 1,
        "html_url": f"https://github.com/aws-e/adp/actions/runs/{run_id}",
    }


def github_runs(
    jobs=None, steps=None, artifacts=None, attempt_records=None, **overrides
):
    """A GitHub transport serving runs, per-attempt records/jobs, artifacts and source contents.

    `steps` maps a role to per-step conclusion overrides, which is how the empty-state
    skipped-destroy path is reproduced. `jobs` replaces a role's whole job list. `artifacts`
    replaces GitHub's artifact records wholesale; by default it serves the module-level
    `ARTIFACTS` the attestation fixture builds, so a republished archive is picked up here.
    `attempt_records` overrides the `actions/runs/{id}/attempts/{n}` response for one
    `(run_id, attempt)`, which is how the attempt-provenance refusals are driven.
    """
    runs = {
        DEPLOY_RUN_ID: _run_record(
            DEPLOY_RUN_ID,
            t.APPLY_WORKFLOW,
            DEPLOY_SHA,
            DEPLOYED_AT - timedelta(minutes=5),
            DEPLOYED_AT,
        ),
        ROLLOUT_RUN_ID: _run_record(
            ROLLOUT_RUN_ID,
            t.ROLLOUT_WORKFLOW,
            ROLLOUT_SHA,
            ROLLED_OUT_AT - timedelta(minutes=3),
            ROLLED_OUT_AT,
        ),
        UNDEPLOY_RUN_ID: _run_record(
            UNDEPLOY_RUN_ID,
            t.DESTROY_WORKFLOW,
            UNDEPLOY_SHA,
            UNDEPLOYED_AT - timedelta(minutes=5),
            UNDEPLOYED_AT,
        ),
        K8S_RUN_ID: _run_record(
            K8S_RUN_ID,
            K8S_LANE,
            K8S_SHA,
            K8S_TORN_DOWN_AT - timedelta(minutes=5),
            K8S_TORN_DOWN_AT,
        ),
        RECORDER_RUN_ID: _run_record(
            RECORDER_RUN_ID,
            RECORDER_LANE,
            RECORDER_SHA,
            OBSERVED_AT - timedelta(minutes=2),
            OBSERVED_AT,
        ),
    }
    for run_id, patch in overrides.items():
        runs[int(run_id)] = {**runs[int(run_id)], **patch} if patch else patch

    role_of = {
        DEPLOY_RUN_ID: "deploy",
        ROLLOUT_RUN_ID: "rollout",
        UNDEPLOY_RUN_ID: "undeploy",
    }
    steps = steps or {}
    jobs = jobs or {}

    def job_list(run_id: int, attempt: int):
        if run_id == K8S_RUN_ID:
            return jobs.get("k8s_teardown", [k8s_run()])
        if run_id == RECORDER_RUN_ID:
            return jobs.get("recorder", [recorder_run()])
        role = role_of[run_id]
        if role in jobs:
            return jobs[role]
        _, job_name, _ = t.LANE_EXECUTION[role]
        return [
            {
                "name": job_name,
                "run_id": run_id,
                "run_attempt": attempt,
                "status": "completed",
                "conclusion": "success",
                "steps": _steps(role, **steps.get(role, {})),
            }
        ]

    def transport(path: str):
        if path.startswith("contents/"):
            reference, sha = path[len("contents/") :].split("?ref=")
            return _contents(reference, sha)
        if "/artifacts" in path:
            run_id = int(path.split("/")[2])
            records = ARTIFACTS if artifacts is None else artifacts
            return {
                "artifacts": [
                    item
                    for item in records
                    if item.get("workflow_run", {}).get("id") == run_id
                ]
            }
        if "/attempts/" in path:
            parts = path.split("/")
            run_id, attempt = int(parts[2]), int(parts[4])
            if path.endswith("/jobs"):
                return {"jobs": job_list(run_id, attempt)}
            # `actions/runs/{id}/attempts/{n}` — the per-attempt record, which is where GitHub
            # really reports an attempt's number, commit and execution window. Artifact
            # provenance binds to the attempt through this, because the artifacts API does not
            # report `run_attempt` at all (U1-201).
            return attempt_record(run_id, attempt)
        run_id = int(path.rsplit("/", 1)[-1])
        value = runs.get(run_id)
        if value is None:
            raise EvidenceError("BLOCKED: GitHub metadata/source request failed")
        return value

    def attempt_record(run_id: int, attempt: int):
        run = runs.get(run_id)
        if run is None:
            raise EvidenceError("BLOCKED: GitHub metadata/source request failed")
        # Keyed membership, not a truthiness test: a test that overrides the response with
        # `None` or `{}` is exercising an unusable record, and treating either as "no override"
        # would silently serve the good one instead.
        if (run_id, attempt) in (attempt_records or {}):
            return attempt_records[(run_id, attempt)]
        if attempt != run.get("run_attempt"):
            raise EvidenceError("BLOCKED: GitHub metadata/source request failed")
        # The real response carries the attempt's own identity, commit, outcome and window.
        return {
            "id": run["id"],
            "run_attempt": attempt,
            "head_sha": run["head_sha"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "run_started_at": run["run_started_at"],
            "updated_at": run["updated_at"],
        }

    return transport


# The retained namespace: U3's teardown keeps it deliberately, so PRESENT is its expected
# observation and the default transport answers accordingly.
RETAINED_READ = ("kubectl", "get", "namespace")


def commands(absent=True, identity=None, cluster=None, context=None, per_resource=None):
    """A read-only command transport. `per_resource` overrides one lookup's outcome.

    Keys in `per_resource` are either a bare name or `"<kubectl token>/<name>"`, because the
    rendered objects deliberately share names across kinds (`serviceaccount/skypilot-api`,
    `role/skypilot-api`, `service/skypilot-api`, …) and a bare name cannot address one of them.
    """
    identity = identity if identity is not None else {"Account": ACCOUNT}
    cluster = (
        cluster
        if cluster is not None
        else {
            "cluster": {
                "endpoint": ENDPOINT,
                "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}",
            }
        }
    )
    context = (
        context
        if context is not None
        else {"clusters": [{"cluster": {"server": ENDPOINT}}]}
    )
    per_resource = per_resource or {}

    def transport(argv: tuple[str, ...]):
        shape = tuple(argv[:3])
        if shape == ("aws", "sts", "get-caller-identity"):
            return 0, json.dumps(identity), ""
        if shape == ("aws", "eks", "describe-cluster"):
            return 0, json.dumps(cluster), ""
        if shape == ("kubectl", "config", "view"):
            return 0, json.dumps(context), ""
        name = argv[argv.index("--role-name") + 1] if "--role-name" in argv else None
        if name is None and "--name" in argv:
            name = argv[argv.index("--name") + 1]
        if name is None and "--repository-names" in argv:
            name = argv[argv.index("--repository-names") + 1]
        qualified = None
        if name is None and shape[0] == "kubectl" and shape[1] == "get":
            name, qualified = argv[3], f"{argv[2]}/{argv[3]}"
        if qualified in per_resource:
            return per_resource[qualified]
        if name in per_resource:
            return per_resource[name]
        if shape[0] == "kubectl":
            token = "NotFound"
        else:
            token = {
                ("aws", "iam", "get-role"): "NoSuchEntity",
                ("aws", "ssm", "get-parameter"): "ParameterNotFound",
                ("aws", "ecr", "describe-repositories"): "RepositoryNotFoundException",
            }[shape]
        # The namespace is RETAINED by U3's teardown, so in the passing world it is still there.
        # Reporting it absent is the retention deviation, asserted separately.
        if absent and shape != RETAINED_READ:
            return 255, "", f"An error occurred ({token}) when calling the operation"
        return 0, json.dumps({"exists": True}), ""

    return transport


# The two lanes that do not exist yet.
#
# `K8S_TEARDOWN_WORKFLOWS` and `RECORDER_WORKFLOWS` are empty in the shipped module because no
# workflow in this repository deletes the rendered objects or records an authenticated
# pre-teardown receipt. Those refusals are asserted directly, against the real empty registries,
# in `test_kubernetes_deletion_blocks_because_no_lane_deletes_the_objects_u3_removes` and
# `test_the_receipt_cannot_be_authenticated_because_no_recorder_lane_exists`.
#
# Every OTHER test needs to get past those blocks to reach what it is actually about, so these
# fixtures register hypothetical lanes. They are test doubles for missing lanes, not relaxations
# of the rule: the code paths they unlock are the real `verify_steps`, `recorded_artifact` and
# `read_attestation` ones, so a supplied receipt is held to exactly the same standard as the
# Terraform lanes' evidence.
K8S_LANE = ".github/workflows/superplane-k8s-teardown.yml"
K8S_JOB = "Tear down Superplane manifests"
K8S_STEPS = ("Delete the rendered objects",)

RECORDER_LANE = ".github/workflows/superplane-pre-teardown-record.yml"
RECORDER_JOB = "Record the pre-teardown observation"
RECORDER_STEPS = ("Record the pre-teardown observation",)


@pytest.fixture(autouse=True)
def hypothetical_k8s_teardown_lane(monkeypatch):
    monkeypatch.setitem(t.K8S_TEARDOWN_WORKFLOWS, K8S_LANE, (K8S_JOB, K8S_STEPS))


@pytest.fixture(autouse=True)
def hypothetical_recorder_lane(monkeypatch):
    monkeypatch.setitem(
        t.RECORDER_WORKFLOWS, RECORDER_LANE, (RECORDER_JOB, RECORDER_STEPS)
    )


def k8s_run(conclusion="success", **overrides):
    """A job record for the hypothetical Kubernetes teardown lane."""
    return {
        "name": K8S_JOB,
        "run_id": K8S_RUN_ID,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "steps": [{"name": name, "conclusion": conclusion} for name in K8S_STEPS],
        **overrides,
    }


def recorder_run(conclusion="success", **overrides):
    """A job record for the hypothetical pre-teardown recording lane."""
    return {
        "name": RECORDER_JOB,
        "run_id": RECORDER_RUN_ID,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "steps": [{"name": name, "conclusion": conclusion} for name in RECORDER_STEPS],
        **overrides,
    }


def verify(config, **kwargs):
    if config.get("k8s_teardown_run") is None:
        config = {
            **config,
            "k8s_teardown_run": {"run_id": K8S_RUN_ID, "run_attempt": 1},
        }
    return t.verify(
        config,
        github=kwargs.pop("github", github_runs()),
        run=kwargs.pop("run", commands()),
    )


# ---------------------------------------------------------------------------
# The stubbed positive case: mechanics only, and it says so.
# ---------------------------------------------------------------------------
def test_stubbed_positive_checks_mechanics_and_cannot_close_the_criterion(config):
    """OFFLINE. Proves the verifier wiring works; establishes no live acceptance."""
    report = verify(config)
    assert report["evidence_kind"] == "offline-fixture"
    assert report["status"] == "matched"
    assert report["u1_acceptance"] == "incomplete"
    # Absence is expected of everything the deploy created and the teardown deletes. The one
    # exception is U3's retained namespace, for which PRESENT is the contract-conforming
    # observation — so this is not a uniform "everything is gone" assertion.
    by_name = {
        (r["type"], r.get("namespace"), r["name"]): r for r in report["resources"]
    }
    retained = [r for r in report["resources"] if r["expected"] == t.PRESENT]
    assert [(r["type"], r["name"]) for r in retained] == [
        ("kubernetes_namespace", "skypilot")
    ]
    assert {r["observation"] for r in retained} == {t.PRESENT}
    deleted = [r for r in report["resources"] if r["expected"] == t.ABSENT]
    assert {r["observation"] for r in deleted} == {t.ABSENT}
    # Every derived resource was actually observed, not merely listed.
    assert (
        len(by_name) == len(report["resources"]) == report["inventory"]["derived_count"]
    )
    assert report["operations"]["deploy"]["revision"] == DEPLOY_SHA
    assert report["operations"]["rollout"]["revision"] == ROLLOUT_SHA
    assert report["operations"]["undeploy"]["revision"] == UNDEPLOY_SHA


def test_fixture_evidence_can_never_be_published_as_live(monkeypatch, environment):
    """An `offline-fixture` record must be refused by the live entry point.

    `verify` marks a record offline whenever a transport was injected, and this asserts the
    publication guard that consumes that mark — so a fixture transport can never leave a
    passing artifact behind.
    """
    real_verify = t.verify
    monkeypatch.setattr(
        t,
        "verify",
        lambda config: real_verify(config, github=github_runs(), run=commands()),
    )
    values = environment()
    with pytest.raises(EvidenceError, match="Fixture evidence cannot be published"):
        t.run_live(values)
    assert not Path(values["SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE"]).exists()


# ---------------------------------------------------------------------------
# Missing and malformed inputs BLOCK; nothing defaults.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", t.INPUTS)
def test_every_input_is_required(environment, name):
    with pytest.raises(EvidenceError, match=f"BLOCKED: missing.*{name}"):
        t.settings(environment(**{name: None}))


def test_unreviewed_target_is_refused(environment):
    with pytest.raises(EvidenceError, match="not in the reviewed registry"):
        t.settings(environment(SUPERPLANE_LIVE_ENVIRONMENT="someone-elses/prod"))


@pytest.mark.parametrize("value", ["", "DEV", "dev/extra", "a" * 17, "1dev"])
def test_invalid_terraform_environment_token_is_refused(environment, value):
    with pytest.raises(EvidenceError, match="BLOCKED"):
        t.settings(environment(SUPERPLANE_LIVE_TF_ENVIRONMENT=value))


@pytest.mark.parametrize("value", ["has space", "-leading", "a" * 101, "semi;colon"])
def test_invalid_cluster_name_is_refused(environment, value):
    with pytest.raises(EvidenceError, match="not a valid EKS cluster name"):
        t.settings(environment(SUPERPLANE_LIVE_CLUSTER=value))


@pytest.mark.parametrize(
    "key,value",
    [
        ("SUPERPLANE_LIVE_DEPLOY_RUN_ID", "0"),
        ("SUPERPLANE_LIVE_DEPLOY_RUN_ID", "12x"),
        ("SUPERPLANE_LIVE_UNDEPLOY_RUN_ID", "-5"),
        ("SUPERPLANE_LIVE_DEPLOY_SHA", "a" * 39),
        ("SUPERPLANE_LIVE_DEPLOY_SHA", "main"),
        ("SUPERPLANE_LIVE_UNDEPLOY_SHA", "A" * 40),
    ],
)
def test_run_and_revision_inputs_must_be_exact(environment, key, value):
    with pytest.raises(EvidenceError, match="BLOCKED"):
        t.settings(environment(**{key: value}))


def test_evidence_file_must_be_new(environment, tmp_path):
    existing = tmp_path / "already-there.json"
    existing.write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="absolute new filename"):
        t.settings(environment(SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE=str(existing)))


def test_evidence_file_must_not_be_a_symlink(environment, tmp_path):
    """A symlink would let a passing record be written over somebody else's file."""
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "target.json")
    with pytest.raises(EvidenceError, match="absolute new filename"):
        t.settings(environment(SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE=str(link)))


def test_missing_inventory_file_blocks(environment, tmp_path):
    with pytest.raises(EvidenceError, match="existing absolute file"):
        t.settings(
            environment(SUPERPLANE_LIVE_INVENTORY_FILE=str(tmp_path / "nope.json"))
        )


# ---------------------------------------------------------------------------
# The inventory: empty, trimmed, mocked and malformed lists cannot pass.
# ---------------------------------------------------------------------------
def test_empty_resource_list_cannot_prove_cleanup(config, inventory_file):
    """The headline failure mode: a user-supplied empty list must not read as clean."""
    config["inventory_file"] = inventory_file(inventory_document(resources=[]))
    with pytest.raises(EvidenceError, match="empty list cannot prove cleanup"):
        verify(config)


def test_incomplete_inventory_is_refused_per_missing_resource(config, inventory_file):
    complete = inventory_document()["resources"]
    for dropped in complete:
        trimmed = [e for e in complete if e["name"] != dropped["name"]]
        config["inventory_file"] = inventory_file(inventory_document(resources=trimmed))
        with pytest.raises(EvidenceError) as caught:
            verify(config)
        assert "does not cover every resource" in str(caught.value)
        assert dropped["name"] in str(caught.value)


def test_inventory_from_another_account_or_environment_is_refused(
    config, inventory_file
):
    for key, value in (
        ("account", "210987654321"),
        ("region", "eu-west-1"),
        ("environment", "staging"),
    ):
        config["inventory_file"] = inventory_file(inventory_document(**{key: value}))
        with pytest.raises(EvidenceError, match="cannot describe this deployment"):
            verify(config)


def test_malformed_inventory_json_blocks(config, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    config["inventory_file"] = str(path)
    with pytest.raises(EvidenceError, match="not valid JSON"):
        verify(config)


def test_oversized_inventory_blocks(config, tmp_path):
    path = tmp_path / "huge.json"
    path.write_text(" " * (t.MAX_INVENTORY_BYTES + 1), encoding="utf-8")
    config["inventory_file"] = str(path)
    with pytest.raises(EvidenceError, match="exceeds the size limit"):
        verify(config)


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "aws_iam_role"},
        {"name": "adp-dev-superplane-control-plane"},
        {"type": "aws_iam_role", "name": ""},
        {"type": "aws_s3_bucket", "name": "adp-dev-superplane-thing"},
        {"type": "aws_iam_role", "name": "adp-dev-superplane-x", "arn": ""},
        "not-an-object",
    ],
)
def test_unobservable_inventory_entries_are_refused(config, inventory_file, entry):
    resources = inventory_document()["resources"] + [entry]
    config["inventory_file"] = inventory_file(inventory_document(resources=resources))
    with pytest.raises(EvidenceError):
        verify(config)


# ---------------------------------------------------------------------------
# F2: the inventory must be an OBSERVATION RECEIPT, not an assertion.
#
# The previous input was a resource list. A list of names establishes nothing about whether
# those resources ever existed: it can be typed up after the teardown by copying the names the
# verifier itself derives, and it satisfies coverage exactly as well as a real observation
# would. Resources that were never created would then be reported as successfully cleaned up.
# These cases are what close that off.
# ---------------------------------------------------------------------------
def test_a_hand_written_resource_list_is_not_evidence(config, inventory_file):
    """The exact F2 artifact: coverage-complete, correctly targeted, and unverifiable.

    Every name is right, the account/region/environment all match, and it would have passed
    before. It is refused because nothing in it records that anything was ever observed.
    """
    typed_up = {
        "environment": "dev",
        "account": ACCOUNT,
        "region": REGION,
        "resources": [
            {"type": entry["type"], "name": entry["name"]} for entry in derived_for()
        ],
    }
    config["inventory_file"] = inventory_file(typed_up)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert "is not a " + t.RECEIPT_SCHEMA in str(caught.value)
    assert "cannot establish that these resources were ever present" in str(
        caught.value
    )


@pytest.mark.parametrize(
    "schema",
    [None, "", "superplane.u1l1.pre-teardown-observation/2", "some.other/1", 1],
)
def test_only_this_receipt_schema_is_accepted(config, inventory_file, schema):
    config["inventory_file"] = inventory_file(inventory_document(schema=schema))
    with pytest.raises(EvidenceError, match="is not a superplane"):
        verify(config)


@pytest.mark.parametrize("observation", [t.ABSENT, t.INDETERMINATE, None, "yes", True])
def test_an_entry_not_observed_present_cannot_prove_cleanup(
    config, inventory_file, observation
):
    """A resource nobody saw before teardown says nothing about cleanup afterwards.

    `INDETERMINATE` is the important one: the recorder could not tell. Folding that into
    "it was there" would make a denied or throttled pre-teardown read into evidence.
    """
    resources = [{**entry, "observation": t.PRESENT} for entry in derived_for()]
    resources[0] = {**resources[0], "observation": observation}
    config["inventory_file"] = inventory_file(inventory_document(resources=resources))
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert "was not observed PRESENT before teardown" in str(caught.value)
    assert resources[0]["name"] in str(caught.value)


@pytest.mark.parametrize("complete", [False, None, "true", 1])
def test_a_receipt_that_calls_itself_incomplete_is_refused(
    config, inventory_file, complete
):
    """A recorder that could not see everything must not be able to launder that."""
    config["inventory_file"] = inventory_file(inventory_document(complete=complete))
    with pytest.raises(
        EvidenceError, match="reports its own observation as incomplete"
    ):
        verify(config)


# --- F2: provenance. A well-formed receipt must still be THIS deployment's receipt. ---
@pytest.mark.parametrize(
    "deploy,expected",
    [
        (None, "names no deploy"),
        ("not-an-object", "names no deploy"),
        (
            {"run_id": 999, "run_attempt": 1, "revision": DEPLOY_SHA},
            "different deploy run",
        ),
        (
            {"run_id": 101, "run_attempt": 2, "revision": DEPLOY_SHA},
            "different deploy run",
        ),
        ({"run_id": 101, "run_attempt": 1, "revision": "c" * 40}, "revision differs"),
        ({"run_id": 101, "run_attempt": 1}, "revision differs"),
    ],
)
def test_the_receipt_must_name_the_verified_deploy(
    config, inventory_file, deploy, expected
):
    config["inventory_file"] = inventory_file(inventory_document(deploy=deploy))
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"cluster": "adp-staging"}, "different cluster"),
        ({"cluster": None}, "different cluster"),
        (
            {"cluster_arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/other"},
            "does not match the cluster observed now",
        ),
        ({"cluster_arn": None}, "does not match the cluster observed now"),
    ],
)
def test_the_receipt_must_name_the_cluster_being_read_now(
    config, inventory_file, overrides, expected
):
    """A receipt from another cluster describes another deployment entirely."""
    config["inventory_file"] = inventory_file(inventory_document(**overrides))
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


def test_a_receipt_recorded_from_different_source_cannot_be_compared(
    config, inventory_file
):
    """The receipt's coverage is only comparable if it came from the same contract text."""
    drifted = source_hashes()
    drifted[SOURCE_FILES[0]] = "9" * 64
    config["inventory_file"] = inventory_file(inventory_document(source_sha256=drifted))
    with pytest.raises(EvidenceError, match="different deployed source"):
        verify(config)


@pytest.mark.parametrize("value", [None, {}, "abc"])
def test_a_receipt_with_no_source_hashes_is_refused(config, inventory_file, value):
    config["inventory_file"] = inventory_file(inventory_document(source_sha256=value))
    with pytest.raises(EvidenceError, match="different deployed source"):
        verify(config)


def test_a_receipt_taken_after_the_teardown_began_is_not_pre_teardown(
    config, inventory_file
):
    """An observation from after the destroy started cannot be a pre-teardown one.

    Combined with the deploy-completion bound below, this is a window that has already closed:
    a receipt written now, after both operations, cannot land inside it.
    """
    after = (UNDEPLOYED_AT + timedelta(minutes=1)).isoformat()
    config["inventory_file"] = inventory_file(inventory_document(observed_at=after))
    with pytest.raises(EvidenceError, match="taken after the teardown began"):
        verify(config)


def test_a_receipt_taken_before_the_deploy_finished_is_refused(config, inventory_file):
    """It cannot describe resources the deploy had not created yet."""
    before = (DEPLOYED_AT - timedelta(minutes=1)).isoformat()
    config["inventory_file"] = inventory_file(inventory_document(observed_at=before))
    with pytest.raises(EvidenceError, match="taken before the deploy finished"):
        verify(config)


@pytest.mark.parametrize(
    "observed_at,expected",
    [
        (None, "is missing"),
        ("", "is missing"),
        ("not-a-time", "is malformed"),
        ("2026-09-18T10:10:00", "not timezone-aware"),
    ],
)
def test_a_receipt_without_a_usable_observation_time_blocks(
    config, inventory_file, observed_at, expected
):
    config["inventory_file"] = inventory_file(
        inventory_document(observed_at=observed_at)
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


def test_the_receipt_provenance_is_carried_into_the_evidence(config):
    """A reader must be able to see what the presence claim rests on.

    Including WHERE the receipt's authority comes from: the recorder block is GitHub's own
    artifact record, not anything the receipt says about itself.
    """
    report = verify(config)
    provenance = report["pre_teardown_observation"]
    assert provenance["cluster_arn"] == CLUSTER_ARN
    assert datetime.fromisoformat(provenance["observed_at"]) == OBSERVED_AT
    recorder = provenance["recorder"]
    assert recorder["workflow"] == RECORDER_LANE
    assert recorder["run_id"] == RECORDER_RUN_ID
    assert recorder["run_attempt"] == 1
    assert recorder["revision"] == RECORDER_SHA
    assert recorder["artifact"] == "superplane-pre-teardown-receipt"
    assert recorder["digest"].startswith("sha256:")
    assert recorder["recorder_sha256"] == verifier_sha256()
    assert len(recorder["recorder_sha256"]) == 64
    assert [step["name"] for step in recorder["executed_steps"]] == list(RECORDER_STEPS)
    assert report["inventory"]["coverage"].startswith("every resource derived")


# ---------------------------------------------------------------------------
# F2: authenticating the RECORDER EXECUTION, not the receipt's claims about itself.
#
# The F2 finding in full: "Reject offline receipts on the live path and authenticate recorder
# execution/hash/time." Field-level cross-checks are necessary but insufficient — every field
# `bind_receipt` compares lives inside a local JSON file, and an author who knows the deploy run,
# attempt, revision, cluster ARN and source hashes (all of which this module derives and prints)
# can write a document that agrees with all of them. So the authority is GitHub's artifact record
# for a verified recorder run, and these cases break each part of it in turn.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("evidence_kind", ["offline-fixture", None, "", "LIVE", True])
def test_an_offline_receipt_is_rejected_on_the_live_path(
    config, inventory_file, evidence_kind
):
    """F2's first clause, stated directly: a fixture receipt cannot be live evidence.

    `record_pre_teardown` stamps `offline-fixture` whenever a transport was injected, and the
    loader used to ignore the field entirely — so a receipt the recorder itself declared to be
    fixture output was accepted by the live path.
    """
    config["inventory_file"] = inventory_file(
        inventory_document(evidence_kind=evidence_kind)
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "classifies its own evidence as" in message
    assert (
        "a fixture cannot establish that these resources were really present" in message
    )


def test_the_receipt_cannot_be_authenticated_because_no_recorder_lane_exists(
    config, monkeypatch
):
    """Against the REAL empty registry: the honest outcome is BLOCKED, naming the producer.

    This is the F2 counterpart of the Kubernetes deletion block. `record_pre_teardown` writes the
    document, but no workflow executes it under an identity GitHub can attest to, so the
    pre-teardown presence claim is unauthenticated — which makes U1-L1 acceptance 4 unprovable,
    not passing. Inventing an attestation is exactly what the finding forbids.
    """
    monkeypatch.setattr(t, "RECORDER_WORKFLOWS", {})
    assert t.RECORDER_WORKFLOWS == {}, "the shipped registry must stay empty"
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert message.startswith("BLOCKED:")
    # It says why the receipt's own fields are not enough...
    assert "are all contents of a local file" in message
    assert "establish only that somebody wrote them down" in message
    # ...names the producer precisely enough to build, including the artifact and the registry...
    assert "REQUIRED PRODUCER" in message
    assert "'superplane-pre-teardown-receipt'" in message
    assert "RECORDER_WORKFLOWS" in message
    assert "record_pre_teardown" in message
    # ...says what the authority would then be...
    assert "none of which the receipt's author controls" in message
    # ...and refuses to call the criterion passing.
    assert "unprovable, not passing" in message


def test_a_registered_recorder_lane_still_needs_a_supplied_run(config):
    """Omitting the run blocks; it does not fall back to trusting the receipt."""
    config["recorder_run"] = None
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "a recorder run must be supplied" in message
    assert "SUPERPLANE_LIVE_RECORDER_RUN_ID" in message


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"path": t.APPLY_WORKFLOW}, "not the registered recording lane"),
        (
            {"repository": {"full_name": "attacker/adp"}},
            "not the registered recording lane",
        ),
        ({"id": 999}, "not the registered recording lane"),
        ({"event": "push"}, f"was not a deliberate {t.DISPATCH_EVENT}"),
        ({"conclusion": "failure"}, "did not complete successfully"),
        ({"status": "in_progress"}, "did not complete successfully"),
        ({"run_attempt": 2}, "recorded attempt is not the attempt supplied"),
        ({"head_sha": "not-a-sha"}, "records no usable revision"),
        ({"head_sha": None}, "records no usable revision"),
    ],
)
def test_the_recorder_run_is_held_to_the_same_standard_as_the_other_lanes(
    config, overrides, expected
):
    """A receipt is only as good as the execution that produced it."""
    github = github_runs(**{str(RECORDER_RUN_ID): overrides})
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert expected in str(caught.value)


@pytest.mark.parametrize("conclusion", ["skipped", "failure", None])
def test_a_recorder_run_whose_recording_step_did_not_execute_is_refused(
    config, conclusion
):
    """A green run that skipped its recording step observed nothing."""
    github = github_runs(jobs={"recorder": [recorder_run(conclusion=conclusion)]})
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert f"'{RECORDER_STEPS[0]}' step did not execute" in str(caught.value)


def test_a_recorder_job_from_another_run_is_refused(config):
    github = github_runs(jobs={"recorder": [recorder_run(run_id=999)]})
    with pytest.raises(EvidenceError, match="belongs to a different run or attempt"):
        verify(config, github=github)


def test_the_receipt_verified_must_be_the_document_the_run_uploaded(
    config, attestations, tmp_path
):
    """The load-bearing F2 check: the local file must equal GitHub's attested document.

    Both documents here are internally consistent and would each pass every field-level binding
    on their own. What refuses is that they are not the same document — the one GitHub holds a
    digest for is not the one the verifier was handed.
    """
    uploaded = inventory_document()
    publish_artifact(attestations, "recorder", uploaded)
    # A receipt differing only in a field nothing else checks, so the equality comparison is
    # unambiguously what refuses rather than some other binding.
    handed_over = {**uploaded, "note": "same observations, different bytes"}
    path = tmp_path / "substituted-receipt.json"
    path.write_text(json.dumps(handed_over), encoding="utf-8")
    config["inventory_file"] = str(path)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "is not the document the recorder run uploaded" in message
    assert "not the ones GitHub holds a record of" in message


def test_a_receipt_whose_archive_was_edited_fails_the_digest(config, attestations):
    """The receipt artifact is authenticated through the same channel as every other one."""
    name = t.EXECUTION_ATTESTATION["recorder"][0]
    (Path(attestations) / f"{name}.zip").write_bytes(
        _archive(name, inventory_document(complete=True, note="rewritten"))
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert "does not match the digest GitHub recorded" in str(caught.value)


def test_a_recorder_run_publishing_no_receipt_artifact_blocks(config, attestations):
    drop_artifact("recorder")
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert (
        "does not publish exactly one 'superplane-pre-teardown-receipt' artifact"
        in message
    )
    assert "REQUIRED PRODUCER" in message


@pytest.mark.parametrize(
    "created,expected",
    [
        # GitHub stamped the upload before the deploy finished: it cannot describe what that
        # deploy created, whatever `observed_at` inside the document says. The bound is the
        # deploy's COMPLETION, so one minute before that is outside the window.
        (DEPLOYED_AT - timedelta(minutes=1), "uploaded before the deploy finished"),
        # Or after the destroy began. The bound is the undeploy's START — five minutes before
        # UNDEPLOYED_AT in the fixture timeline — so a minute after that start is outside too.
        (
            UNDEPLOYED_AT - timedelta(minutes=4),
            "uploaded after the teardown began",
        ),
    ],
)
def test_the_upload_time_github_stamped_must_fall_inside_the_closed_window(
    config, attestations, created, expected
):
    """GitHub's timestamp, not the receipt's. A backdated `observed_at` cannot move it.

    The window — deploy completion to undeploy start — has already closed, so a receipt produced
    after the fact cannot have been uploaded inside it. `observed_at` is left untouched and
    consistent here precisely so that the document's own claim is NOT what refuses.

    The recorder run's OWN attempt window is moved to contain the artifact (U1-201), so the
    artifact is legitimately that attempt's output and the only thing wrong with it is where it
    falls relative to the deploy and the teardown. Without this the attempt-provenance check
    would refuse first and this test would pass for the wrong reason — a different rule than the
    one it names.
    """
    github = github_runs(
        **{
            str(RECORDER_RUN_ID): {
                "run_started_at": (created - timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "updated_at": (created + timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        }
    )
    publish_artifact(
        attestations, "recorder", inventory_document(), created_at=created.isoformat()
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    "forged",
    [
        "forged-unvalidated-value",
        None,
        "",
        "0" * 64,
        # The hash of a DIFFERENT real file in the module, so the case cannot pass by the value
        # merely looking wrong.
        hashlib.sha256(
            (MODULE_ROOT / "superplane_acceptance" / "cli_delivery.py").read_bytes()
        ).hexdigest(),
    ],
)
def test_a_forged_recorder_hash_is_compared_not_copied(config, inventory_file, forged):
    """The exact defect F2 named: this field used to be copied into the evidence unchecked.

    So a value like "forged-unvalidated-value" appeared in the published record as if it meant
    something. It is now compared against the verifier module as GitHub served it at the recorder
    run's OWN revision, which is what makes it mean "this code took the observation".
    """
    config["inventory_file"] = inventory_file(
        inventory_document(recorder_sha256=forged)
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert (
        "is not the hash of this module at the recorder run's own revision" in message
    )
    assert "not the code that was deployed and reviewed" in message


def test_the_recorder_hash_is_taken_at_the_recorder_revision_not_the_deploy_revision(
    config,
):
    """Pinning it at the deploy revision would compare against code the recorder never ran.

    The recorder lane can legitimately be at a different commit from the apply — it runs between
    the deploy and the teardown. So this read is the one deliberate exception to "everything is
    read at the deployed revision", and the request GitHub actually receives proves it.
    """
    requested = []
    transport = github_runs()

    def github(path: str):
        requested.append(path)
        return transport(path)

    verify(config, github=github)
    verifier_reads = [path for path in requested if VERIFIER_PATH in path]
    assert verifier_reads == [f"contents/{VERIFIER_PATH}?ref={RECORDER_SHA}"]
    assert RECORDER_SHA != DEPLOY_SHA


def test_a_verifier_module_unreadable_at_the_recorder_revision_blocks(
    config, attestations
):
    """Without the module's bytes at that revision the hash cannot be checked, so it must block.

    Not "accept the receipt's value because the comparison could not be made" — that would restore
    the copied-without-checking behaviour by another route.
    """
    transport = github_runs()

    def github(path: str):
        if VERIFIER_PATH in path and RECORDER_SHA in path:
            raise EvidenceError("BLOCKED: GitHub metadata/source request failed")
        return transport(path)

    with pytest.raises(EvidenceError, match="BLOCKED"):
        verify(config, github=github)


def test_the_published_recorder_hash_is_the_verified_one_not_the_receipts(
    config, inventory_file, attestations
):
    """What lands in the evidence must be the value that was checked, from GitHub's bytes."""
    report = verify(config)
    recorder = report["pre_teardown_observation"]["recorder"]
    # The receipt's field agreed, so the two coincide — but the published value is the one read
    # from GitHub at the recorder revision, which is why it equals the module's real hash.
    assert recorder["recorder_sha256"] == verifier_sha256()
    assert recorder["uploaded_at"] == OBSERVED_AT.isoformat()
    assert recorder["artifact_id"] == ARTIFACT_ID["recorder"]
    # The receipt's own unvalidated claims are NOT carried through as provenance.
    assert "evidence_kind" not in recorder
    assert "complete" not in recorder


# ---------------------------------------------------------------------------
# Ownership: foreign and platform identities cannot be "verified absent".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "foreign",
    [
        # A name that merely CONTAINS the domain prefix — the unanchored-substring defect
        # U3's ownership contract was rewritten to reject.
        {"type": "aws_iam_role", "name": "gateway-adp-superplane-api"},
        # A type this domain owns, but the gateway's instance of it.
        {"type": "aws_iam_role", "name": "adp-dev-gateway-task"},
        # Right shape, wrong environment.
        {"type": "aws_iam_role", "name": "adp-prod-superplane-control-plane"},
        # Right shape, wrong account in the ARN.
        {
            "type": "aws_iam_role",
            "name": "adp-dev-superplane-control-plane",
            "arn": "arn:aws:iam::210987654321:role/adp-dev-superplane-control-plane",
        },
        # An SSM path outside the domain's own prefix.
        {"type": "aws_ssm_parameter", "name": "/adp/dev/gateway/database"},
        # Correctly shaped repository the release lock does not declare.
        {"type": "aws_ecr_repository", "name": "adp-superplane-api-gateway"},
        # A core namespace, which this domain must never report on. Well-formed as a receipt
        # entry — namespace and lifecycle present — so ownership is what has to refuse it and
        # the case cannot pass on a shape complaint instead.
        {
            "type": "kubernetes_namespace",
            "name": "kube-system",
            "namespace": "kube-system",
            "lifecycle": t.RETAINED,
        },
        {
            "type": "kubernetes_namespace",
            "name": "adp-gateway",
            "namespace": "adp-gateway",
            "lifecycle": t.RETAINED,
        },
        # F3: a namespaced object whose kind and name this domain DOES render, but in the
        # platform's namespace. Keying ownership by name alone would accept this and report
        # someone else's ServiceAccount as cleaned up.
        {
            "type": "kubernetes_service_account",
            "name": "skypilot-api",
            "namespace": "kube-system",
            "lifecycle": t.DELETED,
        },
        # Same kind and namespace as a real owned object, but a name nothing renders.
        {
            "type": "kubernetes_deployment",
            "name": "gateway-api",
            "namespace": "skypilot",
            "lifecycle": t.DELETED,
        },
    ],
)
def test_foreign_and_core_identities_are_refused(config, inventory_file, foreign):
    # Recorded as observed-present, so the receipt is well formed and ownership is what has to
    # do the refusing. A foreign resource really WAS present — it belongs to someone else —
    # which is precisely why presence alone must not make it reportable.
    resources = inventory_document()["resources"] + [
        {**foreign, "observation": t.PRESENT}
    ]
    config["inventory_file"] = inventory_file(inventory_document(resources=resources))
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert "not ours" in str(caught.value) or "does not own" in str(caught.value)


def test_ownership_refusal_happens_before_any_lookup(config, inventory_file):
    """A foreign identity must never reach the cloud APIs at all."""
    seen = []

    def recording(argv):
        seen.append(argv)
        return commands()(argv)

    resources = inventory_document()["resources"] + [
        {"type": "aws_iam_role", "name": "adp-dev-gateway-task"}
    ]
    config["inventory_file"] = inventory_file(inventory_document(resources=resources))
    with pytest.raises(EvidenceError):
        verify(config, run=recording)
    assert seen == []


# ---------------------------------------------------------------------------
# Absence vs. "could not tell": the distinction the criterion turns on.
# ---------------------------------------------------------------------------
def test_a_resource_still_present_fails_and_is_named(config):
    target = "adp-dev-superplane-control-plane"
    with pytest.raises(EvidenceError) as caught:
        verify(config, run=commands(per_resource={target: (0, '{"Role":{}}', "")}))
    assert "did not remove every resource" in str(caught.value)
    assert target in str(caught.value)


def _resource_key(entry: dict) -> str:
    """How `commands(per_resource=...)` addresses one derived resource.

    Kubernetes objects need the kind token because the rendered set deliberately reuses
    `skypilot-api` across ServiceAccount, Role, RoleBinding, Service and Deployment — a bare
    name would silently override five lookups at once and the test would prove nothing about
    which of them was read.
    """
    token = t.K8S_RESOURCE_TOKEN.get(entry["type"])
    if entry["type"] == "kubernetes_namespace":
        token = "namespace"
    return f"{token}/{entry['name']}" if token else entry["name"]


def test_every_resource_type_is_actually_observed(config):
    """Every derived resource must be individually read, and each read must be load-bearing.

    Parameterised over the WHOLE derived set, one resource at a time, each flipped to the
    outcome its own lifecycle forbids: a resource the teardown deletes must fail when its
    lookup reports it present, and the namespace U3 retains must fail when its lookup reports
    it absent. A resource the verifier never actually looked up cannot fail either way, so this
    is what catches an object silently dropped from the observation loop.
    """
    inventory = derived_for()
    # 18 AWS resources (including executor and SkyPilot ECR) + 10 rendered Kubernetes objects.
    # The split is asserted so a
    # derivation that stops producing some of them cannot quietly shrink this test.
    assert len(aws_scope(inventory)) == 18
    assert len(k8s_scope(inventory=inventory)) == 10
    assert len(inventory) == 28
    for entry in inventory:
        key = _resource_key(entry)
        if entry.get("lifecycle") == t.RETAINED:
            # Absent, i.e. the retention contract was violated.
            outcome = (
                255,
                "",
                "An error occurred (NotFound) when calling the operation",
            )
            expected = (
                "U3's teardown contract retains these objects, but they are absent"
            )
        else:
            outcome = (0, "{}", "")
            expected = "did not remove every resource the deploy created"
        with pytest.raises(EvidenceError) as caught:
            verify(config, run=commands(per_resource={key: outcome}))
        assert expected in str(caught.value)
        assert entry["name"] in str(caught.value)


@pytest.mark.parametrize(
    "stderr",
    [
        "An error occurred (AccessDenied) when calling the operation",
        "An error occurred (ThrottlingException) when calling the operation",
        "ExpiredToken: the security token included in the request is expired",
        "Unable to locate credentials",
        "Unable to connect to the server: dial tcp: i/o timeout",
        "error: You must be logged in to the server (Unauthorized)",
        "",
    ],
)
def test_denied_or_failed_reads_are_indeterminate_not_absent(config, stderr):
    """A query that did not answer must block, never count as cleanup."""
    target = "adp-dev-superplane-control-plane"
    with pytest.raises(EvidenceError) as caught:
        verify(config, run=commands(per_resource={target: (1, "", stderr)}))
    assert "could not be observed either way" in str(caught.value)
    assert target in str(caught.value)


def test_a_not_found_token_for_another_api_does_not_prove_absence(config):
    """`ParameterNotFound` from an IAM call is not evidence the role is gone."""
    target = "adp-dev-superplane-control-plane"
    with pytest.raises(EvidenceError, match="could not be observed either way"):
        verify(
            config,
            run=commands(
                per_resource={
                    target: (255, "", "An error occurred (ParameterNotFound)")
                }
            ),
        )


def test_absence_classification_is_three_way():
    resource = {"type": "aws_iam_role", "name": "adp-dev-superplane-control-plane"}
    config = {"region": REGION}
    assert t.observe_absence(resource, config, commands(absent=True)) == t.ABSENT
    assert t.observe_absence(resource, config, commands(absent=False)) == t.PRESENT
    assert (
        t.observe_absence(
            resource, config, commands(per_resource={resource["name"]: (1, "", "boom")})
        )
        == t.INDETERMINATE
    )


# ---------------------------------------------------------------------------
# Execution evidence: no receipt, no cleanup claim.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "run_id,patch,expected",
    [
        (101, {"conclusion": "failure"}, "did not complete successfully"),
        (
            101,
            {"status": "in_progress", "conclusion": None},
            "did not complete successfully",
        ),
        (202, {"conclusion": "cancelled"}, "did not complete successfully"),
        (
            101,
            {"path": ".github/workflows/gateway-deploy.yml"},
            "is not superplane-infra-apply",
        ),
        (202, {"path": t.APPLY_WORKFLOW}, "is not superplane-infra-destroy"),
        (101, {"head_sha": "c" * 40}, "did not execute the stated revision"),
        (202, {"head_sha": "d" * 40}, "did not execute the stated revision"),
        (
            101,
            {"repository": {"full_name": "someone/fork"}},
            "belongs to another repository",
        ),
        (202, {"id": 999}, "identity does not match"),
        (101, {"updated_at": None}, "completion time is missing"),
        (101, {"updated_at": "not-a-time"}, "completion time is malformed"),
        (101, {"run_started_at": None}, "start time is missing"),
        (202, {"run_started_at": "not-a-time"}, "start time is malformed"),
        # A naive instant cannot be ordered against the others without guessing a zone.
        (101, {"updated_at": "2026-09-18T10:00:00"}, "not timezone-aware"),
    ],
)
def test_operation_receipts_must_be_real_and_successful(
    config, run_id, patch, expected
):
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github_runs(**{str(run_id): patch}))
    assert expected in str(caught.value)


def test_a_missing_run_blocks(config):
    with pytest.raises(EvidenceError, match="BLOCKED"):
        verify(config, github=github_runs(**{"202": None}))


# --- F1: the run must be the right target, deliberately triggered, on a real attempt. ---
@pytest.mark.parametrize("run_id", [101, 202])
@pytest.mark.parametrize("event", ["push", "schedule", "repository_dispatch", None])
def test_a_run_nobody_dispatched_is_not_one_of_these_lanes(config, run_id, event):
    """Both lanes are `workflow_dispatch`-only, so another trigger is a different thing.

    A push build that happens to carry the workflow path is not an authorized execution of it,
    and crediting one would let an unrelated CI event stand in for an operator's teardown.
    """
    with pytest.raises(EvidenceError, match="was not a deliberate workflow_dispatch"):
        verify(config, github=github_runs(**{str(run_id): {"event": event}}))


@pytest.mark.parametrize("attempt", [None, 0, -1, "1", 1.0])
def test_a_run_without_a_usable_attempt_number_blocks(config, attempt):
    """Steps can only be attributed to an execution if the attempt is known.

    `run_attempt: None` was the concrete F1 case: with no attempt, step evidence was fetched
    from nowhere in particular and the run-level green was all that remained.
    """
    with pytest.raises(EvidenceError, match="records no attempt number"):
        verify(config, github=github_runs(**{"101": {"run_attempt": attempt}}))


def test_steps_from_another_attempt_are_not_credited_to_this_one(config):
    """A re-run makes a new attempt; the earlier attempt's steps are not this one's."""
    _, job_name, _ = t.LANE_EXECUTION["undeploy"]
    stale = [
        {
            "name": job_name,
            "run_id": 202,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "steps": _steps("undeploy"),
        }
    ]
    with pytest.raises(EvidenceError, match="belongs to a different run or attempt"):
        verify(
            config,
            github=github_runs(jobs={"undeploy": stale}, **{"202": {"run_attempt": 2}}),
        )


def test_a_job_from_another_run_is_refused(config):
    """A job record naming a different run cannot evidence this run's execution."""
    _, job_name, _ = t.LANE_EXECUTION["deploy"]
    foreign = [
        {
            "name": job_name,
            "run_id": 999,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "steps": _steps("deploy"),
        }
    ]
    with pytest.raises(EvidenceError, match="belongs to a different run or attempt"):
        verify(config, github=github_runs(jobs={"deploy": foreign}))


@pytest.mark.parametrize("role", ["deploy", "undeploy"])
def test_a_lane_whose_job_is_absent_or_duplicated_blocks(config, role):
    _, job_name, _ = t.LANE_EXECUTION[role]
    template = {
        "name": job_name,
        "run_id": 101 if role == "deploy" else 202,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "steps": _steps(role),
    }
    for job_list in ([], [template, dict(template)], [{**template, "name": "Other"}]):
        with pytest.raises(EvidenceError, match="does not contain exactly one"):
            verify(config, github=github_runs(jobs={role: job_list}))


@pytest.mark.parametrize("role", ["deploy", "undeploy"])
def test_a_green_run_whose_job_failed_or_was_skipped_is_refused(config, role):
    _, job_name, _ = t.LANE_EXECUTION[role]
    for patch in (
        {"conclusion": "skipped"},
        {"conclusion": "failure"},
        {"status": "in_progress", "conclusion": None},
    ):
        job = {
            "name": job_name,
            "run_id": 101 if role == "deploy" else 202,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "steps": _steps(role),
            **patch,
        }
        with pytest.raises(EvidenceError, match="did not complete successfully"):
            verify(config, github=github_runs(jobs={role: [job]}))


# --- F1: the no-op destroy. A lane that did nothing still concludes `success`. ---
@pytest.mark.parametrize(
    "step",
    [
        "Save the destroy plan",
        "Validate every deletion is domain-owned",
        "Terraform Destroy",
    ],
)
def test_the_empty_state_destroy_that_skips_its_work_is_refused(config, step):
    """`EMPTY_STATE=true` skips the plan, the ownership check and the destroy itself.

    superplane-infra-destroy.yml guards each of those steps on a non-empty Terraform state, so
    a destroy against empty state skips all three and the run still concludes `success`. That
    is indistinguishable from a real teardown at run level, and it tore nothing down — the
    resources may never have been in state at all. Step conclusions are the only place the
    difference is visible.
    """
    github = github_runs(steps={"undeploy": {step: "skipped"}})
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert f"{step!r} step did not execute" in str(caught.value)
    assert "has not torn down anything" in str(caught.value)


@pytest.mark.parametrize("conclusion", ["skipped", "failure", "cancelled", None])
def test_the_apply_that_never_applied_is_refused(config, conclusion):
    """Symmetrically for the deploy: no apply, no deployment to have torn down."""
    github = github_runs(steps={"deploy": {"Terraform Apply": conclusion}})
    with pytest.raises(EvidenceError, match="'Terraform Apply' step did not execute"):
        verify(config, github=github)


def test_a_renamed_or_missing_step_blocks_rather_than_passing(config):
    """If the step this check looks for is gone, it can no longer prove execution."""
    _, job_name, required = t.LANE_EXECUTION["undeploy"]
    trimmed = [
        {
            "name": job_name,
            "run_id": 202,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {"name": name, "conclusion": "success"}
                for name in required
                if name != "Terraform Destroy"
            ],
        }
    ]
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github_runs(jobs={"undeploy": trimmed}))
    assert "ran no step named 'Terraform Destroy'" in str(caught.value)
    assert "both must block" in str(caught.value)


@pytest.mark.parametrize("steps", [None, [], "Terraform Destroy", [["not a dict"]]])
def test_a_lane_reporting_no_usable_steps_blocks(config, steps):
    _, job_name, _ = t.LANE_EXECUTION["undeploy"]
    job = {
        "name": job_name,
        "run_id": 202,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "steps": steps,
    }
    with pytest.raises(EvidenceError, match="BLOCKED"):
        verify(config, github=github_runs(jobs={"undeploy": [job]}))


def test_an_unreadable_per_attempt_job_list_blocks(config):
    def github(path: str):
        if "/attempts/" in path:
            return {"jobs": None}
        return github_runs()(path)

    with pytest.raises(EvidenceError, match="per-attempt job list is unreadable"):
        verify(config, github=github)


def test_step_evidence_is_read_from_the_recorded_attempt(
    config, inventory_file, attestations
):
    """Both the job list AND the attempt record must be requested for the run's own attempt.

    A re-run's evidence lives under its own attempt number, so requesting attempt 1 would read
    another execution's jobs and another execution's window. This asserts the URLs actually
    requested, which is the only way to tell the difference from the outside.
    """
    asked = []
    transport = github_runs(**{str(DEPLOY_RUN_ID): {"run_attempt": 3}})

    def github(path: str):
        if "/attempts/" in path:
            asked.append(path)
        return transport(path)

    config["inventory_file"] = inventory_file(
        inventory_document(
            deploy={
                "run_id": DEPLOY_RUN_ID,
                "run_attempt": 3,
                "revision": DEPLOY_SHA,
                "run_url": f"https://github.com/aws-e/adp/actions/runs/{DEPLOY_RUN_ID}",
            }
        )
    )
    # Re-published for attempt 3. The artifact record itself cannot say "attempt 3" — the API
    # has no such field (U1-201) — so the binding is the upload time falling inside attempt 3's
    # window, which the transport derives from the run record above.
    publish_artifact(
        attestations, "deploy", attestation_document("deploy", run_attempt=3)
    )
    verify(config, github=github)
    assert f"actions/runs/{DEPLOY_RUN_ID}/attempts/3/jobs" in asked
    assert f"actions/runs/{DEPLOY_RUN_ID}/attempts/3" in asked
    assert f"actions/runs/{UNDEPLOY_RUN_ID}/attempts/1/jobs" in asked
    assert f"actions/runs/{UNDEPLOY_RUN_ID}/attempts/1" in asked
    # Nothing was read for attempt 1 of the re-run: its jobs and window belong to a different
    # execution and must not be consulted at all.
    assert f"actions/runs/{DEPLOY_RUN_ID}/attempts/1/jobs" not in asked


def test_an_artifact_from_an_earlier_attempt_is_not_credited_to_the_verified_one(
    config, attestations, environment, inventory_file
):
    """F1 / U1-201: the re-run case, established from GitHub's upload timestamp.

    A re-run produces a new attempt, and an artifact left behind by the earlier attempt is
    evidence of what THAT attempt did. Crediting it to the attempt being verified would let a
    no-op re-run inherit its predecessor's proof.

    The previous version of this test expressed the case through the artifact record's
    `workflow_run.run_attempt` — a field the artifacts API does not return, so it was asserting a
    refusal that could only ever fire on fabricated metadata (U1-201). The real signal is time:
    attempts of one run are consecutive, so an artifact uploaded before this attempt started
    belongs to an earlier one. Here the deploy is verified at attempt 2, whose window opens after
    attempt 1 finished, and the artifact carries attempt 1's upload time.
    """
    # Attempt 1 ran and closed; attempt 2 is the one under verification.
    first_upload = DEPLOYED_AT - timedelta(minutes=15)
    github = github_runs(
        attempt_records={
            (DEPLOY_RUN_ID, 2): {
                "id": DEPLOY_RUN_ID,
                "run_attempt": 2,
                "head_sha": DEPLOY_SHA,
                "status": "completed",
                "conclusion": "success",
                # The second attempt's window opens well after the first one closed.
                "run_started_at": (DEPLOYED_AT - timedelta(minutes=5))
                .isoformat()
                .replace("+00:00", "Z"),
                "updated_at": DEPLOYED_AT.isoformat().replace("+00:00", "Z"),
            }
        },
        **{str(DEPLOY_RUN_ID): {"run_attempt": 2}},
    )
    # The leftover artifact: uploaded by attempt 1, inside attempt 1's window.
    publish_artifact(
        attestations,
        "deploy",
        attestation_document("deploy", run_attempt=2),
        created_at=first_upload.isoformat().replace("+00:00", "Z"),
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    message = str(caught.value)
    assert "before attempt 2 began" in message
    assert "evidence from an earlier attempt of that run" in message


def test_an_attestation_published_by_another_run_is_not_found(config, attestations):
    """F1: the artifact is looked up on the verified run, so another run's copy is not visible.

    GitHub scopes artifacts to the run that uploaded them. Moving the destroy attestation onto
    the apply's run therefore leaves the destroy run publishing nothing — and the refusal names
    the producer that is missing rather than falling back to the copy that does exist.
    """
    publish_artifact(
        attestations,
        "undeploy",
        attestation_document("undeploy"),
        workflow_run={"id": DEPLOY_RUN_ID, "run_attempt": 1},
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "does not publish exactly one 'superplane-destroy-attestation'" in message
    assert "REQUIRED PRODUCER" in message


# --- F1, the target claims. Each one names something this run established for itself, so the
# case below breaks exactly that agreement and nothing else. ---
@pytest.mark.parametrize(
    "role,overrides,expected",
    [
        # WHERE: the account. `superplane-infra-apply.yml` resolves its account from the runner's
        # own STS identity and records it nowhere in the run metadata, so a genuinely green apply
        # in another account was previously pairable with observations taken in this one.
        (
            "deploy",
            {"account": "210987654321"},
            "operated in account '210987654321', not the account these observations",
        ),
        (
            "undeploy",
            {"account": "210987654321"},
            "operated in account '210987654321', not the account these observations",
        ),
        # WHERE: the environment. The destroy lane takes it as a dispatch input, so two runs of
        # the same lane at the same revision can legitimately target two environments.
        ("deploy", {"environment": "prod"}, "operated on environment 'prod'"),
        ("undeploy", {"environment": "staging"}, "operated on environment 'staging'"),
        ("deploy", {"region": "eu-west-1"}, "operated in region 'eu-west-1'"),
        # WHERE: the state object. Two values of `environment` address two different state keys in
        # the same bucket; a run that destroyed another state's contents is not this teardown.
        (
            "undeploy",
            {"state_key": "prod/modules/superplane/terraform.tfstate"},
            "operated on state object",
        ),
        (
            "undeploy",
            {"state_bucket": "adp-terraform-state-210987654321"},
            "operated on state object",
        ),
        # A literal placeholder reaching the comparison would compare against a bucket that does
        # not exist — so `ACCOUNT_ID` must have been resolved, and an unresolved one is refused.
        (
            "deploy",
            {"state_bucket": "adp-terraform-state-ACCOUNT_ID"},
            "operated on state object",
        ),
        # WHAT: the plan's own action. An apply attestation cannot evidence a destroy.
        ("undeploy", {"action": "create"}, "records a 'create' plan, not the 'delete'"),
        ("deploy", {"action": "delete"}, "records a 'delete' plan, not the 'create'"),
        # WHICH execution: the run, attempt, revision and workflow the document claims must be
        # the ones `verify_operations` verified.
        (
            "deploy",
            {"run_id": 999},
            "does not describe the execution that was verified",
        ),
        (
            "deploy",
            {"revision": "c" * 40},
            "does not describe the execution that was verified",
        ),
        (
            "undeploy",
            {"workflow": t.APPLY_WORKFLOW},
            "does not describe the execution that was verified",
        ),
        (
            "deploy",
            {"repository": "attacker/adp"},
            "does not describe the execution that was verified",
        ),
        # Not this schema at all.
        ("deploy", {"schema": "superplane.u1l1.execution-attestation/2"}, "is not a "),
    ],
)
def test_an_attestation_that_disagrees_with_an_established_fact_is_refused(
    config, attestations, role, overrides, expected
):
    """F1: every field the check trusts is compared against something it established itself.

    The account comes from the STS read taken moments ago, the environment and region from the
    selected target, the bucket and key from the deployed revision's own backend tfvars, and the
    run identity from `verify_operations`. A lane's attestation supplies the lane's side of each;
    these cases each break one and assert the specific refusal.
    """
    publish_artifact(attestations, role, attestation_document(role, **overrides))
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


@pytest.mark.parametrize("field", t.ATTESTATION_FIELDS)
def test_an_attestation_missing_a_required_field_names_it(config, attestations, field):
    """A refusal must name the missing field, so the producer is buildable from the message."""
    document = attestation_document("deploy")
    document.pop(field)
    publish_artifact(attestations, "deploy", document)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert message.startswith("BLOCKED: the deploy attestation omits")
    assert field in message


@pytest.mark.parametrize("field", t.STATE_ATTESTATION_FIELDS)
def test_a_terraform_attestation_must_carry_its_state_object(
    config, attestations, field
):
    """The state binding is what makes "the same environment" verifiable rather than a label."""
    document = attestation_document("undeploy")
    document.pop(field)
    publish_artifact(attestations, "undeploy", document)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert field in str(caught.value)


@pytest.mark.parametrize("field", t.CLUSTER_ATTESTATION_FIELDS)
def test_a_kubernetes_attestation_must_carry_its_cluster(config, attestations, field):
    """A namespaced object's identity is only complete with the cluster it lives in."""
    document = attestation_document("rollout")
    document.pop(field)
    publish_artifact(attestations, "rollout", document)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert field in str(caught.value)


@pytest.mark.parametrize(
    "overrides,expected",
    [
        (
            {"cluster": "adp-staging"},
            "acted on cluster 'adp-staging', not the cluster these observations",
        ),
        (
            {"cluster_arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/other"},
            "acted on cluster",
        ),
    ],
)
def test_a_kubernetes_attestation_from_another_cluster_is_refused(
    config, attestations, overrides, expected
):
    """The same namespace and name exist independently in every cluster."""
    publish_artifact(
        attestations, "rollout", attestation_document("rollout", **overrides)
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


# --- F1, the artifact record. This is the authority the whole mechanism rests on, so the checks
# on GitHub's own fields get their own cases. ---
def test_an_expired_artifact_cannot_authenticate_anything(config, attestations):
    """Expired means GitHub no longer holds the bytes, so nothing can be hashed against it."""
    publish_artifact(
        attestations, "deploy", attestation_document("deploy"), expired=True
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "artifact has expired" in message
    assert (
        "can no longer be authenticated against the digest GitHub recorded" in message
    )


def test_artifact_fixtures_match_the_real_api_shape(attestations):
    """The guard against the bug class U1-201 was: fixtures modelling a field GitHub never sends.

    Every one of the 357 tests passed while the verifier required
    `workflow_run.run_attempt`, because the fixtures invented it too. The suite therefore
    confirmed the invention instead of the API, and the defect only surfaced when a reviewer
    compared the code against a real response.

    So this asserts the fixtures against the recorded real shape directly. A future author who
    adds a convenient field to make a check pass has to change this tuple, which is where the
    comment explaining that GitHub does not send it lives.
    """
    for role in t.EXECUTION_ATTESTATION:
        record = next(
            item
            for item in ARTIFACTS
            if item["name"] == t.EXECUTION_ATTESTATION[role][0]
        )
        assert (
            tuple(sorted(record["workflow_run"])) == REAL_ARTIFACT_WORKFLOW_RUN_KEYS
        ), f"the {role} artifact fixture does not match the real API shape"
        assert "run_attempt" not in record["workflow_run"]


def test_the_verifier_never_reads_an_attempt_from_the_artifact_record(
    config, attestations
):
    """An invented `run_attempt` on the artifact record must buy nothing.

    The positive control for the repair: the deploy artifact is republished carrying a bogus
    attempt number, and verification still succeeds — because the attempt comes from GitHub's
    per-attempt endpoint and the artifact is placed on it by upload time. Under the previous
    implementation this field was the binding, so a document could choose its own attempt.
    """
    publish_artifact(
        attestations,
        "deploy",
        attestation_document("deploy"),
        workflow_run={
            "id": DEPLOY_RUN_ID,
            "repository_id": REPOSITORY_ID,
            "head_repository_id": REPOSITORY_ID,
            "head_branch": "main",
            "head_sha": DEPLOY_SHA,
            "run_attempt": 99,
        },
    )
    assert verify(config)["status"] == "matched"


@pytest.mark.parametrize(
    "record,expected",
    [
        (None, "attempt 1 is unreadable"),
        ({}, "attempt record is not attempt 1"),
        # The attempt record must be the run's own.
        (
            {"id": 999, "run_attempt": 1, "head_sha": DEPLOY_SHA},
            "belongs to a different run than the one verified",
        ),
        # A re-run dispatched against another head is a different execution, and its evidence
        # is not evidence for the revision that was deployed.
        (
            {"id": DEPLOY_RUN_ID, "run_attempt": 1, "head_sha": "7" * 40},
            f"not the {DEPLOY_SHA!r} under verification",
        ),
        # A later attempt's success cannot be credited to the attempt being verified.
        (
            {
                "id": DEPLOY_RUN_ID,
                "run_attempt": 1,
                "head_sha": DEPLOY_SHA,
                "status": "completed",
                "conclusion": "failure",
            },
            "did not itself complete successfully",
        ),
        # The window bounds the artifact check, so an unusable one must block rather than be
        # treated as "unknown, therefore fine".
        (
            {
                "id": DEPLOY_RUN_ID,
                "run_attempt": 1,
                "head_sha": DEPLOY_SHA,
                "status": "completed",
                "conclusion": "success",
            },
            "attempt 1 start time is missing",
        ),
    ],
)
def test_the_per_attempt_record_must_establish_the_execution(
    config, attestations, record, expected
):
    """The attempt is authoritative only if the record it comes from is usable (U1-201).

    Provenance moved onto this endpoint, so each way its answer can fail to establish the
    execution has to refuse. Otherwise the repair would have replaced a field that does not
    exist with a source that is not checked.
    """
    github = github_runs(attempt_records={(DEPLOY_RUN_ID, 1): record})
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert expected in str(caught.value)


def test_two_artifacts_of_one_name_leave_the_document_unknown(config, attestations):
    """Which of them the run produced is then undecidable, so neither may be credited."""
    duplicate_artifact("deploy")
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert "does not publish exactly one 'superplane-apply-attestation'" in str(
        caught.value
    )


def test_a_lane_publishing_no_attestation_blocks_naming_every_required_field(
    config, attestations
):
    """The fail-closed contract: name the producer and the fields, do not infer the target."""
    drop_artifact("undeploy")
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert message.startswith("BLOCKED:")
    assert "REQUIRED PRODUCER" in message
    assert "superplane-destroy-attestation" in message
    # Every field a producer owes is listed, including the state binding a Terraform lane adds.
    for field in t.ATTESTATION_FIELDS + t.STATE_ATTESTATION_FIELDS:
        assert field in message
    assert "resolved at execution time rather than restated from its inputs" in message
    # And it says WHY step success is not enough on its own.
    assert (
        "recorded nowhere cannot be paired with observations taken elsewhere" in message
    )


@pytest.mark.parametrize(
    "record,expected",
    [
        # No digest at all, or one that is not a sha256.
        ({"digest": None}, "carries no sha256 digest"),
        ({"digest": "md5:" + "0" * 32}, "carries no sha256 digest"),
        ({"digest": "sha256:not-hex"}, "carries no sha256 digest"),
        # A digest that IS well-formed but is not this archive's: the tampered-archive case.
        (
            {"digest": "sha256:" + "0" * 64},
            "does not match the digest GitHub recorded for that run's artifact",
        ),
        # GitHub's creation time is load-bearing for the recorder window, so an unusable one
        # blocks rather than being treated as "unknown, therefore fine".
        ({"created_at": None}, "creation time is missing"),
        ({"created_at": "not-a-time"}, "creation time is malformed"),
        ({"created_at": "2026-09-18T10:00:00"}, "creation time is not timezone-aware"),
    ],
)
def test_githubs_own_artifact_fields_must_be_usable(
    config, attestations, record, expected
):
    """None of these is the document's to supply, and an unusable one must block."""
    publish_artifact(attestations, "deploy", attestation_document("deploy"), **record)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    "workflow_run,expected",
    [
        # No binding at all, or an unusable one: nothing attributes the artifact to a run.
        (None, "carries no run binding"),
        ("run-101", "carries no run binding"),
        ({}, "belongs to a different run than the one verified"),
        # Another run's artifact, listed under this one.
        (
            {"id": 999, "head_sha": DEPLOY_SHA},
            "belongs to a different run than the one verified",
        ),
        # The right run, but the commit it executed is not the revision under verification —
        # a build of another branch cannot supply this revision's evidence. `head_sha` is a
        # field the artifacts API really returns, which is why the check can rest on it.
        (
            {"id": DEPLOY_RUN_ID, "head_sha": "9" * 40},
            "was produced for revision",
        ),
        ({"id": DEPLOY_RUN_ID}, "was produced for revision"),
    ],
)
def test_an_artifact_whose_run_binding_is_unusable_is_refused(
    config, attestations, workflow_run, expected
):
    """The binding must be checked even when GitHub lists the artifact under the right run.

    The default transport filters the listing by `workflow_run.id`, exactly as the API does, so a
    mismatched binding normally makes the artifact invisible and the refusal becomes "publishes no
    artifact" — a different check. This transport deliberately lists it anyway, which is what
    isolates the binding comparison.

    Every case here uses only fields the artifacts API actually returns (U1-201): the run `id` and
    the `head_sha` of the commit that run executed. The attempt is not among them and is therefore
    established elsewhere — see
    `test_an_artifact_from_an_earlier_attempt_is_not_credited_to_the_verified_one`.
    """
    record = publish_artifact(
        attestations,
        "deploy",
        attestation_document("deploy"),
        workflow_run=workflow_run,
    )
    transport = github_runs()

    def github(path: str):
        if path == f"actions/runs/{DEPLOY_RUN_ID}/artifacts?per_page=100&page=1":
            return {"artifacts": [record]}
        return transport(path)

    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert expected in str(caught.value)


def test_an_edited_attestation_that_is_not_republished_fails_the_digest(
    config, attestations
):
    """The concrete tampering case: the file on disk is the caller's, the digest is not.

    This is what makes `SUPERPLANE_LIVE_ATTESTATION_DIR` being caller-controlled harmless. The
    archive is rewritten with a different account, GitHub's record is left alone, and the
    substitution is caught by the hash rather than by anything the document says.
    """
    name = t.EXECUTION_ATTESTATION["deploy"][0]
    original = (Path(attestations) / f"{name}.zip").read_bytes()
    (Path(attestations) / f"{name}.zip").write_bytes(
        _archive(name, attestation_document("deploy", account="210987654321"))
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert "does not match the digest GitHub recorded" in str(caught.value)
    # Restoring the original bytes restores agreement, which proves the digest is what refused
    # rather than some incidental difference between the two documents.
    (Path(attestations) / f"{name}.zip").write_bytes(original)
    verify(config)


def test_a_missing_downloaded_archive_says_what_to_download(config, attestations):
    (Path(attestations) / "superplane-apply-attestation.zip").unlink()
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "was not found at superplane-apply-attestation.zip" in message
    assert "its digest is checked against GitHub's own record" in message


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"", "is empty or exceeds the size limit"),
        (b"not-a-zip-at-all", "could not be read"),
    ],
)
def test_an_unreadable_attestation_archive_blocks(config, attestations, raw, expected):
    publish_bytes(attestations, "deploy", raw)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


def test_an_oversized_attestation_archive_is_refused(config, attestations):
    """A bound on what this checker will read, so a hostile archive cannot exhaust it."""
    publish_bytes(attestations, "deploy", b"P" * (t.MAX_ATTESTATION_BYTES + 1))
    with pytest.raises(EvidenceError, match="exceeds the size limit"):
        verify(config)


@pytest.mark.parametrize(
    "members,expected",
    [
        ({"readme.txt": b"nothing here"}, "does not contain exactly one JSON document"),
        (
            {"a.json": b"{}", "b.json": b"{}"},
            "does not contain exactly one JSON document",
        ),
        ({"one.json": b"{not json"}, "is not valid JSON"),
        ({"one.json": b"[]"}, "must be a JSON object"),
        ({"one.json": b'"a string"'}, "must be a JSON object"),
    ],
)
def test_an_archive_that_is_not_one_json_document_blocks(
    config, attestations, members, expected
):
    """Two documents leave which one is the attestation undecided; zero leaves nothing to read."""
    publish_bytes(attestations, "deploy", _zip(members))
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


# --- F1, the plan contents. Step success says a plan was applied, not which identities were in
# it, so the attested identity set is compared against the derivation. ---
def test_an_attestation_omitting_a_derived_resource_cannot_account_for_it(
    config, attestations
):
    """A destroy plan that never mentioned a resource cannot be why it is absent now."""
    scope = aws_scope()
    dropped = scope[0]
    publish_artifact(
        attestations,
        "undeploy",
        attestation_document(
            "undeploy",
            resources=[{"type": e["type"], "name": e["name"]} for e in scope[1:]],
        ),
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "did not contain every resource in scope" in message
    assert label(dropped) in message


def test_an_attested_plan_may_carry_more_than_the_derivation(config, attestations):
    """Coverage is one-directional: an extra identity in the plan is not this check's business.

    An operator-imported resource or an inline policy legitimately appears in a plan without
    appearing in the derivation. Refusing that would make the check unsatisfiable rather than
    stricter.
    """
    resources = [{"type": e["type"], "name": e["name"]} for e in aws_scope()] + [
        {"type": "aws_iam_policy", "name": "adp-dev-superplane-extra"}
    ]
    publish_artifact(
        attestations, "undeploy", attestation_document("undeploy", resources=resources)
    )
    assert verify(config)["status"] == "matched"


def test_a_kubernetes_identity_attested_in_another_namespace_does_not_count(
    config, attestations
):
    """F3 inside F1: the namespace is part of the identity in the attestation too.

    The rollout attests the right kinds and the right names in the platform's namespace. Keying
    coverage on name alone would accept that and credit someone else's objects as this
    deployment's.
    """
    publish_artifact(
        attestations,
        "rollout",
        attestation_document(
            "rollout",
            resources=[
                {
                    "type": e["type"],
                    "name": e["name"],
                    **({"namespace": "kube-system"} if e.get("namespace") else {}),
                }
                for e in k8s_scope()
            ],
        ),
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert "rollout lane's plan did not contain every resource in scope" in message
    for entry in k8s_scope():
        assert label(entry) in message


@pytest.mark.parametrize(
    "resources,expected",
    [
        ([], "lists no resource identities"),
        (None, "lists no resource identities"),
        ("everything", "lists no resource identities"),
        (["adp-dev-superplane-control-plane"], "malformed resource identity"),
        ([{"type": "aws_iam_role"}], "malformed resource identity"),
        ([{"type": "aws_iam_role", "name": ""}], "malformed resource identity"),
        ([{"type": 1, "name": "x"}], "malformed resource identity"),
        (
            [{"type": "kubernetes_role", "name": "x", "namespace": ""}],
            "malformed namespace",
        ),
        (
            [{"type": "kubernetes_role", "name": "x", "namespace": 7}],
            "malformed namespace",
        ),
    ],
)
def test_an_attestation_without_usable_identities_blocks(
    config, attestations, resources, expected
):
    """With no identities, step success is all that remains — and that is the F1 inference."""
    # Assigned after construction, because the helper's `resources=None` default means "build the
    # real set" while the None case here means a literal null in the published document.
    document = attestation_document("deploy")
    document["resources"] = resources
    publish_artifact(attestations, "deploy", document)
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    assert expected in str(caught.value)


def test_the_attested_step_must_be_among_the_steps_proven_to_have_executed(
    config, attestations
):
    """An attestation claiming a destroy is not evidence if the destroy step was skipped.

    This is the empty-state path with an attestation bolted on: the run concludes `success`, the
    document says `delete`, and the step that would have deleted anything never ran. The two
    mechanisms are deliberately joined here rather than trusted separately.
    """
    github = github_runs(steps={"undeploy": {"Terraform Destroy": "skipped"}})
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert "'Terraform Destroy' step did not execute" in str(caught.value)
    # And the attestation's own step requirement is stated in the registry, not inferred.
    assert t.EXECUTION_ATTESTATION["undeploy"][1] == "Terraform Destroy"
    assert t.EXECUTION_ATTESTATION["deploy"][1] == "Terraform Apply"


def test_the_attestation_evidence_records_githubs_record_not_the_document(config):
    """What a reader is shown must be the authority, not a restatement of the claim."""
    report = verify(config)
    for role, run_id in (
        ("deploy", DEPLOY_RUN_ID),
        ("rollout", ROLLOUT_RUN_ID),
        ("undeploy", UNDEPLOY_RUN_ID),
    ):
        attested = report["operations"][role]["attestation"]
        assert attested["artifact"] == t.EXECUTION_ATTESTATION[role][0]
        assert attested["artifact_id"] == ARTIFACT_ID[role]
        assert attested["digest"] == next(
            item["digest"] for item in ARTIFACTS if item["id"] == ARTIFACT_ID[role]
        )
        assert datetime.fromisoformat(attested["recorded_at"]) == ARTIFACT_CREATED[role]
        assert attested["account"] == ACCOUNT
        assert attested["environment"] == "dev"
        assert attested["action"] == t.EXECUTION_ATTESTATION[role][2]
        # No private cross-check state leaks into the published record.
        assert "_identities" not in attested and "_created_at" not in attested
    # The Terraform lanes record the state object; the rollout records the cluster.
    for role in ("deploy", "undeploy"):
        attested = report["operations"][role]["attestation"]
        assert attested["state_bucket"] == STATE_BUCKET
        assert attested["state_key"] == STATE_KEY
    rollout = report["operations"]["rollout"]["attestation"]
    assert rollout["cluster_arn"] == CLUSTER_ARN
    # And the state object the whole verification derived is published once, resolved.
    assert report["state_object"] == {"bucket": STATE_BUCKET, "key": STATE_KEY}
    assert "ACCOUNT_ID" not in report["state_object"]["bucket"]


def test_the_state_object_is_read_from_the_environments_own_backend_configuration(
    config,
):
    """Not a literal restated in this module: an edit to the tfvars must move what is accepted."""
    sources = t.RevisionSources(github_runs(), DEPLOY_SHA)
    assert t.backend_state_location(sources, "dev", ACCOUNT) == (
        STATE_BUCKET,
        STATE_KEY,
    )
    # The placeholder is resolved with the account THIS check read from STS, so a different
    # caller identity derives a different bucket rather than silently reusing this one.
    assert t.backend_state_location(sources, "dev", "210987654321") == (
        "adp-terraform-state-210987654321",
        STATE_KEY,
    )
    # Read at the deployed revision, and from the file the lanes actually pass to `-backend-config`.
    assert "environments/dev/modules/superplane-backend.tfvars" in sources.hashes, (
        sorted(sources.hashes)
    )


@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            'key = "dev/modules/superplane/terraform.tfstate"\n',
            "does not define 'bucket'",
        ),
        ('bucket = "adp-terraform-state-ACCOUNT_ID"\n', "does not define 'key'"),
    ],
)
def test_an_unusable_backend_configuration_blocks(payload, expected):
    """Without the state object, WHERE an operation targeted cannot be identified at all."""
    transport = github_runs()

    def github(path: str):
        if "superplane-backend.tfvars?ref=" in path:
            return {
                "encoding": "base64",
                "content": base64.b64encode(payload.encode()).decode("ascii"),
            }
        return transport(path)

    sources = t.RevisionSources(github, DEPLOY_SHA)
    with pytest.raises(EvidenceError) as caught:
        t.backend_state_location(sources, "dev", ACCOUNT)
    message = str(caught.value)
    assert message.startswith("BLOCKED:")
    assert expected in message
    assert "cannot be identified" in message


def test_an_environment_with_no_backend_configuration_blocks():
    sources = t.RevisionSources(github_runs(), DEPLOY_SHA)
    with pytest.raises(EvidenceError) as caught:
        t.backend_state_location(sources, "nosuchenv", ACCOUNT)
    assert "has no superplane-backend.tfvars at the deployed revision" in str(
        caught.value
    )


def test_undeploy_must_start_after_the_deploy_finished(config):
    """A destroy that began before the deploy ended cannot be that deploy's teardown.

    Ordering is on the undeploy's START, not its completion: a destroy that was already running
    while the apply was still creating resources would observe a half-built deployment, and its
    "everything is gone" would say nothing about what the apply went on to create.
    """
    earlier = (DEPLOYED_AT - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    with pytest.raises(EvidenceError, match="did not start after the deploy finished"):
        verify(config, github=github_runs(**{"202": {"run_started_at": earlier}}))


def test_simultaneous_start_and_finish_is_refused(config):
    """Equal instants are refused: the ordering must be strict to mean anything."""
    same = DEPLOYED_AT.isoformat().replace("+00:00", "Z")
    with pytest.raises(EvidenceError, match="did not start after the deploy finished"):
        verify(config, github=github_runs(**{"202": {"run_started_at": same}}))


def test_a_run_finishing_before_it_started_is_refused(config):
    later = (DEPLOYED_AT + timedelta(hours=9)).isoformat().replace("+00:00", "Z")
    with pytest.raises(EvidenceError, match="times are inconsistent"):
        verify(config, github=github_runs(**{"101": {"run_started_at": later}}))


def test_executed_steps_are_recorded_in_the_evidence(config):
    """The record must show WHICH operations were proven, not just that a run was green."""
    report = verify(config)
    for role in ("deploy", "undeploy"):
        _, _, required = t.LANE_EXECUTION[role]
        proven = [step["name"] for step in report["operations"][role]["executed_steps"]]
        assert proven == list(required)
        assert all(
            step["conclusion"] == "success"
            for step in report["operations"][role]["executed_steps"]
        )
        assert report["operations"][role]["event"] == t.DISPATCH_EVENT
        assert report["operations"][role]["run_attempt"] == 1


# ---------------------------------------------------------------------------
# F3, Kubernetes: the absence of an object the teardown deletes needs a deletion execution to
# attribute it to — and the object U3 RETAINS must not be held to an absence standard at all.
#
# The Terraform destroy lane says in its own summary that it does not touch Kubernetes. The
# rollout lane's only mutating step applies manifests. `k8s/rollback.sh --teardown` deletes the
# nine workload/identity/policy objects and states that it deliberately leaves the namespace. So
# nothing in this repository executes that deletion, and the previous code asked for a deletion
# receipt for the NAMESPACE — the one object the contract keeps — while asking for nothing at all
# about the nine it removes. Both halves of that are regression-tested here.
# ---------------------------------------------------------------------------
def test_kubernetes_deletion_blocks_because_no_lane_deletes_the_objects_u3_removes(
    config, hypothetical_k8s_teardown_lane, monkeypatch
):
    """Against the REAL empty registry, not the test double the other cases use.

    This is the accurate outcome for the Kubernetes portion of U1-L1 today: unprovable, not
    passing. It stays a refusal until a deletion lane exists to cite.
    """
    monkeypatch.setattr(t, "K8S_TEARDOWN_WORKFLOWS", {})
    assert t.K8S_TEARDOWN_WORKFLOWS == {}, "the shipped registry must stay empty"
    with pytest.raises(EvidenceError) as caught:
        verify(config)
    message = str(caught.value)
    assert message.startswith("BLOCKED:")
    assert "no workflow in this repository deletes them" in message
    # The reason is named, so the refusal is actionable rather than mysterious.
    assert "superplane-infra-destroy.yml" in message
    assert "superplane-k8s-deploy.yml only applies" in message
    assert "k8s/rollback.sh --teardown does delete exactly this set" in message
    assert "no lane invokes" in message
    assert "genuinely unprovable, not passing" in message
    # The producer is specified precisely enough to build, including the artifact it must upload.
    assert "REQUIRED PRODUCER" in message
    assert "superplane-k8s-teardown-attestation" in message
    assert "K8S_TEARDOWN_WORKFLOWS" in message
    # And it names every object whose deletion is unproven — all nine, each with its namespace.
    for entry in k8s_scope(t.DELETED):
        assert label(entry) in message
    # The retained namespace is NOT among them, and the message says so rather than leaving a
    # reader to assume the namespace was covered.
    assert label({"type": "kubernetes_namespace", "name": "skypilot"}) not in message
    assert "The namespace is NOT part of this: U3 retains it by design." in message


def test_the_shipped_module_registers_no_kubernetes_teardown_lane():
    """Guards the registry against being populated with a lane that does not delete.

    Registering `superplane-k8s-deploy.yml` here would make the block disappear while changing
    nothing about whether anything was ever deleted — the same false inference as F1.
    """
    source = (MODULE_ROOT / "superplane_acceptance" / "teardown.py").read_text(
        encoding="utf-8"
    )
    # Read from the shipped source, because the autouse fixtures have test doubles installed in
    # the live dicts for the duration of every test in this file.
    assert (
        "K8S_TEARDOWN_WORKFLOWS: dict[str, tuple[str, tuple[str, ...]]] = {}" in source
    )
    assert "RECORDER_WORKFLOWS: dict[str, tuple[str, tuple[str, ...]]] = {}" in source


def test_the_rollout_lane_is_creation_evidence_and_never_teardown_evidence(config):
    """The applying lane proves objects were CREATED; it can never prove they were deleted.

    It is a named constant because the rollout is required evidence in its own right (F1: it is
    what establishes the Kubernetes half of the inventory really existed in this cluster). The
    boundary being asserted is that the same constant is not reachable as deletion evidence: it
    appears in `LANE_EXECUTION` under `rollout`, and in neither teardown registry.
    """
    assert t.ROLLOUT_WORKFLOW == ".github/workflows/superplane-k8s-deploy.yml"
    assert t.LANE_EXECUTION["rollout"][0] == t.ROLLOUT_WORKFLOW
    assert t.ROLLOUT_WORKFLOW not in t.K8S_TEARDOWN_WORKFLOWS
    assert t.ROLLOUT_WORKFLOW not in t.RECORDER_WORKFLOWS
    assert t.EXECUTION_ATTESTATION["rollout"][2] == "create"
    assert t.EXECUTION_ATTESTATION["k8s_teardown"][2] == "delete"
    # And in the produced record the two are separate, differently-named blocks.
    report = verify(config)
    assert report["operations"]["rollout"]["workflow"] == t.ROLLOUT_WORKFLOW
    assert report["kubernetes_teardown"]["workflow"] == K8S_LANE


def test_a_registered_lane_still_needs_a_supplied_run(config):
    """With a lane registered, omitting its run blocks; it does not skip the objects."""
    config["k8s_teardown_run"] = None
    with pytest.raises(EvidenceError) as caught:
        t.verify(config, github=github_runs(), run=commands())
    assert "a Kubernetes teardown run must be supplied" in str(caught.value)
    assert "cannot be read as cleanup" in str(caught.value)


@pytest.mark.parametrize("conclusion", ["skipped", "failure", None])
def test_a_kubernetes_lane_that_did_not_delete_is_refused(config, conclusion):
    """The registered lane is held to the same step standard as the Terraform ones."""
    github = github_runs(jobs={"k8s_teardown": [k8s_run(conclusion=conclusion)]})
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert f"'{K8S_STEPS[0]}' step did not execute" in str(caught.value)


def test_a_kubernetes_receipt_from_another_run_is_refused(config):
    github = github_runs(jobs={"k8s_teardown": [k8s_run(run_id=999)]})
    with pytest.raises(EvidenceError, match="belongs to a different run or attempt"):
        verify(config, github=github)


def test_the_kubernetes_teardown_must_follow_the_rollout_that_created_the_objects(
    config,
):
    """A deletion that ran before the objects were applied deleted something else.

    The rollout is what put these objects in the cluster. A teardown that started first cannot
    be the teardown of what that rollout created, and crediting it would let a deletion of an
    earlier generation of objects stand in for this one.
    """
    before = (ROLLED_OUT_AT - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    github = github_runs(**{str(K8S_RUN_ID): {"run_started_at": before}})
    with pytest.raises(EvidenceError, match="did not start after the rollout finished"):
        verify(config, github=github)


def test_the_kubernetes_receipt_records_both_lifecycles_in_the_evidence(config):
    """The record must distinguish what was deleted from what was kept, and by whose authority."""
    report = verify(config)
    receipt = report["kubernetes_teardown"]
    assert receipt["workflow"] == K8S_LANE
    assert receipt["run_id"] == K8S_RUN_ID
    assert receipt["revision"] == K8S_SHA
    expected_deleted = sorted(label(e) for e in k8s_scope(t.DELETED))
    assert receipt["deleted"] == expected_deleted
    assert len(receipt["deleted"]) == 9
    # The retained namespace appears under `retained`, not under `deleted`: the record must not
    # read as if the namespace had been cleaned up.
    assert receipt["retained"] == [
        "kubernetes_namespace skypilot in namespace skypilot"
    ]
    assert [step["name"] for step in receipt["executed_steps"]] == list(K8S_STEPS)
    assert receipt["attestation"]["action"] == "delete"
    # And the inventory block carries the same split plus the authority it was read from.
    inventory = report["inventory"]
    assert inventory["kubernetes_deleted"] == expected_deleted
    assert inventory["kubernetes_retained"] == receipt["retained"]
    assert "k8s/rollback.sh --teardown" in inventory["retention_authority"]


def test_the_kubernetes_requirement_is_scoped_to_the_deletion_set(config, monkeypatch):
    """With nothing the teardown deletes in scope there is nothing to cite.

    Called directly rather than through `verify`, because a deployed revision whose manifests
    render nothing `rollback.sh` deletes is not reachable: `derived_k8s_objects` refuses an
    empty deletion set on purpose. This asserts the scoping is conditional on the inventory,
    not that such a deployment exists.

    The AWS resources and the RETAINED namespace are both still in scope here, which is the
    point: neither of them may drag in a deletion-lane requirement.
    """
    monkeypatch.setattr(t, "K8S_TEARDOWN_WORKFLOWS", {})
    without_deletions = [
        entry for entry in derived_for() if entry.get("lifecycle") != t.DELETED
    ]
    assert len(without_deletions) == 19
    assert any(entry["type"] == "kubernetes_namespace" for entry in without_deletions)
    assert (
        t.verify_k8s_teardown(
            without_deletions, {**config, "k8s_teardown_run": None}, None
        )
        is None
    )


def test_no_deletion_requirement_is_invented_for_the_retained_namespace(config):
    """F3, stated directly: the namespace's own absence is never asked for.

    Three separate guarantees, because "do not invent a namespace-deletion requirement" can be
    violated in three different places: the derivation must classify it RETAINED, the absence
    check must expect it PRESENT, and the deletion-lane requirement must not include it.
    """
    namespace = [e for e in derived_for() if e["type"] == "kubernetes_namespace"]
    assert len(namespace) == 1
    assert namespace[0]["lifecycle"] == t.RETAINED

    report = verify(config)
    entry = next(r for r in report["resources"] if r["type"] == "kubernetes_namespace")
    assert entry["expected"] == t.PRESENT
    assert entry["observation"] == t.PRESENT
    assert entry["lifecycle"] == t.RETAINED
    assert report["kubernetes_teardown"]["deleted"] == sorted(
        label(e) for e in k8s_scope(t.DELETED)
    )
    # The report says out loud that namespace deletion is not established, so a reader cannot
    # mistake the pass for a full teardown.
    assert any(
        "deletion of the retained namespace" in item
        for item in report["not_established"]
    )


def test_the_retained_namespace_being_absent_is_a_reported_deviation(config):
    """Absence of a RETAINED object is not cleanup; it means something undocumented ran.

    And it matters concretely: deleting that namespace takes the out-of-band
    'skypilot-api-db' Secret with it, which is exactly why U3 keeps it.
    """
    gone = (255, "", "An error occurred (NotFound) when calling the operation")
    with pytest.raises(EvidenceError) as caught:
        verify(config, run=commands(per_resource={"namespace/skypilot": gone}))
    message = str(caught.value)
    assert (
        "U3's teardown contract retains these objects, but they are absent" in message
    )
    assert "skypilot-api-db" in message
    assert "kubernetes_namespace skypilot" in message


def test_every_rendered_object_is_looked_up_with_its_namespace(config):
    """F3: each namespaced object is read by kind, name AND namespace.

    Names are reused across kinds in this manifest set (`skypilot-api` is a ServiceAccount, a
    Role, a RoleBinding, a Service and a Deployment), so a lookup keyed on name alone would
    read one object five times and call the other four absent.
    """
    issued = []

    def recording(argv):
        issued.append(tuple(argv))
        return commands()(argv)

    verify(config, run=recording)
    reads = {argv for argv in issued if argv[0] == "kubectl" and argv[1] == "get"}
    expected = set()
    for entry in k8s_scope():
        if entry["type"] == "kubernetes_namespace":
            expected.add(
                ("kubectl", "get", "namespace", entry["name"], "--output", "name")
            )
        else:
            expected.add(
                (
                    "kubectl",
                    "get",
                    t.K8S_RESOURCE_TOKEN[entry["type"]],
                    entry["name"],
                    "-n",
                    entry["namespace"],
                    "--output",
                    "name",
                )
            )
    assert reads == expected
    # Ten distinct reads for ten distinct objects: no object was read twice or missed.
    assert len(reads) == 10


# ---------------------------------------------------------------------------
# Account and cluster binding.
# ---------------------------------------------------------------------------
def test_wrong_account_credentials_are_refused(config):
    with pytest.raises(EvidenceError, match="not for the selected account"):
        verify(config, run=commands(identity={"Account": "210987654321"}))


def test_unreadable_caller_identity_blocks(config):
    def run(argv):
        if tuple(argv[:3]) == ("aws", "sts", "get-caller-identity"):
            return 1, "", "Unable to locate credentials"
        return commands()(argv)

    with pytest.raises(EvidenceError, match="caller identity could not be read"):
        verify(config, run=run)


def test_malformed_caller_identity_blocks_without_echoing_it(config):
    def run(argv):
        if tuple(argv[:3]) == ("aws", "sts", "get-caller-identity"):
            return 0, "<html>arn:aws:iam::secret-detail</html>", ""
        return commands()(argv)

    with pytest.raises(EvidenceError) as caught:
        verify(config, run=run)
    assert "unreadable output" in str(caught.value)
    assert "secret-detail" not in str(caught.value)


def test_undescribable_cluster_blocks(config):
    def run(argv):
        if tuple(argv[:3]) == ("aws", "eks", "describe-cluster"):
            return 254, "", "An error occurred (ResourceNotFoundException)"
        return commands()(argv)

    with pytest.raises(EvidenceError, match="named cluster could not be described"):
        verify(config, run=run)


def test_cluster_in_another_account_or_region_is_refused(config):
    for arn in (
        f"arn:aws:eks:{REGION}:210987654321:cluster/{CLUSTER}",
        f"arn:aws:eks:eu-west-1:{ACCOUNT}:cluster/{CLUSTER}",
    ):
        cluster = {"cluster": {"endpoint": ENDPOINT, "arn": arn}}
        with pytest.raises(
            EvidenceError, match="not in the selected account and region"
        ):
            verify(config, run=commands(cluster=cluster))


def test_kubectl_pointed_at_a_different_cluster_is_refused(config):
    """Namespace reads from another cluster would describe the wrong thing entirely."""
    context = {"clusters": [{"cluster": {"server": "https://other.eks.amazonaws.com"}}]}
    with pytest.raises(EvidenceError, match="not the named cluster"):
        verify(config, run=commands(context=context))


@pytest.mark.parametrize(
    "context",
    [
        {"clusters": []},
        {
            "clusters": [
                {"cluster": {"server": ENDPOINT}},
                {"cluster": {"server": ENDPOINT}},
            ]
        },
        {"clusters": [{"cluster": {}}]},
        {},
    ],
)
def test_ambiguous_kubernetes_context_blocks(config, context):
    with pytest.raises(EvidenceError, match="does not name exactly one cluster"):
        verify(config, run=commands(context=context))


def test_trailing_slash_on_the_context_server_still_matches(config):
    """A cosmetic difference must not fail a correctly targeted observation."""
    context = {"clusters": [{"cluster": {"server": ENDPOINT + "/"}}]}
    report = verify(config, run=commands(context=context))
    assert report["identity"]["cluster"] == CLUSTER


# ---------------------------------------------------------------------------
# Derivation: the inventory tracks U3's sources, and never derives nothing.
# ---------------------------------------------------------------------------
def test_derived_inventory_matches_the_terraform_and_lock_sources():
    """The revision-pinned derivation must agree with U3's own naming contract.

    U3's `source_derived_names` reads the local checkout; at a revision whose source IS the
    checkout the two must produce the same names. That is what shows the revision pinning
    changed only WHICH bytes are read, not how a name is derived from them.
    """
    names = t.derived_names()
    config = {"tf_environment": "dev", "account": ACCOUNT, "region": REGION}
    sources = t.RevisionSources(github_runs(), DEPLOY_SHA)
    derived = t.derived_inventory(config, sources)
    by_type: dict[str, set[str]] = {}
    for entry in derived:
        by_type.setdefault(entry["type"], set()).add(entry["name"])
    assert by_type["aws_iam_role"] == set(names.iam_role_names("dev"))
    assert by_type["aws_ssm_parameter"] == set(names.ssm_parameter_names("dev"))
    assert by_type["aws_ecr_repository"] == set(names.ecr_repository_names())
    assert by_type["kubernetes_namespace"] == {"skypilot"}
    # Every derived resource carries an identity the ownership contract can attribute.
    t.attribute(derived, {**config, "account": ACCOUNT}, derived)


def test_every_rendered_kubernetes_object_is_derived_with_its_lifecycle():
    """F3: the inventory is the ACTUAL owned rendered object set, not just the namespace.

    The previous derivation produced `kind: Namespace` only — the one object U3's teardown
    deliberately keeps — so the nine objects a teardown really removes were never in scope at
    all. This pins the full set, object by object, against the manifests on disk, and pins each
    object's lifecycle against what `k8s/rollback.sh --teardown` actually deletes.

    Written out literally rather than recomputed, because a derivation compared against another
    run of itself would agree with any bug in it.
    """
    sources = t.RevisionSources(github_runs(), DEPLOY_SHA)
    derived = t.derived_k8s_objects(sources, "dev")
    assert [
        (entry["type"], entry["name"], entry["namespace"], entry["lifecycle"])
        for entry in derived
    ] == [
        ("kubernetes_config_map", "skypilot-config", "skypilot", t.DELETED),
        ("kubernetes_deployment", "skypilot-api", "skypilot", t.DELETED),
        # The Namespace object itself: rendered, owned, and RETAINED by U3's contract.
        ("kubernetes_namespace", "skypilot", "skypilot", t.RETAINED),
        ("kubernetes_network_policy", "default-deny", "skypilot", t.DELETED),
        ("kubernetes_network_policy", "skypilot-api-egress", "skypilot", t.DELETED),
        ("kubernetes_network_policy", "skypilot-api-ingress", "skypilot", t.DELETED),
        ("kubernetes_role", "skypilot-api", "skypilot", t.DELETED),
        ("kubernetes_role_binding", "skypilot-api", "skypilot", t.DELETED),
        ("kubernetes_service", "skypilot-api", "skypilot", t.DELETED),
        ("kubernetes_service_account", "skypilot-api", "skypilot", t.DELETED),
    ]
    # Exactly the nine objects rollback.sh names, and no tenth invented for the namespace.
    assert len([e for e in derived if e["lifecycle"] == t.DELETED]) == 9


def test_the_deletion_set_is_read_from_u3s_own_teardown_script():
    """The lifecycle split is U3's statement, not this checker's opinion.

    `rollback.sh` is the single place that says which objects a teardown removes, so the pairs
    are read out of its `kubectl delete` lines — including `deployment/${DEPLOYMENT}`, whose
    name comes from the script's own variable rather than being assumed.
    """
    sources = t.RevisionSources(github_runs(), DEPLOY_SHA)
    deleted = t.teardown_deleted_resources(sources)
    assert deleted == {
        ("kubernetes_deployment", "skypilot-api"),
        ("kubernetes_service", "skypilot-api"),
        ("kubernetes_network_policy", "skypilot-api-egress"),
        ("kubernetes_network_policy", "skypilot-api-ingress"),
        ("kubernetes_network_policy", "default-deny"),
        ("kubernetes_config_map", "skypilot-config"),
        ("kubernetes_role_binding", "skypilot-api"),
        ("kubernetes_role", "skypilot-api"),
        ("kubernetes_service_account", "skypilot-api"),
    }
    # The namespace is absent from the deletion set because the script says it keeps it.
    assert not any(kind == "kubernetes_namespace" for kind, _name in deleted)


# Stand-in `rollback.sh` bodies for the contract-read refusals below. Named constants because a
# parametrize list of multi-line shell reads as noise inline.
NO_DELETIONS_SCRIPT = """DEPLOYMENT="skypilot-api"
if [ "$TEARDOWN" = "true" ]; then
  echo doing nothing
  exit 0
fi
"""

UNRESOLVED_NAME_SCRIPT = """if [ "$TEARDOWN" = "true" ]; then
  for object in "deployment/${DEPLOYMENT}"; do
    run kubectl delete "$object" -n "$NAMESPACE" --ignore-not-found
  done
  exit 0
fi
"""


@pytest.mark.parametrize(
    "script,expected",
    [
        # No teardown block at all: the lifecycle contract cannot be read, so there is no
        # basis for classifying anything, and guessing would mean inventing a requirement.
        ("#!/usr/bin/env bash\necho nothing\n", "has no --teardown block"),
        # A teardown block that deletes nothing recognisable.
        (NO_DELETIONS_SCRIPT, "names no recognised object"),
        # `${DEPLOYMENT}` used but never assigned: the object's real name is unknown, and
        # recording the literal `${DEPLOYMENT}` would produce a lookup that can never match.
        (UNRESOLVED_NAME_SCRIPT, "cannot be resolved"),
    ],
)
def test_an_unreadable_teardown_contract_blocks_rather_than_guessing(script, expected):
    """F3's "do not invent a resource action": an unreadable contract must refuse.

    If `rollback.sh` cannot be read for its deletion set, the honest outcome is a block. The
    alternative — defaulting every rendered object to DELETED, or to RETAINED — would either
    invent a deletion requirement U3 never stated or silently exempt the whole object set.
    """
    path = f"{MODULE_PATH}/k8s/rollback.sh"

    def github(argv: str):
        if argv.startswith(f"contents/{path}"):
            return {
                "encoding": "base64",
                "content": base64.b64encode(script.encode()).decode("ascii"),
            }
        return github_runs()(argv)

    with pytest.raises(EvidenceError) as caught:
        t.derived_k8s_objects(t.RevisionSources(github, DEPLOY_SHA), "dev")
    assert expected in str(caught.value)
    assert str(caught.value).startswith("BLOCKED:")


def test_a_rendered_kind_with_no_read_only_lookup_blocks(config):
    """An object nobody can look up is an object whose survival cannot be noticed.

    Dropping it would shrink coverage invisibly — the F3 defect in its general form — so an
    unrecognised kind refuses instead.
    """

    def github(argv: str):
        if ".yaml?ref=" in argv and f"{MODULE_PATH}/k8s/" in argv:
            manifest = (
                b"kind: StatefulSet\nmetadata:\n  name: skypilot-api\n"
                b"  namespace: skypilot\n"
            )
            return {
                "encoding": "base64",
                "content": base64.b64encode(manifest).decode("ascii"),
            }
        return github_runs()(argv)

    with pytest.raises(EvidenceError) as caught:
        t.derived_k8s_objects(t.RevisionSources(github, DEPLOY_SHA), "dev")
    message = str(caught.value)
    assert "renders a StatefulSet object" in message
    assert "no read-only lookup" in message
    assert "whose survival cannot be noticed" in message


def test_a_namespaced_object_without_its_namespace_blocks():
    """F3: namespace is part of the identity, so a manifest omitting it is not derivable.

    Two objects of one kind can share a name in different namespaces. Defaulting the namespace
    would produce a lookup against a namespace nobody deployed to, and its NotFound would be
    read as cleanup.
    """

    def github(argv: str):
        if ".yaml?ref=" in argv and f"{MODULE_PATH}/k8s/" in argv:
            manifest = b"kind: ConfigMap\nmetadata:\n  name: skypilot-config\n"
            return {
                "encoding": "base64",
                "content": base64.b64encode(manifest).decode("ascii"),
            }
        return github_runs()(argv)

    with pytest.raises(EvidenceError) as caught:
        t.derived_k8s_objects(t.RevisionSources(github, DEPLOY_SHA), "dev")
    assert "with no metadata.namespace" in str(caught.value)
    assert "it cannot be looked up" in str(caught.value)


def test_a_nested_name_is_not_mistaken_for_an_object_identity():
    """`roleRef`/`subjects` carry their own `name:` keys, which are not object identities.

    `10-skypilot-rbac.yaml` really does contain those keys. A reader that took the last or the
    deepest `name:` it saw would derive a RoleBinding named after its subject, and then look up
    an object that does not exist — whose NotFound would read as cleanup.
    """
    sources = t.RevisionSources(github_runs(), DEPLOY_SHA)
    rendered = t._rendered_objects(sources, "10-skypilot-rbac.yaml")
    assert [(obj["kind"], obj["name"]) for obj in rendered] == [
        ("ServiceAccount", "skypilot-api"),
        ("Role", "skypilot-api"),
        ("RoleBinding", "skypilot-api"),
    ]
    # Every object carries the placeholder namespace, resolved later from the deploy inputs.
    assert {obj["namespace"] for obj in rendered} == {"REPLACE_WITH_SKYPILOT_NAMESPACE"}


def test_the_derivation_reads_only_the_deployed_revision(config):
    """Every source fetch must be pinned to the verified deploy SHA.

    This is F3 in its most direct form: if any read reached the verifier's working tree, or
    another ref, the inventory would describe source nobody deployed.
    """
    requested = []

    def github(path: str):
        if path.startswith("contents/"):
            requested.append(path)
        return github_runs()(path)

    report = verify(config, github=github)
    assert requested
    inventory_reads = [
        path for path in requested if not path.endswith(f"?ref={RECORDER_SHA}")
    ]
    assert all(path.endswith(f"?ref={DEPLOY_SHA}") for path in inventory_reads), (
        inventory_reads
    )
    # Every source the inventory rests on: the Terraform naming contract, the release lock, the
    # environment's tfvars, the rendered manifests AND U3's teardown script.
    for path in SOURCE_FILES:
        assert f"contents/{path}?ref={DEPLOY_SHA}" in inventory_reads
    assert any(
        f"{MODULE_PATH}/k8s/00-namespace.yaml" in path for path in inventory_reads
    )
    assert report["inventory"]["derived_at_revision"] == DEPLOY_SHA
    # The ONE read pinned elsewhere is the recorder's own module hash, which must be taken at
    # the recorder run's revision — reading it at the deploy revision would compare the receipt
    # against code the recorder did not run.
    recorder_reads = [
        path for path in requested if path.endswith(f"?ref={RECORDER_SHA}")
    ]
    assert recorder_reads == [f"contents/{VERIFIER_PATH}?ref={RECORDER_SHA}"]


def test_the_derivation_sources_are_hashed_into_the_evidence(config):
    """A reader must be able to confirm which contract text produced the inventory."""
    report = verify(config)
    hashes = report["inventory"]["derived_from_sha256"]
    assert hashes == source_hashes()
    assert all(len(digest) == 64 for digest in hashes.values())
    for path in SOURCE_FILES:
        assert path in hashes


# --- F3: coverage is bound to the DEPLOYED source, not to whatever HEAD says now. ---
def test_a_resource_removed_from_later_source_is_still_checked(
    config, inventory_file, attestations
):
    """A resource the deploy created and a later commit deleted must stay in scope.

    This is the F3 failure made concrete. The deployed revision creates an SSM parameter; a
    later commit removes it from config.tf. Reading the current checkout would drop it from the
    derivation, so nothing would ever look for it, and a teardown that left it behind would
    still pass. Pinning to the deploy revision keeps it in scope, and it is refused when still
    present.
    """
    removed = "/adp/dev/superplane/retired-parameter"
    deployed_config_tf = (
        REPOSITORY_ROOT / MODULE_PATH / "infra/control-plane/config.tf"
    ).read_text(encoding="utf-8")
    at_deploy = deployed_config_tf.replace(
        'resource "aws_ssm_parameter" "namespace" {',
        'resource "aws_ssm_parameter" "retired" {\n'
        '  name  = "${local.parameter_prefix}/retired-parameter"\n'
        '  type  = "String"\n'
        '  value = "gone"\n'
        "}\n\n"
        'resource "aws_ssm_parameter" "namespace" {',
        1,
    )
    assert at_deploy != deployed_config_tf, "the anchor for this fixture moved"
    path = f"{MODULE_PATH}/infra/control-plane/config.tf"

    def github(argv: str):
        # Only the DEPLOYED revision carries the retired parameter; HEAD no longer does.
        if argv == f"contents/{path}?ref={DEPLOY_SHA}":
            return {
                "encoding": "base64",
                "content": base64.b64encode(at_deploy.encode()).decode("ascii"),
            }
        return github_runs()(argv)

    hashes = source_hashes(
        overrides={path: hashlib.sha256(at_deploy.encode()).hexdigest()}
    )
    sources = t.RevisionSources(github, DEPLOY_SHA)
    derived = t.derived_inventory(
        {"tf_environment": "dev", "account": ACCOUNT, "region": REGION}, sources
    )
    assert any(entry["name"] == removed for entry in derived), (
        "the retired parameter must be derived from the deployed revision"
    )

    # A receipt that omits it fails coverage rather than quietly narrowing the check.
    config["inventory_file"] = inventory_file(inventory_document(source_sha256=hashes))
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert "does not cover every resource" in str(caught.value)
    assert removed in str(caught.value)

    # Recorded in the receipt but absent from the lanes' plans, it still refuses: a resource no
    # apply is shown to have created and no destroy is shown to have deleted cannot be accounted
    # for by those executions, however complete the receipt is. (F1's coverage half.)
    config["inventory_file"] = inventory_file(
        inventory_document(
            resources=[{**entry, "observation": t.PRESENT} for entry in derived],
            source_sha256=hashes,
        )
    )
    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert "plan did not contain every resource in scope" in str(caught.value)
    assert removed in str(caught.value)

    # With the lanes attesting to it too, it is held to the same absence standard as anything
    # else — and being still present is what fails, not being unlisted.
    for role in ("deploy", "undeploy"):
        publish_artifact(
            attestations,
            role,
            attestation_document(
                role,
                resources=[
                    {"type": entry["type"], "name": entry["name"]}
                    for entry in derived
                    if entry["type"].startswith("aws_")
                ],
            ),
        )
    with pytest.raises(EvidenceError, match="did not remove every resource"):
        verify(
            config,
            github=github,
            run=commands(per_resource={removed: (0, "{}", "")}),
        )


def test_source_unreadable_at_the_deployed_revision_blocks(config):
    """If the deployed source cannot be read, the inventory is simply not derivable."""
    path = f"{MODULE_PATH}/infra/control-plane/irsa.tf"

    def github(argv: str):
        if argv.startswith(f"contents/{path}"):
            raise EvidenceError("BLOCKED: GitHub metadata/source request failed")
        return github_runs()(argv)

    with pytest.raises(EvidenceError) as caught:
        verify(config, github=github)
    assert "could not be read at the deployed revision" in str(caught.value)
    assert path in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"encoding": "none", "content": "x"},
        {"encoding": "base64", "content": "not base64!!"},
        {"encoding": "base64"},
        {"encoding": "base64", "content": base64.b64encode(b"\xff\xfe").decode()},
        [],
    ],
)
def test_malformed_source_at_the_deployed_revision_blocks(config, payload):
    path = f"{MODULE_PATH}/releases/superplane.lock.yaml"

    def github(argv: str):
        if argv.startswith(f"contents/{path}"):
            return payload
        return github_runs()(argv)

    with pytest.raises(EvidenceError, match="BLOCKED"):
        verify(config, github=github)


def test_a_namespace_placeholder_this_checker_cannot_resolve_is_refused(monkeypatch):
    """An un-nameable object must block, not be silently dropped from coverage.

    This is the case where U3 adds an object whose namespace placeholder this checker has
    not been taught. Dropping it would shrink coverage invisibly, so it refuses instead.
    """
    monkeypatch.setattr(t, "NAMESPACE_PLACEHOLDER_VARIABLE", {})
    with pytest.raises(EvidenceError, match="cannot resolve to a deploy input"):
        t.derived_k8s_objects(t.RevisionSources(github_runs(), DEPLOY_SHA), "dev")


def test_an_unconfigured_environment_cannot_be_derived():
    """No tfvars means no deploy inputs, so no inventory — not an empty one."""
    with pytest.raises(EvidenceError, match="has no superplane.tfvars"):
        t.derived_k8s_objects(t.RevisionSources(github_runs(), DEPLOY_SHA), "nosuchenv")


def test_missing_manifests_block_rather_than_deriving_nothing():
    """An empty manifest directory at the deployed revision must block, not derive zero."""

    def github(argv: str):
        if argv.startswith(f"contents/{MODULE_PATH}/k8s?"):
            return []
        return github_runs()(argv)

    with pytest.raises(EvidenceError, match="empty or unreadable"):
        t.derived_k8s_objects(t.RevisionSources(github, DEPLOY_SHA), "dev")


def test_manifests_rendering_no_object_at_all_block():
    """Manifests that parse to zero objects are a refusal, not zero coverage.

    A derivation that produced nothing would make the whole Kubernetes half of the check
    vacuous while still reporting a pass.
    """

    def github(argv: str):
        if ".yaml?ref=" in argv and f"{MODULE_PATH}/k8s/" in argv:
            return {
                "encoding": "base64",
                "content": base64.b64encode(b"# only a comment\n").decode("ascii"),
            }
        return github_runs()(argv)

    with pytest.raises(EvidenceError, match="no Kubernetes object found"):
        t.derived_k8s_objects(t.RevisionSources(github, DEPLOY_SHA), "dev")


def test_manifests_that_render_nothing_the_teardown_deletes_block():
    """A rendered set with no deletion set has nothing to hold a teardown to.

    Note what this is NOT: it does not add a namespace-deletion requirement to rescue the
    case. If the manifests render only the object U3 retains, there is no deletion to verify
    and the honest answer is a refusal.
    """

    def github(argv: str):
        if ".yaml?ref=" in argv and f"{MODULE_PATH}/k8s/" in argv:
            manifest = (
                b"kind: Namespace\nmetadata:\n  name: REPLACE_WITH_SKYPILOT_NAMESPACE\n"
            )
            return {
                "encoding": "base64",
                "content": base64.b64encode(manifest).decode("ascii"),
            }
        return github_runs()(argv)

    with pytest.raises(EvidenceError) as caught:
        t.derived_k8s_objects(t.RevisionSources(github, DEPLOY_SHA), "dev")
    assert (
        "no rendered object matches anything k8s/rollback.sh --teardown deletes"
        in str(caught.value)
    )


def test_coverage_is_one_directional(config, inventory_file):
    """Extra domain-owned entries are fine and are checked; omissions are not."""
    extra = "/adp/dev/superplane/extra-recorded"
    resources = inventory_document()["resources"] + [
        {"type": "aws_ssm_parameter", "name": extra, "observation": t.PRESENT}
    ]
    config["inventory_file"] = inventory_file(inventory_document(resources=resources))
    report = verify(config)
    assert any(r["name"] == extra for r in report["resources"])


# ---------------------------------------------------------------------------
# The recorder: the only thing that can produce a presence receipt.
#
# `record_pre_teardown` is the other half of F2. The verifier needs evidence that the resources
# existed, and only a real read taken while they still did can supply it. So the recorder is held
# to the same standard as the verifier: same read-only commands, same three-way classification,
# and it refuses to write anything unless it actually saw every derived resource.
# ---------------------------------------------------------------------------
@pytest.fixture
def recorder_environment(tmp_path):
    def build(**overrides) -> dict:
        values = {
            "SUPERPLANE_LIVE_ENVIRONMENT": "embark1/dev",
            "SUPERPLANE_LIVE_TF_ENVIRONMENT": "dev",
            "SUPERPLANE_LIVE_CLUSTER": CLUSTER,
            "SUPERPLANE_LIVE_DEPLOY_RUN_ID": "101",
            "SUPERPLANE_LIVE_DEPLOY_SHA": DEPLOY_SHA,
            "SUPERPLANE_LIVE_INVENTORY_FILE": str(tmp_path / "receipt.json"),
        }
        values.update(overrides)
        return {k: v for k, v in values.items() if v is not None}

    return build


def record(environment, **kwargs):
    return t.record_pre_teardown(
        environment,
        github=kwargs.pop("github", github_runs()),
        # Everything PRESENT: the pre-teardown world, where the deployment still exists.
        run=kwargs.pop("run", commands(absent=False)),
    )


def test_the_recorder_writes_what_it_actually_observed(recorder_environment):
    values = recorder_environment()
    receipt = record(values)
    assert receipt["schema"] == t.RECEIPT_SCHEMA
    assert receipt["complete"] is True
    assert {entry["observation"] for entry in receipt["resources"]} == {t.PRESENT}
    assert receipt["deploy"] == {
        "run_id": 101,
        "run_attempt": 1,
        "revision": DEPLOY_SHA,
        "run_url": "https://github.com/aws-e/adp/actions/runs/101",
    }
    assert receipt["cluster_arn"] == CLUSTER_ARN
    assert receipt["source_sha256"] == source_hashes()
    # Written where the verifier will read it, and byte-identical to the returned value.
    published = Path(values["SUPERPLANE_LIVE_INVENTORY_FILE"])
    assert json.loads(published.read_text(encoding="utf-8")) == receipt
    assert published.stat().st_mode & 0o077 == 0


def test_a_receipt_the_recorder_produced_satisfies_the_verifier(
    recorder_environment, environment, attestations
):
    """End to end across the two halves: record before, verify after.

    This is the property that matters — the verifier's input contract and the recorder's output
    are the same contract, so the only way to obtain a passing receipt is to have observed the
    resources while they existed. In particular the recorder must read EXACTLY the source set
    the verifier derives from, or an honest receipt would be refused for a difference in reads
    rather than a difference in evidence.
    """
    recorded = recorder_environment()
    receipt = record(recorded)
    assert receipt["evidence_kind"] == "offline-fixture"
    # The recorder stamps the real clock, so pin the whole operation timeline around it rather
    # than inventing a timestamp for the receipt. The order is the real one: apply, rollout,
    # observation, Kubernetes teardown, destroy.
    observed = datetime.fromisoformat(receipt["observed_at"])

    def at(minutes: int) -> str:
        return (
            (observed + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        )

    github = github_runs(
        **{
            str(DEPLOY_RUN_ID): {"run_started_at": at(-9), "updated_at": at(-5)},
            str(ROLLOUT_RUN_ID): {"run_started_at": at(-4), "updated_at": at(-2)},
            str(RECORDER_RUN_ID): {"run_started_at": at(-1), "updated_at": at(0)},
            str(K8S_RUN_ID): {"run_started_at": at(1), "updated_at": at(3)},
            str(UNDEPLOY_RUN_ID): {"run_started_at": at(5), "updated_at": at(9)},
        }
    )
    # Every lane's artifact is republished with GitHub's creation time inside that lane's own
    # attempt window, because the timeline above is pinned to the recorder's real clock rather
    # than to the fixture constants (U1-201: an artifact is credited to an attempt by upload
    # time, so the whole set has to move together).
    for role, minutes in (("deploy", -6), ("rollout", -3), ("undeploy", 7)):
        publish_artifact(
            attestations,
            role,
            attestation_document(role),
            created_at=at(minutes),
        )
    publish_artifact(
        attestations,
        "k8s_teardown",
        attestation_document("k8s_teardown"),
        created_at=at(2),
    )
    # The recorder's receipt is the recorder lane's artifact, so it is republished as such with
    # GitHub's creation time inside the closed deploy-completion → teardown-start window. A
    # fixture that skipped this would be testing a receipt nothing authenticates.
    publish_artifact(attestations, "recorder", receipt, created_at=observed.isoformat())
    config = t.settings(
        environment(
            SUPERPLANE_LIVE_INVENTORY_FILE=recorded["SUPERPLANE_LIVE_INVENTORY_FILE"]
        )
    )
    # The receipt the RECORDER wrote is offline-fixture, so the live path must still refuse it;
    # this asserts the end-to-end mechanics, not a live pass.
    with pytest.raises(EvidenceError, match="classifies its own evidence as"):
        t.verify(config, github=github, run=commands(absent=True))

    # Re-stamped `live` — as it would be when the recorder runs with real transports — the same
    # document satisfies every other binding the verifier applies.
    live_receipt = {**receipt, "evidence_kind": "live"}
    Path(config["inventory_file"]).write_text(
        json.dumps(live_receipt), encoding="utf-8"
    )
    publish_artifact(
        attestations, "recorder", live_receipt, created_at=observed.isoformat()
    )
    report = t.verify(config, github=github, run=commands(absent=True))
    assert report["status"] == "matched"
    assert report["inventory"]["observed_count"] == len(receipt["resources"])
    # The recorder's own source reads and the verifier's are the same set, which is what makes
    # the hash comparison in `bind_receipt` a real check rather than an unsatisfiable one.
    assert receipt["source_sha256"] == report["inventory"]["derived_from_sha256"]
    # And the recorder's module hash is confirmed against the verifier module at the recorder
    # run's own revision, not copied from the receipt.
    recorder = report["pre_teardown_observation"]["recorder"]
    assert recorder["recorder_sha256"] == receipt["recorder_sha256"]


@pytest.mark.parametrize("name", t.RECORDER_INPUTS)
def test_every_recorder_input_is_required(recorder_environment, name):
    with pytest.raises(EvidenceError, match=f"BLOCKED: missing.*{name}"):
        record(recorder_environment(**{name: None}))


def test_the_recorder_needs_no_undeploy_run_or_evidence_file():
    """It runs before the teardown, so neither exists yet; requiring them would be wrong."""
    assert "SUPERPLANE_LIVE_UNDEPLOY_RUN_ID" not in t.RECORDER_INPUTS
    assert "SUPERPLANE_LIVE_UNDEPLOY_SHA" not in t.RECORDER_INPUTS
    assert "SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE" not in t.RECORDER_INPUTS


@pytest.mark.parametrize(
    "outcome",
    [
        # Absent before teardown: the resource was never there to clean up.
        (255, "", "An error occurred (NoSuchEntity) when calling the operation"),
        # Could not tell: a denial must not become a presence claim.
        (1, "", "An error occurred (AccessDenied) when calling the operation"),
        (1, "", "Unable to locate credentials"),
    ],
)
def test_the_recorder_refuses_to_write_an_unobserved_resource(
    recorder_environment, outcome
):
    """A resource it could not see PRESENT makes the receipt worthless, so none is written."""
    values = recorder_environment()
    target = "adp-dev-superplane-control-plane"
    with pytest.raises(EvidenceError) as caught:
        record(
            values,
            run=commands(absent=False, per_resource={target: outcome}),
        )
    assert "were not observed PRESENT before teardown" in str(caught.value)
    assert target in str(caught.value)
    assert not Path(values["SUPERPLANE_LIVE_INVENTORY_FILE"]).exists()


def test_the_recorder_refuses_an_unverified_deploy(recorder_environment):
    """The deploy must be a real execution before its revision is used as a source."""
    values = recorder_environment()
    for patch, expected in (
        ({"conclusion": "failure"}, "did not complete successfully"),
        ({"event": "push"}, "not a deliberate workflow_dispatch"),
        ({"path": t.DESTROY_WORKFLOW}, "not this module's apply lane"),
        ({"head_sha": "c" * 40}, "not this module's apply lane"),
        ({"run_attempt": None}, "records no attempt number"),
    ):
        with pytest.raises(EvidenceError) as caught:
            record(values, github=github_runs(**{"101": patch}))
        assert expected in str(caught.value)
        assert not Path(values["SUPERPLANE_LIVE_INVENTORY_FILE"]).exists()


def test_the_recorder_refuses_a_deploy_that_never_applied(recorder_environment):
    """Same step-level standard as the verifier: a skipped apply created nothing."""
    values = recorder_environment()
    github = github_runs(steps={"deploy": {"Terraform Apply": "skipped"}})
    with pytest.raises(EvidenceError, match="'Terraform Apply' step did not execute"):
        record(values, github=github)
    assert not Path(values["SUPERPLANE_LIVE_INVENTORY_FILE"]).exists()


def test_the_recorder_refuses_to_overwrite_an_existing_receipt(
    recorder_environment, tmp_path
):
    """Overwriting would let a second run quietly replace an earlier observation."""
    existing = tmp_path / "already-there.json"
    existing.write_text("{}", encoding="utf-8")
    with pytest.raises(EvidenceError, match="absolute new filename"):
        record(recorder_environment(SUPERPLANE_LIVE_INVENTORY_FILE=str(existing)))
    assert existing.read_text(encoding="utf-8") == "{}"


def test_the_recorder_refuses_a_symlinked_or_relative_target(
    recorder_environment, tmp_path
):
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "elsewhere.json")
    for path in (str(link), "relative/receipt.json"):
        with pytest.raises(EvidenceError, match="absolute new filename"):
            record(recorder_environment(SUPERPLANE_LIVE_INVENTORY_FILE=path))
    assert not (tmp_path / "elsewhere.json").exists()


def test_the_recorder_refuses_an_unreviewed_target(recorder_environment):
    with pytest.raises(EvidenceError, match="not in the reviewed registry"):
        record(recorder_environment(SUPERPLANE_LIVE_ENVIRONMENT="prod/live"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("SUPERPLANE_LIVE_TF_ENVIRONMENT", "../dev"),
        ("SUPERPLANE_LIVE_DEPLOY_SHA", "abc123"),
        ("SUPERPLANE_LIVE_DEPLOY_SHA", "A" * 40),
        ("SUPERPLANE_LIVE_DEPLOY_RUN_ID", "0"),
        ("SUPERPLANE_LIVE_DEPLOY_RUN_ID", "101; rm -rf /"),
    ],
)
def test_the_recorder_validates_its_inputs_exactly(recorder_environment, key, value):
    with pytest.raises(EvidenceError, match="BLOCKED"):
        record(recorder_environment(**{key: value}))


def test_the_recorder_runs_no_command_outside_the_read_only_allowlist(
    recorder_environment,
):
    """The recorder is as observational as the verifier; it deploys and deletes nothing."""
    issued = []

    def recording(argv):
        issued.append(tuple(argv))
        return commands(absent=False)(argv)

    record(recorder_environment(), run=recording)
    assert issued
    for argv in issued:
        assert tuple(argv[:3]) in t.READ_ONLY_SHAPES, argv


def test_the_recorder_marks_fixture_evidence_as_such(recorder_environment):
    """An injected transport must be visible in the receipt, never labelled live."""
    receipt = record(recorder_environment())
    assert receipt["evidence_kind"] == "offline-fixture"


def test_the_recorder_writes_no_credential(recorder_environment):
    serialised = json.dumps(record(recorder_environment()))
    for secret in ("Bearer ", "AKIA", "ASIA", "aws_secret", "PRIVATE KEY", "password"):
        assert secret not in serialised


# ---------------------------------------------------------------------------
# The observational boundary: this checker cannot change anything.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ("terraform", "destroy", "-auto-approve"),
        ("terraform", "apply", "tfplan"),
        (
            "aws",
            "iam",
            "delete-role",
            "--role-name",
            "adp-dev-superplane-control-plane",
        ),
        ("aws", "ssm", "delete-parameter", "--name", "/adp/dev/superplane/namespace"),
        ("aws", "ecr", "delete-repository", "--repository-name", "adp-superplane-api"),
        ("kubectl", "delete", "namespace", "skypilot"),
        ("kubectl", "apply", "-f", "k8s/"),
        # The issue names this one specifically: despite the flag, it runs platform phases.
        ("bash", "platform/scripts/deploy-all.sh", "--superplane-only"),
        ("sh", "-c", "aws iam delete-role --role-name x"),
        ("platform/scripts/deploy-all.sh", "--superplane-only", "--destroy"),
        ("gh", "workflow", "run", "superplane-infra-destroy.yml"),
        ("aws", "ssm", "put-parameter", "--name", "/adp/dev/superplane/namespace"),
    ],
)
def test_mutating_commands_are_outside_the_read_only_allowlist(argv):
    with pytest.raises(EvidenceError, match="read-only allowlist"):
        t.ReadOnlyCommands()(argv)


def test_every_allowlisted_shape_is_genuinely_read_only():
    """A guard against the allowlist itself being widened to a mutating verb."""
    mutating = (
        "delete",
        "remove",
        "rm",
        "put",
        "create",
        "apply",
        "destroy",
        "update",
        "set",
        "write",
        "patch",
        "replace",
        "edit",
        "run",
        "dispatch",
        "scale",
        "enable",
        "disable",
        "deploy",
        "undeploy",
        "migrate",
        "exec",
        "attach",
    )
    # A resource NOUN may legitimately contain a mutating verb as a substring: the rendered
    # object set includes a Deployment, and `kubectl get deployment` reads it. So the noun
    # position is exempted EXPLICITLY, by name, rather than by loosening the verb check.
    resource_nouns = {resource for _type, resource in t.K8S_OBJECT_KINDS.values()}
    for shape in t.READ_ONLY_SHAPES:
        assert shape[0] in {"aws", "kubectl"}, shape
        # The ACTION token is the one that decides whether a shape can change anything: the
        # operation in `aws <service> <operation>`, the verb in `kubectl <verb> <resource>`.
        action = shape[2] if shape[0] == "aws" or shape[1] == "config" else shape[1]
        assert action.split("-")[0] in {"get", "describe", "list", "view"}, shape
        for index, part in enumerate(shape):
            if index == 2 and shape[0] == "kubectl" and part in resource_nouns:
                continue
            assert not any(verb in part for verb in mutating), shape


def test_the_verifier_runs_no_command_outside_its_allowlist(config):
    """Belt and braces: assert on what the real verifier actually asks for."""
    issued = []

    def recording(argv):
        issued.append(tuple(argv))
        return commands()(argv)

    verify(config, run=recording)
    assert issued
    for argv in issued:
        assert tuple(argv[:3]) in t.READ_ONLY_SHAPES, argv


# ---------------------------------------------------------------------------
# The evidence record.
# ---------------------------------------------------------------------------
def test_no_artifact_is_written_on_failure(environment, monkeypatch):
    """A failed or partial check must leave nothing that later reads as acceptance."""
    monkeypatch.setattr(t, "GitHub", lambda: github_runs())
    monkeypatch.setattr(
        t,
        "ReadOnlyCommands",
        lambda: commands(
            per_resource={"adp-dev-superplane-control-plane": (0, "{}", "")}
        ),
    )
    values = environment()
    with pytest.raises(EvidenceError, match="did not remove every resource"):
        t.run_live(values)
    assert not Path(values["SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE"]).exists()


def test_live_record_is_published_atomically_with_private_permissions(
    environment, monkeypatch
):
    """Publication mechanics only.

    The record is stamped `live` here so the publication path downstream of the guard can be
    reached offline. That substitution is precisely why this test cannot establish live
    acceptance, and the guard it steps past is asserted separately above.
    """
    real_verify = t.verify

    def stamped_live(config):
        report = real_verify(config, github=github_runs(), run=commands())
        return {**report, "evidence_kind": "live", "status": "observed"}

    monkeypatch.setattr(t, "verify", stamped_live)
    values = environment()
    report = t.run_live(values)
    published = Path(values["SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE"])
    assert json.loads(published.read_text(encoding="utf-8")) == report
    assert published.stat().st_mode & 0o077 == 0


def test_record_carries_auditable_metadata_and_no_credential(config):
    report = verify(config)
    assert report["criterion"].startswith("U1-L1")
    assert report["target"]["account"] == ACCOUNT
    assert report["identity"]["cluster_arn"].endswith(f"cluster/{CLUSTER}")
    assert len(report["verifier_sha256"]) == 64
    assert len(report["inventory"]["observed_sha256"]) == 64
    assert report["inventory"]["derived_count"] > 0
    for role in ("deploy", "undeploy"):
        assert report["operations"][role]["run_url"].startswith("https://github.com/")
    # The record states what it does NOT establish, so it cannot be read as full U1.
    assert any("browser" in claim for claim in report["not_established"])
    assert any("feature API" in claim for claim in report["not_established"])
    serialised = json.dumps(report)
    for secret in ("Bearer ", "AKIA", "SUPERPLANE_LIVE_ADP_TOKEN", "aws_secret"):
        assert secret not in serialised


def test_record_is_json_serialisable_and_carries_no_helper_objects(config):
    report = verify(config)
    assert json.loads(json.dumps(report)) == report
    # The ordering helper must not leak into the published record.
    assert "_completed" not in json.dumps(report)


# ---------------------------------------------------------------------------
# The named command: explicit invocation fails on missing inputs, never skips.
# ---------------------------------------------------------------------------
def test_the_named_live_command_fails_without_inputs_and_does_not_skip(tmp_path):
    """Runs the real pytest command from the issue, with no live inputs present."""
    repository_root = REPOSITORY_ROOT
    target = f"{MODULE_PATH}/tests/acceptance/test_u1_live.py"
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith("SUPERPLANE_LIVE_")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            target,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=repository_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
        # A nonzero status is the expected outcome and is asserted below, so raising on it
        # would fail the test for demonstrating exactly what it set out to demonstrate.
        check=False,
    )
    assert result.returncode != 0, result.stdout
    assert "BLOCKED: missing explicit teardown acceptance inputs" in result.stdout
    # Zero skipped acceptance criteria: a skip here would read as "nothing to check".
    assert " skipped" not in result.stdout
    assert "1 failed" in result.stdout
