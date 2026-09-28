"""The controller's Kubernetes permissions stay scoped to calls it actually makes.

Work package A19 (#5684) of the 2026-09-21 AWS security scan; parent #5677, daily epic
#5599. The shipped ClusterRole in
``src/superplane-controller/deploy/controller.yaml`` granted ``secrets: ["list"]``
cluster-wide — read access to every password, API token, TLS private key and cloud
credential any team stored in that cluster — to populate one heartbeat field.

## Why a test file, and why it asserts on the manifest rather than on prose

Nothing failed when that grant was added, and nothing would fail if it came back. An
RBAC rule has no compiler and no runtime check: Kubernetes accepts an over-broad rule
silently (it just works, too well) and accepts an over-narrow one just as silently —
the affected call fails later, at the moment it is needed. For this controller that
moment is a node drain, so a permissions mistake surfaces as capacity that will not
release rather than as anything a reviewer sees.

That asymmetry is the whole reason these tests exist, and it is why they are written in
both directions:

  * **Too broad** — the cluster-wide Secret read must not return, and no rule may use a
    wildcard. These are the properties the security work established.
  * **Too narrow / wrong** — the grants the controller genuinely needs must remain, and
    each must carry a justification. A file trimmed until it looks minimal but no longer
    authorises eviction would pass a naive "no broad grants" check while breaking drain.

## The eviction rule, which was broken on arrival

``pods/eviction`` was granted under ``apiGroups: ["policy"]``. RBAC matches a
subresource against the API group in the **request path** —
``POST /api/v1/namespaces/{ns}/pods/{name}/eviction``, the core group — not against the
``policy/v1`` apiVersion that the Eviction *body* carries. Upstream's own bootstrap
policy uses the core group for this in both ``editRules()`` and ``NodeRules()``, as do
cluster-autoscaler and Karpenter.

A mismatched group is not a validation error. The rule loads cleanly and simply never
matches, so the eviction POST is refused 403 at the moment a drain runs — bypassing the
PodDisruptionBudgets that ``RETIREMENT.md`` documents drain as respecting. That is
pinned here because it is invisible everywhere else: no lint, no schema and no unit test
reads an apiGroup, and the failure needs a live cluster and a real drain to appear.

## What `resourceNames` cannot do, asserted rather than assumed

The issue asks for minimal grants *without* "pretending resourceNames can filter
unsupported list patterns", and that caveat is load-bearing. ``resourceNames``
authorises a ``list``/``watch`` **only** when the client sends a matching
``metadata.name`` field selector; without one, ``requestInfo.Name`` is empty, no
configured name equals it, and the rule does not match at all. It also never works for
``deletecollection`` or top-level ``create``.

So adding ``resourceNames`` to this controller's Pod or SuperplaneNode reads would not
tighten them — it would stop authorising them, silently disabling the pod watcher and
the consolidator. Those loops search for objects they cannot name in advance: an
unschedulable pod is found precisely by *not* knowing which pod it is, and auto-repair
creates records with ``generateName``, so the name does not exist until after the call
that would need authorising for it. ``test_broad_reads_are_not_filtered_by_resource_names``
asserts the absence with that reasoning attached, so a later reviewer reading "why is
this cluster-wide" finds the answer instead of narrowing it into an outage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = MODULE_ROOT / "src" / "superplane-controller"
MANIFEST = CONTROLLER / "deploy" / "controller.yaml"

# Resources whose cluster-wide read is a credential-exfiltration surface. `secrets` is
# the finding; `serviceaccounts/token` is listed because minting tokens is the same class
# of escalation by a different route, and a future edit reaching for it should have to
# argue the case here rather than inherit this file's silence.
CREDENTIAL_RESOURCES = ("secrets", "serviceaccounts/token")

# Grants the controller's code demonstrably needs, each with the call that proves it.
# A trim that breaks one of these fails here rather than during a live drain.
#
# (apiGroup, resource, verb) -> the behaviour requiring it
REQUIRED_GRANTS = {
    ("", "pods", "list"): (
        "pod_watcher reconciles Pods; consolidator lists pods per node"
    ),
    ("", "pods", "watch"): (
        "the cached client backs every read with an informer ListWatch"
    ),
    ("", "pods", "patch"): (
        "pod_watcher.go:380,393 annotates first-seen/provisioning-triggered"
    ),
    ("", "pods/eviction", "create"): (
        "consolidator.go:736 drains via SubResource('eviction').Create, which is what "
        "makes the API server enforce PodDisruptionBudgets"
    ),
    ("", "nodes", "get"): "health_monitor.go:67 reads a Node's Ready condition",
    ("", "nodes", "update"): (
        "consolidator.go:689 cordons spec.unschedulable before draining"
    ),
    ("", "nodes", "delete"): (
        "consolidator.go:777 removes the Node after confirmed teardown"
    ),
    ("coordination.k8s.io", "leases", "get"): "leader election LeaseLock.Get",
    ("coordination.k8s.io", "leases", "create"): "leader election LeaseLock.Create",
    ("coordination.k8s.io", "leases", "update"): (
        "leader election LeaseLock.Update renew"
    ),
    ("superplane.ai", "nodepools", "list"): (
        "pod_watcher.go:256 matches a pool to a pod"
    ),
    ("superplane.ai", "nodepools/status", "update"): (
        "nodepool_reconciler.go:48 writes phase"
    ),
    ("superplane.ai", "superplanenodes", "list"): (
        "consolidator.go:347 assesses capacity"
    ),
    ("superplane.ai", "superplanenodes", "create"): (
        "health_monitor.go:367 auto-repair replacement; pod_watcher.go:351 new capacity"
    ),
    ("superplane.ai", "superplanenodes", "update"): (
        "provisioner.go:583 writes back spec.cloud/spec.region after a cloud fallback"
    ),
    ("superplane.ai", "superplanenodes/status", "update"): "phase/message/cost writes",
}

# Verbs removed by A19 because no call in the tree performs them. Re-adding one without
# a call is the regression this guards; adding the call first is a deliberate change that
# updates this map.
REMOVED_GRANTS = {
    ("", "configmaps"): (
        "leader election in controller-runtime v0.20.1 defaults to LeasesResourceLock "
        "(pkg/leaderelection/leader_election.go); the ConfigMap lock was the pre-v0.12 "
        "default and nothing here selects it. No controller in this tree reads or writes "
        "a ConfigMap through the API — the Deployment's configMap mounts are resolved by "
        "the kubelet, not by this ServiceAccount"
    ),
    ("superplane.ai", "superplanenodes", "delete"): (
        "nothing deletes a SuperplaneNode. A failed teardown must RETAIN the record and "
        "its status.skypilotCluster (RETIREMENT.md) — that cluster name is the only handle "
        "on a possibly-live GPU cluster, so clearing it is a human decision"
    ),
    ("superplane.ai", "nodepools", "delete"): (
        "pools are declared by an administrator; this controller only reports on them"
    ),
    ("superplane.ai", "nodepools", "create"): "the controller never declares a pool",
}


def _documents() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def _cluster_role() -> dict:
    roles = [d for d in _documents() if d.get("kind") == "ClusterRole"]
    assert len(roles) == 1, (
        f"expected exactly one ClusterRole in {MANIFEST.name}, found {len(roles)}. "
        "A second role would split the permission surface across rules this suite "
        "checks one at a time."
    )
    return roles[0]


def _rules() -> list[dict]:
    rules = _cluster_role().get("rules") or []
    assert rules, "the ClusterRole has no rules — this suite would vacuously pass"
    return rules


def _granted() -> set[tuple[str, str, str]]:
    """Every (apiGroup, resource, verb) triple the ClusterRole authorises."""
    granted = set()
    for rule in _rules():
        for group in rule.get("apiGroups", []):
            for resource in rule.get("resources", []):
                for verb in rule.get("verbs", []):
                    granted.add((group, resource, verb))
    return granted


# --- Too broad -------------------------------------------------------------------


@pytest.mark.parametrize("resource", CREDENTIAL_RESOURCES)
def test_no_cluster_wide_credential_read(resource: str) -> None:
    """The A19 finding itself: no path to every Secret in the cluster.

    Asserted against parsed rule *values*, not raw text, because the manifest documents
    this removal at length and names `secrets` while doing so. A substring scan would fail
    on the explanation it is meant to protect, and the cheapest way to pass would be
    deleting the reasoning — leaving a file that no longer says why the grant is absent.
    """
    offenders = [
        (group, verb)
        for (group, res, verb) in _granted()
        if res == resource or res == "*"
    ]
    assert not offenders, (
        f"the controller ClusterRole grants {resource!r} ({offenders}). A19 (#5684) "
        f"removed cluster-wide credential access: it existed only to serve "
        f"checkVaultSyncStatus, which returned the constant 'synced' on every branch "
        f"including the failure branch, and whose value the receiver "
        f"(cluster_health.go) classified as uninterpretable anyway.\n\n"
        f"If a real credential check is genuinely needed, it reads ExternalSecret CR "
        f"status conditions — grant `externalsecrets` on `external-secrets.io`, not "
        f"cluster-wide Secret reads. Update this test deliberately if that changes."
    )


def test_no_wildcard_grants() -> None:
    """A wildcard re-opens every finding this package closed, in one character."""
    for rule in _rules():
        for field in ("apiGroups", "resources", "verbs"):
            values = rule.get(field, [])
            assert "*" not in values, (
                f"rule {rule!r} uses a wildcard in {field}. A19 requires each grant be "
                f"justified by controller behaviour; '*' asserts the opposite."
            )


def test_no_secret_grant_survives_anywhere_in_the_role() -> None:
    """Belt-and-braces on the rule shape, not only on the triples.

    `_granted()` expands the cross-product, so a rule listing `secrets` beside another
    resource is already caught. This checks the raw resource lists too, so the finding
    cannot return disguised as a multi-resource core rule.
    """
    for rule in _rules():
        resources = [str(r) for r in rule.get("resources", [])]
        assert "secrets" not in resources, (
            f"a rule still lists `secrets` among its resources: {rule!r}"
        )


# --- Too narrow / wrong ----------------------------------------------------------


@pytest.mark.parametrize(
    ("grant", "justification"),
    sorted(REQUIRED_GRANTS.items()),
    ids=lambda v: f"{v[0] or 'core'}/{v[1]}:{v[2]}" if isinstance(v, tuple) else "",
)
def test_required_grants_survive_scoping(
    grant: tuple[str, str, str], justification: str
) -> None:
    """Least privilege must not become too little privilege.

    Each of these is exercised by a named call. Trimming one produces a role that looks
    tighter and breaks a controller loop at runtime — which for eviction and cordon means
    capacity that cannot be released.
    """
    group, resource, verb = grant
    assert grant in _granted(), (
        f"the ClusterRole no longer grants {verb!r} on "
        f"{resource!r} in apiGroup {group or '(core)'!r}, but the controller needs it: "
        f"{justification}.\n\nRemoving it does not fail any Go test — it fails against a "
        f"live API server, during the operation that needs it."
    )


def test_eviction_is_granted_under_the_core_group_not_policy() -> None:
    """The live defect A19 found: an apiGroup that never matches.

    RBAC authorises a subresource against the request path's group
    (`/api/v1/.../pods/{name}/eviction` — core), not the `policy/v1` apiVersion in the
    Eviction body. `apiGroups: ["policy"]` loads without error and never matches, so
    every drain is refused 403 at the moment it runs, bypassing the PodDisruptionBudgets
    drain exists to respect.
    """
    granted = _granted()
    assert ("", "pods/eviction", "create") in granted, (
        'pods/eviction must be granted under apiGroups: [""] (core). Upstream\'s '
        "bootstrappolicy uses the core group for it in both editRules() and NodeRules(), "
        "as do cluster-autoscaler and Karpenter."
    )
    assert ("policy", "pods/eviction", "create") not in granted, (
        'pods/eviction is granted under apiGroups: ["policy"]. That rule can never '
        "match: RBAC compares the request path's API group (core, via /api/v1/...), not "
        "the policy/v1 apiVersion of the Eviction body. The result is a silent 403 on "
        "every node drain — PDBs are not consulted because the call never gets that far."
    )


@pytest.mark.parametrize(
    ("grant", "reason"), sorted(REMOVED_GRANTS.items(), key=lambda kv: str(kv[0]))
)
def test_removed_grants_stay_removed(grant: tuple, reason: str) -> None:
    """Re-adding one of these re-widens the role past what any call needs."""
    granted = _granted()
    if len(grant) == 3:
        offenders = [grant] if grant in granted else []
    else:
        group, resource = grant
        offenders = [t for t in granted if t[0] == group and t[1] == resource]
    assert not offenders, (
        f"{offenders} was removed by A19 (#5684) and is back. Reason it was removed: "
        f"{reason}.\n\nIf the controller now genuinely performs this operation, that is a "
        f"scope change: add the call, then update this test in the same commit so the "
        f"grant and its justification stay together."
    )


def test_broad_reads_are_not_filtered_by_resource_names() -> None:
    """`resourceNames` on a list/watch rule is an outage, not a tightening.

    This is the acceptance item that warns against "pretending resourceNames can filter
    unsupported list patterns". A rule carrying `resourceNames` authorises a list or watch
    ONLY if the client sends a matching `metadata.name` field selector; otherwise
    `requestInfo.Name` is empty, no configured name equals it, and the rule does not match.

    This controller's reads are the opposite pattern by design — it looks for pods it
    cannot name in advance, and auto-repair creates records via `generateName`, so the
    name does not exist until after the call. So the correct scoping here is the absence
    of `resourceNames`, and that absence is asserted rather than left to look like an
    oversight.
    """
    for rule in _rules():
        names = rule.get("resourceNames")
        if not names:
            continue
        unsupported = sorted(
            set(rule.get("verbs", [])) & {"list", "watch", "deletecollection"}
        )
        assert not unsupported, (
            f"rule {rule!r} pairs resourceNames={names} with {unsupported}. "
            f"resourceNames does not authorise an unfiltered list/watch (the client must "
            f"send a matching metadata.name field selector) and never applies to "
            f"deletecollection. This does not narrow the grant — it stops the call being "
            f"authorised at all, silently disabling the loop that makes it."
        )


def test_status_writes_do_not_carry_object_write_verbs() -> None:
    """A `/status` grant is for Status().Update, and must not smuggle in more.

    `create`/`delete` on a status subresource authorises nothing the controller does, and
    `patch` is not used because Status().Update issues a PUT. Keeping this tight means a
    status grant cannot become a general write path to the object.
    """
    for group, resource, verb in sorted(_granted()):
        if not resource.endswith("/status"):
            continue
        assert verb in {"get", "update"}, (
            f"{group or '(core)'}/{resource} grants {verb!r}. Status writes in this "
            f"controller are Status().Update (a PUT); nothing patches, creates or deletes "
            f"a status subresource."
        )


# --- The grant and its justification stay together -------------------------------


def test_every_rule_carries_a_justifying_comment() -> None:
    """The property whose absence produced this finding.

    The cluster-wide Secret grant shipped under the comment "Secrets: list for vault sync
    status check (heartbeat)" — which named a caller but not the fact that the caller
    ignored the result. An unannotated rule is indistinguishable from an inherited one, so
    the next reviewer cannot tell a deliberate grant from a leftover without re-deriving
    the whole file, which is exactly what did not happen before.

    Checked structurally: every `- apiGroups:` line that opens a rule must be preceded by
    a comment line within the ClusterRole block.
    """
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    try:
        start = next(
            i for i, ln in enumerate(lines) if ln.strip() == "kind: ClusterRole"
        )
    except StopIteration:  # pragma: no cover - guarded by _cluster_role()
        pytest.fail("no ClusterRole document found")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == "---"),
        len(lines),
    )

    unannotated = []
    for i in range(start, end):
        if not lines[i].strip().startswith("- apiGroups:"):
            continue
        preceding = [ln.strip() for ln in lines[start:i] if ln.strip()]
        if not preceding or not preceding[-1].startswith("#"):
            unannotated.append((i + 1, lines[i].strip()))

    assert not unannotated, (
        "these ClusterRole rules have no comment naming the controller behaviour that "
        f"needs them: {unannotated}. A19 (#5684) requires each grant be justified by "
        "controller behaviour; an unexplained rule is how the cluster-wide Secret read "
        "survived review."
    )
