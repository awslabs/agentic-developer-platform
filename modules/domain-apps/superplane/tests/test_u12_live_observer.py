"""Offline regressions for U12's read-only baseline observer.

Issue #5289. Two jobs, mirroring ``test_u12_live_baseline.py``.

First, prove the observer reads and maps correctly: it speaks the maintained
SkyPilot and Kubernetes contracts, refuses anything outside its allow-listed
reads, correlates a machine across its records, and reports what it actually saw
-- including honest refutations and honest indeterminates for the checks a
steady-state read cannot settle.

Second, and load-bearing: prove that driving the real observer class through
offline transports still yields **no** live evidence. The fixtures here are the
exact response shapes pinned by the maintained Go client's own tests, so the
mapping is regressed against the real wire contract without an environment --
and :meth:`transport_is_live` reports False throughout, so nothing here is
publishable.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from spike.parity_matrix import Dimension, dimension_by_name
from superplane_acceptance import live_baseline as lb
from superplane_acceptance import live_observer as lo
from superplane_acceptance import operation_receipts as receipts_module
from superplane_acceptance.cli_delivery import EvidenceError

ENVIRONMENT = "baseline/selected"
REVISION = "abc1234def5678901234567890abcdef12345678"
SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567"
PROVIDER = "nebius"
REGION = "eu-north1"
ACCOUNT = "111122223333"
CLUSTER = "workspace-eks"
CONTROLLER = "superplane-controller"
RUNTIME = "0.12.0"
SKY_CLUSTER = "sky-baseline-1"
NODE = "sky-node-1"
INSTANCE = "mi-0baseline"


def cluster_arn(
    name: str = CLUSTER, region: str = REGION, account: str = ACCOUNT
) -> str:
    """The canonical identity a context is compared against, whole."""
    return f"arn:aws:eks:{region}:{account}:cluster/{name}"


def api_endpoint(name: str = CLUSTER) -> str:
    return f"https://{name}.eks.amazonaws.com"


TARGET_METADATA = {
    "provider": PROVIDER,
    "region": REGION,
    "workspace_cluster": CLUSTER,
    # Account, region and name together. A bare name admitted the lookalike the
    # second review reproduced, so the registered target carries the full ARN and
    # the address the capture expects to be talking to.
    "workspace_cluster_arn": cluster_arn(),
    "workspace_api_endpoint": api_endpoint(),
    "skypilot_api": "http://skypilot-api.skypilot.svc.cluster.local:46580",
    "skypilot_runtime_version": RUNTIME,
    "controller": CONTROLLER,
}


# ---------------------------------------------------------------------------
# Fixtures in the maintained wire shapes.
#
# Field names follow src/superplane-controller/skypilot/types.go and the
# SuperplaneNode CRD, not a guess: `cluster_names`, a bare array from /status,
# `enabled_clouds`, and status.k8sNodeName / skypilotCluster / ssmInstanceId /
# hourlyCost.
# ---------------------------------------------------------------------------


def sky_status(**overrides) -> list[dict]:
    entry = {
        "name": SKY_CLUSTER,
        "status": "UP",
        "autostop": 120,
        "to_down": False,
        "last_use": "sky launch",
        "launched_at": 1758240000,
        "handle": {
            "cluster_name": SKY_CLUSTER,
            "head_ip": "10.0.0.4",
            "num_node": 1,
            "launched_resources": {
                "cloud": "Nebius",
                "instance_type": "gpu-h100-sxm",
                "region": "eu-north1",
                "zone": "eu-north1-a",
                "accelerators": {"H100": 1},
            },
        },
    }
    entry.update(overrides)
    return [entry]


ENABLED_CLOUDS = {
    "enabled_clouds": [
        {"name": "Nebius", "enabled": True},
        {"name": "AWS", "enabled": False},
    ]
}


def superplane_node(*, owner: str = CONTROLLER, **status_overrides) -> dict:
    status = {
        "phase": "Ready",
        "k8sNodeName": NODE,
        "skypilotCluster": SKY_CLUSTER,
        "ssmInstanceId": INSTANCE,
        "hourlyCost": 2.95,
        "provisionedAt": "2026-09-19T09:00:00Z",
    }
    status.update(status_overrides)
    metadata: dict = {"name": "node-1", "namespace": "superplane"}
    if owner:
        # Ownership as the cluster reports it. The observer reads the controller
        # from here, not from the selection -- copying the expectation in and then
        # comparing it back was the defect, so an unowned record must stay unowned.
        metadata["ownerReferences"] = [
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": owner}
        ]
    return {
        "apiVersion": "superplane.ai/v1",
        "kind": "SuperplaneNode",
        "metadata": metadata,
        "spec": {"cloud": PROVIDER, "gpuType": "H100", "gpuCount": 1},
        "status": status,
    }


def k8s_node(ready: str = "True", gpu: str = "1") -> dict:
    return {
        "kind": "Node",
        "metadata": {
            "name": NODE,
            "labels": {"node.kubernetes.io/instance-type": "h100"},
        },
        "status": {
            "conditions": [
                {"type": "MemoryPressure", "status": "False"},
                {"type": "Ready", "status": ready},
            ],
            "allocatable": {"cpu": "32", "memory": "128Gi", "nvidia.com/gpu": gpu},
        },
    }


def gpu_pod(phase: str = "Running", node_name: str = NODE) -> dict:
    return {
        "kind": "Pod",
        "metadata": {"name": "train-job-1", "namespace": "workloads"},
        "spec": {
            "nodeName": node_name,
            "containers": [
                {"name": "train", "resources": {"limits": {"nvidia.com/gpu": "1"}}}
            ],
        },
        "status": {"phase": phase},
    }


def kube_context(name: str = CLUSTER, *, arn: str = "", server: str = "") -> dict:
    """A kubeconfig view. ``arn`` and ``server`` override for the hostile cases."""
    return {
        "clusters": [
            {
                "name": arn or cluster_arn(name),
                "cluster": {"server": server or api_endpoint(name)},
            }
        ]
    }


def listing(*items) -> dict:
    return {"apiVersion": "v1", "kind": "List", "items": list(items)}


class FakeSkyPilot:
    """An offline stand-in for the SkyPilot read transport.

    Structurally identical to :class:`live_observer.SkyPilotReads`, which is the
    point: driving the real observer through it must still produce fixture
    evidence.
    """

    def __init__(self, **responses) -> None:
        self.responses = {
            "/api/health": {"status": "healthy", "version": "0.12.0"},
            "/status": sky_status(),
            "/enabled_clouds": ENABLED_CLOUDS,
        }
        self.responses.update(responses)
        self.calls: list[str] = []

    def __call__(self, selector: str) -> bytes:
        self.calls.append(selector)
        if selector not in self.responses:
            raise EvidenceError(f"BLOCKED: SkyPilot read {selector} failed")
        return json.dumps(self.responses[selector]).encode()


class FakeKubectl:
    """An offline stand-in for the kubectl read transport."""

    def __init__(self, **responses) -> None:
        self.responses = {
            "context": kube_context(),
            "nodes": listing(k8s_node()),
            "superplane-nodes": listing(superplane_node()),
            "nodepools": listing(
                {"kind": "NodePool", "metadata": {"name": "h100-pool"}}
            ),
            "pods": listing(gpu_pod()),
            "api-state": listing(
                {"kind": "Deployment", "metadata": {"name": "skypilot-api"}}
            ),
            "controllers": listing(
                {"kind": "Deployment", "metadata": {"name": CONTROLLER}}
            ),
        }
        self.responses.update(responses)
        self.calls: list[str] = []

    def __call__(self, selector: str) -> bytes:
        self.calls.append(selector)
        if selector not in self.responses:
            raise EvidenceError(f"BLOCKED: kubectl read {selector} failed")
        return json.dumps(self.responses[selector]).encode()


@pytest.fixture(autouse=True)
def registered_target(monkeypatch):
    monkeypatch.setitem(lb.BASELINE_TARGETS, ENVIRONMENT, dict(TARGET_METADATA))


# ---------------------------------------------------------------------------
# Retained evidence of the authorized operation.
#
# These are files an operation left behind, not something this test suite
# performs. Writing them here is how the ingestion path is regressed without an
# environment: the observer reads them exactly as it would read an operator's,
# recomputing every digest and re-checking every window and identity claim.
#
# The shape matters and is the repair the foreground review asked for. Everything
# a verdict depends on lives INSIDE the body that gets hashed and attested; the
# submission file beside it is only a pointer. ``retained`` therefore builds the
# body and derives the submission from it, so a test cannot accidentally
# reintroduce the hand-written-facts shape that drove a verdict past a
# byte-identical digest.
# ---------------------------------------------------------------------------

PRODUCER_LANE = "superplane-baseline-lane"
RUN = "run-baseline-1"


class FakeProducer:
    """An independent lane's record of the digests it published.

    Stands in for :class:`lo.WorkflowRunReads`. It answers only from what it was
    told it published, so a test cannot make it vouch for a body by editing that
    body -- which is the property under test.
    """

    def __init__(self) -> None:
        self.published: dict[tuple[str, int], str] = {}
        self.asked: list[tuple[str, int]] = []

    def attest(self, digest: str, *, run: str = RUN, attempt: int = 1) -> None:
        self.published[(run, attempt)] = digest

    def published_digest(self, *, run: str, attempt: int, revision: str) -> str:
        self.asked.append((run, attempt, revision))
        return self.published.get((run, attempt), "")


def manifest_digest(directory) -> str:
    """What an operation's whole retained evidence set hashes to.

    Recomputed here from the files on disk rather than imported, so the fixture
    asserts the shape independently of the implementation. A producer vouches for
    the set, not for each body, so that adding or withholding a record is itself a
    change to the attested value.
    """
    bodies = []
    for submission in sorted(directory.glob("*.json")):
        pointer = json.loads(submission.read_text())
        body = directory / str(pointer.get("body", ""))
        if body.is_file():
            # Keyed on the BODY name, because this value must be derivable by the
            # lane that publishes the bodies -- it never saw the submission files.
            bodies.append((body.name, hashlib.sha256(body.read_bytes()).hexdigest()))
    lines = "\n".join(f"{name}:{digest}" for name, digest in sorted(bodies))
    return hashlib.sha256(lines.encode()).hexdigest()


# The producer the current test's evidence is vouched for by. A module global
# rather than a parameter on every call site because *authenticated* is the
# ordinary case -- the interesting tests are the ones that opt out of it, and
# those say so explicitly with ``attest=False``.
_PRODUCER: FakeProducer | None = None


@pytest.fixture(autouse=True)
def evidence_producer(monkeypatch):
    """Register one reviewed producer lane for the duration of a test.

    Registration is per-test and undone afterwards: the shipped registry is empty
    by default because pointing it at a lane is an authorization decision, and
    :func:`test_shipping_the_producer_client_does_not_register_a_lane` regresses
    that the default stays empty.
    """
    global _PRODUCER
    producer = FakeProducer()
    monkeypatch.setitem(receipts_module.EVIDENCE_PRODUCERS, PRODUCER_LANE, producer)
    _PRODUCER = producer
    yield producer
    _PRODUCER = None


def retained(
    directory,
    *,
    check_id: str = "",
    authority: str,
    facts: dict | None = None,
    services: list | None = None,
    resource: dict | None = None,
    observed_at: datetime | None = None,
    name: str = "",
    producer: str = PRODUCER_LANE,
    run: str = RUN,
    attempt: int = 1,
    body_overrides: dict | None = None,
    attest: bool = True,
    **overrides,
) -> str:
    """Write one evidence body plus its pointer; return the body's sha256.

    The digest is returned because that is what an independent producer has to
    vouch for. ``attest=True`` records it with the test's registered producer, as
    an authorized operation's own lane would; ``attest=False`` writes the same
    bytes with nobody vouching for them, which is how the unauthenticated path is
    exercised.
    """
    directory.mkdir(parents=True, exist_ok=True)
    stem = name or (check_id or authority).replace(".", "-")
    body = {
        "environment": ENVIRONMENT,
        "deployed_revision": REVISION,
        "authority": authority,
        "observed_at": (
            observed_at or (datetime.now(timezone.utc) - timedelta(minutes=30))
        ).isoformat(),
        "run_id": run,
        "attempt": attempt,
    }
    if check_id:
        body["check_id"] = check_id
        body["resource"] = (
            resource
            if resource is not None
            else {"skypilot_cluster": SKY_CLUSTER, "kubernetes_node": NODE}
        )
        body["observations"] = facts or {}
    else:
        body["services"] = services or []
    body.update(body_overrides or {})
    payload = json.dumps(body).encode()
    (directory / f"{stem}.body").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    submission = {
        "record_kind": "check_evidence" if check_id else "service_inventory",
        "body": f"{stem}.body",
        "body_sha256": digest,
        "producer": producer,
    }
    submission.update(overrides)
    (directory / f"{stem}.json").write_text(json.dumps(submission))
    if attest and _PRODUCER is not None:
        # Re-attested over the whole directory, because the producer vouches for
        # the set. Writing a second record therefore supersedes the first
        # attestation rather than adding to it -- which is what makes "a forged
        # record slipped in beside genuine ones" detectable.
        _PRODUCER.attest(manifest_digest(directory), run=run, attempt=attempt)
    return digest


def service_listing(
    directory,
    *,
    name: str = "sky-serve-llama",
    controller: str = CONTROLLER,
    **overrides,
) -> str:
    """The authoritative ``sky serve status`` listing, retained."""
    return retained(
        directory,
        authority="skyserve.status",
        services=[
            {
                "name": name,
                "controller": controller,
                "endpoint": "http://10.0.0.9:8080",
            }
        ],
        name="serve-status",
        **overrides,
    )


def config(tmp_path, **overrides) -> dict:
    base = {
        "environment": ENVIRONMENT,
        "revision": REVISION,
        "source_revision": SOURCE_REVISION,
        "source_provenance": {
            "verified": True,
            "checkout_revision": SOURCE_REVISION,
            "dirty": False,
            "detail": "the executing checkout reports the recorded revision",
        },
        "scenarios": tuple(sorted(lb.KNOWN_SCENARIOS)),
        "authorization": "wave-6 retained authorization record",
        "window_start": datetime.now(timezone.utc) - timedelta(hours=1),
        "evidence_file": str(tmp_path / "evidence.json"),
        # No retained records by default: the checks a steady-state read cannot
        # settle must stay unsatisfied and named, which is most of these tests.
        "receipts_dir": "",
        "target_metadata": dict(TARGET_METADATA),
    }
    base.update(overrides)
    return base


def observer(tmp_path, skypilot=None, kubectl=None, provider=None, **overrides):
    return lo.build_observer(
        config(tmp_path, **overrides),
        skypilot=skypilot or FakeSkyPilot(),
        kubectl=kubectl or FakeKubectl(),
        # Explicitly none unless a test supplies one: the selected target's
        # provider has no reviewed client, and the absence check must say so.
        provider=provider,
    )


def captured(tmp_path, **overrides):
    """A full capture over the least evidence a capture can legally complete on.

    ``capture`` reports serving separately and blocks when nothing authoritative
    says whether this baseline serves anything, so a whole-report test has to
    supply that listing even when the report is not what it is asserting. An empty
    retained listing is the honest minimum: it records that the authoritative
    source was read and revealed no services, which is a finding, rather than
    assuming the absence. Returns the subject too, since building a second
    observer over the same evidence path is itself refused.
    """
    receipts = tmp_path / "capture-receipts"
    retained(receipts, authority="skyserve.status", services=[], name="serve-status")
    settings = config(tmp_path, receipts_dir=str(receipts))
    subject = observer(tmp_path, receipts_dir=str(receipts), **overrides)
    return subject, lb.capture(settings, subject)


def outcomes(facts, check_id: str) -> list[lb.Outcome]:
    return [fact.outcome for fact in facts if fact.check_id == check_id]


# ---------------------------------------------------------------------------
# The load-bearing property first: the real class, offline, is never live.
# ---------------------------------------------------------------------------


def test_the_reviewed_observer_driven_offline_is_not_live_evidence(tmp_path):
    """Registered type, offline transports: still fixture evidence."""
    subject, report = captured(tmp_path)
    assert type(subject) in lb.LIVE_OBSERVERS
    assert subject.transport_is_live() is False
    assert report["evidence_kind"] == "offline-fixture"
    for criterion in report["criteria"].values():
        assert criterion["satisfied"] is False


def test_a_partly_real_transport_pair_is_not_live(tmp_path):
    """Both transports must be the reviewed ones, not just one."""
    subject = lo.build_observer(
        config(tmp_path),
        skypilot=lo.SkyPilotReads(TARGET_METADATA["skypilot_api"]),
        kubectl=FakeKubectl(),
    )
    assert subject.transport_is_live() is False


def test_both_real_transports_report_live(tmp_path):
    """The positive half of the same gate, without performing any read."""
    subject = lo.build_observer(
        config(tmp_path),
        skypilot=lo.SkyPilotReads(TARGET_METADATA["skypilot_api"]),
        kubectl=lo.KubectlReads("superplane", "skypilot"),
    )
    assert subject.transport_is_live() is True


# ---------------------------------------------------------------------------
# Read-only by construction: the transports expose no mutating call.
# ---------------------------------------------------------------------------


def test_the_skypilot_transport_permits_only_reads():
    assert set(lo.SKYPILOT_READS) == {"/api/health", "/status", "/enabled_clouds"}
    assert lo.SKYPILOT_READS["/status"] == "POST"


@pytest.mark.parametrize(
    "selector",
    ["/launch", "/down", "/api/stream", "/status/../launch", "", "/enabled_clouds?x=1"],
)
def test_the_skypilot_transport_refuses_anything_else(selector, monkeypatch):
    """No argument makes this transport launch or tear down anything."""
    monkeypatch.setenv(lo.SKYPILOT_TOKEN_VARIABLE, "offline-token-not-a-credential")
    transport = lo.SkyPilotReads(TARGET_METADATA["skypilot_api"])
    with pytest.raises(EvidenceError, match="not a permitted read"):
        transport(selector)


@pytest.mark.parametrize("selector", ["delete", "apply", "nodes -o yaml", ""])
def test_the_kubectl_transport_refuses_anything_else(selector):
    transport = lo.KubectlReads("superplane", "skypilot")
    with pytest.raises(EvidenceError, match="not a permitted read"):
        transport(selector)


def test_every_kubectl_command_is_a_read():
    for selector, arguments in (
        lo.KubectlReads("superplane", "skypilot").commands().items()
    ):
        assert arguments[0] in {"get", "config"}, selector
        assert not any(
            word in " ".join(arguments)
            for word in ("delete", "apply", "patch", "exec", "scale", "drain")
        ), selector


def test_a_namespace_cannot_smuggle_an_argument():
    for hostile in ("superplane --all", "-n=evil", "ns;delete", "UPPER", ""):
        with pytest.raises(EvidenceError, match="must be DNS labels"):
            lo.KubectlReads(hostile, "skypilot")


def test_the_skypilot_base_url_refuses_embedded_credentials():
    for hostile in ("http://user:pass@host:46580", "ftp://host", "host:46580"):
        with pytest.raises(EvidenceError):
            lo.SkyPilotReads(hostile)


def test_the_skypilot_transport_requires_an_existing_token(monkeypatch):
    monkeypatch.delenv(lo.SKYPILOT_TOKEN_VARIABLE, raising=False)
    transport = lo.SkyPilotReads(TARGET_METADATA["skypilot_api"])
    with pytest.raises(EvidenceError, match=lo.SKYPILOT_TOKEN_VARIABLE):
        transport("/api/health")


def test_the_kubectl_transport_requires_an_existing_kubeconfig(monkeypatch):
    monkeypatch.delenv(lo.KUBECONFIG_VARIABLE, raising=False)
    transport = lo.KubectlReads("superplane", "skypilot")
    with pytest.raises(EvidenceError, match=lo.KUBECONFIG_VARIABLE):
        transport("nodes")


def test_only_reviewed_provider_readers_are_registered():
    """The registry reflects review, not the mere existence of a class.

    One entry, for the provider whose instance-describe contract and credential
    boundary were reviewed. The selected baseline target's provider is *not* in it,
    which is why the absence check must report the gap rather than accept the
    teardown tool's own word.
    """
    assert set(lo.PROVIDER_READERS) == {"aws"}
    assert lo.PROVIDER_READERS["aws"] is lo.Ec2InstanceReads
    assert PROVIDER not in lo.PROVIDER_READERS


def test_a_provider_reader_must_be_registered_under_a_lowercase_name():
    for hostile in ("AWS", " aws", ""):
        with pytest.raises(EvidenceError, match="lowercase name"):
            lo.register_provider_reader(hostile, lo.Ec2InstanceReads)


def test_the_provider_reader_exposes_no_mutating_call():
    """Lookup only: there is no terminate or delete path reachable from here."""
    exposed = {name for name in dir(lo.Ec2InstanceReads) if not name.startswith("_")}
    assert exposed == {"describe", "instance_present"}


def test_the_provider_reader_refuses_a_managed_instance_id():
    """An SSM deregistration is not a provider-side absence.

    ``mi-...`` leaving the fleet says the node left the cluster's control plane,
    not that the rented machine stopped billing -- which is the whole point of
    this check. Refusing it keeps the criterion unconfirmed instead of confirmed
    on the wrong evidence.
    """
    reader = lo.Ec2InstanceReads(REGION)
    for hostile in (INSTANCE, "i-nothex", "i-123", "", "i-0abc; rm -rf /"):
        with pytest.raises(EvidenceError, match="EC2 instance id"):
            reader.describe(hostile)


def test_the_provider_reader_refuses_a_hostile_region():
    for hostile in ("eu-north1; whoami", "EU-NORTH-1", "", "a"):
        with pytest.raises(EvidenceError, match="region"):
            lo.Ec2InstanceReads(hostile)


@pytest.mark.parametrize(
    ("state", "present"),
    [
        ("running", True),
        ("stopped", True),
        # Still billing, still a machine: only `terminated` is absence.
        ("shutting-down", True),
        ("stopping", True),
        ("terminated", False),
    ],
)
def test_the_provider_reader_reads_absence_only_from_terminated(state, present):
    payload = {"Reservations": [{"Instances": [{"State": {"name": state}}]}]}
    assert lo.Ec2InstanceReads.instance_present(payload) is present


def test_an_empty_provider_reservation_list_is_absence():
    assert lo.Ec2InstanceReads.instance_present({"Reservations": []}) is False


# ---------------------------------------------------------------------------
# The reads are bound to the selected cluster.
# ---------------------------------------------------------------------------


def test_a_kubeconfig_for_another_cluster_is_refused(tmp_path):
    """Right fields, wrong cluster, is exactly the foreign evidence to exclude."""
    subject = observer(tmp_path, kubectl=FakeKubectl(context=kube_context("other-eks")))
    with pytest.raises(EvidenceError, match="not the selected target's EKS ARN"):
        subject.observe(Dimension.NODE_REGISTRATION)


@pytest.mark.parametrize(
    "hostile",
    [
        # The reviewer's own reproduction: accepted by a containment test, after
        # which its node facts were labelled as the selected cluster's.
        "workspace-eks-attacker",
        # Prefix, suffix and lookalike variants of the same defect.
        "pre-workspace-eks",
        "workspace-ek",
        "workspace-eks-2",
        "workspace_eks",
        "Workspace-EKS",
    ],
)
def test_a_lookalike_cluster_name_is_refused(tmp_path, hostile):
    """Exact canonical identity, so a name that merely contains the right letters fails."""
    subject = observer(tmp_path, kubectl=FakeKubectl(context=kube_context(hostile)))
    with pytest.raises(EvidenceError, match="not the selected target's EKS ARN"):
        subject.observe(Dimension.NODE_REGISTRATION)


def test_the_same_cluster_name_in_another_account_or_region_is_refused(tmp_path):
    """Which is why the name alone was never enough: the ARN carries both."""
    for index, arn in enumerate(
        (cluster_arn(account="999988887777"), cluster_arn(region="us-east-1"))
    ):
        case = tmp_path / f"case-{index}"
        case.mkdir()
        subject = observer(case, kubectl=FakeKubectl(context=kube_context(arn=arn)))
        with pytest.raises(EvidenceError, match="not the selected target's EKS ARN"):
            subject.observe(Dimension.NODE_REGISTRATION)


def test_a_context_name_that_is_not_an_arn_is_refused(tmp_path):
    """Without account and region there is nothing to compare, so do not compare loosely."""
    subject = observer(tmp_path, kubectl=FakeKubectl(context=kube_context(arn=CLUSTER)))
    with pytest.raises(EvidenceError, match="not the selected target's EKS ARN"):
        subject.observe(Dimension.NODE_REGISTRATION)


def test_the_right_arn_at_the_wrong_address_is_refused(tmp_path):
    """A proxy pointed elsewhere must not be able to answer for the right ARN."""
    subject = observer(
        tmp_path,
        kubectl=FakeKubectl(
            context=kube_context(server="https://elsewhere.example.com")
        ),
    )
    with pytest.raises(EvidenceError, match="not the selected cluster's endpoint"):
        subject.observe(Dimension.NODE_REGISTRATION)


def test_an_ambiguous_kubeconfig_is_refused(tmp_path):
    ambiguous = {
        "clusters": [kube_context()["clusters"][0], kube_context("b")["clusters"][0]]
    }
    subject = observer(tmp_path, kubectl=FakeKubectl(context=ambiguous))
    with pytest.raises(EvidenceError, match="exactly one cluster"):
        subject.observe(Dimension.NODE_REGISTRATION)


def test_the_cluster_context_is_checked_once(tmp_path):
    kubectl = FakeKubectl()
    subject = observer(tmp_path, kubectl=kubectl)
    subject.observe(Dimension.NODE_REGISTRATION)
    subject.observe(Dimension.BATCH_WORKLOAD)
    assert kubectl.calls.count("context") == 1


# ---------------------------------------------------------------------------
# Mapping the maintained contracts: what the observer concludes from a read.
# ---------------------------------------------------------------------------


def test_a_ready_node_satisfies_the_join_check(tmp_path):
    facts = observer(tmp_path).observe(Dimension.NODE_REGISTRATION)
    assert outcomes(facts, "node.join-produces-ready-node") == [lb.Outcome.SATISFIED]
    assert outcomes(facts, "node.status-links-to-k8s-node") == [lb.Outcome.SATISFIED]


def test_a_not_ready_node_refutes_the_join_check(tmp_path):
    """The reviewers' case: the node never became Ready."""
    subject = observer(
        tmp_path, kubectl=FakeKubectl(nodes=listing(k8s_node(ready="False")))
    )
    facts = subject.observe(Dimension.NODE_REGISTRATION)
    assert outcomes(facts, "node.join-produces-ready-node") == [lb.Outcome.REFUTED]
    assert "Ready=False" in next(
        f.detail for f in facts if f.check_id == "node.join-produces-ready-node"
    )


def test_an_unknown_ready_condition_is_indeterminate(tmp_path):
    subject = observer(
        tmp_path, kubectl=FakeKubectl(nodes=listing(k8s_node(ready="Unknown")))
    )
    facts = subject.observe(Dimension.NODE_REGISTRATION)
    assert outcomes(facts, "node.join-produces-ready-node") == [
        lb.Outcome.INDETERMINATE
    ]


def test_a_record_naming_no_present_node_refutes_the_join(tmp_path):
    subject = observer(tmp_path, kubectl=FakeKubectl(nodes=listing()))
    facts = subject.observe(Dimension.NODE_REGISTRATION)
    assert outcomes(facts, "node.join-produces-ready-node") == [lb.Outcome.REFUTED]
    assert outcomes(facts, "node.status-links-to-k8s-node") == [lb.Outcome.REFUTED]


def test_activation_material_in_a_record_is_a_refutation(tmp_path):
    subject = observer(
        tmp_path,
        kubectl=FakeKubectl(
            **{
                "superplane-nodes": listing(
                    superplane_node(activationCode="redacted-marker")
                )
            }
        ),
    )
    facts = subject.observe(Dimension.NODE_REGISTRATION)
    assert outcomes(facts, "node.activation-secret-not-leaked") == [lb.Outcome.REFUTED]


def test_a_clean_record_satisfies_the_no_leak_check(tmp_path):
    facts = observer(tmp_path).observe(Dimension.NODE_REGISTRATION)
    assert outcomes(facts, "node.activation-secret-not-leaked") == [
        lb.Outcome.SATISFIED
    ]


def test_a_running_gpu_pod_satisfies_scheduling(tmp_path):
    facts = observer(tmp_path).observe(Dimension.BATCH_WORKLOAD)
    assert outcomes(facts, "batch.gpu-workload-schedules") == [lb.Outcome.SATISFIED]
    assert outcomes(facts, "batch.device-plugin-advertises-gpu") == [
        lb.Outcome.SATISFIED
    ]


def test_a_pending_gpu_pod_refutes_scheduling(tmp_path):
    """The reviewers' case: the workload failed to schedule."""
    subject = observer(
        tmp_path, kubectl=FakeKubectl(pods=listing(gpu_pod(phase="Pending")))
    )
    facts = subject.observe(Dimension.BATCH_WORKLOAD)
    assert outcomes(facts, "batch.gpu-workload-schedules") == [lb.Outcome.REFUTED]


def test_no_gpu_allocatable_refutes_the_device_plugin_check(tmp_path):
    subject = observer(tmp_path, kubectl=FakeKubectl(nodes=listing(k8s_node(gpu="0"))))
    facts = subject.observe(Dimension.BATCH_WORKLOAD)
    assert outcomes(facts, "batch.device-plugin-advertises-gpu") == [lb.Outcome.REFUTED]


def test_a_pod_on_another_node_is_not_this_machines_workload(tmp_path):
    subject = observer(
        tmp_path, kubectl=FakeKubectl(pods=listing(gpu_pod(node_name="other-node")))
    )
    facts = subject.observe(Dimension.BATCH_WORKLOAD)
    assert outcomes(facts, "batch.gpu-workload-schedules") == [lb.Outcome.INDETERMINATE]


def test_a_cloud_outside_the_enabled_set_refutes_restriction(tmp_path):
    clusters = sky_status()
    clusters[0]["handle"]["launched_resources"]["cloud"] = "GCP"
    subject = observer(tmp_path, skypilot=FakeSkyPilot(**{"/status": clusters}))
    facts = subject.observe(Dimension.PROVIDER_SELECTION)
    assert outcomes(facts, "provider.configured-clouds-restrict-selection") == [
        lb.Outcome.REFUTED
    ]


def test_an_enabled_cloud_satisfies_restriction(tmp_path):
    facts = observer(tmp_path).observe(Dimension.PROVIDER_SELECTION)
    assert outcomes(facts, "provider.configured-clouds-restrict-selection") == [
        lb.Outcome.SATISFIED
    ]


def test_a_non_default_autostop_refutes_the_launch_shape(tmp_path):
    subject = observer(
        tmp_path, skypilot=FakeSkyPilot(**{"/status": sky_status(autostop=-1)})
    )
    facts = subject.observe(Dimension.PROVIDER_SELECTION)
    assert outcomes(facts, "provider.launch-request-shape") == [lb.Outcome.REFUTED]


def test_a_mapped_cluster_status_satisfies_the_status_check(tmp_path):
    facts = observer(tmp_path).observe(Dimension.STATUS_AND_LOGS)
    assert outcomes(facts, "status.cluster-status-mapped") == [lb.Outcome.SATISFIED]


def test_an_unmappable_cluster_status_refutes_it(tmp_path):
    subject = observer(
        tmp_path, skypilot=FakeSkyPilot(**{"/status": sky_status(status="WEIRD")})
    )
    facts = subject.observe(Dimension.STATUS_AND_LOGS)
    assert outcomes(facts, "status.cluster-status-mapped") == [lb.Outcome.REFUTED]


def test_two_records_claiming_one_cluster_refute_single_ownership(tmp_path):
    subject = observer(
        tmp_path,
        kubectl=FakeKubectl(
            **{
                "superplane-nodes": listing(
                    superplane_node(), superplane_node(k8sNodeName="", ssmInstanceId="")
                )
            }
        ),
    )
    facts = subject.observe(Dimension.CONTROLLER_LIFECYCLE)
    assert outcomes(facts, "lifecycle.single-owner-per-resource") == [
        lb.Outcome.REFUTED,
        lb.Outcome.REFUTED,
    ]


def test_a_reported_hourly_cost_is_recorded_as_an_estimate(tmp_path):
    facts = observer(tmp_path).observe(Dimension.COST_AND_CLEANUP)
    cost = next(f for f in facts if f.check_id == lb.COST_CHECK)
    assert cost.outcome is lb.Outcome.SATISFIED
    assert cost.hourly_cost == pytest.approx(2.95)
    assert "estimate" in cost.detail


def test_a_missing_hourly_cost_is_unknown_not_zero(tmp_path):
    record = superplane_node()
    del record["status"]["hourlyCost"]
    subject = observer(
        tmp_path, kubectl=FakeKubectl(**{"superplane-nodes": listing(record)})
    )
    facts = subject.observe(Dimension.COST_AND_CLEANUP)
    cost = next(f for f in facts if f.check_id == lb.COST_CHECK)
    assert cost.outcome is lb.Outcome.INDETERMINATE
    assert cost.hourly_cost is None
    assert "unknown, not zero" in cost.detail


# ---------------------------------------------------------------------------
# Honest gaps: what a read-only steady-state observation cannot settle.
# ---------------------------------------------------------------------------


RECEIPT_CHECKS = (
    "provider.ordering-cheapest-first",
    "provider.fallback-on-launch-failure",
    "node.cni-prerequisite-recorded",
    "status.progress-lines-streamed",
    "status.terminal-event-ends-stream",
    "cancel.in-flight-launch-stops",
    "cancel.timeout-bounded",
    "cancel.cancelled-launch-releases-capacity",
    "lifecycle.restart-resumes-not-duplicates",
    "lifecycle.api-state-store-survives-redeploy",
    "cost.observation-not-a-spend-control",
    "cleanup.down-then-purge-fallback",
    lb.PROVIDER_ABSENCE_CHECK,
)


@pytest.mark.parametrize("check_id", RECEIPT_CHECKS)
def test_a_check_needing_an_operation_receipt_is_indeterminate(check_id, tmp_path):
    """Not fabricated, not silently skipped: indeterminate with the input named."""
    subject = observer(tmp_path)
    facts = [
        fact
        for dimension in lb.BASELINE_DIMENSIONS
        for fact in subject.observe(dimension)
        if fact.check_id == check_id
    ]
    assert facts, check_id
    assert all(fact.outcome is lb.Outcome.INDETERMINATE for fact in facts)
    assert all("Required input:" in fact.detail for fact in facts)


def test_provider_absence_names_the_missing_provider_client(tmp_path):
    facts = observer(tmp_path).observe(Dimension.COST_AND_CLEANUP)
    absence = next(f for f in facts if f.check_id == lb.PROVIDER_ABSENCE_CHECK)
    assert absence.provider_absence_confirmed is False
    assert "instance-describe client" in absence.detail
    assert PROVIDER in absence.detail


# ---------------------------------------------------------------------------
# Retained records: the path that makes those gaps closeable -- and the five
# screens a record must survive before it closes one.
# ---------------------------------------------------------------------------


def launch_record(directory, prices=(1.5, 4.0), **overrides) -> dict:
    """A controller.launch-decision record for the ordering check."""
    defaults = {
        "check_id": "provider.ordering-cheapest-first",
        "authority": "controller.launch-decision",
        "facts": {
            "offered_options": [
                {"provider": PROVIDER, "hourly_price": price} for price in prices
            ]
        },
    }
    return retained(directory, **{**defaults, **overrides})


def only(facts, check_id: str):
    matching = [fact for fact in facts if fact.check_id == check_id]
    assert len(matching) == 1, matching
    return matching[0]


def provider_dimension(tmp_path, receipts, **overrides):
    subject = observer(tmp_path, receipts_dir=str(receipts), **overrides)
    return subject.observe(Dimension.PROVIDER_SELECTION)


def test_a_valid_record_settles_a_check_a_steady_state_read_cannot(tmp_path):
    """The repair's whole point: a real record produces a real outcome.

    Which order providers were offered at launch is gone by the time anything
    steady-state can look. With the authorized operation's retained record, the
    check is settled from evidence rather than reported unsettleable forever.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    ordering = only(
        provider_dimension(tmp_path, receipts), "provider.ordering-cheapest-first"
    )
    assert ordering.outcome is lb.Outcome.SATISFIED
    assert "cheapest first" in ordering.detail


def test_the_record_supplies_facts_and_the_checker_derives_the_outcome(tmp_path):
    """Options priced high-then-low are refuted, whatever the record says.

    A record that could declare its own verdict would make this a check of the
    operator's opinion. So the facts are read and the outcome derived here.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(4.0, 1.5))
    ordering = only(
        provider_dimension(tmp_path, receipts), "provider.ordering-cheapest-first"
    )
    assert ordering.outcome is lb.Outcome.REFUTED
    assert "not in cheapest-first order" in ordering.detail


def test_a_declared_verdict_in_a_record_is_refused(tmp_path):
    """Not ignored -- refused, so an attempt to assert an outcome is visible.

    The verdict is written into the *body*, which is the strong form of the case:
    an authenticated body whose producer vouches for these exact bytes still may
    not declare its own outcome.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, body_overrides={"outcome": "satisfied"})
    with pytest.raises(EvidenceError, match="supplies observations, not verdicts"):
        provider_dimension(tmp_path, receipts)


def test_a_single_option_cannot_evidence_an_ordering(tmp_path):
    """One option is not an order. Refused rather than trivially satisfied."""
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5,))
    with pytest.raises(EvidenceError, match="at least 2 entries"):
        provider_dimension(tmp_path, receipts)


def test_a_record_whose_body_was_edited_after_the_fact_is_refused(tmp_path):
    """Screen 1, hash: the digest is recomputed from the bytes on disk."""
    receipts = tmp_path / "receipts"
    launch_record(receipts)
    body = receipts / "provider-ordering-cheapest-first.body"
    body.write_bytes(body.read_bytes() + b" ")
    with pytest.raises(EvidenceError, match="does not match its declared sha256"):
        provider_dimension(tmp_path, receipts)


@pytest.mark.parametrize(
    "hostile",
    ["../escape.body", "/etc/hostname", "nested/body.json", ".", "-rf"],
    ids=["parent", "absolute", "nested", "directory", "flag-shaped"],
)
def test_a_record_cannot_name_a_body_outside_its_own_directory(tmp_path, hostile):
    """The body is a plain file beside the record, not an arbitrary path.

    Without this the operator-supplied directory would be a read primitive for
    anything the capture's user can open.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts)
    submission = receipts / "provider-ordering-cheapest-first.json"
    pointer = json.loads(submission.read_text())
    pointer["body"] = hostile
    submission.write_text(json.dumps(pointer))
    with pytest.raises(
        EvidenceError, match="plain file beside the submission|regular file"
    ):
        provider_dimension(tmp_path, receipts)


@pytest.mark.parametrize(
    ("offset", "reason"),
    [
        (timedelta(hours=3), "before the window opened"),
        (-timedelta(hours=1), "dated into the future"),
    ],
    ids=["replayed-from-an-earlier-session", "dated-forward"],
)
def test_a_record_observed_outside_the_authorized_window_is_refused(
    tmp_path, offset, reason
):
    """Screen 2, time. The window is what stops an old session being replayed."""
    receipts = tmp_path / "receipts"
    launch_record(receipts, observed_at=datetime.now(timezone.utc) - offset)
    with pytest.raises(EvidenceError, match="authorized execution window"):
        provider_dimension(tmp_path, receipts)
    assert reason  # documents which end of the window the case exercises


@pytest.mark.parametrize(
    "drift",
    [{"environment": "baseline/other"}, {"deployed_revision": "999999999999"}],
    ids=["another-environment", "another-revision"],
)
def test_a_record_from_another_target_or_revision_is_refused(tmp_path, drift):
    """Screen 3, target and revision. A neighbouring environment is not this one."""
    receipts = tmp_path / "receipts"
    launch_record(receipts, body_overrides=drift)
    with pytest.raises(EvidenceError, match="was produced against another"):
        provider_dimension(tmp_path, receipts)


def test_a_record_about_another_operations_machine_is_refused(tmp_path):
    """Screen 4, resource: the record must name a machine this capture saw.

    This is what stops a valid, correctly-hashed, in-window record from a
    *different* operation settling this baseline's checks.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, resource={"skypilot_cluster": "sky-someone-else"})
    with pytest.raises(EvidenceError, match="names no resource this capture observed"):
        provider_dimension(tmp_path, receipts)


def test_a_record_from_an_authority_that_cannot_speak_for_the_check_is_refused(
    tmp_path,
):
    """Screen 5, authority.

    The tool that performed a teardown cannot answer what the teardown did to the
    provider's billing, and the SkyPilot API server cannot report what the
    controller was offered at launch.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, authority="skypilot.api-state")
    with pytest.raises(EvidenceError, match="may only be evidenced by"):
        provider_dimension(tmp_path, receipts)


def test_an_unrecognized_authority_is_refused_by_name(tmp_path):
    """A record cannot invent an authority and so choose what it may answer."""
    receipts = tmp_path / "receipts"
    launch_record(receipts, authority="operator.assertion")
    with pytest.raises(EvidenceError, match="authority"):
        provider_dimension(tmp_path, receipts)


def test_a_record_for_an_unknown_check_is_refused_rather_than_ignored(tmp_path):
    receipts = tmp_path / "receipts"
    launch_record(receipts, check_id="provider.ordering-is-fine-actually")
    with pytest.raises(EvidenceError, match="check"):
        provider_dimension(tmp_path, receipts)


def test_records_for_other_checks_leave_those_checks_unsatisfied(tmp_path):
    """Settling one check does not settle its neighbours.

    The ordering record is valid and satisfies its own check; fallback, which
    needs a record of a launch whose first option *failed*, stays indeterminate
    and still names what to retain.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts)
    facts = provider_dimension(tmp_path, receipts)
    assert (
        only(facts, "provider.ordering-cheapest-first").outcome is lb.Outcome.SATISFIED
    )
    fallback = only(facts, "provider.fallback-on-launch-failure")
    assert fallback.outcome is lb.Outcome.INDETERMINATE
    assert "Required input:" in fallback.detail


def test_the_retained_record_is_republished_as_a_reference_not_a_body(tmp_path):
    """A finding stays re-derivable from this run's own evidence directory."""
    receipts = tmp_path / "receipts"
    launch_record(receipts)
    ordering = only(
        provider_dimension(tmp_path, receipts), "provider.ordering-cheapest-first"
    )
    name = ordering.evidence_reference.split("/", 1)[1]
    content = (tmp_path / "evidence.json.raw" / name).read_bytes()
    assert hashlib.sha256(content).hexdigest() == ordering.evidence_sha256


def test_a_receipts_directory_beyond_the_file_bound_is_refused(tmp_path):
    """Operator-supplied: too large to read must fail, not read anyway."""
    receipts = tmp_path / "receipts"
    for index in range(receipts_module.MAX_RECORD_FILES + 1):
        launch_record(receipts, name=f"record-{index}")
    with pytest.raises(EvidenceError, match="more than 200 records"):
        provider_dimension(tmp_path, receipts)


# ---------------------------------------------------------------------------
# Teardown: a record may claim a release; only the provider establishes it.
# ---------------------------------------------------------------------------


class FakeProvider:
    """A reviewed-shaped read-only provider client, offline."""

    def __init__(self, state: str = "terminated") -> None:
        self.state = state
        self.asked: list[str] = []

    def describe(self, instance: str) -> bytes:
        self.asked.append(instance)
        reservations = (
            []
            if self.state == "absent"
            else [{"Instances": [{"State": {"name": self.state}}]}]
        )
        return json.dumps({"Reservations": reservations}).encode()

    @staticmethod
    def instance_present(payload: dict) -> bool:
        return lo.Ec2InstanceReads.instance_present(payload)


def teardown_record(directory, **overrides) -> dict:
    defaults = {
        "check_id": "cleanup.provider-side-absence-verified",
        "authority": "skypilot.teardown",
        # An EC2-shaped id, because the reviewed reader answers only for those: an
        # `mi-` handle leaving SSM is not a rented machine ceasing to bill.
        "facts": {"instance": "i-0baseline1234", "teardown_succeeded": True},
    }
    return retained(directory, **{**defaults, **overrides})


def absence_fact(tmp_path, receipts, provider):
    facts = observer(tmp_path, receipts_dir=str(receipts), provider=provider).observe(
        Dimension.COST_AND_CLEANUP
    )
    return only(facts, lb.PROVIDER_ABSENCE_CHECK)


def test_the_provider_overrides_a_records_release_claim(tmp_path):
    """The reviewed rule, now exercised against a real claim.

    A teardown tool reporting success only proves it dropped its local handle. The
    machine is what bills, so when the provider still reports it, the record's
    claim is refuted rather than averaged with it.
    """
    receipts = tmp_path / "receipts"
    teardown_record(receipts)
    absence = absence_fact(tmp_path, receipts, FakeProvider("running"))
    assert absence.outcome is lb.Outcome.REFUTED
    assert absence.provider_absence_confirmed is False
    assert "still reports instance" in absence.detail


def test_independent_provider_confirmation_satisfies_the_absence_check(tmp_path):
    receipts = tmp_path / "receipts"
    teardown_record(receipts)
    provider = FakeProvider("terminated")
    absence = absence_fact(tmp_path, receipts, provider)
    assert absence.outcome is lb.Outcome.SATISFIED
    assert absence.provider_absence_confirmed is True
    assert provider.asked == ["i-0baseline1234"]


def test_a_teardown_record_alone_cannot_confirm_absence(tmp_path):
    """No reader configured: the claim is not promoted to a confirmation."""
    receipts = tmp_path / "receipts"
    teardown_record(receipts)
    absence = absence_fact(tmp_path, receipts, None)
    assert absence.outcome is lb.Outcome.INDETERMINATE
    assert absence.provider_absence_confirmed is False
    assert "cannot be confirmed" in absence.detail


def test_a_failed_teardown_cannot_begin_to_establish_absence(tmp_path):
    receipts = tmp_path / "receipts"
    teardown_record(
        receipts, facts={"instance": "i-0baseline1234", "teardown_succeeded": False}
    )
    with pytest.raises(EvidenceError, match="did not succeed"):
        absence_fact(tmp_path, receipts, FakeProvider())


def test_the_provider_read_is_retained_alongside_the_record(tmp_path):
    """Both halves of the finding are pinned: the claim and the confirmation."""
    receipts = tmp_path / "receipts"
    teardown_record(receipts)
    absence = absence_fact(tmp_path, receipts, FakeProvider("terminated"))
    # A combined reference over both halves -- the record's claim and the
    # provider's confirmation -- so neither can be swapped out after the fact.
    parts = absence.evidence_reference.split("; ")
    assert len(parts) == 2
    assert "receipt-cleanup-provider-side-absence-verified" in parts[0]
    assert "provider-describe-i-0baseline1234" in parts[1]
    combined = "\n".join(
        f"{name}:{hashlib.sha256((tmp_path / 'evidence.json.raw' / name.split('/', 1)[1]).read_bytes()).hexdigest()}"
        for name in parts
    )
    assert hashlib.sha256(combined.encode()).hexdigest() == absence.evidence_sha256


def test_serving_checks_name_their_missing_requests(tmp_path):
    """A listed service with no probe or teardown record: each named, none passed."""
    receipts = tmp_path / "receipts"
    service_listing(receipts)
    subject = observer(tmp_path, receipts_dir=str(receipts))
    facts = subject.observe(Dimension.SERVING_WORKLOAD)
    assert {f.check_id for f in facts} == {
        check.check_id for check in dimension_by_name(Dimension.SERVING_WORKLOAD).checks
    }
    assert all(f.outcome is lb.Outcome.INDETERMINATE for f in facts)
    unauthenticated = next(
        f for f in facts if f.check_id == "serving.unauthenticated-request-refused"
    )
    assert "unauthenticated request" in unauthenticated.detail
    # The identity is the service itself, not the first machine that happened to be
    # observed: "one service was reachable" is not "this service was".
    assert {f.resource.skypilot_cluster for f in facts} == {"sky-serve-llama"}


def test_serving_absence_is_established_only_from_the_authoritative_listing(tmp_path):
    """The reviewer's scenario: a live service the cluster list does not reveal.

    ``sky serve status`` names a service whose controller cluster appears nowhere in
    the API server's ``/status`` output. The old implementation scanned exactly that
    output for names beginning ``sky-serve-controller``, found nothing, and concluded
    the baseline had no serving -- so a real service was invisible to the migration
    and the serving criterion passed on its absence branch. It must now be seen.
    """
    receipts = tmp_path / "receipts"
    service_listing(receipts, name="sky-serve-llama")
    subject = observer(tmp_path, receipts_dir=str(receipts))
    # The cluster list is the default fixture: one batch cluster, no serve controller.
    assert [entry["name"] for entry in sky_status()] == [SKY_CLUSTER]
    found = subject.serving_inventory()
    assert found.services == ("sky-serve-llama",)
    assert "sky serve status" in found.enumerated_via
    assert subject.observe(Dimension.SERVING_WORKLOAD) != ()


def test_a_serve_controller_cluster_alone_does_not_establish_serving(tmp_path):
    """Nor can a cluster name create serving: the listing is the only authority.

    The mirror image of the previous test. A cluster named like a serve controller
    is still only a cluster name, so with no retained listing the inventory is
    unavailable and the criterion is blocked -- not passed as "nothing to check".
    """
    serve_cluster = {
        "name": "sky-serve-controller-1",
        "status": "UP",
        "handle": {"launched_resources": {"cloud": "Nebius", "region": REGION}},
    }
    subject = observer(
        tmp_path,
        skypilot=FakeSkyPilot(**{"/status": [*sky_status(), serve_cluster]}),
    )
    assert subject.serving_inventory() is None
    assert subject.observe(Dimension.SERVING_WORKLOAD) == ()
    with pytest.raises(EvidenceError, match="serving absence needs observed"):
        lb.capture(config(tmp_path), subject)


def test_no_serving_facts_are_produced_when_the_listing_found_none(tmp_path):
    """Keeps the criterion's absence branch consistent with the inventory."""
    receipts = tmp_path / "receipts"
    retained(receipts, authority="skyserve.status", services=[], name="serve-status")
    subject = observer(tmp_path, receipts_dir=str(receipts))
    assert subject.observe(Dimension.SERVING_WORKLOAD) == ()
    assert subject.serving_inventory().services == ()


def test_two_service_listings_cannot_both_be_authoritative(tmp_path):
    receipts = tmp_path / "receipts"
    service_listing(receipts, name="one")
    retained(
        receipts,
        authority="skyserve.status",
        services=[{"name": "two", "controller": CONTROLLER, "endpoint": "http://x"}],
        name="serve-status-second",
    )
    subject = observer(tmp_path, receipts_dir=str(receipts))
    with pytest.raises(EvidenceError, match="enumerated once"):
        subject.serving_inventory()


def test_a_cluster_listing_cannot_stand_in_for_the_service_listing(tmp_path):
    """Authority per record: the cluster list is not the serving authority."""
    receipts = tmp_path / "receipts"
    retained(
        receipts,
        authority="skypilot.api-state",
        services=[{"name": "s", "controller": CONTROLLER, "endpoint": "http://x"}],
        name="serve-status",
    )
    subject = observer(tmp_path, receipts_dir=str(receipts))
    with pytest.raises(EvidenceError, match="cannot establish serving presence"):
        subject.serving_inventory()


def test_a_record_with_no_handle_yields_no_facts(tmp_path):
    """A record identifying nothing cannot carry a fact or seed a correlation."""
    subject = observer(
        tmp_path,
        kubectl=FakeKubectl(
            **{
                "superplane-nodes": listing(
                    superplane_node(
                        k8sNodeName="", skypilotCluster="", ssmInstanceId=""
                    )
                )
            }
        ),
    )
    assert subject.observe(Dimension.NODE_REGISTRATION) == ()


# ---------------------------------------------------------------------------
# Identity and correlation: one machine, named consistently.
# ---------------------------------------------------------------------------


def test_every_fact_carries_the_selected_targets_identity(tmp_path):
    subject = observer(tmp_path)
    for dimension in lb.BASELINE_DIMENSIONS:
        for fact in subject.observe(dimension):
            assert fact.resource.provider == PROVIDER
            # The canonical ARN of the cluster the reads were actually verified
            # against, not the bare name a lookalike could also satisfy.
            assert fact.resource.cluster == cluster_arn()
            assert fact.resource.controller == CONTROLLER
            assert fact.resource.kubernetes_node == NODE
            assert fact.resource.provider_resource_id == INSTANCE
            assert fact.environment == ENVIRONMENT
            assert fact.revision == REVISION


# ---------------------------------------------------------------------------
# Observed identity and provenance: read from the system, then compared.
# ---------------------------------------------------------------------------


def controller_listing(*names) -> dict:
    return listing(*({"kind": "Deployment", "metadata": {"name": n}} for n in names))


def test_identity_is_read_from_the_environments_own_responses(tmp_path):
    """Each field traced to the read it came from, not to the selection.

    The defect was filling these from the operator's configuration and then
    comparing them back to it -- a check that cannot fail. Here every value is
    sourced: provider and region from the clusters the API server launched,
    runtime from its health response, controller from the deployments running.
    """
    identity = observer(tmp_path).environment_identity()
    assert identity.provider == PROVIDER
    assert identity.region == REGION
    assert identity.runtime_version == RUNTIME
    assert identity.controller == CONTROLLER
    assert identity.cluster_arn == cluster_arn()
    assert identity.api_endpoint == api_endpoint()
    assert identity.unreported == ()


@pytest.mark.parametrize(
    ("kind", "responses", "reported"),
    [
        (
            "provider",
            {
                "/status": sky_status(
                    handle={"launched_resources": {"cloud": "AWS", "region": REGION}}
                )
            },
            "aws",
        ),
        (
            "region",
            {
                "/status": sky_status(
                    handle={
                        "launched_resources": {"cloud": "Nebius", "region": "us-east1"}
                    }
                )
            },
            "us-east1",
        ),
        (
            "runtime",
            {"/api/health": {"status": "healthy", "version": "0.9.1"}},
            "0.9.1",
        ),
    ],
    ids=["another-provider", "another-region", "another-runtime"],
)
def test_an_environment_reporting_something_else_is_refused(
    tmp_path, kind, responses, reported
):
    """A mismatch surfaces instead of being erased.

    A run pointed at a different cloud, region or runtime than the one selected
    would previously have been labelled as the selected baseline and passed.
    """
    subject = observer(tmp_path, skypilot=FakeSkyPilot(**responses))
    identity = subject.environment_identity()
    assert getattr(identity, {"runtime": "runtime_version"}.get(kind, kind)) == reported
    with pytest.raises(EvidenceError, match="which is not the selected target's"):
        lb._confirm_environment_identity(
            config(tmp_path),
            identity,
            lb._Window(
                datetime.now(timezone.utc) - timedelta(hours=1),
                datetime.now(timezone.utc),
            ),
        )


def test_an_absent_controller_is_unreported_rather_than_assumed(tmp_path):
    """Reporting the only deployment would pass the comparison for the wrong reason.

    If the expected controller is not among the deployments running, the honest
    observation is that nothing identified it -- not "the one thing deployed must
    be it". The capture then refuses, because nothing binds the evidence.
    """
    kubectl = FakeKubectl(controllers=controller_listing("some-other-operator"))
    identity = observer(tmp_path, kubectl=kubectl).environment_identity()
    assert identity.controller == ""
    assert "controller" in identity.unreported


def test_two_providers_in_one_baseline_is_a_finding_not_a_tie_break(tmp_path):
    """Refused rather than resolved by majority: it means the reads are mixed."""
    entries = sky_status() + sky_status(
        name="sky-baseline-2",
        handle={"launched_resources": {"cloud": "AWS", "region": REGION}},
    )
    subject = observer(tmp_path, skypilot=FakeSkyPilot(**{"/status": entries}))
    with pytest.raises(EvidenceError, match="more than one provider"):
        subject.environment_identity()


def test_the_identity_record_pins_all_three_reads_it_derives_from(tmp_path):
    """Altering any one of the three changes the digest."""
    identity = observer(tmp_path).environment_identity()
    parts = identity.evidence_reference.split("; ")
    assert len(parts) == 3
    combined = "\n".join(
        f"{name}:{hashlib.sha256((tmp_path / 'evidence.json.raw' / name.split('/', 1)[1]).read_bytes()).hexdigest()}"
        for name in parts
    )
    assert hashlib.sha256(combined.encode()).hexdigest() == identity.evidence_sha256


def test_facts_only_belong_to_their_own_dimension(tmp_path):
    subject = observer(tmp_path)
    for dimension in lb.BASELINE_DIMENSIONS + lb.SERVING_DIMENSIONS:
        known = {check.check_id for check in dimension_by_name(dimension).checks}
        assert {fact.check_id for fact in subject.observe(dimension)} <= known


def test_an_unknown_dimension_is_refused(tmp_path):
    with pytest.raises(EvidenceError, match="per parity dimension"):
        observer(tmp_path).observe("node_registration_and_readiness")


# ---------------------------------------------------------------------------
# Existing state: five classes, every decision undecided.
# ---------------------------------------------------------------------------


def test_all_five_state_classes_are_enumerated(tmp_path):
    records = observer(tmp_path).existing_state()
    assert {record.kind for record in records} == lb.KNOWN_STATE_KINDS
    assert all(record.decision == "undecided" for record in records)
    assert all(record.environment == ENVIRONMENT for record in records)


def test_an_empty_class_is_recorded_as_verified_empty(tmp_path):
    """No services found is a finding, not a silence -- but only once looked for."""
    receipts = tmp_path / "receipts"
    retained(receipts, authority="skyserve.status", services=[], name="serve-status")
    records = observer(tmp_path, receipts_dir=str(receipts)).existing_state()
    serving = next(r for r in records if r.kind == "skyserve_services")
    assert serving.handles == ()
    assert serving.empty_verified is True
    assert serving.resolved is True


def test_a_class_with_no_authoritative_source_is_unresolved_not_empty(tmp_path):
    """The distinction the second review asked for: unsettled is not verified-empty.

    With no retained service listing, serving has no controller-side inventory to
    fall back on, so the class is reported unresolved rather than filled in as "no
    services". Previously an unsettled class was padded out with whatever listing
    was to hand, which is how a live service became invisible to the migration.
    """
    records = observer(tmp_path).existing_state()
    serving = next(r for r in records if r.kind == "skyserve_services")
    assert serving.resolved is False
    assert serving.empty_verified is False
    assert serving.handles == ()
    assert "no retained sky serve status listing" in serving.unresolved_reason


def test_enumerated_handles_are_recorded(tmp_path):
    records = observer(tmp_path).existing_state()
    clusters = next(r for r in records if r.kind == "skypilot_clusters")
    assert clusters.handles == (SKY_CLUSTER,)
    nodes = next(r for r in records if r.kind == "joined_eks_hybrid_nodes")
    # Correlated three ways, so the handle names the node, its SSM managed instance
    # and the live SkyPilot cluster rather than just a node name.
    assert nodes.handles == (f"node/{NODE} ssm/{INSTANCE} cluster/{SKY_CLUSTER}",)
    crs = next(r for r in records if r.kind == "superplane_node_crs")
    assert set(crs.handles) == {"superplanenode/node-1", "nodepool/h100-pool"}


# ---------------------------------------------------------------------------
# The backing store: which kind, because that is what decides whether a
# redeploy silently orphans running machines and keeps billing for them.
# ---------------------------------------------------------------------------


def api_workload(*, env=None, mounts=None, volumes=None, name="skypilot-api") -> dict:
    container: dict = {"name": "api"}
    if env is not None:
        container["env"] = env
    if mounts is not None:
        container["volumeMounts"] = mounts
    template: dict = {"spec": {"containers": [container]}}
    if volumes is not None:
        template["spec"]["volumes"] = volumes
    return {
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {"template": template},
    }


def store_state(tmp_path, workload) -> lb.ExistingStateRecord:
    kubectl = FakeKubectl(**{"api-state": listing(workload)})
    records = observer(tmp_path, kubectl=kubectl).existing_state()
    return next(r for r in records if r.kind == "skypilot_api_server_state")


DB_ENV = [
    {
        "name": lo.DB_URI_VARIABLE,
        "valueFrom": {
            "secretKeyRef": {"name": "skypilot-api-db", "key": "connection-uri"}
        },
    }
]
SKY_MOUNT = [{"name": "sky-state", "mountPath": lo.SKY_STATE_PATH}]


def test_a_secret_referenced_database_is_recorded_as_durable_and_external(tmp_path):
    """The maintained manifest's own arrangement: a Postgres URI via secretKeyRef.

    Its identity is the secret and key, which is recordable; the URI itself is a
    credential and is not read.
    """
    record = store_state(tmp_path, api_workload(env=DB_ENV))
    assert record.handles == (
        (
            "deployment/skypilot-api state=external-database from secret "
            "skypilot-api-db/connection-uri"
        ),
    )
    assert record.resolved is True


def test_a_persistentvolumeclaim_is_recorded_as_node_bound_storage(tmp_path):
    record = store_state(
        tmp_path,
        api_workload(
            mounts=SKY_MOUNT,
            volumes=[
                {"name": "sky-state", "persistentVolumeClaim": {"claimName": "sky-pvc"}}
            ],
        ),
    )
    assert record.handles == (
        "deployment/skypilot-api state=sqlite-on-persistentvolumeclaim claim=sky-pvc",
    )


def test_an_emptydir_is_recorded_as_scratch_lost_on_redeploy(tmp_path):
    """The case that orphans running machines, named as such.

    k8s/40-skypilot-api.yaml mounts ~/.sky from an emptyDir it documents as "NOT a
    PersistentVolumeClaim". A name alone -- which is all the previous version
    recorded -- cannot distinguish this from the durable case.
    """
    record = store_state(
        tmp_path,
        api_workload(mounts=SKY_MOUNT, volumes=[{"name": "sky-state", "emptyDir": {}}]),
    )
    assert record.handles == (
        "deployment/skypilot-api state=sqlite-on-emptydir (scratch; lost on redeploy)",
    )


def test_an_inline_connection_uri_is_unresolved_rather_than_published(tmp_path):
    """An inline URI holds a password.

    Refusing it is the only option that neither publishes the credential nor
    records "external database" without the identity that claim rests on.
    """
    record = store_state(
        tmp_path,
        api_workload(
            env=[{"name": lo.DB_URI_VARIABLE, "value": "postgres://u:p@h/db"}]
        ),
    )
    assert record.resolved is False
    assert record.handles == ()
    assert "without publishing a credential" in record.unresolved_reason
    assert "postgres://" not in record.unresolved_reason


@pytest.mark.parametrize(
    ("workload", "expected"),
    [
        (api_workload(), f"no {lo.DB_URI_VARIABLE} and no volume mounted"),
        (
            api_workload(
                mounts=SKY_MOUNT, volumes=[{"name": "sky-state", "hostPath": {}}]
            ),
            "neither a persistentVolumeClaim nor an emptyDir",
        ),
        (api_workload(mounts=SKY_MOUNT, volumes=[]), "is not declared"),
    ],
    ids=["no-uri-no-volume", "unclassified-volume", "undeclared-volume"],
)
def test_an_unclassifiable_store_is_unresolved_not_guessed(
    tmp_path, workload, expected
):
    """Durability unknown is reported as unknown, in either direction."""
    record = store_state(tmp_path, workload)
    assert record.resolved is False
    assert expected in record.unresolved_reason


# ---------------------------------------------------------------------------
# Correlated membership: only this system's nodes, and what is in flight.
# ---------------------------------------------------------------------------


def test_an_unrelated_node_is_not_recorded_as_migration_state(tmp_path):
    """The reviewer's reproduction: a node the cluster merely happens to have.

    Draining someone else's node is a workload-affecting action, so listing every
    Kubernetes node as state the migration must handle is not a cosmetic error.
    """
    kubectl = FakeKubectl(
        nodes=listing(
            k8s_node(), {"kind": "Node", "metadata": {"name": "unrelated-managed-node"}}
        )
    )
    records = observer(tmp_path, kubectl=kubectl).existing_state()
    nodes = next(r for r in records if r.kind == "joined_eks_hybrid_nodes")
    assert nodes.handles == (f"node/{NODE} ssm/{INSTANCE} cluster/{SKY_CLUSTER}",)
    assert "unrelated-managed-node" not in " ".join(nodes.handles)
    # The exclusion is stated rather than silent: a reader can see that the
    # enumeration is narrower than the cluster's node list.
    assert "1" in nodes.enumerated_via


def test_a_partially_correlated_node_leaves_the_class_unresolved(tmp_path):
    """Named by a record but not correlated to a live cluster: not guessed either way.

    Recording it would assert migration state that may not exist; dropping it
    would hide a node that may need handling. Both are decisions this check is
    not entitled to make.
    """
    kubectl = FakeKubectl(
        **{"superplane-nodes": listing(superplane_node(skypilotCluster="sky-vanished"))}
    )
    records = observer(tmp_path, kubectl=kubectl).existing_state()
    nodes = next(r for r in records if r.kind == "joined_eks_hybrid_nodes")
    assert nodes.resolved is False
    assert nodes.handles == ()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"status": "INIT"}, "launch-in-progress/"),
        ({"to_down": True}, "teardown-pending/"),
    ],
    ids=["launching", "tearing-down"],
)
def test_an_operation_in_flight_is_reported(tmp_path, overrides, expected):
    """A cutover mid-operation is exactly what a handover plan must cover."""
    subject = observer(
        tmp_path, skypilot=FakeSkyPilot(**{"/status": sky_status(**overrides)})
    )
    clusters = next(
        r for r in subject.existing_state() if r.kind == "skypilot_clusters"
    )
    assert any(op.startswith(expected) for op in clusters.active_operations)


def test_a_steady_cluster_reports_no_operation_in_flight(tmp_path):
    clusters = next(
        r for r in observer(tmp_path).existing_state() if r.kind == "skypilot_clusters"
    )
    assert clusters.active_operations == ()


def test_a_multi_read_state_record_pins_every_file_it_names(tmp_path):
    """A record naming two retained reads must pin both of them.

    The SuperplaneNode CR record's handles come from the SuperplaneNode listing
    and the NodePool listing together. Publishing only the first file's digest
    left the second half of the evidence unverifiable -- a later reader could not
    tell whether the nodepool listing behind an enumerated handle had been
    altered. The digest must therefore change when either read changes.
    """
    records = observer(tmp_path).existing_state()
    crs = next(r for r in records if r.kind == "superplane_node_crs")
    named = [part.strip() for part in crs.evidence_reference.split(";")]
    assert len(named) == 2, crs.evidence_reference

    retained = {}
    for path in (tmp_path / "evidence.json.raw").iterdir():
        retained[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # The published digest is not any single constituent file's digest: it is a
    # commitment over both, so neither half can be swapped undetected.
    assert crs.evidence_sha256 not in retained.values()

    parts = [lo._Reference(name, retained[name.split("/")[-1]]) for name in named]
    assert lo._Reference.combined(*parts).digest == crs.evidence_sha256
    # Changing either constituent read changes the published digest.
    for index in range(len(parts)):
        mutated = list(parts)
        mutated[index] = lo._Reference(parts[index].reference, "f" * 64)
        assert lo._Reference.combined(*mutated).digest != crs.evidence_sha256


def test_the_state_enumeration_satisfies_the_captures_completeness_rule(tmp_path):
    """The observer's own output passes the rule the capture enforces."""
    _, report = captured(tmp_path)
    assert len(report["existing_state_for_u19"]) == len(lb.KNOWN_STATE_KINDS)


# ---------------------------------------------------------------------------
# Malformed and hostile responses are refused without echoing a body.
# ---------------------------------------------------------------------------


def test_a_non_array_status_response_is_refused(tmp_path):
    subject = observer(tmp_path, skypilot=FakeSkyPilot(**{"/status": {"clusters": []}}))
    with pytest.raises(EvidenceError, match="did not return an array"):
        subject.serving_inventory()


def test_malformed_json_does_not_echo_the_body(tmp_path):
    class Broken:
        def __call__(self, selector: str) -> bytes:
            return b'{"secret-body-marker": '

    subject = observer(tmp_path, skypilot=Broken())
    with pytest.raises(EvidenceError) as error:
        subject.serving_inventory()
    assert "secret-body-marker" not in str(error.value)
    assert error.value.__cause__ is None


def test_a_duplicate_json_field_is_refused(tmp_path):
    class Duplicated:
        def __call__(self, selector: str) -> bytes:
            return b'[{"name": "a", "name": "b"}]'

    subject = observer(tmp_path, skypilot=Duplicated())
    with pytest.raises(EvidenceError, match="duplicate field"):
        subject.serving_inventory()


def test_a_json_numeric_constant_is_refused(tmp_path):
    class Constant:
        def __call__(self, selector: str) -> bytes:
            return b'[{"name": "a", "autostop": NaN}]'

    subject = observer(tmp_path, skypilot=Constant())
    with pytest.raises(EvidenceError, match="non-JSON numeric constant"):
        subject.serving_inventory()


def test_a_hostile_service_name_cannot_reach_the_inventory(tmp_path):
    """The sanitiser applies to a retained record's contents too.

    A retained file is operator-supplied input, so it gets the same refusal as a
    wire response: a service name carrying what looks like a credential is refused
    rather than redacted into the published record.
    """
    receipts = tmp_path / "receipts"
    service_listing(receipts, name="Bearer abcdefghijklmnopqrstuvwxyz01")
    subject = observer(tmp_path, receipts_dir=str(receipts))
    with pytest.raises(EvidenceError, match="looks like a credential"):
        subject.serving_inventory()


# ---------------------------------------------------------------------------
# Retained raw evidence: referenced and hashed, never republished.
# ---------------------------------------------------------------------------


def test_raw_observations_are_retained_privately_beside_the_evidence(tmp_path):
    subject = observer(tmp_path)
    subject.observe(Dimension.NODE_REGISTRATION)
    directory = tmp_path / "evidence.json.raw"
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    retained = sorted(directory.iterdir())
    assert retained
    for path in retained:
        assert path.stat().st_mode & 0o777 == 0o600


def test_each_fact_references_a_retained_file_by_digest(tmp_path):
    import hashlib

    subject = observer(tmp_path)
    facts = subject.observe(Dimension.NODE_REGISTRATION)
    for fact in facts:
        name = fact.evidence_reference.split("/", 1)[1]
        content = (tmp_path / "evidence.json.raw" / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == fact.evidence_sha256


def test_a_read_is_performed_once_and_reused(tmp_path):
    kubectl = FakeKubectl()
    subject = observer(tmp_path, kubectl=kubectl)
    subject.observe(Dimension.NODE_REGISTRATION)
    subject.observe(Dimension.COST_AND_CLEANUP)
    assert kubectl.calls.count("superplane-nodes") == 1


def test_an_existing_retained_directory_is_refused(tmp_path):
    (tmp_path / "evidence.json.raw").mkdir()
    with pytest.raises(EvidenceError, match="retained-evidence directory"):
        observer(tmp_path)


def test_the_published_record_carries_references_not_bodies(tmp_path):
    _, report = captured(tmp_path)
    serialized = json.dumps(report)
    # A distinctive value from the raw fixture bodies must not appear.
    assert "10.0.0.4" not in serialized
    assert "gpu-h100-sxm" not in serialized
    assert "evidence.json.raw/" in serialized


def test_the_observer_reads_nothing_at_construction(tmp_path):
    skypilot, kubectl = FakeSkyPilot(), FakeKubectl()
    observer(tmp_path, skypilot=skypilot, kubectl=kubectl)
    assert skypilot.calls == []
    assert kubectl.calls == []


def test_build_observer_defaults_to_the_real_transports(tmp_path, monkeypatch):
    monkeypatch.setenv(lo.NAMESPACE_VARIABLE, "superplane")
    monkeypatch.setenv(lo.SKYPILOT_NAMESPACE_VARIABLE, "skypilot")
    subject = lo.build_observer(config(tmp_path))
    assert subject.transport_is_live() is True


def test_no_token_or_kubeconfig_value_is_retained_on_the_observer(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(lo.SKYPILOT_TOKEN_VARIABLE, "token-sentinel-not-a-credential")
    monkeypatch.setenv(lo.KUBECONFIG_VARIABLE, str(tmp_path / "kubeconfig"))
    subject = observer(tmp_path)
    rendered = json.dumps(
        {key: str(value) for key, value in vars(subject).items()}, default=str
    )
    assert "token-sentinel-not-a-credential" not in rendered
    assert os.environ[lo.SKYPILOT_TOKEN_VARIABLE] == "token-sentinel-not-a-credential"


# ---------------------------------------------------------------------------
# U12-201: evidence is authenticated by an independent producer, or it settles
# nothing.
#
# The finding these regress is not "a weak check". A prior version accepted
# caller-authored local JSON as authoritative operation evidence: the digest
# covered a sibling body nobody read, while the verdict came from a separate
# hand-written field. Supplying an arbitrary body and labelling the submission
# `controller.launch-decision` yielded SATISFIED for cheapest-first ordering, and
# reversing only the prices flipped it to REFUTED with a byte-identical reported
# digest. So these tests split into two questions kept deliberately separate:
# whether the bytes a verdict is computed from are the bytes that were hashed,
# and whether anyone independent vouches for those bytes.
# ---------------------------------------------------------------------------


def ordering_evidence(tmp_path, receipts, **overrides):
    """The ordering check's outcome as the observer reports it, or None."""
    facts = provider_dimension(tmp_path, receipts, **overrides)
    matching = [f for f in facts if f.check_id == "provider.ordering-cheapest-first"]
    return matching[0] if matching else None


def test_a_self_authored_receipt_no_producer_vouches_for_settles_nothing(tmp_path):
    """The reproduction's exact shape, now unable to yield a verdict.

    The body is well-formed, in-window, names the selected target and a machine
    this capture saw, and is correctly hashed -- everything the old screens
    checked. With no producer vouching for it, the honest answer is that nothing
    is established, and the report says why rather than falling back on the
    derived opinion.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0), attest=False)
    fact = ordering_evidence(tmp_path, receipts)
    assert fact.outcome is lb.Outcome.INDETERMINATE
    assert "unauthenticated" in fact.detail
    assert "no record of publishing evidence" in fact.detail


def test_a_lane_with_no_registered_client_settles_nothing(tmp_path, monkeypatch):
    """The shipped default: no lane registered at all.

    Distinct from a registered lane that has no record for the attempt, and worth
    separating because this is the state the module ships in -- so the reported
    reason has to name the missing registration rather than implying the lane was
    asked and had nothing.
    """
    monkeypatch.delitem(receipts_module.EVIDENCE_PRODUCERS, PRODUCER_LANE)
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0), attest=False)
    fact = ordering_evidence(tmp_path, receipts)
    assert fact.outcome is lb.Outcome.INDETERMINATE
    assert "No reviewed read-only producer client is registered" in fact.detail


def test_authentic_matching_evidence_satisfies_its_intended_check(tmp_path):
    """The positive control, and the reason this repair is a check at all.

    A repair that made every outcome INDETERMINATE would be indistinguishable
    from deleting the feature. Genuine evidence the lane vouches for has to be
    able to settle its criterion, so this is asserted directly and not merely
    implied by the negative cases.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    fact = ordering_evidence(tmp_path, receipts)
    assert fact.outcome is lb.Outcome.SATISFIED
    assert "cheapest first" in fact.detail
    assert "unauthenticated" not in fact.detail


def test_editing_an_observation_after_attestation_is_refused(tmp_path):
    """The reproduction's flip, now a contradiction between two sources.

    Reversing the prices is what used to change the verdict while the reported
    digest stayed identical. The observations now live in the hashed bytes, so
    the edit changes what the retained set hashes to and disagrees with what the
    lane published. That is a contradiction rather than a gap, so it refuses the
    capture instead of downgrading to indeterminate.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    body = receipts / "provider-ordering-cheapest-first.body"
    tampered = json.loads(body.read_text())
    tampered["observations"]["offered_options"] = [
        {"provider": PROVIDER, "hourly_price": price} for price in (4.0, 1.5)
    ]
    payload = json.dumps(tampered).encode()
    body.write_bytes(payload)
    submission = receipts / "provider-ordering-cheapest-first.json"
    pointer = json.loads(submission.read_text())
    # The declared digest is updated too, so this is the capable attacker: the
    # submission is fully self-consistent and only the producer disagrees.
    pointer["body_sha256"] = hashlib.sha256(payload).hexdigest()
    submission.write_text(json.dumps(pointer))
    with pytest.raises(EvidenceError, match="contradict each other"):
        provider_dimension(tmp_path, receipts)


def test_a_record_added_beside_attested_evidence_is_refused(tmp_path):
    """Why the producer vouches for the set rather than for each body.

    Per-body authentication would leave the directory's *composition*
    unattested: every genuine record would still verify while a forged listing
    sat beside them. Slipping in an extra record changes what the retained set
    hashes to, so the addition itself is what the producer contradicts.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    # Written directly, bypassing the helper's re-attestation, because an
    # attacker adding a file does not get to update the lane's published record.
    retained(
        receipts, authority="skyserve.status", services=[], name="serve", attest=False
    )
    with pytest.raises(EvidenceError, match="contradict each other"):
        provider_dimension(tmp_path, receipts)


def test_a_forged_empty_serving_listing_cannot_establish_verified_empty(tmp_path):
    """Serving absence is the one an unvouched file most wants to assert.

    Serving has no controller-side inventory to observe independently, so the
    listing is the only authority on which services exist -- which is exactly why
    an unauthenticated empty listing must not be able to establish that this
    baseline runs none. The capture blocks rather than recording a verified
    absence.
    """
    receipts = tmp_path / "receipts"
    retained(
        receipts,
        authority="skyserve.status",
        services=[],
        name="serve-status",
        attest=False,
    )
    subject = observer(tmp_path, receipts_dir=str(receipts))
    listing = subject.serving_inventory()
    assert listing is not None, "the listing is still read and retained"
    assert listing.services == ()
    assert listing.authenticated is False
    with pytest.raises(EvidenceError, match="BLOCKED: the serving inventory is unauth"):
        lb.capture(config(tmp_path, receipts_dir=str(receipts)), subject)


def test_an_unvouched_listing_cannot_lend_its_service_handles(tmp_path):
    """The second-order form: an unauthenticated listing naming a service.

    A serving check may name a service from the authoritative listing, because
    the capture has no other way to see one. If an unvouched listing could lend
    that handle, a forged serving submission would name it back and pass the
    resource screen using a handle the capture never independently observed.
    """
    receipts = tmp_path / "receipts"
    service_listing(receipts, name="sky-serve-llama", attest=False)
    retained(
        receipts,
        check_id="serving.endpoint-reachable",
        authority="skyserve.probe",
        resource={"skypilot_cluster": "sky-serve-llama"},
        facts={"authenticated": True, "status_code": 200},
        name="serving-probe",
        attest=False,
    )
    subject = observer(tmp_path, receipts_dir=str(receipts))
    with pytest.raises(EvidenceError, match="names no resource this capture observed"):
        subject.observe(Dimension.SERVING_WORKLOAD)


@pytest.mark.parametrize(
    "drift",
    [{"run_id": "run-someone-elses"}, {"attempt": 2}],
    ids=["another-run", "another-attempt"],
)
def test_evidence_from_a_different_run_or_attempt_is_not_authenticated(tmp_path, drift):
    """The lane is asked about the run the body names, not a nearby one.

    An attestation is *about* a run attempt. Evidence naming an attempt the lane
    published nothing for is a gap, not a contradiction -- the lane simply has no
    record -- so it stays unauthenticated diagnostics rather than refusing.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    # Re-point the body at a different attempt, leaving the lane's published
    # record where it was.
    body = receipts / "provider-ordering-cheapest-first.body"
    content = json.loads(body.read_text())
    content.update(drift)
    payload = json.dumps(content).encode()
    body.write_bytes(payload)
    submission = receipts / "provider-ordering-cheapest-first.json"
    pointer = json.loads(submission.read_text())
    pointer["body_sha256"] = hashlib.sha256(payload).hexdigest()
    submission.write_text(json.dumps(pointer))
    fact = ordering_evidence(tmp_path, receipts)
    assert fact.outcome is lb.Outcome.INDETERMINATE
    assert "no record of publishing evidence" in fact.detail


def test_evidence_must_say_which_run_attempt_it_came_from(tmp_path):
    """A body nobody could ever vouch for is refused, not accepted as unvouched.

    Without a run and attempt there is no question to put to a producer, so such
    a body is structurally unauthenticatable. Accepting it as permanently
    unvouched would make "unauthenticated" reachable by simply omitting a field.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, body_overrides={"run_id": None})
    with pytest.raises(EvidenceError, match="run_id"):
        provider_dimension(tmp_path, receipts)


def test_records_from_two_operations_cannot_be_attested_as_one_set(tmp_path):
    """One capture reads one authorized operation's evidence.

    Mixing attempts would let an attacker pick, per record, whichever attempt the
    lane happened to have a convenient published digest for.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    retained(
        receipts,
        authority="skyserve.status",
        services=[],
        name="serve-status",
        attempt=2,
    )
    with pytest.raises(EvidenceError, match="more than one run attempt"):
        provider_dimension(tmp_path, receipts)


def test_records_naming_two_producer_lanes_are_refused(tmp_path):
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    retained(
        receipts,
        authority="skyserve.status",
        services=[],
        name="serve-status",
        producer="some-other-lane",
    )
    with pytest.raises(EvidenceError, match="more than one producer lane"):
        provider_dimension(tmp_path, receipts)


def test_the_old_hand_written_fact_shape_is_refused_by_name(tmp_path):
    """Refused, not ignored, so the old behaviour cannot survive silently.

    A submission carrying `facts` beside the pointer is the reproduction's shape.
    Dropping it quietly would leave an operator believing their hand-written
    facts were honoured while the check read something else entirely.
    """
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    submission = receipts / "provider-ordering-cheapest-first.json"
    pointer = json.loads(submission.read_text())
    pointer["facts"] = {
        "offered_options": [{"provider": PROVIDER, "hourly_price": 1.0}]
    }
    submission.write_text(json.dumps(pointer))
    with pytest.raises(
        EvidenceError, match="must be inside the attested evidence body"
    ):
        provider_dimension(tmp_path, receipts)


def test_a_producer_answering_with_a_non_digest_is_refused(tmp_path):
    """A producer client is held to its contract, not trusted to be well-behaved."""
    receipts = tmp_path / "receipts"
    launch_record(receipts, prices=(1.5, 4.0))
    _PRODUCER.published[(RUN, 1)] = "not-a-sha256"
    with pytest.raises(EvidenceError, match="producer digest"):
        provider_dimension(tmp_path, receipts)


# ---------------------------------------------------------------------------
# The producer client itself: read-only by construction, and registered by
# authorization rather than by import.
# ---------------------------------------------------------------------------


def test_shipping_the_producer_client_does_not_register_a_lane():
    """Empty by default, for the same reason BASELINE_TARGETS is.

    Shipping a reviewed client is engineering; deciding which execution lane is
    authoritative for a baseline is authorization and stays the supervisor's. So
    importing this module must not make any evidence authenticatable.
    """
    assert receipts_module.EVIDENCE_PRODUCERS == {} or set(
        receipts_module.EVIDENCE_PRODUCERS
    ) == {PRODUCER_LANE}, "only this suite's own registration may be present"
    assert "producer" not in lb.BASELINE_TARGETS


def test_a_registered_producer_does_not_make_offline_fixtures_publishable(tmp_path):
    """Authentication is orthogonal to liveness, and neither implies the other.

    An authenticated body proves who produced the evidence. It says nothing about
    whether *this* capture read a live system, so the load-bearing property still
    holds: driving the real observer over offline transports is not live evidence
    no matter who vouched for the records it read.
    """
    receipts = tmp_path / "capture-receipts"
    retained(receipts, authority="skyserve.status", services=[], name="serve-status")
    subject = observer(tmp_path, receipts_dir=str(receipts))
    assert subject.serving_inventory().authenticated is True
    assert subject.transport_is_live() is False
    report = lb.capture(config(tmp_path, receipts_dir=str(receipts)), subject)
    assert report["evidence_kind"] == "offline-fixture"
    assert report["evidence_kind"] != "live"


# ---------------------------------------------------------------------------
# The evidence producer's GitHub integration (U12-202, U12-203).
#
# Two mechanical errors previously made the authenticated path unusable for
# honest evidence, and neither was caught by 379 passing tests -- because the
# suite drove a fake producer that returned the expected answer directly, which
# tests nothing about the integration. So these tests exercise the REAL
# WorkflowRunReads transport and parsers against sanitised documented response
# shapes and a real in-memory ZIP, and assert the exact URLs requested.
#
#   U12-202: artifacts were requested at
#     /actions/runs/{run}/attempts/{attempt}/artifacts, which GitHub does not
#     implement (confirmed 404 by read-only probe on this repository). The
#     attempt is instead established from the per-attempt record, and the
#     artifact bound to it by GitHub's own upload stamp -- the pattern PR #5544
#     arrived at for the same class of bug. Deliberately NOT from an artifact
#     run_attempt field: the API does not return one.
#
#   U12-203: GitHub's recorded digest authenticates the uploaded ZIP archive,
#     while the comparison was against the evidence set's manifest digest. Those
#     are different byte sequences, so a genuine artifact could never match. The
#     archive is now authenticated against GitHub's digest FIRST, and the
#     manifest derived from its verified contents.
# ---------------------------------------------------------------------------

PRODUCER_REPOSITORY = "aws-e/adp"
PRODUCER_WORKFLOW = ".github/workflows/superplane-baseline-evidence.yml"
PRODUCER_JOB = "publish-evidence"
PRODUCER_STEP = "Upload baseline evidence"
PRODUCER_RUN = "35501463427"
PRODUCER_REVISION = "c" * 40

# The attempt's own execution window, and an upload stamp inside it.
ATTEMPT_STARTED = "2026-09-20T10:00:00Z"
ATTEMPT_FINISHED = "2026-09-20T10:30:00Z"
UPLOADED_AT = "2026-09-20T10:20:00Z"


def evidence_archive(bodies: dict[str, bytes]) -> bytes:
    """A real ZIP archive of evidence bodies, as the reviewed lane publishes it.

    A real archive rather than a stub, because the whole point of U12-203 is that
    the archive's own bytes are what GitHub's digest authenticates. Its hash is
    computed from these bytes by the test and placed in the metadata, so the
    metadata and the archive agree the way they would for a genuine artifact.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in bodies.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


def attempt_record(**overrides) -> dict:
    """A sanitised copy of the documented per-attempt response shape.

    Fields are those GitHub really returns at
    /actions/runs/{run}/attempts/{attempt} -- the endpoint a read-only probe on
    this repository confirmed answers 200 while the artifacts-per-attempt path
    returns 404.
    """
    record = {
        "id": int(PRODUCER_RUN),
        "run_attempt": 1,
        "head_sha": PRODUCER_REVISION,
        "path": PRODUCER_WORKFLOW,
        "status": "completed",
        "conclusion": "success",
        "run_started_at": ATTEMPT_STARTED,
        "updated_at": ATTEMPT_FINISHED,
        "repository": {"full_name": PRODUCER_REPOSITORY},
    }
    record.update(overrides)
    return record


def jobs_record(**overrides) -> dict:
    """The documented jobs-for-attempt shape, with the publishing step present."""
    step = {"name": PRODUCER_STEP, "conclusion": "success"}
    step.update(overrides.pop("step", {}))
    job = {
        "name": PRODUCER_JOB,
        "conclusion": "success",
        "steps": [{"name": "Set up job", "conclusion": "success"}, step],
    }
    job.update(overrides.pop("job", {}))
    return {"jobs": [job], **overrides}


def artifacts_record(archive: bytes, **overrides) -> dict:
    """The documented per-run artifacts shape, digest matching the real archive.

    Note what is absent: no `run_attempt` under `workflow_run`. The API does not
    return that field, so binding to it rejected every genuine artifact. The
    binding here is `created_at` against the verified attempt's window.
    """
    artifact = {
        "name": lo.EVIDENCE_ARTIFACT_NAME,
        "expired": False,
        "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
        "created_at": UPLOADED_AT,
        "size_in_bytes": len(archive),
        "workflow_run": {"id": int(PRODUCER_RUN), "head_sha": PRODUCER_REVISION},
    }
    artifact.update(overrides.pop("artifact", {}))
    return {"artifacts": [artifact], **overrides}


class RecordingTransport:
    """Answers the real client's reads from documented shapes, recording URLs.

    The client builds the paths; this only maps them to canned bodies. So the
    test can assert the client asked GitHub a question GitHub can answer, which
    is exactly what U12-202 is about.
    """

    def __init__(self, responses: dict[str, object]):
        self._responses = responses
        self.requested: list[str] = []

    def __call__(self, path: str) -> bytes:
        self.requested.append(path)
        if path not in self._responses:
            raise EvidenceError(f"BLOCKED: no such endpoint: {path}")
        return json.dumps(self._responses[path]).encode()


def producer_client(tmp_path, *, responses: dict, archive: bytes | None = None):
    """The real WorkflowRunReads over an injected transport and a real archive."""
    directory = tmp_path / "archives"
    directory.mkdir(exist_ok=True)
    if archive is not None:
        (directory / f"{lo.EVIDENCE_ARTIFACT_NAME}.zip").write_bytes(archive)
    transport = RecordingTransport(responses)
    client = lo.WorkflowRunReads(
        "https://api.github.com",
        PRODUCER_REPOSITORY,
        workflow=PRODUCER_WORKFLOW,
        job=PRODUCER_JOB,
        step=PRODUCER_STEP,
        archive_dir=str(directory),
        transport=transport,
    )
    return client, transport


def lane_responses(archive: bytes, **overrides) -> dict:
    responses = {
        f"actions/runs/{PRODUCER_RUN}/attempts/1": attempt_record(
            **overrides.pop("attempt", {})
        ),
        f"actions/runs/{PRODUCER_RUN}/attempts/1/jobs": jobs_record(
            **overrides.pop("jobs", {})
        ),
        f"actions/runs/{PRODUCER_RUN}/artifacts": artifacts_record(
            archive, **overrides.pop("artifacts", {})
        ),
    }
    responses.update(overrides)
    return responses


def ask(client, *, run: str = PRODUCER_RUN, attempt: int = 1) -> str:
    return client.published_digest(run=run, attempt=attempt, revision=PRODUCER_REVISION)


# -- the positive control ---------------------------------------------------


def test_the_real_producer_transport_authenticates_a_genuine_archive(tmp_path):
    """THE POSITIVE CONTROL (U12-202, U12-203).

    The honest path must work, and it must work through the real transport,
    parsers and archive reader rather than a fake that returns the answer. This
    drives WorkflowRunReads over documented response shapes and a real ZIP whose
    independently computed hash matches its metadata, and asserts the value
    returned is the canonical manifest digest load_ledger computes from the same
    bodies.

    A check that can only ever answer "unestablished" is not a check, so this is
    the regression that would fail if either mismatch were reintroduced.
    """
    bodies = {
        "launch-decision.body": b'{"facts": [{"provider": "nebius"}]}',
        "serve-status.body": b'{"services": []}',
    }
    archive = evidence_archive(bodies)
    client, transport = producer_client(
        tmp_path, responses=lane_responses(archive), archive=archive
    )

    answer = ask(client)

    expected = receipts_module._manifest_digest(
        [
            (name, hashlib.sha256(content).hexdigest())
            for name, content in bodies.items()
        ]
    )
    assert answer == expected, "the producer must yield the ledger's manifest digest"
    # U12-202: the endpoints actually requested are the documented ones.
    assert transport.requested == [
        f"actions/runs/{PRODUCER_RUN}/attempts/1",
        f"actions/runs/{PRODUCER_RUN}/attempts/1/jobs",
        f"actions/runs/{PRODUCER_RUN}/artifacts",
    ]
    assert not any("attempts/1/artifacts" in path for path in transport.requested), (
        "GitHub has no per-attempt artifacts endpoint; it returns 404"
    )


def test_the_archive_digest_and_the_manifest_digest_are_not_the_same_value(tmp_path):
    """U12-203 stated as a regression: chaining, not comparing.

    GitHub's digest authenticates the ZIP; the ledger's digest covers the
    evidence set. Asserting they differ pins why comparing them directly could
    never authenticate a genuine artifact, and that the code now chains them.
    """
    bodies = {"launch-decision.body": b'{"facts": []}'}
    archive = evidence_archive(bodies)
    github_recorded = "sha256:" + hashlib.sha256(archive).hexdigest()
    manifest = lo.WorkflowRunReads.manifest_of_archive(
        archive, {"digest": github_recorded}
    )
    assert manifest != github_recorded.removeprefix("sha256:")
    assert manifest == receipts_module._manifest_digest(
        [
            (name, hashlib.sha256(content).hexdigest())
            for name, content in bodies.items()
        ]
    )


# -- negative controls: the archive ----------------------------------------


def test_an_archive_that_does_not_match_githubs_digest_is_refused(tmp_path):
    """The operator holds the file, so its bytes must answer to GitHub's record."""
    genuine = evidence_archive({"launch-decision.body": b'{"facts": []}'})
    substituted = evidence_archive({"launch-decision.body": b'{"facts": ["edited"]}'})
    client, _ = producer_client(
        tmp_path, responses=lane_responses(genuine), archive=substituted
    )
    with pytest.raises(EvidenceError, match="does not match the digest GitHub"):
        ask(client)


@pytest.mark.parametrize(
    "bodies",
    [
        {"launch-decision.body": b"{}", "extra-forged.body": b'{"services": []}'},
        {},
    ],
    ids=["added-member", "missing-member"],
)
def test_the_attested_set_covers_the_archives_whole_membership(tmp_path, bodies):
    """Addition and omission both change the one value GitHub's digest covers.

    This is why the attestation is of the set: an attacker unable to alter any
    single body could otherwise add a forged empty serve listing beside genuine
    records, or withhold the record that would have refuted a check.
    """
    reference = receipts_module._manifest_digest(
        [("launch-decision.body", hashlib.sha256(b"{}").hexdigest())]
    )
    archive = evidence_archive(bodies)
    if not bodies:
        with pytest.raises(EvidenceError, match="empty"):
            lo.WorkflowRunReads.manifest_of_archive(
                archive, {"digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
            )
        return
    answer = lo.WorkflowRunReads.manifest_of_archive(
        archive, {"digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
    )
    assert answer != reference, "an added member must change the attested value"


@pytest.mark.parametrize(
    "member",
    [
        "/etc/passwd.body",
        "../escape.body",
        "nested/dir.body",
        "..\\escape.body",
        " leading-space.body",
        "-rf",
    ],
    ids=[
        "absolute",
        "traversal",
        "nested",
        "windows-traversal",
        "leading-space",
        "option-like",
    ],
)
def test_an_unsafe_archive_member_name_is_refused(member):
    """Refused rather than skipped: reading it best-effort attests a subset.

    Screened by the same rule that validates a submission's ``body`` name, so a
    member this admitted could always be looked for on the ingestion side. A
    merely *uncontracted* member -- a well-named file the lane should not have
    included -- is not caught here but by the set digest, which every addition
    changes; see the whole-membership control above.
    """
    archive = evidence_archive({member: b"{}"})
    with pytest.raises(EvidenceError, match="member|zip archive"):
        lo.WorkflowRunReads.manifest_of_archive(
            archive, {"digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
        )


def test_a_body_that_is_not_a_zip_archive_is_refused():
    raw = b"not a zip archive at all"
    with pytest.raises(EvidenceError, match="zip archive"):
        lo.WorkflowRunReads.manifest_of_archive(
            raw, {"digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        )


def test_the_archive_directory_must_be_supplied(tmp_path):
    """Absent, the capture says which input is missing rather than passing."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    client, _ = producer_client(tmp_path, responses=lane_responses(archive))
    client._archive_dir = ""
    with pytest.raises(EvidenceError, match=lo.ARCHIVE_DIR_VARIABLE):
        ask(client)


# -- negative controls: the attempt binding -------------------------------


def test_an_artifact_uploaded_before_this_attempt_began_is_a_previous_attempts(
    tmp_path,
):
    """THE RE-RUN CASE, established from data GitHub really stamps.

    Attempts of a run are consecutive, so an artifact left by an earlier failed
    attempt is created before the later attempt started. That is the guarantee
    the invented artifact `run_attempt` field was reaching for.
    """
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(
        archive, artifacts={"artifact": {"created_at": "2026-09-20T09:00:00Z"}}
    )
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    assert ask(client) == "", "an earlier attempt's artifact is not this one's evidence"


def test_an_artifact_uploaded_after_the_attempt_finished_is_refused(tmp_path):
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(
        archive, artifacts={"artifact": {"created_at": "2026-09-20T11:00:00Z"}}
    )
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    assert ask(client) == ""


def test_an_attempt_that_ran_another_revision_is_refused(tmp_path):
    """A re-run dispatched against another head is a different execution."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive, attempt={"head_sha": "d" * 40})
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError, match="different revision"):
        ask(client)


def test_an_attempt_from_a_foreign_producer_workflow_is_refused(tmp_path):
    """An artifact of the right name from another workflow is not this lane's."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(
        archive, attempt={"path": ".github/workflows/unrelated.yml"}
    )
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError, match="reviewed evidence producer workflow"):
        ask(client)


def test_an_attempt_from_another_repository_is_refused(tmp_path):
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(
        archive, attempt={"repository": {"full_name": "someone/else"}}
    )
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError, match="another repository"):
        ask(client)


@pytest.mark.parametrize(
    "attempt_override",
    [
        {"run_attempt": 2},
        {"id": 999},
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"run_started_at": None},
        {"updated_at": "not-a-time"},
        {"run_started_at": ATTEMPT_FINISHED, "updated_at": ATTEMPT_STARTED},
    ],
    ids=[
        "another-attempt",
        "another-run",
        "failed-attempt",
        "still-running",
        "no-start-time",
        "unreadable-time",
        "inconsistent-window",
    ],
)
def test_an_attempt_record_that_does_not_describe_this_execution_is_refused(
    tmp_path, attempt_override
):
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive, attempt=attempt_override)
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError):
        ask(client)


# -- negative controls: the publishing step -------------------------------


@pytest.mark.parametrize(
    "jobs_override",
    [
        {"job": {"conclusion": "failure"}},
        {"step": {"conclusion": "failure"}},
        {"step": {"conclusion": "skipped"}},
        {"job": {"name": "some-other-job"}},
        {"job": {"steps": []}},
    ],
    ids=[
        "job-failed",
        "step-failed",
        "step-skipped",
        "no-publishing-job",
        "no-steps",
    ],
)
def test_a_lane_that_failed_to_publish_is_not_read_as_having_published_nothing(
    tmp_path, jobs_override
):
    """Failing to publish and publishing nothing are different facts.

    Left unchecked they are indistinguishable, and the caller treats an absent
    record as an honest gap -- so a run that died before its upload step would
    quietly read as "this lane produced no evidence".
    """
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive, jobs=jobs_override)
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError):
        ask(client)


# -- negative controls: the artifact listing -------------------------------


def test_an_artifact_the_lane_did_not_publish_as_evidence_is_ignored(tmp_path):
    """The artifact name is fixed, not chosen by the submission."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive, artifacts={"artifact": {"name": "build-logs"}})
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    assert ask(client) == ""


def test_an_expired_artifact_authenticates_nothing(tmp_path):
    """Its bytes are gone, so nothing can be authenticated against its digest."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive, artifacts={"artifact": {"expired": True}})
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    assert ask(client) == ""


def test_no_artifact_at_all_is_a_gap_not_an_error(tmp_path):
    """An attempt that published no evidence leaves the capture unauthenticated."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive)
    responses[f"actions/runs/{PRODUCER_RUN}/artifacts"] = {"artifacts": []}
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    assert ask(client) == ""


def test_two_evidence_artifacts_in_one_window_are_ambiguous(tmp_path):
    """Refused rather than resolved by picking one: the lane's record is unclear."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    listing = artifacts_record(archive)
    listing["artifacts"].append(dict(listing["artifacts"][0]))
    responses = lane_responses(archive)
    responses[f"actions/runs/{PRODUCER_RUN}/artifacts"] = listing
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError, match="ambiguous|more than one"):
        ask(client)


@pytest.mark.parametrize(
    "artifact_override",
    [
        {"digest": None},
        {"digest": "a" * 64},
        {"digest": "md5:abc"},
        {"created_at": None},
        {"size_in_bytes": 0},
    ],
    ids=[
        "no-digest",
        "unprefixed-digest",
        "another-algorithm",
        "no-upload-stamp",
        "empty-artifact",
    ],
)
def test_an_artifact_breaking_the_lanes_own_contract_is_named(
    tmp_path, artifact_override
):
    """A producer answering wrongly is named, not silently read as a gap."""
    archive = evidence_archive({"launch-decision.body": b"{}"})
    responses = lane_responses(archive, artifacts={"artifact": artifact_override})
    client, _ = producer_client(tmp_path, responses=responses, archive=archive)
    with pytest.raises(EvidenceError):
        ask(client)


@pytest.mark.parametrize(
    "malformed",
    [[], {"artifacts": {}}, {}],
    ids=["not-an-object", "artifacts-not-a-list", "no-artifacts-key"],
)
def test_a_malformed_artifact_listing_is_refused(malformed):
    window = {
        "attempt": 1,
        "started_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc),
    }
    client = lo.WorkflowRunReads(
        "https://api.github.com",
        PRODUCER_REPOSITORY,
        workflow=PRODUCER_WORKFLOW,
        job=PRODUCER_JOB,
        step=PRODUCER_STEP,
    )
    with pytest.raises(EvidenceError):
        client.recorded_artifact(malformed, window=window)


# -- the client's own configuration ---------------------------------------


def test_the_producer_endpoint_must_be_https_without_embedded_credentials():
    for hostile in ("http://lane.example", "https://user:pw@lane.example"):
        with pytest.raises(EvidenceError, match="BLOCKED: the evidence producer API"):
            lo.WorkflowRunReads(
                hostile,
                "owner/repo",
                workflow=PRODUCER_WORKFLOW,
                job=PRODUCER_JOB,
                step=PRODUCER_STEP,
            )


def test_the_producer_needs_a_readable_owner_repository():
    for hostile in ("owner", "owner/repo/extra", "owner/", "../etc/passwd"):
        with pytest.raises(EvidenceError, match="owner/repository"):
            lo.WorkflowRunReads(
                "https://lane.example",
                hostile,
                workflow=PRODUCER_WORKFLOW,
                job=PRODUCER_JOB,
                step=PRODUCER_STEP,
            )


@pytest.mark.parametrize("missing", ["workflow", "job", "step"])
def test_the_producer_needs_the_reviewed_workflow_job_and_step(missing):
    """Named rather than inferred, so a foreign lane cannot supply evidence."""
    arguments = {
        "workflow": PRODUCER_WORKFLOW,
        "job": PRODUCER_JOB,
        "step": PRODUCER_STEP,
    }
    arguments[missing] = "   "
    with pytest.raises(EvidenceError, match=f"reviewed {missing} name"):
        lo.WorkflowRunReads("https://lane.example", "owner/repo", **arguments)


def test_the_producer_refuses_to_read_without_a_token(monkeypatch):
    monkeypatch.delenv(lo.PRODUCER_TOKEN_VARIABLE, raising=False)
    client = lo.WorkflowRunReads(
        "https://lane.example",
        "owner/repo",
        workflow=PRODUCER_WORKFLOW,
        job=PRODUCER_JOB,
        step=PRODUCER_STEP,
    )
    with pytest.raises(EvidenceError, match=lo.PRODUCER_TOKEN_VARIABLE):
        ask(client)


@pytest.mark.parametrize(
    ("run", "attempt"),
    [("../../etc", 1), ("run 1", 1), ("run-1", 0), ("run-1", 10_000)],
    ids=["traversal", "space", "zero-attempt", "absurd-attempt"],
)
def test_the_producer_is_asked_about_one_readable_run_attempt(run, attempt):
    """The run and attempt reach a URL path, so they are bounded before use."""
    client = lo.WorkflowRunReads(
        "https://lane.example",
        "owner/repo",
        workflow=PRODUCER_WORKFLOW,
        job=PRODUCER_JOB,
        step=PRODUCER_STEP,
    )
    with pytest.raises(EvidenceError, match="BLOCKED: the producer is asked about"):
        client.published_digest(run=run, attempt=attempt, revision=PRODUCER_REVISION)


def test_the_producer_is_asked_about_one_exact_deployed_revision():
    client = lo.WorkflowRunReads(
        "https://lane.example",
        "owner/repo",
        workflow=PRODUCER_WORKFLOW,
        job=PRODUCER_JOB,
        step=PRODUCER_STEP,
    )
    with pytest.raises(EvidenceError, match="40-hex deployed revision"):
        client.published_digest(run="run-1", attempt=1, revision="main")


def test_the_real_producer_transport_refuses_a_redirect_away_from_the_endpoint():
    """A Location can carry the bearer token forward, so redirects are refused."""
    assert isinstance(lo._NoRedirect(), urllib.request.HTTPRedirectHandler)
    with pytest.raises(EvidenceError, match="redirect"):
        lo._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://evil.example"
        )


# -- the whole chain, real client into real ledger -------------------------


def test_the_real_producer_client_authenticates_a_real_ledger_end_to_end(
    tmp_path, monkeypatch
):
    """THE INTEGRATION CONTROL: real WorkflowRunReads registered as the producer.

    Every other ledger test registers ``FakeProducer``, which returns the expected
    digest directly -- and that is precisely how two mismatched contracts survived
    379 passing tests. This registers the REAL client, with only its HTTP
    transport injected, and drives ``load_ledger`` through it: the archive is
    authenticated against GitHub's recorded digest, its members yield the manifest
    digest, and that must equal what the retained submissions on disk hash to.

    So this asserts the honest path end to end. If the endpoint, the archive-hash
    chaining, or the manifest keying regressed, this fails -- which the pre-fix
    code does.
    """
    receipts = tmp_path / "receipts"
    # Written first, because the archive must contain exactly these bodies: the
    # producer vouches for the set the operator retained, not for a rearrangement.
    teardown_record(receipts, run=PRODUCER_RUN, attempt=1, attest=False)
    bodies = {path.name: path.read_bytes() for path in sorted(receipts.glob("*.body"))}
    archive = evidence_archive(bodies)
    client, transport = producer_client(
        tmp_path, responses=lane_responses(archive), archive=archive
    )
    monkeypatch.setitem(receipts_module.EVIDENCE_PRODUCERS, PRODUCER_LANE, client)
    # The ledger asks the producer about the deployed revision under verification,
    # so the lane's attempt must report that same commit.
    monkeypatch.setitem(
        transport._responses,
        f"actions/runs/{PRODUCER_RUN}/attempts/1",
        attempt_record(head_sha=REVISION),
    )

    provider = FakeProvider("terminated")
    absence = absence_fact(tmp_path, receipts, provider)

    assert absence.outcome is lb.Outcome.SATISFIED, (
        "the real producer client must be able to authenticate genuine evidence"
    )
    assert absence.provider_absence_confirmed is True
    assert transport.requested, "the real transport must have been exercised"


def test_the_real_producer_client_refuses_an_edited_body_end_to_end(
    tmp_path, monkeypatch
):
    """The paired negative, so the control above is not passing for a weak reason.

    The same chain, with one evidence body edited after the archive was published.
    GitHub's digest still authenticates the archive, but the retained set no longer
    hashes to what the archive contains, so the two sources contradict.
    """
    receipts = tmp_path / "receipts"
    teardown_record(receipts, run=PRODUCER_RUN, attempt=1, attest=False)
    bodies = {path.name: path.read_bytes() for path in sorted(receipts.glob("*.body"))}
    archive = evidence_archive(bodies)
    client, transport = producer_client(
        tmp_path, responses=lane_responses(archive), archive=archive
    )
    monkeypatch.setitem(receipts_module.EVIDENCE_PRODUCERS, PRODUCER_LANE, client)
    monkeypatch.setitem(
        transport._responses,
        f"actions/runs/{PRODUCER_RUN}/attempts/1",
        attempt_record(head_sha=REVISION),
    )
    # Edited after publication, and its submission digest updated to match, so the
    # only thing that can detect it is the producer's attestation of the set.
    target = next(iter(sorted(receipts.glob("*.body"))))
    edited = json.loads(target.read_text())
    edited["observations"]["teardown_succeeded"] = "definitely"
    payload = json.dumps(edited).encode()
    target.write_bytes(payload)
    submission = target.with_suffix(".json")
    pointer = json.loads(submission.read_text())
    pointer["body_sha256"] = hashlib.sha256(payload).hexdigest()
    submission.write_text(json.dumps(pointer))

    with pytest.raises(EvidenceError, match="contradict|different evidence digest"):
        absence_fact(tmp_path, receipts, FakeProvider("terminated"))


def test_an_unvouched_teardown_record_never_reaches_the_provider(tmp_path):
    """U12-201, second order: the provider read must not launder forged evidence.

    Provider confirmation is the strongest signal this capture has -- it is the one
    thing an operator's own file may not assert. But the *instance id* the provider
    is asked about comes from the record. An unauthenticated record naming any
    long-terminated machine would therefore come back "confirmed absent", turning
    the independent read into a laundering step for the very evidence it exists to
    check, and setting `provider_absence_confirmed` off unvouched material.

    So an unauthenticated record is not taken to the provider at all. It stays the
    indeterminate the ledger already made it, and the provider is never asked.
    """
    receipts = tmp_path / "receipts"
    teardown_record(receipts, attest=False)
    provider = FakeProvider(state="terminated")
    absence = absence_fact(tmp_path, receipts, provider)
    assert absence.outcome is lb.Outcome.INDETERMINATE
    assert absence.provider_absence_confirmed is False
    assert "unauthenticated" in absence.detail
    assert provider.asked == [], "the provider must not be asked about unvouched ids"


def test_an_authenticated_teardown_record_still_reaches_the_provider(tmp_path):
    """The paired positive control, so the gate above is not a blanket refusal.

    Preserving the existing direct-provider-confirmation behaviour is explicitly
    required, so it is asserted here rather than left to the untouched tests: a
    vouched-for record is taken to the provider and its absence is confirmed.
    """
    receipts = tmp_path / "receipts"
    teardown_record(receipts)
    provider = FakeProvider(state="terminated")
    absence = absence_fact(tmp_path, receipts, provider)
    assert absence.outcome is lb.Outcome.SATISFIED
    assert absence.provider_absence_confirmed is True
    assert provider.asked, "the provider is asked about an authenticated instance id"
