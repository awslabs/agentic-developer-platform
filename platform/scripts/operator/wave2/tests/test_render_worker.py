#!/usr/bin/env python3
"""Tests for render_worker_job -- the protected fixture worker (issue #3968).

WHAT THESE TESTS ARE ABOUT
--------------------------
The fixture worker is only useful if the GATEWAY'S OWN VERIFIER accepts it. That
verifier (`modules/gateway/src/agentauth/workload.py`) is strict and its refusals
are indistinguishable from the feature being broken: one extra env entry named
ADP_AGENT_AUTHORITY_ENABLED, a `command` override, or a digest that is not on the
approved list all produce the same opaque "workload refused" -- after the fixture
exists and the operator has paid for the setup.

So every test here is about a condition of that verifier, and asserts the renderer
refuses BEFORE producing an object, naming the condition. Two properties matter
equally:

  * a composition the verifier would reject must not be rendered at all, and
  * the composition that IS rendered must not have lost anything from the live
    template -- the projected bootstrap token, its audience, the keys mount, the
    securityContext. That is why it is a deep copy: hand-assembly is how the
    gateway fixture lost nine secret references.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import copy

import pytest

import render_fixture
from conftest import (
    APPROVED_WORKER_DIGEST,
    WORKER_CONTROL_ENDPOINT,
    live_worker_template,
)
from render_fixture import RenderError, render_worker_job

RUN_ID = "w2-20260924-0130"
NONCE = "deadbeefcafe0123"
JOB_NAME = "w2-fixture-worker-20260924-0130"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-w2-fixture-x.fifo"
IMAGE = f"123.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@{APPROVED_WORKER_DIGEST}"


def render(template: dict | None = None, **overrides):
    kwargs = dict(
        run_id=RUN_ID,
        nonce=NONCE,
        name=JOB_NAME,
        namespace=render_fixture.WORKER_NAMESPACE,
        image=IMAGE,
        control_endpoint=WORKER_CONTROL_ENDPOINT,
        queue_url=QUEUE_URL,
        approved_digests=[APPROVED_WORKER_DIGEST],
    )
    kwargs.update(overrides)
    return render_worker_job(
        template if template is not None else live_worker_template(), **kwargs)


def container_of(job: dict) -> dict:
    spec = job["spec"]["template"]["spec"]
    return next(c for c in spec["containers"] if c["name"] == render_fixture.WORKER_CONTAINER)


def env_of(job: dict) -> dict:
    return {e["name"]: e.get("value") for e in container_of(job)["env"]}


# ---------------------------------------------------------------------------
# it renders at all -- the headline change
# ---------------------------------------------------------------------------
def test_a_worker_job_is_actually_produced() -> None:
    """The previous revision refused this outright and hardcoded worker_job_created:false.

    That refusal was correct when written -- every https control endpoint routed to
    the ORDINARY gateway's pods by label, so a fixture worker would have bootstrapped
    against live traffic. #5836's fixture-only edge removes the constraint, so the
    refusal goes with it.
    """
    job, report = render()
    assert job["kind"] == "Job"
    assert job["metadata"]["name"] == JOB_NAME
    assert job["metadata"]["namespace"] == render_fixture.WORKER_NAMESPACE
    assert report["image_digest"] == APPROVED_WORKER_DIGEST


def test_the_control_endpoint_is_the_fixture_edge_not_the_ordinary_one() -> None:
    """The one value the whole change exists to set.

    The live template carries the ORDINARY endpoint. Copying it forward unchanged
    would produce a worker that bootstraps against production -- which is exactly
    what made this Job unsafe to create before #5836.
    """
    job, _ = render()
    assert env_of(job)["ADP_AGENT_CONTROL_ENDPOINT"] == WORKER_CONTROL_ENDPOINT
    assert "ordinary" not in env_of(job)["ADP_AGENT_CONTROL_ENDPOINT"]
    assert env_of(job)["SIGV4_PROXY_TARGET"] == WORKER_CONTROL_ENDPOINT.removesuffix("/internal/v1/agent") + "/agent"


def test_a_non_https_control_endpoint_is_refused() -> None:
    """run_identity.py rejects any other scheme before attempting a bootstrap.

    So an in-cluster http://…:8080 address cannot be used even deliberately, and
    rendering one would produce a worker that fails at its first call for a reason
    having nothing to do with the software under test.
    """
    with pytest.raises(RenderError, match="not https"):
        render(control_endpoint="http://w2-fixture-gateway.adp-gateway.svc:8080/internal/v1/agent")


def test_every_queue_reference_points_at_the_runs_own_queue() -> None:
    """No fixture traffic may reach the shared submit queue, in either direction.

    Three separate variables are read by three different code paths; overriding one
    and inheriting the other two would leave the fixture publishing to production.
    """
    env = env_of(render()[0])
    for var in ("QUEUE_URL", "ADP_RUN_TASK_QUEUE_URL", "AGENT_DISPATCH_QUEUE_URL"):
        assert env[var] == QUEUE_URL, var


# ---------------------------------------------------------------------------
# the verifier's conditions, each one a refusal
# ---------------------------------------------------------------------------
def test_a_worker_outside_adp_agents_is_refused() -> None:
    """The namespace is half of the TokenReview username the gateway compares.

    Relocating the fixture worker would require changing the ORDINARY gateway's
    AGENT_WORKER_NAMESPACE, which this evaluation must not do -- so a fixture in
    another namespace cannot authenticate, and is refused here rather than there.
    """
    with pytest.raises(RenderError, match="only 'adp-agents'"):
        render(namespace="adp-gateway")


def test_a_different_service_account_is_refused() -> None:
    """Checked twice by the verifier: the TokenReview username AND spec.serviceAccountName."""
    template = live_worker_template()
    template["spec"]["serviceAccountName"] = "default"
    with pytest.raises(RenderError, match="agent-authority-worker-sa"):
        render(template)


@pytest.mark.parametrize("field_name,value", [
    ("command", ["/bin/sh"]),
    ("args", ["-c", "sleep 3600"]),
])
def test_a_substituted_entry_point_is_refused(field_name: str, value: list) -> None:
    """This is the condition that makes the fixture worker the REAL program.

    The verifier refuses any pod that sets command or args, so there is no way to
    render a worker that both authenticates and does something other than what the
    image does. A debug shell cannot hold protected authority.
    """
    template = live_worker_template()
    template["spec"]["containers"][0][field_name] = value
    with pytest.raises(RenderError, match="overrides the entry point"):
        render(template)


def test_a_duplicate_authority_flag_is_refused() -> None:
    """The verifier compares the FILTERED LIST for equality, not membership.

    Kubernetes itself accepts a duplicated env name (last wins), so this composition
    would deploy happily and then be refused at bootstrap. The renderer replaces
    rather than appends for exactly this reason; the test pins the behaviour by
    handing it a template that already carries a duplicate.
    """
    template = live_worker_template()
    template["spec"]["containers"][0]["env"] += [
        {"name": render_fixture.AUTHORITY_FLAG, "value": "true"},
    ]
    with pytest.raises(RenderError, match="exactly"):
        render(template)


def test_a_template_authority_flag_from_a_secret_is_replaced_with_a_literal() -> None:
    """`valueFrom` is not `value`, and the verifier's equality check sees the difference.

    A reviewer reading such a manifest would call it correct; the gateway would not.
    The renderer REPLACES the entry rather than leaving whatever the template had, so
    a template carrying a secret-sourced flag still yields the one literal entry the
    verifier's list comparison accepts.
    """
    template = live_worker_template()
    for entry in template["spec"]["containers"][0]["env"]:
        if entry["name"] == render_fixture.AUTHORITY_FLAG:
            entry.pop("value")
            entry["valueFrom"] = {"secretKeyRef": {"name": "s", "key": "k"}}
    job, _ = render(template)
    flags = [e for e in container_of(job)["env"]
             if e["name"] == render_fixture.AUTHORITY_FLAG]
    assert flags == [{"name": render_fixture.AUTHORITY_FLAG, "value": "true"}]


def test_assert_verifier_admissible_rejects_a_valuefrom_authority_flag() -> None:
    """The check runs on the RENDERED object, so it does not depend on the renderer.

    Exercised directly because the renderer cannot be made to emit this shape -- which
    is the point. If a later edit to the override logic ever let a valueFrom entry
    survive, this assertion is what refuses the object.
    """
    job, _ = render()
    container = container_of(job)
    container["env"] = [
        e for e in container["env"] if e["name"] != render_fixture.AUTHORITY_FLAG
    ] + [{"name": render_fixture.AUTHORITY_FLAG,
          "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]
    with pytest.raises(RenderError, match="exactly"):
        render_fixture.assert_verifier_admissible(job)


def test_more_than_one_agent_worker_container_is_refused() -> None:
    """`len(worker_specs) != 1` refuses, in both spec and status."""
    template = live_worker_template()
    template["spec"]["containers"].append(copy.deepcopy(template["spec"]["containers"][0]))
    with pytest.raises(RenderError, match="container"):
        render(template)


def test_a_renamed_container_is_refused() -> None:
    """The container NAME is what the verifier filters on; it is not cosmetic."""
    template = live_worker_template()
    template["spec"]["containers"][0]["name"] = "worker"
    with pytest.raises(RenderError, match="container"):
        render(template)


# ---------------------------------------------------------------------------
# the image: pinned is not the same as APPROVED
# ---------------------------------------------------------------------------
def test_a_tag_referenced_image_is_refused() -> None:
    with pytest.raises(RenderError, match="not digest-pinned"):
        render(image="123.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime:latest")


def test_a_digest_that_is_not_on_the_approved_list_is_refused() -> None:
    """THE distinction. Pinned to some digest is not pinned to an ALLOWED digest.

    The verifier compares the pod's resolved imageID against
    AGENT_WORKER_IMAGE_DIGESTS. A fixture cannot add to that list -- it is
    Terraform-gated -- so an unapproved digest is a bootstrap refusal that arrives
    only after the fixture is live.
    """
    other = "sha256:" + "ef" * 32
    with pytest.raises(RenderError, match="not on the approved list"):
        render(image=f"123.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@{other}")


def test_an_empty_approved_list_is_refused_rather_than_treated_as_permissive() -> None:
    """An empty allowlist admits nothing, so it must not be read as admitting everything.

    This is the vacuous-pass shape: `digest in []` is False for every digest, and a
    renderer that skipped the check when the list was empty would produce a worker
    no gateway can accept while reporting the check as satisfied.
    """
    with pytest.raises(RenderError, match="approved worker image digest list is empty"):
        render(approved_digests=[])


def test_a_whitespace_only_approved_list_is_also_empty() -> None:
    """Splitting a comma-separated env var yields [''] when it is unset, not []."""
    with pytest.raises(RenderError, match="approved worker image digest list is empty"):
        render(approved_digests=["", "  "])


# ---------------------------------------------------------------------------
# what must be CARRIED OVER, not re-listed
# ---------------------------------------------------------------------------
def test_the_projected_bootstrap_token_survives_the_copy() -> None:
    """Without it the worker authenticates as nothing.

    Asserted on the output rather than trusted, because the deep copy is the whole
    mechanism: a renderer that rebuilt the volumes list would drop it silently.
    """
    job, _ = render()
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    sources = volumes[render_fixture.WORKLOAD_VOLUME]["projected"]["sources"]
    token = sources[0]["serviceAccountToken"]
    assert token["audience"] == render_fixture.BOOTSTRAP_AUDIENCE
    assert token["path"] == "token"
    assert render_fixture.CONTROL_KEYS_VOLUME in volumes


def test_a_template_without_the_projected_token_is_refused() -> None:
    """Agent authority off on the live cluster means there is nothing to copy.

    Synthesising the volume here would produce a worker whose token is minted for an
    audience the gateway may not be configured to review -- a fixture that fails for
    a reason the evaluation would then have to explain away.
    """
    with pytest.raises(RenderError, match="audience"):
        render(live_worker_template(authority=False))


def test_a_token_projected_for_another_audience_is_refused() -> None:
    """TokenReview is called with one audience and rejects every other."""
    template = live_worker_template()
    volume = next(v for v in template["spec"]["volumes"]
                  if v["name"] == render_fixture.WORKLOAD_VOLUME)
    volume["projected"]["sources"][0]["serviceAccountToken"]["audience"] = "sts.amazonaws.com"
    with pytest.raises(RenderError, match="audience"):
        render(template)


def test_a_writable_credential_mount_is_refused() -> None:
    """A process that can write its own identity material has asserted, not proved."""
    template = live_worker_template()
    mounts = template["spec"]["containers"][0]["volumeMounts"]
    next(m for m in mounts if m["mountPath"] == render_fixture.WORKLOAD_TOKEN_DIR)["readOnly"] = False
    with pytest.raises(RenderError, match="not readOnly"):
        render(template)


def test_a_literal_pod_ip_is_refused() -> None:
    """The control listener binds to POD_IP explicitly and never to 0.0.0.0.

    A literal is a claim about where this pod is; only the API server's downwardAPI
    value is an observation.
    """
    template = live_worker_template()
    env = template["spec"]["containers"][0]["env"]
    for entry in env:
        if entry["name"] == "POD_IP":
            entry.pop("valueFrom")
            entry["value"] = "10.0.0.1"
    with pytest.raises(RenderError, match="status.podIP"):
        render(template)


def test_a_missing_pod_ip_is_refused() -> None:
    """Absent POD_IP means the worker starts NO listener, so there is nothing to measure."""
    template = live_worker_template()
    template["spec"]["containers"][0]["env"] = [
        e for e in template["spec"]["containers"][0]["env"] if e["name"] != "POD_IP"
    ]
    with pytest.raises(RenderError, match="POD_IP"):
        render(template)


def test_the_security_context_is_carried_over_unchanged() -> None:
    """Not in the override list, so it must survive byte-for-byte."""
    template = live_worker_template()
    job, _ = render(template)
    spec = job["spec"]["template"]["spec"]
    assert spec["securityContext"] == template["spec"]["securityContext"]
    assert container_of(job)["securityContext"] == \
        template["spec"]["containers"][0]["securityContext"]
    assert container_of(job)["resources"] == template["spec"]["containers"][0]["resources"]


def test_the_live_template_is_not_mutated() -> None:
    """A deep copy, so rendering twice from one template cannot drift."""
    template = live_worker_template()
    before = copy.deepcopy(template)
    render(template)
    assert template == before


# ---------------------------------------------------------------------------
# the pod-identity projection -- the replacement for an in-worker kubectl
# ---------------------------------------------------------------------------
def test_the_pod_uid_is_projected_through_the_downward_api() -> None:
    """The protected SA cannot get or list pods, and that boundary stays closed.

    metadata.uid is the one identity the process cannot rewrite for itself, and a
    downwardAPI volume fieldRef supports it. It does NOT support
    spec.serviceAccountName -- which is why an earlier revision's dependence on a
    `service-account.name` file could never have worked.
    """
    job, report = render()
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    items = volumes[render_fixture.POD_IDENTITY_VOLUME]["downwardAPI"]["items"]
    fields = {item["path"]: item["fieldRef"]["fieldPath"] for item in items}
    assert fields["pod-uid"] == "metadata.uid"
    assert report["pod_identity_projection"]["mount"] == render_fixture.POD_IDENTITY_DIR


def test_the_projection_uses_only_supported_field_refs() -> None:
    """A downwardAPI volume supports exactly annotations, labels, name, namespace, uid.

    Anything else is rejected by the API server at creation, so an unsupported
    fieldPath here would be a fixture that cannot be created at all.
    """
    supported = {"metadata.annotations", "metadata.labels", "metadata.name",
                 "metadata.namespace", "metadata.uid"}
    job, _ = render()
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    for item in volumes[render_fixture.POD_IDENTITY_VOLUME]["downwardAPI"]["items"]:
        assert item["fieldRef"]["fieldPath"] in supported, item


def test_the_identity_mount_is_read_only() -> None:
    job, _ = render()
    mount = next(m for m in container_of(job)["volumeMounts"]
                 if m["mountPath"] == render_fixture.POD_IDENTITY_DIR)
    assert mount["readOnly"] is True


# ---------------------------------------------------------------------------
# labels, ownership and bounded lifetime
# ---------------------------------------------------------------------------
def test_the_fixture_worker_does_not_wear_the_ordinary_workers_label() -> None:
    """`app.kubernetes.io/name: agent-scaledjob` is what the ORDINARY policies select.

    Wearing it would put a control-enabled fixture pod inside the ordinary
    agent-control-listener-ingress allowlist -- widening a production boundary to
    make a fixture convenient. The fixture gets its own policies instead.
    """
    job, _ = render()
    for labels in (job["metadata"]["labels"], job["spec"]["template"]["metadata"]["labels"]):
        assert labels.get("app.kubernetes.io/name") != "agent-scaledjob"
        assert labels["adp.io/w2-fixture"] == RUN_ID
        assert labels["adp.io/w2-nonce"] == NONCE


def test_the_pod_carries_the_run_and_nonce_labels_the_fixture_policy_selects() -> None:
    """render_policies selects worker pods by `adp.io/w2-fixture`; the Job must set it
    on the POD template, not only on the Job, or the policy matches nothing."""
    job, _ = render()
    assert job["spec"]["template"]["metadata"]["labels"]["adp.io/w2-fixture"] == RUN_ID


def test_the_job_does_not_retry() -> None:
    """A replacement pod is a DIFFERENT uid.

    The whole identity binding is to one uid, so a silent retry would leave the
    operator's recorded identity describing a pod that no longer exists while a new
    one runs unobserved.
    """
    job, _ = render()
    assert job["spec"]["backoffLimit"] == 0


def test_the_job_has_a_bounded_lifetime() -> None:
    """The backstop for the case where cleanup never runs.

    A control-enabled workload that outlives the evaluation is the thing the ledger
    exists to prevent; this is the layer that does not depend on the ledger.
    """
    job, _ = render(deadline_seconds=900)
    assert job["spec"]["activeDeadlineSeconds"] == 900
    assert render()[0]["spec"]["activeDeadlineSeconds"] == 1800


def test_task_api_worker_uses_fixture_identity_and_endpoints():
    template = live_worker_template()
    container = template['spec']['containers'][0]
    container['env'].extend([
        {'name': 'ADP_TASK_API_WORKER_ENABLED', 'value': 'false'},
        {'name': 'ADP_TASK_API_QUEUE_URL', 'value': 'https://example/ordinary.fifo'},
        {'name': 'ADP_GATEWAY_ENDPOINT', 'value': 'https://ordinary.invalid'},
    ])
    before = copy.deepcopy(template)
    job, _ = render(template)
    env = env_of(job)
    assert env['ADP_TASK_API_WORKER_ENABLED'] == 'true'
    assert env['ADP_TASK_API_QUEUE_URL'] == QUEUE_URL
    assert env['ADP_GATEWAY_ENDPOINT'] == WORKER_CONTROL_ENDPOINT.removesuffix('/internal/v1/agent')
    assert template == before
