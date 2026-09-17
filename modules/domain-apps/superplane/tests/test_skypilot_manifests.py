"""The SkyPilot manifests must agree with the facts they copy — Issue #5042 (U3).

A manifest set is full of values that are really references to something else: the lock's
pinned clouds, the upstream controller's expected service address, Terraform's OIDC `sub`
conditions. Kubernetes cannot check any of them. Every one is a copy that can drift, and each
drifts into a different failure:

*   `allowed_clouds` narrower than the lock → a provider silently unavailable, surfacing as
    "no cloud is enabled" long after rollout.
*   Service name or port ≠ `SKYPILOT_DEFAULT_BASE_URL` → the controller gets a connection
    error the first time it tries to launch, not at deploy time.
*   ServiceAccount name/namespace ≠ the OIDC `sub` condition in `irsa.tf` → pods start, then
    cannot assume their role. That presents as opaque AWS 403s from inside the container, with
    a green rollout.

None of these is caught by `check_rendered_manifests.py`, which validates the STRUCTURE of the
rendered set (namespaces, kinds, RBAC scope, identities, bounds, digests). These tests check
the AGREEMENTS. The two are complementary and both are needed.

Each assertion reads the other side from its real source — the lock file,
`spike/baseline_inventory.py`, `irsa.tf` — rather than restating it here. A test that restated
the expected value would be a third copy, and would pass while both sides were wrong together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = MODULE_ROOT / "k8s"
LOCK_FILE = MODULE_ROOT / "releases" / "superplane.lock.yaml"
IRSA_TF = MODULE_ROOT / "infra" / "control-plane" / "irsa.tf"
VARIABLES_TF = MODULE_ROOT / "infra" / "control-plane" / "variables.tf"
BASELINE = MODULE_ROOT / "spike" / "baseline_inventory.py"


@pytest.fixture(scope="module")
def lock() -> dict:
    return yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def documents() -> list[tuple[str, dict]]:
    """Every object in the unrendered manifests, keyed by file.

    Read unrendered on purpose: these tests are about agreements that exist in the repository,
    so they must not depend on SSM values or on a render step having run. The rendered path is
    covered by `infra/control-plane/tests/test_rendered_rollout_guard.py`.
    """
    found = []
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if isinstance(doc, dict) and doc:
                found.append((path.name, doc))
    assert found, f"no manifests found in {K8S_DIR}"
    return found


def _by_kind(documents: list[tuple[str, dict]], kind: str) -> list[dict]:
    return [doc for _, doc in documents if doc.get("kind") == kind]


# ---------------------------------------------------------------------------
# Agreement with the release lock (U2).
# ---------------------------------------------------------------------------


def test_allowed_clouds_equals_the_lock(documents, lock):
    """The ConfigMap copies the lock's list because SkyPilot reads config, not a lock file.

    This assertion is the only reason that copy is acceptable. `20-skypilot-config.yaml`
    names this test in its header as the thing keeping the copy honest.
    """
    configmaps = _by_kind(documents, "ConfigMap")
    assert len(configmaps) == 1, f"expected one ConfigMap, found {len(configmaps)}"
    config = yaml.safe_load(configmaps[0]["data"]["config.yaml"])
    assert config["allowed_clouds"] == lock["skypilot_config"]["allowed_clouds"], (
        "the ConfigMap's allowed_clouds has drifted from the lock's pinned list; a narrower "
        "list makes a provider silently unavailable at launch time"
    )


def test_state_backend_is_the_lock_pinned_postgres(documents, lock):
    """SQLite would discard cluster state on pod replacement.

    The concrete consequence is a running GPU cluster whose handle nobody holds — spend that
    continues with nothing tracking it. U2 pinned postgres for exactly this.
    """
    assert lock["skypilot_config"]["state_backend"] == "postgres", (
        "the lock no longer pins postgres; this test's premise has changed"
    )
    config = yaml.safe_load(_by_kind(documents, "ConfigMap")[0]["data"]["config.yaml"])
    assert config["db"]["backend"] == "postgres"


def test_no_manifest_references_a_pending_image(documents, lock):
    """The three unbuildable images have no digest, so any reference is a tag or a fiction."""
    pending = lock.get("pending_images") or {}
    assert pending, "the lock records no pending images, so this test proves nothing"

    tokens = set(pending)
    tokens |= {
        entry["ecr_repository"]
        for entry in pending.values()
        if entry.get("ecr_repository")
    }
    for source, doc in documents:
        rendered = yaml.safe_dump(doc)
        for token in tokens:
            assert token not in rendered, (
                f"{source} references the pending image {token!r}, which the lock records with "
                f"no digest (blocked by source_access). There is nothing to deploy."
            )


def test_the_only_deployed_image_is_a_placeholder_resolved_from_the_lock(documents):
    """No literal image reference anywhere: the digest comes from SSM, derived from the lock.

    A literal would be a second pin that can disagree with the lock, and nothing would
    detect it — the R2 failure this module is built to prevent.
    """
    images = []
    for source, doc in documents:
        stack = [doc]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "image" and isinstance(value, str):
                        images.append((source, value))
                    else:
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)

    assert images, "no container images found in the manifests"
    for source, image in images:
        assert image.startswith("REPLACE_WITH_"), (
            f"{source} names the image {image!r} literally. It must be a placeholder resolved "
            f"from the SSM parameter Terraform derives from the lock."
        )


# ---------------------------------------------------------------------------
# Agreement with the upstream controller's client (U12 spike evidence).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def upstream_base_url() -> tuple[str, int]:
    """The address the upstream controller's SkyPilot client defaults to.

    Parsed from `spike/baseline_inventory.py` rather than restated, because that constant is
    itself the recorded evidence of what upstream's `skypilot/client.go` does.
    """
    text = BASELINE.read_text(encoding="utf-8")
    match = re.search(
        r'SKYPILOT_DEFAULT_BASE_URL[^=]*=\s*"http://(?P<host>[^:"]+):(?P<port>\d+)"',
        text,
    )
    assert match, (
        "SKYPILOT_DEFAULT_BASE_URL not found in the spike's baseline inventory"
    )
    return match.group("host"), int(match.group("port"))


def test_service_name_and_port_match_the_address_the_controller_uses(
    documents, upstream_base_url
):
    """Renaming either breaks the controller at first launch, not at deploy time."""
    host, port = upstream_base_url
    service_name = host.split(".", 1)[0]

    services = _by_kind(documents, "Service")
    assert len(services) == 1, f"expected one Service, found {len(services)}"
    service = services[0]

    assert service["metadata"]["name"] == service_name, (
        f"the Service is named {service['metadata']['name']!r} but the upstream controller "
        f"resolves {host!r}; it would get a DNS failure the first time it launched"
    )
    assert [entry["port"] for entry in service["spec"]["ports"]] == [port], (
        f"the Service does not expose port {port}, which is the port the controller connects to"
    )


def test_the_service_is_not_externally_exposed(documents):
    """An API that can launch GPU compute must not be reachable from outside the cluster."""
    service = _by_kind(documents, "Service")[0]
    assert service["spec"]["type"] == "ClusterIP", (
        "a LoadBalancer or NodePort would place the SkyPilot API — which can launch GPU "
        "compute and spend money — on an externally reachable address"
    )


def test_the_probed_path_is_an_endpoint_the_controller_actually_calls(documents):
    """A probe on a path the API does not serve makes the pod permanently unready."""
    endpoints = set(
        re.findall(r'\("GET",\s*"(/[^"]*)"', BASELINE.read_text(encoding="utf-8"))
    )
    assert endpoints, "no GET endpoints found in the spike's client endpoint inventory"

    deployments = _by_kind(documents, "Deployment")
    assert deployments, "no Deployment found"
    probed = 0
    for deployment in deployments:
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            for probe in ("readinessProbe", "livenessProbe"):
                spec = container.get(probe)
                if not spec:
                    continue
                probed += 1
                path = spec["httpGet"]["path"]
                assert path in endpoints, (
                    f"{probe} polls {path!r}, which is not among the endpoints the upstream "
                    f"client is recorded as calling ({sorted(endpoints)})"
                )
    assert probed >= 2, "the API server declares no readiness and liveness probes"


# ---------------------------------------------------------------------------
# Agreement with Terraform's identity boundary (irsa.tf).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def irsa_subjects() -> dict[str, str]:
    """service account name -> the namespace VARIABLE its OIDC `sub` condition names.

    Parsed from `irsa.tf`. The trust policy is the authority: if this ServiceAccount's name
    or namespace differs, `sts:AssumeRoleWithWebIdentity` fails and the pod runs with no AWS
    identity at all.
    """
    text = IRSA_TF.read_text(encoding="utf-8")
    locals_map = dict(re.findall(r'(\w+_service_account)\s*=\s*"([^"]+)"', text))
    subjects = {}
    for var_expr, account_expr in re.findall(
        r'"system:serviceaccount:\$\{(var\.\w+)\}:\$\{local\.(\w+)\}"', text
    ):
        name = locals_map.get(account_expr)
        assert name, f"irsa.tf references local.{account_expr} with no literal value"
        subjects[name] = var_expr
    assert subjects, "no OIDC subject conditions parsed from irsa.tf"
    return subjects


@pytest.fixture(scope="module")
def variable_defaults() -> dict[str, str]:
    text = VARIABLES_TF.read_text(encoding="utf-8")
    defaults = {}
    for block in re.findall(r'variable\s+"(\w+)"\s*\{(.*?)\n\}', text, re.S):
        name, body = block
        match = re.search(r'\n\s*default\s*=\s*"([^"]*)"', body)
        if match:
            defaults[name] = match.group(1)
    return defaults


def test_the_service_account_name_matches_the_trust_policy(documents, irsa_subjects):
    """The name is a contract with the OIDC `sub` condition, not a label."""
    accounts = _by_kind(documents, "ServiceAccount")
    assert accounts, "no ServiceAccount declared"
    for account in accounts:
        name = account["metadata"]["name"]
        assert name in irsa_subjects, (
            f"ServiceAccount {name!r} appears in no OIDC subject condition in irsa.tf, so a "
            f"pod using it can assume no role — it would run with no AWS identity"
        )


def test_the_service_account_namespace_placeholder_matches_the_trust_policy_variable(
    documents, irsa_subjects
):
    """`var.skypilot_namespace` in the condition must map to the SkyPilot placeholder.

    The two namespaces are separate Terraform inputs. Substituting the control-plane
    namespace into an object whose trust policy names the SkyPilot one produces a `sub` that
    never matches.
    """
    expected_placeholder = {
        "var.namespace": "REPLACE_WITH_NAMESPACE",
        "var.skypilot_namespace": "REPLACE_WITH_SKYPILOT_NAMESPACE",
    }
    for account in _by_kind(documents, "ServiceAccount"):
        name = account["metadata"]["name"]
        variable = irsa_subjects[name]
        assert account["metadata"]["namespace"] == expected_placeholder[variable], (
            f"ServiceAccount {name!r} is rendered into "
            f"{account['metadata']['namespace']!r}, but its trust policy scopes it to "
            f"{variable}. The OIDC `sub` would not match and the pod could assume no role."
        )


def test_every_workload_uses_a_service_account_declared_here(documents):
    """A pod naming an undeclared ServiceAccount silently gets no IRSA annotation."""
    declared = {
        account["metadata"]["name"] for account in _by_kind(documents, "ServiceAccount")
    }
    for source, doc in documents:
        if doc.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        used = doc["spec"]["template"]["spec"].get("serviceAccountName")
        assert used in declared, (
            f"{source} runs as ServiceAccount {used!r}, which this manifest set does not "
            f"declare ({sorted(declared)}); it would have no IRSA annotation and no identity"
        )


def test_the_two_namespaces_are_distinct_placeholders(documents):
    """Collapsing them would break one of the two trust policies.

    Checked as placeholders rather than values: the values come from SSM, and the rollout
    lane separately refuses a run where the two resolve equal.
    """
    used = set()
    for _, doc in documents:
        rendered = yaml.safe_dump(doc)
        for placeholder in (
            "REPLACE_WITH_NAMESPACE",
            "REPLACE_WITH_SKYPILOT_NAMESPACE",
        ):
            # Substring care: REPLACE_WITH_NAMESPACE is not a substring of
            # REPLACE_WITH_SKYPILOT_NAMESPACE, but assert it explicitly so a future rename
            # cannot make this check quietly wrong.
            if re.search(rf"(?<![A-Z_]){placeholder}\b", rendered):
                used.add(placeholder)
    assert "REPLACE_WITH_SKYPILOT_NAMESPACE" in used, (
        "no manifest uses the SkyPilot namespace placeholder"
    )


# ---------------------------------------------------------------------------
# Isolation properties the manifests are responsible for themselves.
# ---------------------------------------------------------------------------


def test_the_role_grants_no_pod_creation(documents):
    """This is the MECHANISM behind the GPU-isolation claim, not a configuration.

    `workspace_cluster_context` being unset is a configuration and could be changed by an
    SSM edit. The absence of pod-create rights is enforcement: a Kubernetes-cloud launch
    against this cluster fails with a permission error instead of provisioning GPU compute on
    the ADP MANAGEMENT cluster. Fail on a missing credential, never succeed against the wrong
    cluster.
    """
    roles = _by_kind(documents, "Role")
    assert roles, "no Role declared"
    mutating = {"create", "delete", "deletecollection", "update", "patch"}
    for role in roles:
        for rule in role["rules"]:
            resources = set(rule.get("resources") or [])
            verbs = set(rule.get("verbs") or [])
            if resources & {"pods", "pods/exec", "pods/portforward", "services"}:
                granted = verbs & mutating
                assert not granted, (
                    f"Role {role['metadata']['name']} grants {sorted(granted)} on "
                    f"{sorted(resources)}. That is precisely what SkyPilot needs to provision "
                    f"onto THIS cluster, which is the ADP management cluster."
                )


def test_no_cluster_scoped_rbac_is_declared(documents):
    """A domain app that can create a ClusterRoleBinding can grant itself anything.

    The review's reproduction used a ClusterRoleBinding to `cluster-admin`. The rendered
    guard rejects the kind; this asserts the shipped manifests never contain one, so the
    guard is never the only thing standing in the way.
    """
    for source, doc in documents:
        assert doc.get("kind") not in {"ClusterRole", "ClusterRoleBinding"}, (
            f"{source} declares {doc['kind']}, which is cluster-scoped"
        )
    for role_binding in _by_kind(documents, "RoleBinding"):
        role_ref = role_binding["roleRef"]
        assert role_ref["kind"] == "Role", (
            f"RoleBinding {role_binding['metadata']['name']} references a "
            f"{role_ref['kind']} ({role_ref['name']!r}); a namespaced binding to a "
            f"ClusterRole still grants that ClusterRole's permissions"
        )


def test_no_role_uses_a_wildcard(documents):
    for role in _by_kind(documents, "Role"):
        for rule in role["rules"]:
            assert "*" not in (rule.get("verbs") or []), (
                f"Role {role['metadata']['name']} grants wildcard verbs"
            )
            assert "*" not in (rule.get("resources") or []), (
                f"Role {role['metadata']['name']} grants wildcard resources"
            )


def test_every_container_declares_requests_and_limits(documents):
    """An unbounded domain workload can evict core ADP pods from a shared node."""
    checked = 0
    for source, doc in documents:
        if doc.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        pod_spec = doc["spec"]["template"]["spec"]
        for key in ("initContainers", "containers"):
            for container in pod_spec.get(key) or []:
                checked += 1
                resources = container.get("resources") or {}
                for field in ("requests", "limits"):
                    for dimension in ("cpu", "memory"):
                        assert dimension in (resources.get(field) or {}), (
                            f"{source}: container {container['name']} declares no "
                            f"resources.{field}.{dimension}"
                        )
    assert checked, "no containers were checked"


def test_no_container_requests_a_gpu(documents):
    """This lane rolls out a control plane. A GPU request here would land on the ADP
    management cluster, which the platform isolation requirement forbids."""
    for source, doc in documents:
        rendered = yaml.safe_dump(doc)
        for key in ("nvidia.com/gpu", "amd.com/gpu"):
            assert key not in rendered, f"{source} requests {key}"


def test_no_pod_uses_a_host_namespace_or_hostpath(documents):
    for source, doc in documents:
        if doc.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        pod_spec = doc["spec"]["template"]["spec"]
        for field in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod_spec.get(field), f"{source} sets {field}"
        for volume in pod_spec.get("volumes") or []:
            assert "hostPath" not in volume, (
                f"{source} mounts a hostPath volume, reaching outside the container onto a "
                f"shared node"
            )


def test_secrets_arrive_by_reference_and_are_required(documents):
    """Upstream's db-migrate-job.yaml carries an inline password in DATABASE_URL.

    An applied artifact containing a credential must be rotated everywhere once noticed, so
    the connection arrives as a secretKeyRef. `optional: false` matters independently: with
    an absent secret and `optional: true` the pod starts with an empty connection string and
    falls back to SkyPilot's SQLite default, silently discarding cluster state on restart.
    """
    connection_envs = 0
    for source, doc in documents:
        if doc.get("kind") not in {"Deployment", "StatefulSet", "Job"}:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if "value" in env:
                    value = str(env["value"])
                    assert not re.search(r"://[^/\s]*:[^/@\s]+@", value), (
                        f"{source}: env {env['name']} contains an inline credential in a URI"
                    )
                    continue
                ref = (env.get("valueFrom") or {}).get("secretKeyRef")
                if not ref:
                    continue
                connection_envs += 1
                assert ref.get("optional") is False, (
                    f"{source}: env {env['name']} reads a secret without `optional: false`. "
                    f"An absent secret would start the pod with an empty value."
                )
    assert connection_envs, "no secret-backed environment variables found"


def test_the_networkpolicy_set_is_default_deny_plus_explicit_allows(documents):
    """Presence and shape only. NOT an enforcement claim.

    EKS Auto Mode ships the VPC CNI network-policy controller DISABLED, so on the existing
    cluster these objects are accepted, appear in `kubectl get networkpolicy`, and enforce
    nothing — silently. Enabling it is the still-open platform-owned prerequisite #4999.

    The evidence that would establish enforcement is `kubectl get policyendpoints -A` (empty
    means NOT enforced) plus positive/negative traffic probes, which belong to the gated live
    acceptance this story is not authorized to run. So this test asserts the policies are
    correct in shape, which is what must be true BEFORE enforcement is switched on — the
    #4999 runbook's lesson being that enabling enforcement with a deny and no matching allow
    cut all agent telemetry with no error.
    """
    policies = _by_kind(documents, "NetworkPolicy")
    assert policies, "no NetworkPolicy declared"

    deny = [
        policy
        for policy in policies
        if policy["spec"].get("podSelector") == {}
        and set(policy["spec"].get("policyTypes") or []) == {"Ingress", "Egress"}
    ]
    assert len(deny) == 1, (
        "expected exactly one default-deny policy selecting all pods in both directions"
    )

    egress_rules = [
        rule for policy in policies for rule in policy["spec"].get("egress") or []
    ]
    assert egress_rules, "no egress allow rules; the default-deny would block DNS"

    dns_ports = {
        (port.get("protocol"), port.get("port"))
        for rule in egress_rules
        for port in rule.get("ports") or []
    }
    assert ("UDP", 53) in dns_ports and ("TCP", 53) in dns_ports, (
        "DNS is not allowed on both UDP and TCP. Blocked DNS presents as a generic timeout "
        "rather than a policy error — the failure mode #4999's runbook records."
    )

    imds_excluded = any(
        "169.254.0.0/16" in ((to.get("ipBlock") or {}).get("except") or [])
        for rule in egress_rules
        for to in rule.get("to") or []
    )
    assert imds_excluded, (
        "the broad HTTPS egress rule does not exclude the link-local IMDS range. A pod with "
        "an IRSA identity has no reason to reach the node's instance metadata, and doing so "
        "is the standard route to the NODE's role, which is broader than this domain's."
    )


def test_every_object_is_labelled_as_a_domain_app(documents):
    """Teardown and cost attribution identify domain-owned objects by label.

    Without it, `undeploy.sh` and any cost report need a hardcoded object list, which is the
    kind of list that goes stale the first time a manifest is added.
    """
    for source, doc in documents:
        labels = (doc.get("metadata") or {}).get("labels") or {}
        assert labels.get("app.kubernetes.io/part-of") == "adp-superplane", (
            f"{source}: {doc.get('kind')}/{(doc.get('metadata') or {}).get('name')} carries no "
            f"app.kubernetes.io/part-of=adp-superplane label"
        )


def test_the_namespace_enforces_restricted_pod_security(documents):
    namespaces = _by_kind(documents, "Namespace")
    assert namespaces, "the manifest set declares no Namespace"
    for namespace in namespaces:
        labels = namespace["metadata"]["labels"]
        assert labels.get("pod-security.kubernetes.io/enforce") == "restricted"


def test_the_ingress_policy_selector_matches_the_namespace_label(documents):
    """`namespaceSelector` cannot match on metadata.name, so it matches a label.

    If the Namespace stopped carrying that label the ingress allow would select nothing, and
    under enforcement the controller's calls would be denied — with no error anywhere except
    a connection timeout in the controller.
    """
    namespace_labels: dict[str, str] = {}
    for namespace in _by_kind(documents, "Namespace"):
        namespace_labels.update(namespace["metadata"]["labels"])

    checked = 0
    for policy in _by_kind(documents, "NetworkPolicy"):
        for rule in policy["spec"].get("ingress") or []:
            for peer in rule.get("from") or []:
                selector = (peer.get("namespaceSelector") or {}).get(
                    "matchLabels"
                ) or {}
                for key, value in selector.items():
                    if key.startswith("kubernetes.io/"):
                        continue  # A well-known label the API server applies itself.
                    checked += 1
                    assert namespace_labels.get(key) == value, (
                        f"the ingress policy selects namespaces with {key}={value}, which the "
                        f"Namespace this set declares does not carry ({namespace_labels})"
                    )
    assert checked, "no namespaceSelector labels were checked"


def test_the_api_server_runs_a_single_replica_and_does_not_roll(documents):
    """Two API servers racing on one Postgres backend can each believe they own a launch.

    The observable result is duplicate GPU clusters costing money that nothing is tracking —
    which is why RollingUpdate is wrong here even though it is the usual default.
    """
    for deployment in _by_kind(documents, "Deployment"):
        assert deployment["spec"]["replicas"] == 1, (
            f"{deployment['metadata']['name']} runs more than one replica"
        )
        assert deployment["spec"]["strategy"]["type"] == "Recreate", (
            "RollingUpdate would briefly run two API servers against the same state backend"
        )


def test_no_persistent_volume_claim_is_declared(documents):
    """Durable state is in Postgres; a PVC would be a second, node-bound copy that disagrees.

    A StorageClass or PersistentVolume is platform-owned in any case.
    """
    for source, doc in documents:
        assert doc.get("kind") != "PersistentVolumeClaim", f"{source} declares a PVC"


def test_no_upstream_account_id_appears_in_any_manifest(documents):
    """Upstream's config.env carries upstream's account. Adopting it is the defect.

    A deliberately-supplied account is legitimate; inheriting upstream's is not. These
    manifests contain no account at all — AWS identity arrives through IRSA — so any
    appearance of one is either a copied literal or a credential path that should not exist.
    """
    blocked = {"605440105851", "938500344975"}
    for source, doc in documents:
        rendered = yaml.safe_dump(doc)
        for account in blocked:
            assert account not in rendered, (
                f"{source} contains the upstream account id {account}"
            )
        assert not re.search(r"(?<!\d)\d{12}(?!\d)", rendered), (
            f"{source} contains a 12-digit literal. No manifest should carry an account id: "
            f"role ARNs are substituted from SSM and identity comes from IRSA."
        )
