"""Observe that an executed deploy/undeploy actually removed what the deploy created.

This is the U1-L1 / R1 acceptance-4 observer for #5288, EPIC #4910. It is strictly
OBSERVATIONAL: every external command it may run is read-only, and it never deploys,
deletes, applies, destroys, migrates or enables a feature.

## Why absence is hard to observe, and what this module does about it

"The resources are gone" and "I could not see any resources" are different facts that
look identical in a report. A user-supplied empty inventory, a mocked inventory, an
unexecuted plan, an IAM denial and a malformed response would each produce a clean-looking
"nothing found". So this module refuses to treat any of them as cleanup:

*   The expected inventory is DERIVED from U3's merged Terraform/rendered-object contracts
    (`infra/control-plane/*.tf`, `releases/superplane.lock.yaml`, `k8s/*.yaml`) and the
    environment's tfvars, not taken from the caller. The caller's pre-teardown receipt must
    COVER the derived set; a short list is an incomplete-inventory refusal, not a pass.
*   That derivation reads the sources AT THE DEPLOYED REVISION, fetched from GitHub, not
    from whatever the verifier's checkout happens to contain. A resource the deploy created
    and a later commit renamed or deleted therefore stays in scope (PR #5544 review, F3).
*   Every identity is attributed with U3's reviewed ownership contract
    (`infra/scripts/domain_ownership.py`), so a foreign or platform-owned name cannot be
    "verified absent" by this check.
*   Each lookup is classified three ways — ABSENT, PRESENT, or INDETERMINATE. A denial,
    throttle, unreachable cluster or unparseable answer is INDETERMINATE and blocks.
    Indeterminate is never folded into absent.
*   The observation is bound to the selected account, region and cluster as they really
    are, and to successful executions of the module's own dispatch-only apply and destroy
    lanes at the stated revisions, with the destroy after the apply. Each lane is checked
    at the STEP level on its recorded attempt, because both lanes have paths that conclude
    `success` having done nothing (PR #5544 review, F1).
*   The pre-teardown inventory is an OBSERVATION RECEIPT this module produces from real
    reads (`record_pre_teardown`), carrying per-resource observed presence, the identity it
    read, the deploy it belongs to and the source hashes it derived from — not a list of
    names the caller asserts (PR #5544 review, F2).

No passing artifact is written on a failed or partial check, and an injected transport
produces an `offline-fixture` record that the live entry point refuses to publish.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .cli_delivery import TARGETS, EvidenceError, GitHub, pages, require

MODULE_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE = MODULE_ROOT / "infra" / "control-plane"
# Deliberately NO local-checkout paths for the derivation sources. Those are fetched at the
# deployed revision (see `RevisionSources`); a local path would reintroduce F3.

APPLY_WORKFLOW = ".github/workflows/superplane-infra-apply.yml"
DESTROY_WORKFLOW = ".github/workflows/superplane-infra-destroy.yml"
# The ROLLOUT lane. It is the only execution in this repository that CREATES the rendered
# Kubernetes objects, so the check needs it as creation evidence for the Kubernetes half of the
# inventory (PR #5544 review, F3: "bind authoritative rollout and teardown execution receipts").
#
# It is emphatically NOT teardown evidence, and the shape of this module keeps those two roles
# apart: this constant is reachable only through LANE_EXECUTION["rollout"], and
# `K8S_TEARDOWN_WORKFLOWS` — the registry `verify_k8s_teardown` consults — must never contain it.
# Its only mutating step applies manifests; crediting it as a deletion would be F1 exactly.
# `test_the_rollout_lane_is_creation_evidence_and_never_teardown_evidence` asserts that boundary.
ROLLOUT_WORKFLOW = ".github/workflows/superplane-k8s-deploy.yml"

# What each lane must be shown to have ACTUALLY DONE, at the step level, on the attempt it
# recorded — not merely to have concluded `success` (PR #5544 review, F1).
#
# Both Terraform lanes have success-without-work paths. The destroy lane's state-safety step
# sets `EMPTY_STATE=true` when Terraform's state is empty, and the two steps that follow are
# `if: env.EMPTY_STATE != 'true'` — so a run that destroyed nothing is indistinguishable from
# one that destroyed everything if you only read `conclusion`. GitHub records those steps with
# conclusion `skipped`, which is precisely the signal to key on.
#
# Each entry is (job name, required step names). The step names are the workflows' own
# `- name:` values; a rename must fail this check loudly rather than silently stop proving
# anything, which is why they are matched exactly.
LANE_EXECUTION = {
    "deploy": (
        APPLY_WORKFLOW,
        "Terraform Apply",
        (
            "Terraform Init",
            "Plan-safety gate (destructive approval + domain ownership)",
            "Terraform Apply",
        ),
    ),
    "undeploy": (
        DESTROY_WORKFLOW,
        "Destroy Superplane Control Plane",
        # The three steps the empty-state path skips, plus the account guard that binds the
        # run to a real account. "Terraform Destroy" is the one that actually deletes.
        (
            "Validate the typed account ID against the caller identity",
            "Save the destroy plan",
            "Validate every deletion is domain-owned",
            "Terraform Destroy",
        ),
    ),
    # Creation evidence for the rendered Kubernetes objects. `Apply` is `if: inputs.dry_run ==
    # false`, so a dry run concludes `success` having created nothing — the same
    # success-without-work shape as the Terraform lanes, and the reason the step list includes
    # the guard that validated the object set actually applied.
    "rollout": (
        ROLLOUT_WORKFLOW,
        "Roll out Superplane manifests",
        (
            "Resolve and bind the target account",
            "Guard — validate the rendered object set before touching the cluster",
            "Apply",
        ),
    ),
}

# Lanes that DELETE Kubernetes objects. Deliberately empty, and that is a finding rather than
# an oversight (PR #5544 review, F1/F3).
#
# `superplane-infra-destroy.yml` states in its own summary that Kubernetes objects survive it
# and to "delete those via the rollout lane's own teardown". `superplane-k8s-deploy.yml` has no
# teardown — its only mutating step is `Apply`. `k8s/rollback.sh --teardown` DOES delete the
# nine namespaced objects (and deliberately retains the namespace), but no workflow in this
# repository invokes it, so there is no execution record to cite for those deletions.
#
# Applying manifests is not deleting them, and accepting a rollout run as teardown evidence
# would be exactly the "green run proves cleanup" inference F1 is about. The DELETED portion of
# the Kubernetes inventory therefore BLOCKS with its reason named, and stays blocked until a
# dispatch-only lane that runs `k8s/rollback.sh --teardown` exists, publishes an execution
# attestation, and is registered here. The RETAINED namespace does not block on this: U3's
# contract says it survives teardown, so it is checked against that contract instead (F3).
K8S_TEARDOWN_WORKFLOWS: dict[str, tuple[str, tuple[str, ...]]] = {}

# The lane that RECORDS the pre-teardown observation receipt. Also deliberately empty, for the
# same class of reason (PR #5544 review, F2).
#
# A receipt is only evidence if something other than its own author vouches for it. The
# authority this module can use is GitHub's own artifact record: an artifact is bound to a run
# and attempt, carries a digest GitHub computed, and carries a creation time GitHub stamped —
# none of which the receipt's author controls. So `bind_receipt` requires the receipt's bytes to
# hash to the digest GitHub recorded for a verified recorder run's receipt artifact.
#
# No workflow in this repository runs `record_pre_teardown` and uploads that artifact. Until one
# exists and is registered here, the receipt cannot be authenticated and the check BLOCKS naming
# exactly what is missing. `record_pre_teardown` remains the producer of the document; what is
# absent is the lane that executes it under an identity GitHub can attest to.
RECORDER_WORKFLOWS: dict[str, tuple[str, tuple[str, ...]]] = {}

# A dispatched operation, not an incidental branch build. Both Terraform lanes are
# `workflow_dispatch`-only by design, so anything else is not one of this module's lanes.
DISPATCH_EVENT = "workflow_dispatch"

# The Kubernetes `kind: Namespace` placeholders U3's manifests use, mapped to the tfvars
# variable that supplies the real name. An unknown placeholder is refused rather than
# guessed: a namespace this module cannot name is a namespace it cannot check.
NAMESPACE_PLACEHOLDER_VARIABLE = {
    "REPLACE_WITH_NAMESPACE": "namespace",
    "REPLACE_WITH_SKYPILOT_NAMESPACE": "skypilot_namespace",
}

# Every Kubernetes kind U3's manifests render, mapped to this checker's observable type and the
# `kubectl get` resource token used to look one up (PR #5544 review, F3).
#
# The previous version derived only `kind: Namespace`, which is the one object U3 deliberately
# does NOT delete — so the inventory omitted the entire set `rollback.sh --teardown` removes and
# held the retained object to an absence standard. A kind that appears in the manifests and is
# absent from this map BLOCKS rather than being dropped from coverage: an object nobody looks for
# is an object whose survival cannot be noticed.
K8S_OBJECT_KINDS = {
    "Namespace": ("kubernetes_namespace", "namespace"),
    "ServiceAccount": ("kubernetes_service_account", "serviceaccount"),
    "Role": ("kubernetes_role", "role"),
    "RoleBinding": ("kubernetes_role_binding", "rolebinding"),
    "ConfigMap": ("kubernetes_config_map", "configmap"),
    "NetworkPolicy": ("kubernetes_network_policy", "networkpolicy"),
    "Service": ("kubernetes_service", "service"),
    "Deployment": ("kubernetes_deployment", "deployment"),
}

# What the `kubectl delete` lines in `k8s/rollback.sh` name, mapped back to the same types. The
# script writes `deployment/skypilot-api`; the lifecycle classification is read from those
# tokens, so U3's script stays the single statement of which objects a teardown removes.
K8S_RESOURCE_TYPE = {
    resource: kind_type for kind_type, resource in K8S_OBJECT_KINDS.values()
}

# The same map inverted, for building a read. `kubernetes_namespace` is excluded deliberately: a
# Namespace is cluster-scoped, so its read takes no `-n`, and it is handled on its own branch.
K8S_RESOURCE_TOKEN = {
    kind_type: resource
    for kind_type, resource in K8S_OBJECT_KINDS.values()
    if kind_type != "kubernetes_namespace"
}

# The two lifecycles U3's teardown contract distinguishes.
#
# DELETED objects are the ones `rollback.sh --teardown` removes, and their absence after an
# authorized teardown is what U1-L1 acceptance 4 is about. RETAINED objects are the ones the
# script states it deliberately leaves behind — today the namespace, because deleting it would
# take the out-of-band `skypilot-api-db` Secret with it. A retained object's absence is NOT
# cleanup evidence and its presence is NOT a cleanup failure; it is outside the absence
# criterion, and the record says so rather than inventing a deletion requirement for it.
DELETED = "deleted"
RETAINED = "retained"

MAX_INVENTORY_BYTES = 262_144
MAX_ATTESTATION_BYTES = 1_048_576

ABSENT = "absent"
PRESENT = "present"
INDETERMINATE = "indeterminate"

# AWS/Kubernetes error tokens that positively mean "this resource does not exist". Anything
# else — AccessDenied, throttling, an expired token, a network failure — is INDETERMINATE.
# The mapping is per resource type because a token that proves absence for one API says
# nothing about another.
ABSENCE_TOKENS = {
    "aws_iam_role": ("NoSuchEntity",),
    "aws_ssm_parameter": ("ParameterNotFound",),
    "aws_ecr_repository": ("RepositoryNotFoundException",),
    # Every rendered Kubernetes kind. `kubectl` reports a missing object as `NotFound`, and a
    # missing NAMESPACE for a namespaced read the same way — which is correct here, because if
    # the namespace is gone so is everything that was in it.
    **{kind_type: ("NotFound",) for kind_type, _resource in K8S_OBJECT_KINDS.values()},
}


# ---------------------------------------------------------------------------
# U3's merged contracts, loaded by path.
#
# Loaded with importlib rather than by mutating sys.path: these two modules live under
# `infra/`, are not part of an installable package, and a global path insertion would make
# their private module names importable from anywhere in the test session. Loading by path
# also fails loudly if either file moves, which is the correct outcome — this observer's
# inventory and ownership rules are U3's, and it must not silently fall back to its own.
# ---------------------------------------------------------------------------
def _load(name: str, path: Path):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if (
        spec is None or spec.loader is None
    ):  # pragma: no cover - a move must fail loudly
        raise EvidenceError(f"BLOCKED: required U3 contract {path.name} is unavailable")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE execution, not after. `domain_ownership` defines dataclasses, and
    # `@dataclass` resolves its annotations through `sys.modules[cls.__module__]` — absent
    # there, decorating raises `AttributeError: 'NoneType' object has no attribute
    # '__dict__'` from inside dataclasses. Removed again on failure so a partially executed
    # module is never served to a later caller.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        raise EvidenceError(
            f"BLOCKED: required U3 contract {path.name} could not be loaded"
        ) from exc
    return module


def ownership():
    """U3's reviewed ownership contract (`infra/scripts/domain_ownership.py`)."""
    return _load(
        "_sp_domain_ownership",
        MODULE_ROOT / "infra" / "scripts" / "domain_ownership.py",
    )


def derived_names():
    """U3's source-derived resource names (`control-plane/tests/source_derived_names.py`)."""
    return _load(
        "_sp_source_derived_names", CONTROL_PLANE / "tests" / "source_derived_names.py"
    )


# ---------------------------------------------------------------------------
# Read-only command boundary.
# ---------------------------------------------------------------------------
# An allowlist of complete command SHAPES, not a denylist of dangerous verbs. A denylist
# passes any verb nobody thought to forbid, and this module's whole value is that it cannot
# change the environment it observes. `bash`/`sh` are absent, which is also what makes
# `deploy-all.sh --superplane-only` unreachable from here — the issue calls that out
# specifically because the flag still runs shared platform phases.
READ_ONLY_SHAPES = frozenset(
    {
        ("aws", "sts", "get-caller-identity"),
        ("aws", "iam", "get-role"),
        ("aws", "ssm", "get-parameter"),
        ("aws", "ecr", "describe-repositories"),
        ("aws", "eks", "describe-cluster"),
        ("kubectl", "config", "view"),
    }
    # One `kubectl get <resource>` shape per rendered kind (PR #5544 review, F3). Enumerated
    # from `K8S_OBJECT_KINDS` rather than written out, so a kind added there cannot be
    # observable in the inventory while being unreachable through the boundary — and cannot be
    # reached by any verb other than `get`, since that token is fixed here.
    | {("kubectl", "get", resource) for _type, resource in K8S_OBJECT_KINDS.values()}
)


class ReadOnlyCommands:
    """Runs allowlisted read-only commands. Returns (exit status, stdout, stderr)."""

    def __call__(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        __tracebackhide__ = True
        require(
            len(argv) >= 3 and tuple(argv[:3]) in READ_ONLY_SHAPES,
            "Refusing a command outside this checker's read-only allowlist",
        )
        try:
            result = subprocess.run(
                list(argv),
                capture_output=True,
                # A nonzero status is expected and meaningful here: it is how a resource's
                # absence is reported. Raising on it would turn the answer into an error.
                check=False,
                timeout=60,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvidenceError(
                f"BLOCKED: read-only observation with {argv[0]} could not be executed"
            ) from exc
        return result.returncode, result.stdout, result.stderr


def _json(stdout: str, what: str) -> dict:
    __tracebackhide__ = True
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        # The raw body can carry account/identity detail; never echo it.
        raise EvidenceError(f"BLOCKED: {what} returned unreadable output") from None
    require(type(value) is dict, f"BLOCKED: {what} did not return a JSON object")
    return value


# ---------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------
INPUTS = (
    "SUPERPLANE_LIVE_ENVIRONMENT",
    "SUPERPLANE_LIVE_TF_ENVIRONMENT",
    "SUPERPLANE_LIVE_CLUSTER",
    "SUPERPLANE_LIVE_DEPLOY_RUN_ID",
    "SUPERPLANE_LIVE_DEPLOY_SHA",
    # The rollout run. Creation evidence for the rendered Kubernetes objects (PR #5544 review,
    # F3): the Terraform apply creates none of them, so without this the K8s half of the
    # inventory would have no execution behind it at all.
    "SUPERPLANE_LIVE_ROLLOUT_RUN_ID",
    "SUPERPLANE_LIVE_ROLLOUT_SHA",
    "SUPERPLANE_LIVE_UNDEPLOY_RUN_ID",
    "SUPERPLANE_LIVE_UNDEPLOY_SHA",
    "SUPERPLANE_LIVE_INVENTORY_FILE",
    # Where the GitHub-recorded execution attestation archives for those runs have been
    # downloaded to (PR #5544 review, F1/F2). The archives are authenticated against the digest
    # GitHub itself recorded, so this directory's contents cannot be authored by the caller in
    # any way that passes.
    "SUPERPLANE_LIVE_ATTESTATION_DIR",
    "SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE",
)

# The lanes whose executions this check must establish, in the order they must have happened.
OPERATION_ROLES = ("deploy", "rollout", "undeploy")

# What `record_pre_teardown` needs. No undeploy run and no evidence file: it runs before the
# teardown, so neither exists yet. `SUPERPLANE_LIVE_INVENTORY_FILE` is the receipt it WRITES
# and the verifier later READS.
RECORDER_INPUTS = (
    "SUPERPLANE_LIVE_ENVIRONMENT",
    "SUPERPLANE_LIVE_TF_ENVIRONMENT",
    "SUPERPLANE_LIVE_CLUSTER",
    "SUPERPLANE_LIVE_DEPLOY_RUN_ID",
    "SUPERPLANE_LIVE_DEPLOY_SHA",
    "SUPERPLANE_LIVE_INVENTORY_FILE",
)


def settings(environment) -> dict:
    """Validate every input. A missing input BLOCKS; nothing is defaulted."""
    __tracebackhide__ = True
    missing = [name for name in INPUTS if not environment.get(name)]
    require(
        not missing,
        "BLOCKED: missing explicit teardown acceptance inputs: " + ", ".join(missing),
    )
    target = environment["SUPERPLANE_LIVE_ENVIRONMENT"]
    require(target in TARGETS, "BLOCKED: target is not in the reviewed registry")

    tf_environment = environment["SUPERPLANE_LIVE_TF_ENVIRONMENT"]
    # The same token U3's guards require, so the names derived below are the names the
    # deploy actually used. Reusing its validator rather than restating the rule.
    require(
        ownership().VALID_ENVIRONMENT.match(tf_environment) is not None,
        "BLOCKED: SUPERPLANE_LIVE_TF_ENVIRONMENT is not a valid environment token",
    )

    cluster = environment["SUPERPLANE_LIVE_CLUSTER"]
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster) is not None,
        "BLOCKED: SUPERPLANE_LIVE_CLUSTER is not a valid EKS cluster name",
    )

    runs = {}
    for role in OPERATION_ROLES:
        prefix = role.upper()
        run = environment[f"SUPERPLANE_LIVE_{prefix}_RUN_ID"]
        sha = environment[f"SUPERPLANE_LIVE_{prefix}_SHA"]
        require(
            re.fullmatch(r"[1-9][0-9]*", run) is not None,
            f"BLOCKED: the {role} run ID must be a positive GitHub run ID",
        )
        require(
            re.fullmatch(r"[0-9a-f]{40}", sha) is not None,
            f"BLOCKED: the {role} revision must be an exact 40-hex commit",
        )
        runs[role] = {"run_id": int(run), "sha": sha}

    inventory_path = Path(environment["SUPERPLANE_LIVE_INVENTORY_FILE"])
    require(
        inventory_path.is_absolute() and inventory_path.is_file(),
        "BLOCKED: the pre-teardown inventory must be an existing absolute file",
    )
    attestation_dir = Path(environment["SUPERPLANE_LIVE_ATTESTATION_DIR"])
    require(
        attestation_dir.is_absolute() and attestation_dir.is_dir(),
        "BLOCKED: the execution attestations must be in an existing absolute directory",
    )
    output = Path(environment["SUPERPLANE_LIVE_TEARDOWN_EVIDENCE_FILE"])
    require(
        output.is_absolute() and output.parent.is_dir() and not os.path.lexists(output),
        "BLOCKED: evidence needs an absolute new filename in an existing directory",
    )
    # The Kubernetes teardown run, when a deletion lane exists to cite. Optional in shape only:
    # `verify_k8s_teardown` requires it whenever the inventory covers a namespace, so omitting
    # it blocks rather than silently skipping the namespace.
    k8s_run = environment.get("SUPERPLANE_LIVE_K8S_TEARDOWN_RUN_ID")
    k8s_teardown = None
    if k8s_run:
        attempt = environment.get("SUPERPLANE_LIVE_K8S_TEARDOWN_ATTEMPT", "1")
        require(
            re.fullmatch(r"[1-9][0-9]*", k8s_run) is not None
            and re.fullmatch(r"[1-9][0-9]*", attempt) is not None,
            "BLOCKED: the Kubernetes teardown run ID and attempt must be positive integers",
        )
        k8s_teardown = {"run_id": int(k8s_run), "run_attempt": int(attempt)}

    # The recorder run, when a lane exists that executes `record_pre_teardown` and uploads its
    # receipt as an artifact. Optional in shape only: `bind_receipt` requires it, so omitting it
    # blocks rather than accepting an unauthenticated receipt.
    recorder = environment.get("SUPERPLANE_LIVE_RECORDER_RUN_ID")
    recorder_run = None
    if recorder:
        attempt = environment.get("SUPERPLANE_LIVE_RECORDER_ATTEMPT", "1")
        require(
            re.fullmatch(r"[1-9][0-9]*", recorder) is not None
            and re.fullmatch(r"[1-9][0-9]*", attempt) is not None,
            "BLOCKED: the recorder run ID and attempt must be positive integers",
        )
        recorder_run = {"run_id": int(recorder), "run_attempt": int(attempt)}

    # Deliberately does not retain the rest of the process environment.
    return {
        "environment": target,
        "tf_environment": tf_environment,
        "cluster": cluster,
        "runs": runs,
        "k8s_teardown_run": k8s_teardown,
        "recorder_run": recorder_run,
        "inventory_file": str(inventory_path),
        "attestation_dir": str(attestation_dir),
        "evidence_file": str(output),
        **TARGETS[target],
    }


# ---------------------------------------------------------------------------
# The expected inventory, derived from U3's sources.
# ---------------------------------------------------------------------------
MODULE_PATH = "modules/domain-apps/superplane"


class RevisionSources:
    """The module's source AT ONE EXACT REVISION, fetched from GitHub and cached.

    Why not the local checkout (PR #5544 review, F3): the expected inventory defines what the
    check goes looking for, so reading it from whatever the verifier happens to have checked
    out means a resource the deploy created and a later commit renamed or removed silently
    stops being checked. Coverage would shrink and cleanup would "pass" without anyone ever
    looking for that resource. Pinning to the deploy's own revision makes the scope of the
    check a property of the deployment rather than of the verifier's working tree.

    Every file read is hashed, and the hashes go into the evidence, so a reader can confirm
    which contract text the derivation actually used.
    """

    def __init__(self, github, sha: str):
        self._github, self._sha = github, sha
        self._cache: dict[str, str] = {}
        self.hashes: dict[str, str] = {}

    def read(self, path: str) -> str:
        """Fetch one repository-relative path at this revision."""
        __tracebackhide__ = True
        if path in self._cache:
            return self._cache[path]
        try:
            data = self._github(f"contents/{path}?ref={self._sha}")
        except EvidenceError:
            raise EvidenceError(
                f"BLOCKED: {path} could not be read at the deployed revision, so the "
                f"inventory cannot be derived from what was actually deployed"
            ) from None
        require(
            type(data) is dict and data.get("encoding") == "base64",
            f"BLOCKED: {path} is unavailable at the deployed revision",
        )
        try:
            raw = base64.b64decode(data["content"].replace("\n", ""), validate=True)
        except (KeyError, ValueError):
            raise EvidenceError(
                f"BLOCKED: {path} returned malformed content at the deployed revision"
            ) from None
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise EvidenceError(
                f"BLOCKED: {path} is not decodable text at the deployed revision"
            ) from None
        self._cache[path] = text
        self.hashes[path] = hashlib.sha256(raw).hexdigest()
        return text

    def listing(self, directory: str) -> list[str]:
        """File names in one directory at this revision, sorted."""
        __tracebackhide__ = True
        try:
            entries = self._github(f"contents/{directory}?ref={self._sha}")
        except EvidenceError:
            raise EvidenceError(
                f"BLOCKED: {directory} could not be listed at the deployed revision"
            ) from None
        require(
            type(entries) is list and bool(entries),
            f"BLOCKED: {directory} is empty or unreadable at the deployed revision",
        )
        names = [
            entry["name"]
            for entry in entries
            if type(entry) is dict
            and entry.get("type") == "file"
            and type(entry.get("name")) is str
        ]
        require(
            bool(names),
            f"BLOCKED: {directory} contains no files at the deployed revision",
        )
        return sorted(names)


class DerivedNamesAtRevision:
    """U3's derivation rules, applied to source text fetched at the deployed revision.

    U3's `source_derived_names.py` owns the naming contract, and this must not become a second
    competing implementation of it — that is exactly what the issue forbids. So the REGEXES and
    the ARN builders come from U3's module; only the source text is swapped, from
    `CONTROL_PLANE_DIR / filename` to the revision fetch. U3 remains the single definition of
    how a name is derived; this changes which bytes it derives from.
    """

    def __init__(self, sources: RevisionSources):
        self._sources = sources
        self._u3 = derived_names()

    def _read(self, filename: str) -> str:
        return self._sources.read(f"{MODULE_PATH}/infra/control-plane/{filename}")

    def _local_value(self, filename: str, local_name: str) -> str:
        __tracebackhide__ = True
        match = re.search(
            rf'^\s*{re.escape(local_name)}\s*=\s*"([^"]+)"\s*$',
            self._read(filename),
            re.MULTILINE,
        )
        require(
            match is not None,
            f"BLOCKED: local {local_name!r} is absent from {filename} at the deployed "
            f"revision; the naming contract cannot be reproduced",
        )
        return match.group(1)

    def name_prefix(self, environment: str) -> str:
        return self._local_value("main.tf", "name_prefix").replace(
            "${var.environment}", environment
        )

    def parameter_prefix(self, environment: str) -> str:
        return self._local_value("config.tf", "parameter_prefix").replace(
            "${var.environment}", environment
        )

    def iam_role_names(self, environment: str) -> list[str]:
        __tracebackhide__ = True
        suffixes = re.findall(
            r'resource\s+"aws_iam_role"\s+"[^"]+"\s*\{[^}]*?name\s*=\s*'
            r'"\$\{local\.name_prefix\}([^"]*)"',
            self._read("irsa.tf"),
            re.DOTALL,
        )
        require(
            bool(suffixes),
            "BLOCKED: no aws_iam_role names found in irsa.tf at the deployed revision",
        )
        prefix = self.name_prefix(environment)
        return [f"{prefix}{suffix}" for suffix in suffixes]

    def ssm_parameter_names(self, environment: str) -> list[str]:
        __tracebackhide__ = True
        suffixes = re.findall(
            r'name\s*=\s*"\$\{local\.parameter_prefix\}([^"]*)"',
            self._read("config.tf"),
        )
        require(
            bool(suffixes),
            "BLOCKED: no aws_ssm_parameter names found in config.tf at the deployed revision",
        )
        prefix = self.parameter_prefix(environment)
        return [f"{prefix}{suffix}" for suffix in suffixes]

    def ecr_repository_names(self) -> list[str]:
        __tracebackhide__ = True
        lock = self._sources.read(f"{MODULE_PATH}/releases/superplane.lock.yaml")
        names = sorted(set(re.findall(r"ecr_repository:\s*(\S+)", lock)))
        require(
            bool(names),
            "BLOCKED: no ecr_repository entries in the release lock at the deployed revision",
        )
        return names

    # ARN construction is U3's, unchanged and not restated here.
    def iam_role_arn(self, environment: str, account: str, name: str) -> str:
        return self._u3.iam_role_arn(environment, account, name)

    def ssm_parameter_arn(
        self, environment: str, account: str, region: str, name: str
    ) -> str:
        return self._u3.ssm_parameter_arn(environment, account, region, name)

    def ecr_repository_arn(self, account: str, region: str, repository: str) -> str:
        return self._u3.ecr_repository_arn(account, region, repository)


def _tfvars_value(sources: RevisionSources, tf_environment: str, variable: str) -> str:
    """Read one simple string variable out of the environment's real tfvars file.

    This is the file the apply and destroy lanes both pass with `-var-file`, so it is the
    deploy's own input rather than a restatement of it — read at the deployed revision, since
    a later edit to the namespace variable must not change what this check looks for.
    """
    __tracebackhide__ = True
    try:
        source = sources.read(
            f"environments/{tf_environment}/modules/superplane.tfvars"
        )
    except EvidenceError:
        raise EvidenceError(
            f"BLOCKED: environment '{tf_environment}' has no superplane.tfvars at the "
            f"deployed revision, so the deploy inventory cannot be derived"
        ) from None
    match = re.search(
        rf'^\s*{re.escape(variable)}\s*=\s*"([^"\n]+)"\s*$', source, re.MULTILINE
    )
    require(
        match is not None,
        f"BLOCKED: superplane.tfvars does not define {variable!r}; the inventory cannot "
        f"be derived from the deploy's own inputs",
    )
    return match.group(1)


def _resolve_placeholder(
    sources: RevisionSources, tf_environment: str, value: str, document: str, what: str
) -> str:
    """Resolve a manifest value that may be a namespace placeholder, or block.

    A placeholder this checker cannot map to a deploy input names an object it cannot observe,
    and dropping such an object would shrink coverage invisibly. Non-placeholder values (the
    object names, which U3 writes literally) pass through unchanged.
    """
    __tracebackhide__ = True
    if not value.startswith("REPLACE_WITH_"):
        return value
    variable = NAMESPACE_PLACEHOLDER_VARIABLE.get(value)
    require(
        variable is not None,
        f"BLOCKED: {document} uses the placeholder {value!r} as {what}, which this checker "
        f"cannot resolve to a deploy input; refusing to skip it",
    )
    return _tfvars_value(sources, tf_environment, variable)


def _rendered_objects(sources: RevisionSources, document: str) -> list[dict]:
    """Every `kind`/`metadata.name`/`metadata.namespace` triple in one manifest file.

    A deliberately small structural reader rather than a YAML parse: PyYAML is not a dependency
    of this acceptance package, and U3's manifests are the flat, two-space, one-object-per-
    `---`-block form `check_rendered_manifests.py` validates. What matters for coverage is that
    an object present in the text cannot be missed, so an object whose `kind` is recognised but
    whose `metadata.name` is not found BLOCKS rather than being skipped.
    """
    __tracebackhide__ = True
    text = sources.read(f"{MODULE_PATH}/k8s/{document}")
    objects: list[dict] = []
    kind: str | None = None
    fields: dict[str, str] = {}

    def flush() -> None:
        if kind is None:
            return
        require(
            "name" in fields,
            f"BLOCKED: {document} declares a {kind} with no metadata.name at the deployed "
            f"revision, so the object cannot be identified or observed",
        )
        objects.append({"kind": kind, **fields})

    for line in text.splitlines():
        if re.fullmatch(r"kind:\s*(\S+)\s*", line):
            flush()
            kind, fields = re.fullmatch(r"kind:\s*(\S+)\s*", line).group(1), {}
            continue
        if kind is None:
            continue
        # Only `metadata:`'s own two-space children. A deeper `name:`/`namespace:` belongs to a
        # RoleBinding subject, a container, or a selector — not to this object's identity.
        match = re.fullmatch(r"\s{2}(name|namespace):\s*(\S+)\s*", line)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2)
        # A top-level key after metadata ends the identity block; keep scanning for the next
        # `kind:` rather than reading `spec:`'s own names.
        if re.fullmatch(r"(spec|rules|data|subjects|roleRef|stringData):.*", line):
            flush()
            kind, fields = None, {}
    flush()
    return objects


def derived_k8s_objects(sources: RevisionSources, tf_environment: str) -> list[dict]:
    """Every owned rendered Kubernetes object at the deployed revision (F3).

    The previous version derived `kind: Namespace` only. That is the ONE object U3's teardown
    deliberately keeps (`k8s/rollback.sh` says so in as many words), so the inventory omitted the
    entire set a teardown actually removes — ServiceAccount, Role, RoleBinding, ConfigMap, three
    NetworkPolicies, Service and Deployment — while holding the retained object to an absence
    standard. Both halves of that were wrong, and this derives the real set instead.

    Each object is classified by U3's own lifecycle contract, read from the `kubectl delete`
    lines in `k8s/rollback.sh` at the deployed revision: an object that script deletes is
    `DELETED`, anything else it renders is `RETAINED`. The classification is therefore U3's
    statement, not this checker's opinion, and no deletion requirement is invented for an object
    U3 keeps.

    Read at the deployed revision throughout, so a manifest or script edited after the deploy
    does not change what this check holds the teardown to.
    """
    __tracebackhide__ = True
    documents = [
        name
        for name in sources.listing(f"{MODULE_PATH}/k8s")
        if name.endswith((".yaml", ".yml"))
    ]
    require(
        bool(documents),
        "BLOCKED: U3's rendered manifests are unavailable at the deployed revision; "
        "inventory coverage for Kubernetes objects cannot be established",
    )
    deleted = teardown_deleted_resources(sources)

    derived: list[dict] = []
    for document in documents:
        for obj in _rendered_objects(sources, document):
            kind = obj["kind"]
            mapped = K8S_OBJECT_KINDS.get(kind)
            require(
                mapped is not None,
                f"BLOCKED: {document} renders a {kind} object, which this checker has no "
                f"read-only lookup for. An object nobody looks for is an object whose "
                f"survival cannot be noticed, so coverage must not silently exclude it.",
            )
            kind_type, _resource = mapped
            name = _resolve_placeholder(
                sources, tf_environment, obj["name"], document, "an object name"
            )
            if kind == "Namespace":
                namespace = name
            else:
                require(
                    "namespace" in obj,
                    f"BLOCKED: {document} renders a namespaced {kind} {name!r} with no "
                    f"metadata.namespace at the deployed revision; it cannot be looked up",
                )
                namespace = _resolve_placeholder(
                    sources, tf_environment, obj["namespace"], document, "a namespace"
                )
            derived.append(
                {
                    "type": kind_type,
                    "name": name,
                    "namespace": namespace,
                    # U3's script decides this, not this module.
                    "lifecycle": DELETED if (kind_type, name) in deleted else RETAINED,
                }
            )
    require(
        bool(derived),
        "BLOCKED: no Kubernetes object found in U3's manifests at the deployed revision; "
        "refusing to validate Kubernetes cleanup against an empty derivation",
    )
    require(
        any(entry["lifecycle"] == DELETED for entry in derived),
        "BLOCKED: no rendered object matches anything k8s/rollback.sh --teardown deletes at "
        "the deployed revision, so there is no deletion set to hold the teardown to",
    )
    return sorted(
        derived, key=lambda entry: (entry["type"], entry["namespace"], entry["name"])
    )


def teardown_deleted_resources(sources: RevisionSources) -> set[tuple[str, str]]:
    """What `k8s/rollback.sh --teardown` deletes, read from the script at the deployed revision.

    U3's script is the lifecycle contract, so it is read rather than restated. Its teardown loop
    names objects as `deployment/skypilot-api`, `role/skypilot-api` and so on; each becomes a
    `(type, name)` pair here. A resource token this checker does not recognise BLOCKS: an object
    the script deletes but this map cannot classify would otherwise be filed as RETAINED and
    never held to the absence standard, which is the F3 defect in the opposite direction.
    """
    __tracebackhide__ = True
    script = sources.read(f"{MODULE_PATH}/k8s/rollback.sh")
    block = re.search(
        r'if \[ "\$TEARDOWN" = "true" \]; then(.*?)\n  exit 0\n', script, re.DOTALL
    )
    require(
        block is not None,
        "BLOCKED: k8s/rollback.sh has no --teardown block at the deployed revision, so U3's "
        "Kubernetes deletion contract cannot be read",
    )
    pairs: set[tuple[str, str]] = set()
    for resource, name in re.findall(
        r'"(?:\$\{?[A-Za-z_]+\}?/|)([a-z]+)/(\$\{?[A-Za-z_]+\}?|[a-z0-9-]+)"',
        block.group(1),
    ):
        if resource not in K8S_RESOURCE_TYPE:
            continue
        # `deployment/${DEPLOYMENT}` — the script's own variable, whose only assignment is a
        # literal at the top of the file. Resolved from there rather than assumed.
        if name.startswith("$"):
            variable = name.strip("${}")
            match = re.search(
                rf'^{re.escape(variable)}="([^"]+)"\s*$', script, re.MULTILINE
            )
            require(
                match is not None,
                f"BLOCKED: k8s/rollback.sh deletes {resource}/{name} but does not define "
                f"{variable} at the deployed revision; the deletion set cannot be resolved",
            )
            name = match.group(1)
        pairs.add((K8S_RESOURCE_TYPE[resource], name))
    require(
        bool(pairs),
        "BLOCKED: k8s/rollback.sh --teardown names no recognised object at the deployed "
        "revision; refusing to derive an empty Kubernetes deletion set",
    )
    return pairs


def derived_inventory(config: dict, sources: RevisionSources) -> list[dict]:
    """Everything the DEPLOYED revision creates, derived from its own sources.

    Inline IAM role policies, the role-policy attachment and the ECR lifecycle policies are
    deliberately not separate entries: each is deleted with its parent role or repository
    and has no independent existence to observe. The parents ARE entries, so their removal
    is what is checked.
    """
    __tracebackhide__ = True
    names = DerivedNamesAtRevision(sources)
    environment, account, region = (
        config["tf_environment"],
        config["account"],
        config["region"],
    )
    roles = names.iam_role_names(environment)
    parameters = names.ssm_parameter_names(environment)
    repositories = names.ecr_repository_names()
    require(
        bool(roles) and bool(parameters) and bool(repositories),
        "BLOCKED: an empty derivation would make the cleanup check vacuous",
    )

    inventory = [
        {
            "type": "aws_iam_role",
            "name": name,
            "arn": names.iam_role_arn(environment, account, name),
        }
        for name in sorted(set(roles))
    ]
    inventory += [
        {
            "type": "aws_ssm_parameter",
            "name": name,
            "arn": names.ssm_parameter_arn(environment, account, region, name),
        }
        for name in sorted(set(parameters))
    ]
    inventory += [
        {
            "type": "aws_ecr_repository",
            "name": name,
            "arn": names.ecr_repository_arn(account, region, name),
        }
        for name in sorted(set(repositories))
    ]
    inventory += derived_k8s_objects(sources, config["tf_environment"])
    return inventory


def k8s_entries(inventory: list[dict], lifecycle: str | None = None) -> list[dict]:
    """The Kubernetes half of an inventory, optionally narrowed to one lifecycle."""
    return [
        entry
        for entry in inventory
        if entry["type"] in K8S_RESOURCE_TYPE.values()
        and (lifecycle is None or entry.get("lifecycle") == lifecycle)
    ]


def attribute(resources: list[dict], config: dict, expected: list[dict]) -> None:
    """Refuse any identity this domain does not own, using U3's reviewed contract.

    A cleanup checker that accepted arbitrary identities could be pointed at the platform's
    networking or another module's roles, and reporting those "absent" would be actively
    misleading. Kubernetes namespaces are not Terraform resources, so they are attributed
    against the set derived at the deployed revision instead.
    """
    __tracebackhide__ = True
    contract = ownership()
    # Every rendered Kubernetes object is attributed against the set derived at the deployed
    # revision, keyed by (type, namespace, name) so a same-named object in the platform's or
    # another domain's namespace cannot be reported on (PR #5544 review, F3).
    permitted_objects = {
        (entry["type"], entry.get("namespace"), entry["name"])
        for entry in k8s_entries(expected)
    }
    for resource in resources:
        kind, name = resource["type"], resource["name"]
        if kind in K8S_RESOURCE_TYPE.values():
            namespace = resource.get("namespace")
            require(
                (kind, namespace, name) in permitted_objects,
                f"{kind} {name!r} in namespace {namespace!r} is not one this domain's "
                f"manifests declare at the deployed revision; it is not ours to report on",
            )
            continue
        values = {key: resource[key] for key in ("name", "arn") if resource.get(key)}
        try:
            violations = contract.validate_identity(
                f"{kind}.acceptance",
                None,
                values,
                account_id=config["account"],
                environment=config["tf_environment"],
            )
        except contract.OwnershipError as exc:
            raise EvidenceError(f"Ownership could not be established: {exc}") from None
        require(
            not violations,
            "Inventory contains a resource this domain does not own: "
            + "; ".join(str(v) for v in violations),
        )


# ---------------------------------------------------------------------------
# The caller's pre-teardown inventory.
# ---------------------------------------------------------------------------
RECEIPT_SCHEMA = "superplane.u1l1.pre-teardown-observation/1"


def load_inventory(config: dict) -> tuple[list[dict], str, dict]:
    """Read and validate the pre-teardown OBSERVATION RECEIPT.

    Why this is not a plain resource list any more (PR #5544 review, F2): a list of names
    carrying only an account, region and environment can be typed up after the teardown by
    copying the names the verifier itself derives, and it would satisfy coverage even if none
    of those resources had ever existed. Resources that were never created would then be
    reported as successfully cleaned up.

    So the input this accepts is a receipt produced by `record_pre_teardown`, which performs
    the same read-only lookups BEFORE teardown and writes down what it actually saw. Each
    entry must carry an `observation` of PRESENT: the check now rests on evidence that the
    resources were really there and are really gone, rather than on a claim about one half of
    that. The receipt is cross-bound to the verified deploy run, the observed identity, the
    derivation hashes and a timestamp window in `bind_receipt`, and authenticated against GitHub's
    own artifact record in `authenticate_recorder`; this function validates its shape and internal
    consistency only.
    """
    __tracebackhide__ = True
    path = Path(config["inventory_file"])
    try:
        content = path.read_bytes()
    except OSError:
        raise EvidenceError(
            "BLOCKED: the pre-teardown observation receipt could not be read"
        ) from None
    require(
        0 < len(content) <= MAX_INVENTORY_BYTES,
        "BLOCKED: the pre-teardown observation receipt is empty or exceeds the size limit",
    )
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError):
        raise EvidenceError(
            "BLOCKED: the pre-teardown observation receipt is not valid JSON"
        ) from None
    require(
        type(document) is dict,
        "BLOCKED: the pre-teardown observation receipt must be a JSON object",
    )
    require(
        document.get("schema") == RECEIPT_SCHEMA,
        f"BLOCKED: the pre-teardown input is not a {RECEIPT_SCHEMA} receipt. A "
        f"hand-written resource list cannot establish that these resources were ever "
        f"present; produce it with `record_pre_teardown` before tearing down.",
    )
    # F2's concrete case: `record_pre_teardown` stamps `offline-fixture` whenever a transport was
    # injected, and the loader used to ignore that field entirely — so a receipt the recorder
    # itself declared to be fixture output was accepted by the live path. Only a receipt the
    # recorder classified `live` can even be considered; the authentication in `bind_receipt` then
    # has to agree with it, because a label is still the document's own claim.
    require(
        document.get("evidence_kind") == "live",
        f"BLOCKED: the receipt classifies its own evidence as "
        f"{document.get('evidence_kind')!r}, not 'live'. A receipt recorded through injected "
        f"transports describes a fixture, and a fixture cannot establish that these resources "
        f"were really present.",
    )
    # A recorder that could not see a resource must not be able to launder that into evidence,
    # so the receipt states its own completeness and an incomplete one is refused outright.
    require(
        document.get("complete") is True,
        "BLOCKED: the receipt reports its own observation as incomplete, so it cannot "
        "establish pre-teardown presence",
    )
    # The receipt must say which account, region and environment it was taken in. A receipt
    # from somewhere else is a mock as far as this check is concerned.
    for key, expected in (
        ("environment", config["tf_environment"]),
        ("account", config["account"]),
        ("region", config["region"]),
    ):
        require(
            document.get(key) == expected,
            f"The receipt's {key} does not match the selected target; it cannot "
            f"describe this deployment",
        )
    resources = document.get("resources")
    require(
        type(resources) is list and bool(resources),
        "BLOCKED: the receipt lists no resources; an empty list cannot prove cleanup",
    )
    normalised = []
    for entry in resources:
        require(
            type(entry) is dict
            and type(entry.get("type")) is str
            and type(entry.get("name")) is str
            and entry["name"] != ""
            and entry["type"] in ABSENCE_TOKENS,
            "The receipt contains an entry this checker cannot observe; every entry "
            "needs a supported type and a name",
        )
        # The load-bearing field. ABSENT or INDETERMINATE before teardown means the resource
        # was not observed present, so its later absence proves nothing about cleanup.
        require(
            entry.get("observation") == PRESENT,
            f"{entry['type']} {entry['name']} was not observed PRESENT before teardown "
            f"(recorded: {entry.get('observation')!r}). A resource never seen to exist "
            f"cannot be shown to have been cleaned up.",
        )
        arn = entry.get("arn")
        require(
            arn is None or (type(arn) is str and arn != ""),
            "A receipt entry carries a malformed ARN",
        )
        # A Kubernetes object without its namespace cannot be looked up, and two objects of one
        # kind can share a name in different namespaces — so the namespace is part of the
        # identity, not decoration (PR #5544 review, F3).
        namespace, lifecycle = entry.get("namespace"), entry.get("lifecycle")
        if entry["type"] in K8S_RESOURCE_TYPE.values():
            require(
                type(namespace) is str and namespace != "",
                f"{entry['type']} {entry['name']} carries no namespace, so it cannot be "
                f"identified or observed",
            )
            require(
                lifecycle in (DELETED, RETAINED),
                f"{entry['type']} {entry['name']} records no lifecycle from U3's teardown "
                f"contract (recorded: {lifecycle!r}); whether its later absence is cleanup or "
                f"a deviation cannot be decided without it",
            )
        normalised.append(
            {
                "type": entry["type"],
                "name": entry["name"],
                **({"arn": arn} if arn else {}),
                **({"namespace": namespace} if namespace else {}),
                **({"lifecycle": lifecycle} if lifecycle else {}),
            }
        )
    return normalised, hashlib.sha256(content).hexdigest(), document


def authenticate_recorder(
    config: dict, github, receipt: dict, operations: dict
) -> dict:
    """Authenticate the recorder EXECUTION, not just the receipt's own claims (F2).

    WHY THE PREVIOUS BINDINGS WERE NOT ENOUGH

    `bind_receipt` cross-checked the receipt's fields against facts established elsewhere, which
    is necessary but not sufficient: every one of those fields is inside a local JSON file, and an
    author who knows the deploy run, attempt, revision, cluster ARN and source hashes — all of
    which this very module derives and prints — can write a document that agrees with all of them.
    `recorder_sha256` was worse than unhelpful: it was copied into the published record without
    being compared to anything, so a value like "forged-unvalidated-value" appeared in the
    evidence as if it meant something.

    WHAT AUTHENTICATES IT INSTEAD

    GitHub's artifact record for a verified recorder run. Four facts there are not the receipt
    author's to choose:

    *   the digest GitHub computed over the archive it received, which the downloaded archive must
        match — and the receipt being verified must be the document inside that archive;
    *   the run and attempt the artifact is bound to, which must be a completed dispatch-only
        execution of a registered recorder lane whose steps really ran;
    *   the creation time GitHub stamped, which must fall inside the already-closed window between
        the deploy finishing and the teardown starting. A receipt produced after the fact cannot
        have been uploaded before the teardown began;
    *   the recorder run's own revision, against which `recorder_sha256` is CHECKED rather than
        copied, so that field finally means "this code took the observation".

    NO LANE PUBLISHES IT TODAY, so this BLOCKS naming the producer required. That is the honest
    outcome: `record_pre_teardown` writes the document, but a document nobody can attest to is not
    evidence, and inventing an attestation would be exactly what the finding forbids.
    """
    __tracebackhide__ = True
    supplied = config.get("recorder_run")
    artifact = EXECUTION_ATTESTATION["recorder"][0]
    require(
        bool(RECORDER_WORKFLOWS),
        "BLOCKED: the pre-teardown receipt cannot be authenticated. Its fields — complete, "
        "observation, the deploy reference, observed_at and source_sha256 — are all contents of a "
        "local file, so on their own they establish only that somebody wrote them down. REQUIRED "
        "PRODUCER: a dispatch-only workflow that runs `record_pre_teardown` against the live "
        f"deployment and uploads its receipt as a {artifact!r} artifact; register it in "
        "RECORDER_WORKFLOWS. This check then authenticates the receipt against the digest, run, "
        "attempt and creation time GitHub itself recorded for that artifact — none of which the "
        "receipt's author controls. Until that lane exists the pre-teardown presence claim is "
        "unauthenticated, so U1-L1 acceptance 4 is unprovable, not passing.",
    )
    require(
        supplied is not None,
        "BLOCKED: a recorder run must be supplied (SUPERPLANE_LIVE_RECORDER_RUN_ID / "
        "_ATTEMPT) so the receipt can be authenticated against GitHub's own record of it",
    )
    workflow, (job_name, steps) = next(iter(RECORDER_WORKFLOWS.items()))
    run = github(f"actions/runs/{supplied['run_id']}")
    require(type(run) is dict, "BLOCKED: the recorder run metadata is unreadable")
    require(
        run.get("id") == supplied["run_id"]
        and run.get("repository", {}).get("full_name") == "aws-e/adp"
        and run.get("path") == workflow,
        "The recorder run is not the registered recording lane in this repository",
    )
    require(
        run.get("event") == DISPATCH_EVENT,
        f"The recorder run was not a deliberate {DISPATCH_EVENT}",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "The recorder run did not complete successfully",
    )
    require(
        run.get("run_attempt") == supplied["run_attempt"],
        "The recorder run's recorded attempt is not the attempt supplied",
    )
    revision = run.get("head_sha")
    require(
        type(revision) is str and re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
        "BLOCKED: the recorder run records no usable revision",
    )
    executed = verify_steps(
        config,
        github,
        "recorder",
        supplied["run_id"],
        supplied["run_attempt"],
        job_name,
        steps,
    )
    recorder_attempt = attempt_evidence(
        github, "recorder", supplied["run_id"], supplied["run_attempt"], revision
    )
    recorded = recorded_artifact(
        github, "recorder", supplied["run_id"], recorder_attempt
    )
    # Authenticated through the same channel as every other attestation: the archive's bytes must
    # hash to the digest GitHub recorded, and the receipt the verifier is reading must be exactly
    # the document inside it. That is what makes the receipt's own fields meaningful — they are no
    # longer the author's to choose.
    uploaded, _archive = read_attestation(config, "recorder", recorded)
    require(
        uploaded == receipt,
        "The pre-teardown receipt supplied is not the document the recorder run uploaded, so its "
        "observations are not the ones GitHub holds a record of",
    )
    # GitHub's timestamp, not the receipt's. A backdated `observed_at` cannot move this.
    uploaded_at = recorded["created_at"]
    require(
        uploaded_at
        >= _timestamp(
            operations["deploy"]["completed_at"], "the deploy completion time"
        ),
        "The receipt artifact was uploaded before the deploy finished, so it cannot describe "
        "what that deploy created",
    )
    require(
        uploaded_at
        <= _timestamp(operations["undeploy"]["started_at"], "the undeploy start time"),
        "The receipt artifact was uploaded after the teardown began, so it is not a pre-teardown "
        "observation regardless of the time written inside it",
    )
    # `recorder_sha256` used to be copied straight into the published evidence, so any string at
    # all appeared there as if it described the code that took the observation. It is now COMPARED:
    # against the verifier module as it existed at the recorder run's own revision, fetched from
    # GitHub. That is what makes the field mean "this observation was taken by this code".
    recorder_sources = RevisionSources(github, revision)
    verifier_path = f"{MODULE_PATH}/superplane_acceptance/{Path(__file__).name}"
    recorder_sources.read(verifier_path)
    # `RevisionSources` hashes the bytes it fetched, so this is the hash of the file as GitHub
    # served it rather than of a re-encoding of the decoded text.
    expected_hash = recorder_sources.hashes[verifier_path]
    require(
        receipt.get("recorder_sha256") == expected_hash,
        "The receipt's recorder_sha256 is not the hash of this module at the recorder run's own "
        "revision, so the code that produced the observation is not the code that was deployed "
        "and reviewed",
    )
    return {
        "workflow": workflow,
        "run_id": supplied["run_id"],
        "run_attempt": supplied["run_attempt"],
        "revision": revision,
        "artifact": recorded["name"],
        "artifact_id": recorded["artifact_id"],
        "digest": recorded["digest"],
        "uploaded_at": uploaded_at.isoformat(),
        "recorder_sha256": expected_hash,
        "executed_steps": executed,
    }


def bind_receipt(
    receipt: dict,
    config: dict,
    operations: dict,
    identity: dict,
    source_hashes: dict,
    recorder: dict,
) -> dict:
    """Bind the receipt to the verified deploy, the real identity and the deployed source.

    Shape validation alone would still accept a coherent-looking document invented after the
    fact. These are the bindings that make that impractical, because each one has to agree with
    something this run established independently:

    *   Its deploy reference must be the run AND attempt `verify_operations` verified.
    *   Its cluster and account must be the ones `verify_identity` actually read just now.
    *   Its source hashes must equal the bytes the derivation used at the deployed revision.
    *   Its observation must fall after the deploy finished and before the teardown started —
        a window that has already closed, so a receipt written now cannot land inside it.
    *   Its `recorder_sha256` must be the hash of the verifier module AT THE RECORDER RUN'S OWN
        REVISION, fetched from GitHub. Previously this field was copied into the record with no
        comparison at all, so it asserted nothing (PR #5544 review, F2).

    `recorder` is the output of `authenticate_recorder`, which is what makes the fields above
    load-bearing rather than self-reported.
    """
    __tracebackhide__ = True
    deploy, undeploy = operations["deploy"], operations["undeploy"]

    reference = receipt.get("deploy")
    require(
        type(reference) is dict,
        "BLOCKED: the receipt names no deploy, so it cannot be attributed to a deployment",
    )
    require(
        reference.get("run_id") == deploy["run_id"]
        and reference.get("run_attempt") == deploy["run_attempt"],
        "The receipt was taken for a different deploy run or attempt than the one verified",
    )
    require(
        reference.get("revision") == deploy["revision"],
        "The receipt's deploy revision differs from the verified deploy revision",
    )
    require(
        receipt.get("cluster") == identity["cluster"],
        "The receipt was taken against a different cluster than the one being read now",
    )
    require(
        receipt.get("cluster_arn") == identity["cluster_arn"],
        "The receipt's cluster identity does not match the cluster observed now",
    )
    # Ties the receipt to the same contract text the coverage check used. A receipt recorded
    # against different source than the verifier derived from is describing another deployment.
    require(
        receipt.get("source_sha256") == source_hashes,
        "The receipt was recorded from different deployed source than this check derived "
        "from, so its coverage cannot be compared",
    )

    observed_at = _timestamp(
        receipt.get("observed_at"), "the receipt's observation time"
    )
    require(
        observed_at >= _timestamp(deploy["completed_at"], "the deploy completion time"),
        "The receipt was taken before the deploy finished, so it cannot describe what that "
        "deploy created",
    )
    require(
        observed_at <= _timestamp(undeploy["started_at"], "the undeploy start time"),
        "The receipt was taken after the teardown began, so it is not a pre-teardown "
        "observation",
    )
    return {
        "observed_at": observed_at.isoformat(),
        "cluster_arn": receipt["cluster_arn"],
        # The authenticated provenance, not the receipt's own claims about itself.
        "recorder": recorder,
    }


def _identity(entry: dict) -> tuple[str, str | None, str]:
    """The tuple that identifies one resource. Namespace included: it is part of the identity."""
    return entry["type"], entry.get("namespace"), entry["name"]


def _label(entry: dict) -> str:
    """A human-readable identity for a message, namespace-qualified where one applies."""
    namespace = entry.get("namespace")
    suffix = f" in namespace {namespace}" if namespace else ""
    return f"{entry['type']} {entry['name']}{suffix}"


def prove_coverage(observed: list[dict], expected: list[dict]) -> None:
    """Every derived resource must appear in the observed inventory.

    This is what stops a trimmed or empty list from passing. Coverage is one-directional on
    purpose: the observed inventory may legitimately carry MORE than the derivation (an
    operator who recorded extra domain-owned resources), and those extras are attributed
    and checked too. What it may not do is omit something the deploy creates.
    """
    __tracebackhide__ = True
    seen = {_identity(entry) for entry in observed}
    absent_from_inventory = [
        _label(entry) for entry in expected if _identity(entry) not in seen
    ]
    require(
        not absent_from_inventory,
        "BLOCKED: the pre-teardown inventory does not cover every resource U3's sources "
        "say the deploy creates, so cleanup coverage is unproven. Missing: "
        + ", ".join(sorted(absent_from_inventory)),
    )


# ---------------------------------------------------------------------------
# Execution evidence: the deploy and undeploy really ran, in that order.
# ---------------------------------------------------------------------------
def _timestamp(value, what: str) -> datetime:
    """Parse a required timezone-aware ISO-8601 instant, or block."""
    __tracebackhide__ = True
    require(type(value) is str and bool(value), f"BLOCKED: {what} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceError(f"BLOCKED: {what} is malformed") from None
    require(parsed.tzinfo is not None, f"BLOCKED: {what} is not timezone-aware")
    return parsed


def attempt_evidence(
    github, role: str, run_id: int, attempt: int, revision: str
) -> dict:
    """GitHub's own record of ONE attempt of a run: when it ran, and on what commit.

    WHY THIS EXISTS (PR #5544 foreground review, U1-201)

    Artifact provenance has to be pinned to an attempt, because a failed first attempt and a
    successful re-run are different executions and evidence from one must not be credited to the
    other. The previous version got that binding from the artifact record's own
    `workflow_run.run_attempt` — a field the GitHub Actions artifacts API does not return. Real
    artifact metadata read from this repository carries only `id`, `repository_id`,
    `head_repository_id`, `head_branch` and `head_sha` under `workflow_run`. So every genuine
    artifact was refused as "belongs to a different run or attempt", and only a document carrying
    an invented field could pass.

    The attempt IS authoritative here, at the documented per-attempt endpoint, which is also the
    one `verify_steps` already uses for job-level proof. Reading it gives two facts the artifact
    listing cannot supply:

    *   the commit THIS attempt executed, which must be the revision under verification — a
        re-run can be dispatched against a different head, and evidence from it is not evidence
        for the revision that was deployed;
    *   the attempt's own start and end instants, which bound the interval during which it could
        have uploaded anything. `recorded_artifact` uses that interval to place an artifact on
        this attempt using GitHub's own upload timestamp, rather than a self-reported number.

    Attempts of one run are consecutive in time, so an artifact left behind by an earlier attempt
    falls before this attempt's start and is refused. That is the re-run case the missing field
    was reaching for, established from data GitHub actually stamps.
    """
    __tracebackhide__ = True
    record = github(f"actions/runs/{run_id}/attempts/{attempt}")
    require(
        type(record) is dict,
        f"BLOCKED: the {role} run's attempt {attempt} is unreadable, so its execution window "
        f"and revision cannot be established",
    )
    require(
        record.get("run_attempt") == attempt,
        f"The {role} run's attempt record is not attempt {attempt}",
    )
    require(
        record.get("id") == run_id,
        f"The {role} attempt record belongs to a different run than the one verified",
    )
    # A re-run may be dispatched against another head. The attempt that produced the evidence
    # must be the one that executed the revision being verified.
    require(
        record.get("head_sha") == revision,
        f"The {role} run's attempt {attempt} executed revision {record.get('head_sha')!r}, not "
        f"the {revision!r} under verification; a re-run against another head is a different "
        f"execution",
    )
    require(
        record.get("status") == "completed" and record.get("conclusion") == "success",
        f"The {role} run's attempt {attempt} did not itself complete successfully; a later "
        f"attempt's success cannot be credited to it",
    )
    started_at = _timestamp(
        record.get("run_started_at"), f"the {role} attempt {attempt} start time"
    )
    completed_at = _timestamp(
        record.get("updated_at"), f"the {role} attempt {attempt} completion time"
    )
    require(
        started_at <= completed_at,
        f"The {role} run's attempt {attempt} start and completion times are inconsistent",
    )
    return {
        "run_attempt": attempt,
        "revision": revision,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def verify_steps(
    config: dict,
    github,
    role: str,
    run_id: int,
    attempt: int,
    job_name: str,
    required: tuple[str, ...],
) -> list[dict]:
    """Require the lane's load-bearing steps to have really run on THIS attempt.

    This is what a run-level `conclusion` cannot tell you. The destroy lane skips its plan,
    ownership-validation and destroy steps when Terraform's state is empty and still concludes
    `success`; a `skipped` step is therefore the difference between "tore down the deployment"
    and "found nothing in state and exited cleanly". Both read as green from the outside.

    Scoped to the recorded attempt: a re-run creates a new attempt, and steps from the failed
    first attempt must not be credited to it.
    """
    __tracebackhide__ = True
    jobs = github(f"actions/runs/{run_id}/attempts/{attempt}/jobs")
    require(
        type(jobs) is dict and type(jobs.get("jobs")) is list,
        f"BLOCKED: the {role} run's per-attempt job list is unreadable",
    )
    matching = [
        job for job in jobs["jobs"] if type(job) is dict and job.get("name") == job_name
    ]
    require(
        len(matching) == 1,
        f"BLOCKED: the {role} run does not contain exactly one {job_name!r} job, so its "
        f"execution cannot be established",
    )
    job = matching[0]
    require(
        job.get("run_id") == run_id and job.get("run_attempt") == attempt,
        f"The {role} job belongs to a different run or attempt than the one verified",
    )
    require(
        job.get("status") == "completed" and job.get("conclusion") == "success",
        f"The {role} lane's {job_name!r} job did not complete successfully",
    )

    steps = job.get("steps")
    require(
        type(steps) is list and bool(steps),
        f"BLOCKED: the {role} lane reports no steps, so no operation can be shown to have "
        f"executed",
    )
    by_name = {}
    for step in steps:
        require(
            type(step) is dict, f"BLOCKED: the {role} lane has a malformed step record"
        )
        if type(step.get("name")) is str:
            by_name.setdefault(step["name"], step)

    executed = []
    for name in required:
        step = by_name.get(name)
        require(
            step is not None,
            f"BLOCKED: the {role} lane ran no step named {name!r}. Either it did not perform "
            f"the operation, or the workflow was renamed and this check can no longer prove "
            f"that it did; both must block.",
        )
        conclusion = step.get("conclusion")
        # `skipped` is the empty-state path; `success` is the only acceptable answer. Naming
        # the conclusion in the message makes the empty-state case self-diagnosing.
        require(
            conclusion == "success",
            f"The {role} lane's {name!r} step did not execute (conclusion: {conclusion!r}). "
            f"A lane that concluded successfully without performing this step — an empty "
            f"Terraform state, a dry run, an early exit — has not torn down anything.",
        )
        executed.append({"name": name, "conclusion": conclusion})
    return executed


# ---------------------------------------------------------------------------
# F1/F2: authoritative execution attestations.
#
# THE PROBLEM THE PREVIOUS VERSION HAD
#
# `verify_operations` established that a run was this repository's own dispatch-only lane, at the
# stated revision, on a real attempt, with its load-bearing steps executed. Every one of those is
# necessary. None of them says WHERE the run operated or WHAT it touched:
#
# *   `superplane-infra-apply.yml` resolves its account from the runner's own STS identity and
#     fixes `TF_VAR_environment: dev` at workflow level. Nothing about the run's METADATA records
#     which account that was.
# *   `superplane-infra-destroy.yml` takes `account_id` and `environment` as dispatch inputs, so
#     two runs of the same lane at the same revision can legitimately target different accounts
#     and different state objects.
# *   Step success says a plan was applied. It does not say which resource identities were in it.
#
# So a genuinely green run in another account, another environment or against another state key
# was pairable with observations taken in the selected account, and "the plan deleted these
# resources" was never established at all.
#
# WHAT IS REQUIRED INSTEAD, AND WHY IT IS NOT CALLER-AUTHORED
#
# An execution attestation: a document the lane itself writes from values it resolved at
# execution time (the effective STS account, the Terraform environment, the backend bucket and
# state key it initialised, the plan's own resource identities) and uploads as a workflow
# artifact.
#
# The caller cannot author one that passes, because the fields this module trusts do not come
# from the document. They come from GitHub's artifact record for that run: the artifact's
# `workflow_run` binding, its `expired` flag, GitHub's own `digest`, and GitHub's own
# `created_at`. The document's bytes must hash to that digest, so the document is pinned to what
# GitHub received from the run. Everything inside it is then cross-checked against facts this
# verification established independently — the STS account it just read, the state key derived
# from the deployed revision's backend tfvars, and the derived inventory.
#
# NO LANE PUBLISHES ONE TODAY. That is stated, not worked around: `EXECUTION_ATTESTATION` names
# the exact artifact name, publishing step and required fields per lane, and the check BLOCKS
# with that list until a lane produces it. Fabricating the evidence — or inferring the target
# from something the caller supplies — is what the finding forbids.
# ---------------------------------------------------------------------------
ATTESTATION_SCHEMA = "superplane.u1l1.execution-attestation/1"

# Per role: (artifact name, the step that must have published it, the plan action its resource
# identities must carry). The artifact name is what a producing lane must upload; the step name
# is what `verify_steps` must already have proven executed on the attested attempt.
EXECUTION_ATTESTATION = {
    "deploy": ("superplane-apply-attestation", "Terraform Apply", "create"),
    "rollout": ("superplane-rollout-attestation", "Apply", "create"),
    "undeploy": ("superplane-destroy-attestation", "Terraform Destroy", "delete"),
    "k8s_teardown": ("superplane-k8s-teardown-attestation", None, "delete"),
    "recorder": ("superplane-pre-teardown-receipt", None, None),
}

# The fields a producing lane must record, listed here so a refusal can name them rather than
# saying "an attestation is missing" and leaving the next author to guess.
ATTESTATION_FIELDS = (
    "schema",
    "run_id",
    "run_attempt",
    "revision",
    "workflow",
    "repository",
    "account",
    "environment",
    "region",
    "action",
    "resources",
)

# Terraform lanes additionally record the state object they initialised. This is what makes "the
# same environment" a verifiable claim rather than a label: the destroy lane takes `environment`
# as a dispatch input, and two values of it address two different state objects in the same
# bucket. A run that destroyed another state's contents is not this deployment's teardown.
STATE_ATTESTATION_FIELDS = ("state_bucket", "state_key")

# Kubernetes lanes record the cluster they acted on instead. A namespaced object's identity is
# only complete with the cluster: the same namespace and name exist independently in every
# cluster, so a deletion in one says nothing about the other.
CLUSTER_ATTESTATION_FIELDS = ("cluster", "cluster_arn")

# Which extra fields each role owes, by what its operation is bound to.
ATTESTATION_BINDING = {
    "deploy": STATE_ATTESTATION_FIELDS,
    "undeploy": STATE_ATTESTATION_FIELDS,
    "rollout": CLUSTER_ATTESTATION_FIELDS,
    "k8s_teardown": CLUSTER_ATTESTATION_FIELDS,
}


def backend_state_location(
    sources: RevisionSources, tf_environment: str, account: str
) -> tuple[str, str]:
    """The backend bucket and state key the lanes initialise, at the deployed revision.

    Both lanes pass `-backend-config=environments/<env>/modules/superplane-backend.tfvars`, and
    that file is where the state object's identity is written. Reading it here means the state key
    an attestation claims is compared against the deploy's own configuration rather than against a
    literal restated in this module, and reading it at the deployed revision means a later edit to
    the key cannot retroactively change what the check accepts.

    `ACCOUNT_ID` in the bucket name is the repository-wide placeholder the deploy scripts
    substitute, so it is resolved the same way here.
    """
    __tracebackhide__ = True
    path = f"environments/{tf_environment}/modules/superplane-backend.tfvars"
    try:
        source = sources.read(path)
    except EvidenceError:
        raise EvidenceError(
            f"BLOCKED: environment {tf_environment!r} has no superplane-backend.tfvars at the "
            f"deployed revision, so the state object an operation targeted cannot be identified"
        ) from None
    values = {}
    for key in ("bucket", "key"):
        match = re.search(rf'^\s*{key}\s*=\s*"([^"\n]+)"\s*$', source, re.MULTILINE)
        require(
            match is not None,
            f"BLOCKED: superplane-backend.tfvars does not define {key!r} at the deployed "
            f"revision; the state object an operation targeted cannot be identified",
        )
        values[key] = match.group(1)
    # `ACCOUNT_ID` is the repository-wide placeholder bootstrap.sh/deploy-all.sh substitute with
    # the account the operator authenticated to, so it is resolved with the account THIS check
    # read from STS. A literal placeholder reaching an attestation comparison would compare
    # against a bucket that does not exist.
    return values["bucket"].replace("ACCOUNT_ID", account), values["key"]


def recorded_artifact(github, role: str, run_id: int, attempt: dict) -> dict:
    """GitHub's own record for a role's attestation artifact on one run and attempt.

    This is the authority the whole mechanism rests on, so every field read here is GitHub's and
    none is the document's: the artifact must be unique by name within the run, must belong to
    that run, must carry the revision the attempt executed, must have been uploaded inside that
    attempt's own execution window, must not have expired (an expired artifact's bytes are gone,
    so nothing can be authenticated against it), and must carry a sha256 digest.

    HOW THE ATTEMPT IS ESTABLISHED (PR #5544 foreground review, U1-201)

    Not from `workflow_run.run_attempt`: the artifacts API does not return that field, so
    requiring it rejected every real artifact and admitted only fabricated ones. The fields the
    API does return under `workflow_run` are `id`, `repository_id`, `head_repository_id`,
    `head_branch` and `head_sha`, and this function uses exactly those plus the artifact's own
    `created_at`, `expired` and `digest`.

    The attempt binding comes from time. `attempt` is `attempt_evidence`'s output — the verified
    attempt's start and end, read from the per-attempt endpoint — and an artifact is credited to
    that attempt only if GitHub stamped its creation inside that interval. Since attempts of a run
    are consecutive, an artifact from an earlier failed attempt is created before the later
    attempt starts and is refused. A re-run therefore cannot inherit its predecessor's evidence,
    which is the guarantee the invented field was reaching for, now resting on data GitHub
    actually writes.
    """
    __tracebackhide__ = True
    name, _step, _action = EXECUTION_ATTESTATION[role]
    artifacts = pages(github, f"actions/runs/{run_id}/artifacts", "artifacts")
    matching = [
        item for item in artifacts if type(item) is dict and item.get("name") == name
    ]
    require(
        len(matching) == 1,
        f"BLOCKED: the {role} run does not publish exactly one {name!r} artifact, so its "
        f"execution target and resource identities are not established. A lane that operated on "
        f"an account, environment and state object recorded nowhere cannot be paired with "
        f"observations taken elsewhere. REQUIRED PRODUCER: the {role} lane must upload an "
        f"artifact {name!r} containing one JSON document with "
        + ", ".join(ATTESTATION_FIELDS + ATTESTATION_BINDING.get(role, ()))
        + ", each resolved at execution time rather than restated from its inputs.",
    )
    artifact = matching[0]
    require(
        artifact.get("expired") is False,
        f"BLOCKED: the {role} run's {name!r} artifact has expired, so its bytes can no longer "
        f"be authenticated against the digest GitHub recorded",
    )
    workflow_run = artifact.get("workflow_run")
    require(
        type(workflow_run) is dict,
        f"BLOCKED: the {role} attestation artifact carries no run binding, so it cannot be "
        f"attributed to the execution being verified",
    )
    require(
        workflow_run.get("id") == run_id,
        f"The {role} attestation artifact belongs to a different run than the one verified",
    )
    # The commit the artifact's run executed. A build of another branch cannot supply evidence
    # for the revision under verification, and this is the field GitHub really returns for it.
    require(
        workflow_run.get("head_sha") == attempt["revision"],
        f"The {role} attestation artifact was produced for revision "
        f"{workflow_run.get('head_sha')!r}, not the {attempt['revision']!r} under verification",
    )
    digest = artifact.get("digest")
    require(
        type(digest) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
        f"BLOCKED: the {role} attestation artifact carries no sha256 digest, so its contents "
        f"cannot be authenticated",
    )
    created_at = _timestamp(
        artifact.get("created_at"), f"the {role} attestation artifact's creation time"
    )
    # THE ATTEMPT BINDING. GitHub stamps the upload time; the attempt's window comes from the
    # per-attempt endpoint. An artifact created before this attempt began belongs to an earlier
    # attempt of the same run — the re-run case — and one created after it finished was not
    # uploaded by it at all. Neither may be credited to the attempt being verified.
    require(
        created_at >= attempt["started_at"],
        f"The {role} attestation artifact was uploaded at {created_at.isoformat()}, before "
        f"attempt {attempt['run_attempt']} began "
        f"({attempt['started_at'].isoformat()}); it is evidence from an earlier attempt of that "
        f"run, not from the execution being verified",
    )
    require(
        created_at <= attempt["completed_at"],
        f"The {role} attestation artifact was uploaded at {created_at.isoformat()}, after "
        f"attempt {attempt['run_attempt']} finished "
        f"({attempt['completed_at'].isoformat()}); an attempt cannot have published it",
    )
    return {
        "name": name,
        "digest": digest,
        "created_at": created_at,
        "size_in_bytes": artifact.get("size_in_bytes"),
        "artifact_id": artifact.get("id"),
        "run_attempt": attempt["run_attempt"],
        "head_sha": workflow_run.get("head_sha"),
    }


def read_attestation(config: dict, role: str, recorded: dict) -> tuple[dict, bytes]:
    """Read one downloaded artifact archive and authenticate it against GitHub's digest.

    The archive is read from `SUPERPLANE_LIVE_ATTESTATION_DIR`, which is the caller's filesystem —
    and that is fine, because the bytes are only accepted if they hash to the digest GitHub
    recorded for that run's artifact. A substituted or edited archive fails that comparison, so the
    caller's control over the file is control over nothing.
    """
    __tracebackhide__ = True
    archive = Path(config["attestation_dir"]) / f"{recorded['name']}.zip"
    try:
        raw = archive.read_bytes()
    except OSError:
        raise EvidenceError(
            f"BLOCKED: the {role} attestation artifact was not found at {archive.name}. "
            f"Download the artifact GitHub recorded for that run into "
            f"SUPERPLANE_LIVE_ATTESTATION_DIR; its digest is checked against GitHub's own record."
        ) from None
    require(
        0 < len(raw) <= MAX_ATTESTATION_BYTES,
        f"BLOCKED: the {role} attestation archive is empty or exceeds the size limit",
    )
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    require(
        actual == recorded["digest"],
        f"The {role} attestation archive does not match the digest GitHub recorded for that "
        f"run's artifact, so it is not the document the run produced",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            names = [item for item in bundle.namelist() if item.endswith(".json")]
            require(
                len(names) == 1,
                f"BLOCKED: the {role} attestation archive does not contain exactly one JSON "
                f"document",
            )
            content = bundle.read(names[0])
    except (zipfile.BadZipFile, OSError, KeyError):
        raise EvidenceError(
            f"BLOCKED: the {role} attestation archive could not be read"
        ) from None
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError):
        raise EvidenceError(
            f"BLOCKED: the {role} attestation is not valid JSON"
        ) from None
    require(
        type(document) is dict,
        f"BLOCKED: the {role} attestation must be a JSON object",
    )
    return document, content


def verify_attestation(
    config: dict,
    github,
    role: str,
    receipt: dict,
    identity: dict,
    state: tuple[str, str],
    expected: list[dict],
) -> dict:
    """Require an authoritative record of WHERE a lane operated and WHAT it touched (F1).

    Every comparison below is against something this verification established for itself: the
    account from the STS read taken just now, the environment and region from the selected target,
    the backend bucket and state key derived from the deployed revision's own backend tfvars, the
    run identity and revision from `verify_operations`, and the resource identities from the
    derivation. The attestation supplies the lane's side of each of those; agreement is what makes
    the run and the observation describe the same deployment.
    """
    __tracebackhide__ = True
    name, step, action = EXECUTION_ATTESTATION[role]
    binding = ATTESTATION_BINDING[role]
    # The attempt, from GitHub's per-attempt record rather than from the artifact listing (which
    # does not report it) or from the document (which would be self-reported). This establishes
    # the window the artifact must have been uploaded inside and the commit it must carry.
    attempt = attempt_evidence(
        github, role, receipt["run_id"], receipt["run_attempt"], receipt["revision"]
    )
    recorded = recorded_artifact(github, role, receipt["run_id"], attempt)
    document, _content = read_attestation(config, role, recorded)

    missing = [field for field in ATTESTATION_FIELDS + binding if field not in document]
    require(
        not missing,
        f"BLOCKED: the {role} attestation omits " + ", ".join(missing),
    )
    require(
        document["schema"] == ATTESTATION_SCHEMA,
        f"BLOCKED: the {role} attestation is not a {ATTESTATION_SCHEMA} document",
    )
    # The document's own claim about which execution produced it, checked rather than trusted.
    # `receipt`'s run, attempt and revision were established from GitHub's run and per-attempt
    # records, and the artifact holding this document was placed on that attempt by GitHub's
    # upload timestamp — so a document asserting another attempt is refused here, and one
    # asserting this attempt has already had to be uploaded inside its window to be read at all.
    require(
        document["repository"] == "aws-e/adp"
        and document["run_id"] == receipt["run_id"]
        and document["run_attempt"] == receipt["run_attempt"]
        and document["revision"] == receipt["revision"]
        and document["workflow"] == receipt["workflow"],
        f"The {role} attestation does not describe the execution that was verified",
    )
    require(
        document["run_attempt"] == attempt["run_attempt"]
        and document["revision"] == attempt["revision"],
        f"The {role} attestation's stated attempt and revision are not the ones GitHub's "
        f"per-attempt record establishes for that execution",
    )
    # The load-bearing checks. Each names the thing that would otherwise be unestablished.
    require(
        document["account"] == identity["account"],
        f"The {role} lane operated in account {document['account']!r}, not the account these "
        f"observations were taken in ({identity['account']!r}); a green run elsewhere cannot "
        f"describe this deployment's lifecycle",
    )
    require(
        document["environment"] == config["tf_environment"],
        f"The {role} lane operated on environment {document['environment']!r}, not the selected "
        f"{config['tf_environment']!r}",
    )
    require(
        document["region"] == config["region"],
        f"The {role} lane operated in region {document['region']!r}, not the selected "
        f"{config['region']!r}",
    )
    if binding is STATE_ATTESTATION_FIELDS:
        bucket, key = state
        require(
            (document["state_bucket"], document["state_key"]) == (bucket, key),
            f"The {role} lane operated on state object "
            f"{document['state_bucket']}/{document['state_key']}, not the {bucket}/{key} this "
            f"environment's backend configuration names at the deployed revision",
        )
    else:
        require(
            document["cluster"] == identity["cluster"]
            and document["cluster_arn"] == identity["cluster_arn"],
            f"The {role} lane acted on cluster {document['cluster']!r}, not the cluster these "
            f"observations are being read from ({identity['cluster']!r}); a namespaced object's "
            f"identity is only complete with its cluster",
        )
    require(
        document["action"] == action,
        f"The {role} attestation records a {document['action']!r} plan, not the {action!r} this "
        f"lane must have performed",
    )
    if step is not None:
        proven = {entry["name"] for entry in receipt["executed_steps"]}
        require(
            step in proven,
            f"BLOCKED: the {role} attestation claims a {action} but {step!r} was not among the "
            f"steps proven to have executed",
        )
    identities = _attested_identities(role, document)
    return {
        "artifact": name,
        "artifact_id": recorded["artifact_id"],
        "digest": recorded["digest"],
        "recorded_at": recorded["created_at"].isoformat(),
        "account": document["account"],
        "environment": document["environment"],
        "action": document["action"],
        **{field: document[field] for field in binding},
        "resources": sorted(
            _label({"type": kind, "namespace": namespace, "name": res})
            for kind, namespace, res in identities
        ),
        "_identities": identities,
        "_created_at": recorded["created_at"],
    }


def _attested_identities(role: str, document: dict) -> set[tuple[str, str | None, str]]:
    """The resource identities a lane's plan actually carried, normalised for comparison."""
    __tracebackhide__ = True
    resources = document["resources"]
    require(
        type(resources) is list and bool(resources),
        f"BLOCKED: the {role} attestation lists no resource identities, so step success is all "
        f"that remains and that says nothing about what the plan contained",
    )
    identities = set()
    for entry in resources:
        require(
            type(entry) is dict
            and type(entry.get("type")) is str
            and type(entry.get("name")) is str
            and entry["name"] != "",
            f"BLOCKED: the {role} attestation carries a malformed resource identity",
        )
        namespace = entry.get("namespace")
        require(
            namespace is None or (type(namespace) is str and namespace != ""),
            f"BLOCKED: the {role} attestation carries a malformed namespace",
        )
        identities.add((entry["type"], namespace, entry["name"]))
    return identities


def require_attested_coverage(role: str, attested: dict, expected: list[dict]) -> None:
    """The attested plan must cover every identity the derivation says is in scope.

    One-directional, like inventory coverage: a plan may legitimately carry more than the
    derivation (an inline policy, a resource an operator imported), and extras are not this
    check's business. What it may not do is omit something — a destroy plan that never mentioned a
    resource cannot be why that resource is absent now.
    """
    __tracebackhide__ = True
    uncovered = [
        _label(entry)
        for entry in expected
        if _identity(entry) not in attested["_identities"]
    ]
    require(
        not uncovered,
        f"BLOCKED: the {role} lane's plan did not contain every resource in scope, so its "
        f"execution cannot account for them. Missing: " + ", ".join(sorted(uncovered)),
    )


def verify_operations(config: dict, github) -> dict:
    """Require the module's own dispatch-only lanes to have really done the work.

    An unexecuted plan cannot prove cleanup, so nothing here is inferred from source. Each run
    must be the right workflow, in this repository, at the stated revision, a deliberate
    dispatch, completed successfully, on a recorded attempt whose load-bearing steps actually
    executed — and the destroy must have finished after the apply. Without the ordering, a
    destroy receipt from before the deploy would "prove" the deploy's own resources absent.

    Three lanes, not two (PR #5544 review, F3): the Terraform apply creates none of the rendered
    Kubernetes objects, so the rollout lane is the only creation evidence for that half of the
    inventory and is verified to the same standard. It is creation evidence only — teardown
    evidence is `verify_k8s_teardown`'s business, and this lane deletes nothing.

    WHERE each run operated and WHAT its plan contained are not established here: run metadata
    does not carry them. That is `verify_attestation`, called from `verify` once the identity read
    and the derivation exist to compare against.
    """
    __tracebackhide__ = True
    receipts = {}
    for role in OPERATION_ROLES:
        workflow, job_name, required_steps = LANE_EXECUTION[role]
        requested = config["runs"][role]
        run = github(f"actions/runs/{requested['run_id']}")
        require(type(run) is dict, f"BLOCKED: the {role} run metadata is unreadable")
        require(
            run.get("id") == requested["run_id"],
            f"The {role} run identity does not match the requested ID",
        )
        require(
            run.get("repository", {}).get("full_name") == "aws-e/adp",
            f"The {role} run belongs to another repository",
        )
        require(
            run.get("path") == workflow,
            f"The {role} run is not {workflow.rsplit('/', 1)[-1]}",
        )
        require(
            run.get("head_sha") == requested["sha"],
            f"The {role} run did not execute the stated revision",
        )
        # Both lanes are `workflow_dispatch`-only by design. A push build carrying this path
        # is not one of them, and an operation nobody dispatched is not an authorized one.
        require(
            run.get("event") == DISPATCH_EVENT,
            f"The {role} run was not a deliberate {DISPATCH_EVENT}; this lane is "
            f"dispatch-only, so another trigger cannot be one of its executions",
        )
        require(
            run.get("status") == "completed" and run.get("conclusion") == "success",
            f"The {role} run did not complete successfully; an unexecuted or failed "
            f"operation cannot establish cleanup",
        )
        attempt = run.get("run_attempt")
        require(
            type(attempt) is int and attempt > 0,
            f"BLOCKED: the {role} run records no attempt number, so its steps cannot be "
            f"attributed to the execution being verified",
        )
        completed_at = _timestamp(
            run.get("updated_at"), f"the {role} run's completion time"
        )
        started_at = _timestamp(
            run.get("run_started_at"), f"the {role} run's start time"
        )
        require(
            started_at <= completed_at,
            f"The {role} run's start and completion times are inconsistent",
        )
        executed = verify_steps(
            config, github, role, requested["run_id"], attempt, job_name, required_steps
        )
        receipts[role] = {
            "run_id": requested["run_id"],
            "revision": requested["sha"],
            "workflow": workflow,
            "run_url": run.get("html_url"),
            "run_attempt": attempt,
            "event": run.get("event"),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "executed_steps": executed,
            "_completed": completed_at,
            "_started": started_at,
        }

    require(
        receipts["undeploy"]["_started"] > receipts["deploy"]["_completed"],
        "The undeploy did not start after the deploy finished; this evidence cannot "
        "describe the teardown of that deployment",
    )
    # The rollout sits between them: it needs the IAM roles and SSM parameters the apply created
    # (it reads them and fails closed if they are absent), and objects it created after the
    # teardown began would not be objects that teardown removed.
    require(
        receipts["rollout"]["_started"] > receipts["deploy"]["_completed"],
        "The rollout did not start after the deploy finished; it reads the configuration the "
        "apply publishes, so it cannot have rolled out that deployment",
    )
    require(
        receipts["rollout"]["_completed"] < receipts["undeploy"]["_started"],
        "The rollout did not finish before the teardown began; objects created after a teardown "
        "started are not objects that teardown removed",
    )
    return receipts


def verify_k8s_teardown(
    observed: list[dict],
    config: dict,
    github,
    operations: dict | None = None,
    identity: dict | None = None,
    state: tuple[str, str] | None = None,
) -> dict | None:
    """Require a real deletion receipt for the Kubernetes objects U3's teardown removes.

    WHAT CHANGED AND WHY (PR #5544 review, F3)

    The previous version asked for a deletion receipt for the NAMESPACE — the one object U3's
    teardown deliberately keeps. So it blocked on the wrong object and asked for nothing about the
    nine it actually deletes. Both halves are corrected here:

    *   The DELETED set (ServiceAccount, Role, RoleBinding, ConfigMap, three NetworkPolicies,
        Service, Deployment — read from `k8s/rollback.sh` itself) needs a deletion execution. No
        workflow in this repository runs that script, so this BLOCKS naming the exact producer
        required. That is the accurate outcome, not a passing one.
    *   The RETAINED namespace needs no deletion lane at all, because U3's contract says it
        survives. It is checked against that contract in `verify_cleanup` instead of being held to
        an absence standard it was never meant to meet.

    Once a deletion lane exists and is registered, it is held to everything the Terraform lanes
    are: repository, workflow path, exact revision, dispatch event, attempt, step execution,
    ordering after the rollout, and an attestation naming the cluster, environment and the object
    identities it actually deleted.
    """
    __tracebackhide__ = True
    in_scope = k8s_entries(observed, DELETED)
    if not in_scope:
        return None
    labels = sorted(_label(entry) for entry in in_scope)

    supplied = config.get("k8s_teardown_run")
    require(
        bool(K8S_TEARDOWN_WORKFLOWS),
        "BLOCKED: the inventory covers the Kubernetes objects U3's teardown deletes ("
        + ", ".join(labels)
        + "), but no workflow in this repository deletes them: superplane-infra-destroy.yml "
        "excludes Kubernetes objects by design and superplane-k8s-deploy.yml only applies "
        "manifests. k8s/rollback.sh --teardown does delete exactly this set, but no lane invokes "
        "it, so there is no execution record to cite. REQUIRED PRODUCER: a dispatch-only workflow "
        "that runs `k8s/rollback.sh --teardown` for the selected environment and uploads a "
        f"{EXECUTION_ATTESTATION['k8s_teardown'][0]!r} artifact recording the cluster, "
        "environment, account and the object identities it deleted; register it in "
        "K8S_TEARDOWN_WORKFLOWS. Until it exists this portion of U1-L1 is genuinely unprovable, "
        "not passing. (The namespace is NOT part of this: U3 retains it by design.)",
    )
    # Reached once a real deletion lane is registered. Verified with the same step-level rules
    # as the Terraform lanes, so a dry run or a skipped delete cannot count.
    require(
        supplied is not None,
        "BLOCKED: the inventory covers the Kubernetes objects U3's teardown deletes ("
        + ", ".join(labels)
        + "), so a Kubernetes teardown run must be supplied "
        "(SUPERPLANE_LIVE_K8S_TEARDOWN_RUN_ID / _ATTEMPT). Their absence cannot be read as "
        "cleanup.",
    )
    workflow, (job_name, steps) = next(iter(K8S_TEARDOWN_WORKFLOWS.items()))
    run = github(f"actions/runs/{supplied['run_id']}")
    require(type(run) is dict, "BLOCKED: the k8s_teardown run metadata is unreadable")
    require(
        run.get("id") == supplied["run_id"]
        and run.get("repository", {}).get("full_name") == "aws-e/adp"
        and run.get("path") == workflow,
        "The k8s_teardown run is not the registered deletion lane in this repository",
    )
    require(
        run.get("event") == DISPATCH_EVENT,
        f"The k8s_teardown run was not a deliberate {DISPATCH_EVENT}",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "The k8s_teardown run did not complete successfully",
    )
    require(
        run.get("run_attempt") == supplied["run_attempt"],
        "The k8s_teardown run's recorded attempt is not the attempt supplied",
    )
    revision = run.get("head_sha")
    require(
        type(revision) is str and re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
        "BLOCKED: the k8s_teardown run records no usable revision",
    )
    started_at = _timestamp(
        run.get("run_started_at"), "the k8s_teardown run's start time"
    )
    completed_at = _timestamp(
        run.get("updated_at"), "the k8s_teardown run's completion time"
    )
    require(
        started_at <= completed_at,
        "The k8s_teardown run's start and completion times are inconsistent",
    )
    receipt = {
        "workflow": workflow,
        "run_id": supplied["run_id"],
        "run_attempt": supplied["run_attempt"],
        "revision": revision,
        "event": run.get("event"),
        "run_url": run.get("html_url"),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "deleted": labels,
        "retained": sorted(_label(entry) for entry in k8s_entries(observed, RETAINED)),
        "executed_steps": verify_steps(
            config,
            github,
            "k8s_teardown",
            supplied["run_id"],
            supplied["run_attempt"],
            job_name,
            steps,
        ),
    }
    if operations is not None:
        require(
            started_at
            > _timestamp(
                operations["rollout"]["completed_at"], "the rollout completion time"
            ),
            "The Kubernetes teardown did not start after the rollout finished, so it cannot be "
            "the teardown of the objects that rollout created",
        )
    if identity is not None and state is not None:
        attested = verify_attestation(
            config, github, "k8s_teardown", receipt, identity, state, in_scope
        )
        require_attested_coverage("k8s_teardown", attested, in_scope)
        attested.pop("_identities", None)
        attested.pop("_created_at", None)
        receipt["attestation"] = attested
    return receipt


# ---------------------------------------------------------------------------
# Identity: the account and cluster being read are the ones that were deployed to.
# ---------------------------------------------------------------------------
def verify_identity(config: dict, run) -> dict:
    """Bind the reads to the selected account/region and named cluster.

    Without this, a clean lookup in an account that never hosted Superplane would "prove"
    cleanup. The cluster binding compares the EKS API endpoint AWS reports for the named
    cluster with the server the current kubectl context actually talks to, so the namespace
    reads cannot come from some other cluster.
    """
    __tracebackhide__ = True
    status, stdout, _ = run(
        ("aws", "sts", "get-caller-identity", "--output", "json", "--no-cli-pager")
    )
    require(status == 0, "BLOCKED: the caller identity could not be read")
    identity = _json(stdout, "the caller identity")
    require(
        identity.get("Account") == config["account"],
        "The credentials in use are not for the selected account; a lookup elsewhere "
        "cannot prove this deployment's cleanup",
    )

    status, stdout, _ = run(
        (
            "aws",
            "eks",
            "describe-cluster",
            "--name",
            config["cluster"],
            "--region",
            config["region"],
            "--output",
            "json",
            "--no-cli-pager",
        )
    )
    require(
        status == 0,
        "BLOCKED: the named cluster could not be described in the selected account",
    )
    cluster = _json(stdout, "the cluster description").get("cluster")
    require(type(cluster) is dict, "BLOCKED: the cluster description is malformed")
    endpoint, cluster_arn = cluster.get("endpoint"), cluster.get("arn")
    require(
        type(endpoint) is str and endpoint.startswith("https://"),
        "BLOCKED: the cluster reports no usable API endpoint",
    )
    require(
        type(cluster_arn) is str
        and cluster_arn.split(":")[4:5] == [config["account"]]
        and cluster_arn.split(":")[3:4] == [config["region"]],
        "The named cluster is not in the selected account and region",
    )

    status, stdout, _ = run(
        ("kubectl", "config", "view", "--minify", "--output", "json")
    )
    require(status == 0, "BLOCKED: the active Kubernetes context could not be read")
    contexts = _json(stdout, "the Kubernetes context")
    servers = [
        entry.get("cluster", {}).get("server")
        for entry in contexts.get("clusters", [])
        if type(entry) is dict
    ]
    require(
        len(servers) == 1 and servers[0] is not None,
        "BLOCKED: the active Kubernetes context does not name exactly one cluster",
    )
    require(
        servers[0].rstrip("/") == endpoint.rstrip("/"),
        "The active Kubernetes context is not the named cluster; its namespace reads "
        "would describe a different cluster",
    )
    return {
        "account": identity["Account"],
        "region": config["region"],
        "cluster": config["cluster"],
        "cluster_arn": cluster_arn,
    }


# ---------------------------------------------------------------------------
# Absence, observed one resource at a time.
# ---------------------------------------------------------------------------
def _lookup(resource: dict, config: dict) -> tuple[str, ...]:
    kind, name = resource["type"], resource["name"]
    region = ("--region", config["region"])
    if kind == "aws_iam_role":
        return (
            "aws",
            "iam",
            "get-role",
            "--role-name",
            name,
            "--output",
            "json",
            "--no-cli-pager",
        )
    if kind == "aws_ssm_parameter":
        return (
            "aws",
            "ssm",
            "get-parameter",
            "--name",
            name,
            *region,
            "--output",
            "json",
            "--no-cli-pager",
        )
    if kind == "aws_ecr_repository":
        return (
            "aws",
            "ecr",
            "describe-repositories",
            "--repository-names",
            name,
            *region,
            "--output",
            "json",
            "--no-cli-pager",
        )
    if kind == "kubernetes_namespace":
        return ("kubectl", "get", "namespace", name, "--output", "name")
    # Every other rendered kind is NAMESPACED, and the namespace is part of the read: the same
    # kind and name exist independently in every namespace, so a namespace-less `kubectl get`
    # would answer about a different object — or about nothing, and report absence (F3).
    token = K8S_RESOURCE_TOKEN.get(kind)
    if token is not None:
        namespace = resource.get("namespace")
        require(
            type(namespace) is str and namespace != "",
            f"BLOCKED: {kind} {name} carries no namespace, so no read can be addressed to it",
        )
        return ("kubectl", "get", token, name, "-n", namespace, "--output", "name")
    raise EvidenceError(f"No read-only lookup defined for {kind!r}")  # pragma: no cover


def observe_absence(resource: dict, config: dict, run) -> str:
    """Classify one resource as ABSENT, PRESENT or INDETERMINATE.

    The three-way split is the point. A zero exit status means the resource answered, so it
    is still PRESENT. A nonzero status only means absent when the error names this API's own
    not-found condition; a denial, throttle, expired credential or unreachable endpoint is
    INDETERMINATE and must not be read as absence.
    """
    __tracebackhide__ = True
    status, _, stderr = run(_lookup(resource, config))
    if status == 0:
        return PRESENT
    tokens = ABSENCE_TOKENS[resource["type"]]
    # Matched against known tokens only; the diagnostic stream itself is never recorded,
    # because it can carry account identifiers and request context.
    if any(token in stderr for token in tokens):
        return ABSENT
    return INDETERMINATE


def verify_cleanup(observed: list[dict], config: dict, run) -> list[dict]:
    """Observe every inventory entry and judge it against the lifecycle U3 specifies.

    Most entries are held to the absence criterion: the deploy created them, an authorized
    teardown removed them, and anything other than ABSENT fails visibly.

    The RETAINED entries are not (PR #5544 review, F3). `k8s/rollback.sh --teardown` states that it
    deliberately does not delete the namespace, because doing so would take the out-of-band
    `skypilot-api-db` Secret with it. Demanding its absence would be inventing a deletion
    requirement U3 does not have — exactly what the issue forbids — so its expected observation is
    PRESENT, and it is its ABSENCE that is the deviation worth reporting: something deleted an
    object the contract says survives, and the Secret may have gone with it. Either way the outcome
    is recorded against the contract rather than quietly dropped from the report.
    """
    __tracebackhide__ = True
    results = []
    still_present, undetermined, retention_deviation = [], [], []
    for resource in observed:
        outcome = observe_absence(resource, config, run)
        retained = resource.get("lifecycle") == RETAINED
        results.append(
            {
                "type": resource["type"],
                "name": resource["name"],
                **(
                    {"namespace": resource["namespace"]}
                    if resource.get("namespace")
                    else {}
                ),
                **(
                    {"lifecycle": resource["lifecycle"]}
                    if resource.get("lifecycle")
                    else {}
                ),
                "expected": PRESENT if retained else ABSENT,
                "observation": outcome,
            }
        )
        if outcome == INDETERMINATE:
            undetermined.append(_label(resource))
        elif retained:
            if outcome == ABSENT:
                retention_deviation.append(_label(resource))
        elif outcome == PRESENT:
            still_present.append(_label(resource))
    require(
        not undetermined,
        "BLOCKED: these resources could not be observed either way, and an unanswered "
        "query cannot prove absence: " + ", ".join(sorted(undetermined)),
    )
    require(
        not still_present,
        "Teardown did not remove every resource the deploy created; still present: "
        + ", ".join(sorted(still_present)),
    )
    require(
        not retention_deviation,
        "U3's teardown contract retains these objects, but they are absent — something outside "
        "the documented teardown deleted them, and for the namespace that also removes the "
        "out-of-band 'skypilot-api-db' Secret the contract exists to protect: "
        + ", ".join(sorted(retention_deviation)),
    )
    return results


# ---------------------------------------------------------------------------
# The check.
# ---------------------------------------------------------------------------
def verify(config: dict, github=None, run=None) -> dict:
    """Run the whole observation and return the evidence record.

    Injected `github`/`run` transports mark the record `offline-fixture`, which `run_live`
    refuses to publish. That is what keeps a fixture from producing a live pass.
    """
    __tracebackhide__ = True
    selected = TARGETS.get(config.get("environment"))
    require(
        selected is not None and all(config.get(k) == v for k, v in selected.items()),
        "Selected target differs from the reviewed environment registry",
    )
    live = github is None and run is None
    github = GitHub() if github is None else github
    run = ReadOnlyCommands() if run is None else run

    started = datetime.now(timezone.utc).isoformat()
    # Operations first: the deploy revision is only trustworthy as a derivation source once
    # the run that carried it has been shown to be a real, completed execution of this
    # module's own lane.
    operations = verify_operations(config, github)
    sources = RevisionSources(github, operations["deploy"]["revision"])
    expected = derived_inventory(config, sources)
    observed, inventory_sha256, receipt = load_inventory(config)
    prove_coverage(observed, expected)
    attribute(observed, config, expected)
    # Identity before the attestations: they are compared against the account and cluster THIS
    # verification read, not against anything the documents assert about themselves (F1).
    identity = verify_identity(config, run)
    state = backend_state_location(
        sources, config["tf_environment"], identity["account"]
    )

    # WHERE each lane operated and WHAT its plan contained. The Terraform lanes account for the
    # AWS half of the inventory and the rollout for the Kubernetes half, so each is required to
    # cover its own scope and nothing else: an apply plan does not contain Kubernetes objects and
    # a rollout does not contain IAM roles, and demanding otherwise would be unsatisfiable.
    aws_scope = [entry for entry in expected if entry["type"].startswith("aws_")]
    k8s_scope = k8s_entries(expected)
    attestations = {}
    for role, scope in (
        ("deploy", aws_scope),
        ("rollout", k8s_scope),
        ("undeploy", aws_scope),
    ):
        attested = verify_attestation(
            config, github, role, operations[role], identity, state, scope
        )
        require_attested_coverage(role, attested, scope)
        attested.pop("_identities", None)
        attested.pop("_created_at", None)
        attestations[role] = attested

    k8s_teardown = verify_k8s_teardown(
        observed, config, github, operations, identity, state
    )
    recorder = authenticate_recorder(config, github, receipt, operations)
    provenance = bind_receipt(
        receipt, config, operations, identity, dict(sources.hashes), recorder
    )
    results = verify_cleanup(observed, config, run)
    for role, receipt_entry in operations.items():
        receipt_entry.pop("_completed", None)
        receipt_entry.pop("_started", None)
        receipt_entry["attestation"] = attestations[role]

    return {
        "schema_version": 1,
        "criterion": "U1-L1 / R1 acceptance 4 post-undeploy resource absence",
        "evidence_kind": "live" if live else "offline-fixture",
        "status": "observed" if live else "matched",
        "started_at": started,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "environment": config["environment"],
            "tf_environment": config["tf_environment"],
            **selected,
        },
        "identity": identity,
        "operations": operations,
        **({"kubernetes_teardown": k8s_teardown} if k8s_teardown else {}),
        "state_object": {"bucket": state[0], "key": state[1]},
        "inventory": {
            "derived_at_revision": operations["deploy"]["revision"],
            "derived_from_sha256": dict(sources.hashes),
            "derived_count": len(expected),
            "observed_count": len(observed),
            "observed_sha256": inventory_sha256,
            "aws_count": len(aws_scope),
            "kubernetes_deleted": sorted(
                _label(entry) for entry in k8s_entries(expected, DELETED)
            ),
            # Recorded so a reader can see WHICH objects were excluded from the absence criterion
            # and on whose authority, rather than finding them silently missing from the report.
            "kubernetes_retained": sorted(
                _label(entry) for entry in k8s_entries(expected, RETAINED)
            ),
            "retention_authority": f"{MODULE_PATH}/k8s/rollback.sh --teardown, read at the "
            f"deployed revision: the objects it deletes are held to the absence criterion and "
            f"the ones it states it deliberately keeps are not",
            "coverage": "every resource derived at the deployed revision was observed "
            "present before teardown, and every one U3's teardown deletes was observed absent "
            "after it; retained objects were judged against U3's retention contract instead",
        },
        "pre_teardown_observation": provenance,
        "resources": results,
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "u1_acceptance": "incomplete",
        "not_established": [
            "browser gating or enabled Superplane behaviour",
            "unset deployment configuration defaults",
            "feature API contract (a separate merged check)",
            "SkyPilot GPU clusters or database contents, which this module never created",
            "cleanup of Secrets Manager secrets the deploy takes by reference only",
            # Named here because the report must not read as if the namespace had been cleaned up.
            (
                "deletion of the retained namespace, which U3's teardown deliberately keeps "
                "so the out-of-band 'skypilot-api-db' Secret survives; this check does not "
                "require its absence and does not claim it"
            ),
        ],
    }


def record_pre_teardown(environment, github=None, run=None) -> dict:
    """BEFORE teardown: observe the deployed resources and write the presence receipt.

    This is the other half of the F2 repair. The verifier needs evidence that the resources
    existed, and the only thing that can produce that is a real read taken while they still
    did — so this runs the SAME read-only lookups and the SAME derivation, records what it
    actually saw, and writes it where the post-teardown check will consume it.

    It is as observational as the verifier: same allowlisted commands, same three-way
    classification, and it refuses to write a receipt unless every derived resource was seen
    PRESENT. A resource it could not observe is recorded as such and makes the receipt
    incomplete, which `load_inventory` then refuses — an unobserved resource must not become
    silent evidence either way.

    Inputs are the deploy identity only; no teardown has happened yet, so no undeploy run or
    inventory file is required.
    """
    __tracebackhide__ = True
    missing = [name for name in RECORDER_INPUTS if not environment.get(name)]
    require(
        not missing,
        "BLOCKED: missing explicit pre-teardown recording inputs: "
        + ", ".join(missing),
    )
    target = environment["SUPERPLANE_LIVE_ENVIRONMENT"]
    require(target in TARGETS, "BLOCKED: target is not in the reviewed registry")
    tf_environment = environment["SUPERPLANE_LIVE_TF_ENVIRONMENT"]
    require(
        ownership().VALID_ENVIRONMENT.match(tf_environment) is not None,
        "BLOCKED: SUPERPLANE_LIVE_TF_ENVIRONMENT is not a valid environment token",
    )
    sha = environment["SUPERPLANE_LIVE_DEPLOY_SHA"]
    require(
        re.fullmatch(r"[0-9a-f]{40}", sha) is not None,
        "BLOCKED: the deploy revision must be an exact 40-hex commit",
    )
    run_id = environment["SUPERPLANE_LIVE_DEPLOY_RUN_ID"]
    require(
        re.fullmatch(r"[1-9][0-9]*", run_id) is not None,
        "BLOCKED: the deploy run ID must be a positive GitHub run ID",
    )
    output = Path(environment["SUPERPLANE_LIVE_INVENTORY_FILE"])
    require(
        output.is_absolute() and output.parent.is_dir() and not os.path.lexists(output),
        "BLOCKED: the receipt needs an absolute new filename in an existing directory",
    )

    config = {
        "environment": target,
        "tf_environment": tf_environment,
        "cluster": environment["SUPERPLANE_LIVE_CLUSTER"],
        **TARGETS[target],
    }
    live = github is None and run is None
    github = GitHub() if github is None else github
    run = ReadOnlyCommands() if run is None else run

    # The deploy must be a verified execution before its revision is used as a derivation
    # source, for the same reason as in `verify`.
    config["runs"] = {
        "deploy": {"run_id": int(run_id), "sha": sha},
        # The recorder only needs the deploy; reuse the deploy receipt for both slots so the
        # shared verifier can run, then keep only the deploy half.
        "undeploy": {"run_id": int(run_id), "sha": sha},
    }
    deploy = _verify_deploy_only(config, github)
    sources = RevisionSources(github, sha)
    expected = derived_inventory(config, sources)
    attribute(expected, config, expected)
    identity = verify_identity(config, run)
    # The recorder must read EXACTLY the source set the verifier derives from, because
    # `bind_receipt` compares the two hash maps for equality. The backend configuration is part
    # of that set (the verifier reads it to establish where the Terraform lanes kept state), so
    # it is read here too and recorded — otherwise an honest receipt would be refused for
    # covering less source than the verifier read, which is a difference in reads rather than
    # in evidence.
    state_bucket, state_key = backend_state_location(
        sources, tf_environment, identity["account"]
    )

    resources, unobserved = [], []
    for resource in expected:
        outcome = observe_absence(resource, config, run)
        resources.append({**resource, "observation": outcome})
        if outcome != PRESENT:
            unobserved.append(f"{resource['type']} {resource['name']} ({outcome})")

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "evidence_kind": "live" if live else "offline-fixture",
        "complete": not unobserved,
        "environment": tf_environment,
        "account": config["account"],
        "region": config["region"],
        "cluster": identity["cluster"],
        "cluster_arn": identity["cluster_arn"],
        "deploy": {
            "run_id": deploy["run_id"],
            "run_attempt": deploy["run_attempt"],
            "revision": deploy["revision"],
            "run_url": deploy["run_url"],
        },
        # Recorded for the reader's benefit: WHERE the deployment this receipt describes kept its
        # Terraform state. It is not evidence by itself — `verify_attestation` compares the lanes'
        # attested state object against the same derivation, not against this field.
        "state_object": {"bucket": state_bucket, "key": state_key},
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": dict(sources.hashes),
        "resources": resources,
        "recorder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    require(
        not unobserved,
        "BLOCKED: these resources were not observed PRESENT before teardown, so their later "
        "absence could not prove cleanup: " + ", ".join(sorted(unobserved)),
    )
    _publish(receipt, output, ".u1-pre-teardown-")
    return receipt


def _verify_deploy_only(config: dict, github) -> dict:
    """Verify just the deploy lane, for the recorder (no teardown exists yet)."""
    __tracebackhide__ = True
    workflow, job_name, steps = LANE_EXECUTION["deploy"]
    requested = config["runs"]["deploy"]
    run = github(f"actions/runs/{requested['run_id']}")
    require(type(run) is dict, "BLOCKED: the deploy run metadata is unreadable")
    require(
        run.get("id") == requested["run_id"]
        and run.get("repository", {}).get("full_name") == "aws-e/adp"
        and run.get("path") == workflow
        and run.get("head_sha") == requested["sha"],
        "The deploy run is not this module's apply lane at the stated revision",
    )
    require(
        run.get("event") == DISPATCH_EVENT,
        f"The deploy run was not a deliberate {DISPATCH_EVENT}",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "The deploy run did not complete successfully",
    )
    attempt = run.get("run_attempt")
    require(
        type(attempt) is int and attempt > 0,
        "BLOCKED: the deploy run records no attempt number",
    )
    verify_steps(
        config, github, "deploy", requested["run_id"], attempt, job_name, steps
    )
    return {
        "run_id": requested["run_id"],
        "run_attempt": attempt,
        "revision": requested["sha"],
        "run_url": run.get("html_url"),
    }


def _publish(document: dict, output: Path, prefix: str) -> None:
    """Write a record atomically to a NEW file; an existing path is refused."""
    __tracebackhide__ = True
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=output.parent, prefix=prefix, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(document, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # An exclusive link refuses an existing file or symlink, including on a race.
        os.link(temporary, output)
    except OSError:
        raise EvidenceError("Evidence could not be published to a new file") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_live(environment) -> dict:
    """The live entry point: real transports only, published atomically to a new file."""
    __tracebackhide__ = True
    config = settings(environment)
    report = verify(config)
    require(
        report["evidence_kind"] == "live",
        "Fixture evidence cannot be published as live",
    )
    _publish(report, Path(config["evidence_file"]), ".u1-teardown-")
    return report
