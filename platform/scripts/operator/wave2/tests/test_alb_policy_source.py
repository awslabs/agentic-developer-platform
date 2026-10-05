#!/usr/bin/env python3
"""The fixture policy must admit #5836's ALB, and only this run's (issue #3968, item 5).

Root's item 5: "Coordinate ALB-to-pod NetworkPolicy ... with active #5836." The
producer landed at e3c7e6f1 with the `fixture_alb_network_policy_source` output, and
root's instruction on consuming it: "Consume it from the complete Terraform output
document, validate it against this run/edge identity, and compose/observe the fixture
gateway policy before admitting the worker stage. Keep the ordinary policy unchanged
and retain both policy UID gates. Require fresh ALB address observation before
execution; stale allowances can refer to reassigned addresses."

WHY THIS SEAM NEEDS TESTS AT ALL, given nothing errors without it:

`render_policies` admits callers by `namespaceSelector`. An ALB with `target-type: ip`
connects from its OWN elastic network interfaces, which belong to the load balancer
and to no pod and no namespace -- so no selector matches it at any width. Without an
`ipBlock` rule the fixture gateway drops the edge's connections, and NOTHING IN
TERRAFORM OR KUBERNETES ERRORS: the plan applies and the Ingress reconciles, because a
NetworkPolicy is not a validation of anything reachable. The worker's bootstrap
handshake simply never completes.

(An earlier revision of this docstring also claimed the ALB would keep reporting its
targets HEALTHY. It would not, and root corrected the same claim in #5836's output
description: with IP targets the health check takes the SAME path to the same pod port
as a real request, so a policy denying the ALB denies the health check too and the
targets go unhealthy. That matters practically -- unhealthy targets are a real signal
an operator can act on. What misleads is the bootstrap timeout downstream, so that is
what these tests argue from.)

That is the whole risk. The run would read as "the protected worker failed its
bootstrap" -- which is precisely the conclusion Wave 2 exists to establish or refute
-- reached from a networking artefact. A wrong measurement that looks like a finding
is worse than no measurement, so every test below asserts a refusal that names that
consequence rather than merely returning an error.

The division of ownership these tests encode: #5836 OBSERVES the addresses (the
renderer cannot know them -- they belong to a load balancer that did not exist when
the policy was first rendered), `edge_receipt` BINDS the document to this run, and
`render_policies` RENDERS. Neither side can silently widen the other.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/test_alb_policy_source.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, WAVE2 / "lib" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


er = _load("w2_edge_receipt", "edge_receipt.py")
rf = _load("w2_render_fixture", "render_fixture.py")

NONCE = "a1b2c3d4e5f60718"
ACCOUNT = "879318057152"
REGION = "us-east-1"
ENVIRONMENT = "w2fixture"
API_ID = "abc123xyz0"
RUN_ID = "w2-20260924-0130"
GW_NS = "adp-gateway"
AGENT_NS = "adp-agents"
GW_LABEL = "w2-fixture-gateway-20260924-0130"
POLICY_NAME = "w2-fixture-policy-20260924-0130"

# The two /32s an ALB in two subnets publishes. Two rather than one because an ALB
# has one interface per subnet it occupies, and a renderer that emitted only the
# first would produce an INTERMITTENT bootstrap failure -- the worst possible shape
# for this defect, since half the connections would succeed.
CIDRS = ["10.0.10.41/32", "10.0.11.52/32"]


def alb_source(**over) -> dict:
    """#5836's `fixture_alb_network_policy_source` value, in its published shape.

    Field names and shapes are taken from the producer's coordination comment
    (issue #3968 comment 5811020437, producer e3c7e6f1) rather than invented, so a
    rename on their side fails these tests instead of silently going unread.
    """
    doc = {
        "source_cidrs": list(CIDRS),
        "source_ips": [c.split("/")[0] for c in CIDRS],
        "container_port": 8080,
        "alb_listener_port": 80,
        "run_nonce": NONCE,
        "alb_arn": (
            "arn:aws:elasticloadbalancing:us-east-1:879318057152:"
            "loadbalancer/app/w2-fixture/abc123"),
        "apply_to": "the run-scoped FIXTURE gateway NetworkPolicy",
        "verify": "aws ec2 describe-network-interfaces --filters ...",
    }
    doc.update(over)
    return doc


def outputs_doc(*, source=..., **over) -> dict:
    """The whole `terraform output -json` document, every output wrapped.

    The wrapped shape is the one that matters: `terraform output -json ownership`
    cannot carry this output at all, which is the same trap the endpoint had.
    """
    doc = {
        "ownership": {"value": {
            "run_nonce": NONCE, "account_id": ACCOUNT, "region": REGION,
            "environment": ENVIRONMENT, "rest_api_id": API_ID, "resources": [],
        }, "type": "object"},
        "rest_api_id": {"value": API_ID, "type": "string"},
        "worker_control_endpoint": {"value": (
            f"https://{API_ID}.execute-api.{REGION}.amazonaws.com/"
            f"{ENVIRONMENT}/internal/v1/agent"), "type": "string"},
    }
    if source is not ...:
        doc[er.ALB_SOURCE_FIELD] = {"value": source, "type": "object"}
    else:
        doc[er.ALB_SOURCE_FIELD] = {"value": alb_source(), "type": "object"}
    doc.update(over)
    return doc


FIXTURE_ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/w2-fixture/abc123")


def resolved(*, source=..., doc=..., nonce: str = NONCE, account: str = ACCOUNT,
             region: str = REGION, environment: str = ENVIRONMENT,
             alb_arn: str = FIXTURE_ALB_ARN, **kw) -> dict:
    """Resolve with every binding supplied, so a test varies exactly one thing.

    Every binding is a named default rather than an omission: root found that the
    first revision took only the nonce, so a receipt from another account's edge
    resolved cleanly (review of 8a8d81b8). Passing them all here means a test that
    forgets one gets the valid value, not a waived check.
    """
    receipt = er.normalize_outputs(outputs_doc(source=source) if doc is ... else doc)
    return er.resolve_alb_policy_source(
        receipt, run_nonce=nonce, account_id=account, region=region,
        environment=environment, expected_alb_arn=alb_arn, **kw)


def policies(alb=None) -> list[dict]:
    return rf.render_policies(
        run_id=RUN_ID, nonce=NONCE, policy_name=POLICY_NAME,
        gateway_namespace=GW_NS, agent_namespace=AGENT_NS,
        gateway_label=GW_LABEL, worker_label=GW_LABEL, alb_source=alb)


def ingress_of(policy: dict) -> list[dict]:
    return policy["spec"]["ingress"]


# ---------------------------------------------------------------------------
# the value is read from the WHOLE outputs document, as the endpoint is
# ---------------------------------------------------------------------------
def test_the_alb_source_comes_out_of_the_full_outputs_document() -> None:
    """Same producer document, two consumers, one read.

    The endpoint and the ALB source are BOTH separate top-level outputs, so a
    consumer that read `terraform output -json ownership` would get neither. That
    trap already cost this consumer one round trip; this pins that the working shape
    carries both.
    """
    receipt = er.normalize_outputs(outputs_doc())
    assert receipt["_shape"] == "terraform-output-json"
    endpoint = er.resolve_worker_endpoint(
        receipt, run_nonce=NONCE, account_id=ACCOUNT, region=REGION,
        environment=ENVIRONMENT)
    assert endpoint.endswith("/internal/v1/agent")
    source = er.resolve_alb_policy_source(
        receipt, run_nonce=NONCE, account_id=ACCOUNT, region=REGION,
        environment=ENVIRONMENT, expected_alb_arn=FIXTURE_ALB_ARN)
    assert source["cidrs"] == CIDRS
    assert source["container_port"] == 8080
    assert source["alb_arn"] == FIXTURE_ALB_ARN


def test_the_bare_ownership_receipt_still_cannot_supply_it() -> None:
    """`terraform output -json ownership` carries neither the endpoint nor this."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        er.normalize_outputs({"run_nonce": NONCE, "resources": [], "teardown": {}})
    assert "bare `ownership` receipt" in str(exc.value)


def test_an_applied_edge_that_publishes_no_source_is_refused_with_the_consequence() -> None:
    """The case that produced a silent denial, now a loud refusal.

    The message must name the bootstrap-failure consequence, because an operator who
    sees "no ALB source" and shrugs will get a run that looks like a real finding.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=None)
    message = str(exc.value)
    assert "publish no" in message
    assert "belong to no" in message and "namespace" in message
    assert "bootstrap" in message


# ---------------------------------------------------------------------------
# freshness: the nonce INSIDE the source document
#
# Root: "Require fresh ALB address observation before execution; stale allowances
# can refer to reassigned addresses." #5836 puts run_nonce inside this output for
# exactly this check. Re-checking it here is what makes that field do its job --
# trusting the enclosing document's binding would let a hand-pasted source block
# through on the strength of the endpoint's binding.
# ---------------------------------------------------------------------------
def test_a_source_document_from_another_run_is_refused() -> None:
    """A pasted stale value: the receipt is this run's, the addresses are not.

    Those interfaces may since have been released and REASSIGNED, in this same VPC.
    So admitting them both denies this run's edge and admits whatever holds the
    address now.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(run_nonce="ffffffffffffffff"))
    message = str(exc.value)
    assert "belongs to run" in message
    assert "reassigned" in message


def test_a_source_document_with_no_nonce_cannot_be_shown_to_be_fresh() -> None:
    """Absent, not merely wrong: indistinguishable from a stale paste."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(run_nonce=""))
    assert "records no run_nonce" in str(exc.value)


def test_the_binding_cannot_be_waived_by_omitting_the_expectation() -> None:
    """A missing expectation must refuse, never skip the comparison it exists for.

    The same defect root found in #5836's own backend check, and the same rule
    `resolve_worker_endpoint` applies to its four binding fields: `run_nonce=""`
    would otherwise waive the freshness check entirely.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(nonce="")
    assert "must not waive the check it exists for" in str(exc.value)


# ---------------------------------------------------------------------------
# width: every "tempting fix" #5836 enumerated is refused
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cidr,why", [
    ("0.0.0.0/0", "admits every other ALB in the VPC -- wider than the ordinary plane"),
    ("10.0.10.0/24", "the SUBNET: both ordinary gateway ALBs sit in it, so this would "
                     "admit PRODUCTION's edge to the fixture"),
    # .40, not .41: a /31 starting at an odd address has host bits set and would be
    # refused by PARSING rather than by width, which is a different check. This case
    # exists to prove the width check itself catches "two addresses".
    ("10.0.10.40/31", "still more than one address"),
    ("10.0.0.0/8", "the whole VPC range and then some"),
])
def test_anything_wider_than_a_single_address_is_refused(cidr, why) -> None:
    """Narrowed automatically would be worse: it would hide a producer defect.

    The subnet case is the one worth stating out loud. It looks like the careful
    answer -- narrower than 0.0.0.0/0, derived from real infrastructure -- and #5836
    verified read-only in dev that both ordinary gateway ALBs share a subnet with
    the fixture's. So a subnet rule admits production's edge to the fixture, which
    is strictly worse than the namespaceSelector it replaced.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(source_cidrs=[cidr]))
    message = str(exc.value)
    assert "rather than a single address" in message, why
    assert "subnet" in message


def test_a_bare_address_with_no_prefix_is_refused(cidr="10.0.10.41") -> None:
    """`ipaddress` accepts a bare address as a /32; an ipBlock.cidr field does not.

    Worth its own case because #5836 publishes BOTH `source_cidrs` (suffixed) and
    `source_ips` (not), so reading the wrong one is the likeliest way to get here --
    and the resulting manifest would be rejected by the API server mid-run.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(source_cidrs=[cidr]))
    assert "carries no prefix length" in str(exc.value)


@pytest.mark.parametrize("cidr", [
    "not-an-ip/32",
    "10.0.10.999/32",
    "10.0.10/32",
    "10.0.10.41/32/32",
    "2001:db8::1/128",
    "10.0.10.41/33",
    " /32",
])
def test_an_address_that_is_not_a_cidr_is_refused_by_parsing_it(cidr) -> None:
    """Root's first finding on 8a8d81b8: `endswith("/32")` is not CIDR parsing.

    `["not-an-ip/32"]` satisfied the old suffix check, reached the manifest, and would
    then be rejected by the API SERVER -- mid-run, after the gateway and its policies
    already exist, with the whole NetworkPolicy failing rather than the one rule. A
    string that merely looks like an address is worse than a missing one, because it
    passes review.

    The bare address and the IPv6 case are here for the same reason: neither can be
    caught by matching a suffix, and an ipBlock cannot carry a v6 address alongside v4.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(source_cidrs=[cidr]))
    assert "not a valid IPv4 CIDR" in str(exc.value)


def test_an_address_with_host_bits_set_is_refused_rather_than_truncated() -> None:
    """`10.0.10.41/24` is not a network, and silently masking it to 10.0.10.0/24
    would produce the subnet rule this whole check exists to refuse.

    `strict=True` is what makes this a refusal instead of a truncation.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(source_cidrs=["10.0.10.41/24"]))
    assert "not a valid IPv4 CIDR" in str(exc.value)


def test_two_spellings_of_one_address_collapse_to_one_rule_entry() -> None:
    """Normalised through the parser, so the duplicate check cannot be evaded.

    Leading zeros are refused outright by `ipaddress` (ambiguous octal), so the
    surviving equivalence to pin is whitespace, which the producer's sort could not
    have introduced but a hand-edit can.
    """
    source = resolved(source=alb_source(
        source_cidrs=[CIDRS[0], f"  {CIDRS[0]}  ", CIDRS[1]]))
    assert source["cidrs"] == CIDRS


def test_an_empty_address_list_is_refused_rather_than_rendered() -> None:
    """A policy admitting nothing denies the edge -- the original failure, restated."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(source_cidrs=[]))
    assert "non-empty list" in str(exc.value)


def test_a_malformed_entry_is_refused_rather_than_skipped() -> None:
    """Skipping it would admit SOME of the ALB's interfaces.

    That renders an intermittent bootstrap failure -- connections succeed or fail by
    which interface the ALB happened to use -- which is the hardest possible version
    of this defect to diagnose and the easiest to misread as flakiness in the
    software under test.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(source_cidrs=[CIDRS[0], None]))
    assert "not an address" in str(exc.value)
    assert "intermittent" in str(exc.value)


def test_duplicate_addresses_collapse_rather_than_repeat() -> None:
    """Idempotent in the harmless direction: a repeated /32 is one rule entry."""
    source = resolved(source=alb_source(source_cidrs=[CIDRS[0], CIDRS[0], CIDRS[1]]))
    assert source["cidrs"] == CIDRS


# ---------------------------------------------------------------------------
# the port confusion the producer publishes two names to prevent
# ---------------------------------------------------------------------------
def test_the_rule_names_the_container_port_not_the_listener_port() -> None:
    """The ALB listens on 80 and connects to the pod on 8080.

    A rule naming 80 blocks precisely the flow under test, so #5836 publishes the
    two under separate names and asserts they differ. This is the consumer half of
    that contract.
    """
    source = resolved()
    assert source["container_port"] == 8080
    assert source["alb_listener_port"] == 80
    rule = ingress_of(policies(source)[0])[1]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]
    assert all(p["port"] != 80 for p in rule["ports"])


def test_a_document_whose_two_ports_are_equal_is_refused() -> None:
    """#5836 asserts they differ, so equality means edited or misread."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(container_port=80, alb_listener_port=80))
    assert "same value" in str(exc.value)


# ---------------------------------------------------------------------------
# "differs from the listener port" was never the property that mattered
#
# Root's second finding on 8a8d81b8: container_port=8081 with alb_listener_port=80
# passed every check -- both integers, and they differ -- and rendered a rule
# admitting the load balancer to a port NOTHING SERVES. The property is not
# "different from the listener", it is "the port the fixture pod actually serves".
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("port", [8081, 80, 443, 8770])
def test_a_container_port_the_fixture_does_not_serve_is_refused(port) -> None:
    """Each of these is a usable TCP port and none of them is what the pod serves.

    8770 is included because it is a port this fixture genuinely uses (the worker
    control listener), so it is the most plausible wrong answer -- and a rule naming
    it would look deliberate to a reviewer.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(container_port=port, alb_listener_port=8999))
    message = str(exc.value)
    assert "pods serve 8080" in message
    assert "nothing is listening on" in message


def test_the_expected_port_is_the_one_the_renderer_actually_uses() -> None:
    """The check is only worth anything if its expectation tracks the rendered object.

    Compared to the Service the renderer emits rather than to a literal, so a change
    to the fixture's port fails here instead of silently making the ALB rule wrong.
    """
    assert er.FIXTURE_CONTAINER_PORT == rf.FIXTURE_POD_PORT
    _, service, _ = rf.render_gateway(
        __import__("conftest").live_deployment(), run_id=RUN_ID, nonce=NONCE,
        name=GW_LABEL, namespace=GW_NS,
        image=f"1.dkr.ecr.{REGION}.amazonaws.com/x@sha256:{'ab' * 32}",
        queue_url=f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/q.fifo")
    assert service["spec"]["ports"][0]["targetPort"] == rf.FIXTURE_POD_PORT


def test_a_hand_assembled_source_cannot_bypass_the_port_check_at_the_renderer() -> None:
    """The renderer asserts the same identity, so both halves must agree.

    A caller that built an `alb_source` dict itself -- bypassing the resolver -- would
    otherwise reach the manifest with any port at all.
    """
    with pytest.raises(rf.RenderError) as exc:
        policies({"cidrs": list(CIDRS), "container_port": 8081})
    assert "renders the fixture Service" in str(exc.value)


# ---------------------------------------------------------------------------
# the ARN: the only field that says WHICH load balancer was observed
#
# Root's third finding. A null ARN was accepted and normalised to "", and a FOREIGN
# ARN passed as long as the nonce matched -- so "this run" was established while
# "this run's ALB" never was. #5836 reads the interfaces of whatever ALB its
# `fixture_alb_arn` names, so the nonce cannot stand in for the ARN.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("arn", [None, "", "   ", 12345, [], {}])
def test_a_source_that_does_not_name_its_load_balancer_is_refused(arn) -> None:
    """#5836 emits "" when the ALB could not be read, which means the addresses
    are not this edge's either -- so an empty ARN is not a formatting detail."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(alb_arn=arn))
    assert "does not say which load balancer" in str(exc.value)


def test_addresses_read_from_another_load_balancer_are_refused() -> None:
    """Same account, same region, same nonce, different ALB.

    This is the case the nonce cannot catch and the ARN can: an operator who pointed
    #5836 at the wrong `fixture_alb_arn` gets a document that is genuinely this run's
    and genuinely describes someone else's load balancer.
    """
    other = ("arn:aws:elasticloadbalancing:us-east-1:879318057152:"
             "loadbalancer/app/k8s-adpgatew-ordinary/deadbeef")
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(alb_arn=other))
    message = str(exc.value)
    assert "DIFFERENT load balancer" in message
    assert "matching run_nonce does not make it this run's ALB" in message


def test_the_expected_arn_cannot_be_omitted_to_waive_the_comparison() -> None:
    """The same rule as every other expectation here: absent must refuse."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(alb_arn="")
    assert "must not waive the check it exists for" in str(exc.value)


@pytest.mark.parametrize("arn,why", [
    ("arn:aws:elasticloadbalancing:eu-west-1:879318057152:loadbalancer/app/w2-fixture/abc123",
     "another region's ALB has interfaces from another VPC entirely"),
    ("arn:aws:elasticloadbalancing:us-east-1:000000000000:loadbalancer/app/w2-fixture/abc123",
     "another account's ALB likewise"),
])
def test_an_alb_outside_this_run_account_or_region_is_refused(arn, why) -> None:
    """An ARN is self-describing, so a ledger recording a cross-account ALB is
    caught here rather than at apply time.

    Both the expectation and the document are set to the foreign ARN, so they MATCH:
    this is not the mismatch check firing again, it is the account/region binding.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(alb_arn=arn), alb_arn=arn)
    message = str(exc.value)
    assert "but this run is bound to account" in message, why


def test_a_value_that_is_not_an_arn_at_all_is_refused() -> None:
    """Refused for the reason that matters: its account and region cannot be read."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(alb_arn="w2-fixture-alb"), alb_arn="w2-fixture-alb")
    assert "is not an ARN" in str(exc.value)


# ---------------------------------------------------------------------------
# the receipt's own bindings, on the path that renders the policy
#
# Root's fourth structural finding: this resolver's docstring claimed
# resolve_worker_endpoint had already bound the document, but the renderer's CLI
# called load_receipt and came straight here. A docstring is not a prerequisite.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,wrong", [
    ("account_id", "000000000000"),
    ("region", "eu-west-1"),
    ("environment", "prod"),
])
def test_a_receipt_from_another_account_region_or_environment_is_refused(field, wrong) -> None:
    """Each varied alone, against the document's `ownership` half.

    The account matters most here: another account's edge publishes addresses from
    another VPC, and an ipBlock naming them admits nothing this cluster can carry
    while denying the edge that exists.
    """
    doc = outputs_doc()
    doc["ownership"]["value"][field] = wrong
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(doc=doc)
    assert "does not belong to this run" in str(exc.value)


@pytest.mark.parametrize("field", ["account_id", "region", "environment"])
def test_no_binding_expectation_can_be_omitted_on_this_path(field) -> None:
    """A missing expectation refuses rather than skipping its comparison."""
    kw = {"account_id": "account", "region": "region", "environment": "environment"}
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(**{kw[field].replace("account_id", "account"): ""}
                 if field == "account_id" else {field: ""})
    assert "must not waive the check it exists for" in str(exc.value)


def test_the_two_consumers_of_this_document_apply_the_same_binding() -> None:
    """One function, called by both, so they cannot drift apart.

    The endpoint resolver and the ALB resolver bind the same four fields for the same
    reason; the first revision had the check in only one of them.
    """
    doc = outputs_doc()
    doc["ownership"]["value"]["account_id"] = "000000000000"
    receipt = er.normalize_outputs(doc)
    for call in (
        lambda: er.resolve_worker_endpoint(
            receipt, run_nonce=NONCE, account_id=ACCOUNT, region=REGION,
            environment=ENVIRONMENT),
        lambda: er.resolve_alb_policy_source(
            receipt, run_nonce=NONCE, account_id=ACCOUNT, region=REGION,
            environment=ENVIRONMENT, expected_alb_arn=FIXTURE_ALB_ARN),
    ):
        with pytest.raises(er.EdgeReceiptError) as exc:
            call()
        assert "does not belong to this run" in str(exc.value)


@pytest.mark.parametrize("port", [None, "", "8080", 0, -1, "eighty-eighty"])
def test_an_unusable_container_port_refuses_rather_than_defaulting(port) -> None:
    """A defaulted port would silently block the flow under test."""
    with pytest.raises(er.EdgeReceiptError):
        resolved(source=alb_source(container_port=port))


@pytest.mark.parametrize("field", ["source_cidrs", "container_port", "alb_listener_port"])
def test_each_required_field_is_individually_required(field) -> None:
    """Dropped one at a time, so none is satisfied by another's presence."""
    assert field in er.ALB_SOURCE_REQUIRED_FIELDS
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source=alb_source(**{field: None}))
    assert field in str(exc.value)


def test_a_scalar_source_cannot_be_bound_to_a_run() -> None:
    with pytest.raises(er.EdgeReceiptError) as exc:
        resolved(source="10.0.10.41/32")
    assert "not an object" in str(exc.value)


# ---------------------------------------------------------------------------
# the rendered rule: exactly this, on the fixture policy only
# ---------------------------------------------------------------------------
def test_the_rule_matches_the_shape_the_producer_asked_for() -> None:
    """One ipBlock peer per address, TCP, container port. #5836's requested shape."""
    rule = ingress_of(policies(resolved())[0])[1]
    assert rule == {
        "from": [{"ipBlock": {"cidr": CIDRS[0]}}, {"ipBlock": {"cidr": CIDRS[1]}}],
        "ports": [{"protocol": "TCP", "port": 8080}],
    }


def test_the_alb_rule_is_separate_from_the_in_cluster_rule() -> None:
    """Not another `from` entry on the existing rule.

    Merging them would pair the ALB's addresses with the existing namespaceSelectors
    under ONE `ports` clause: it reads as wider than it is, and it would silently
    follow any future change to that rule's ports -- so a later edit intended for
    in-cluster callers would move the ALB's allowance with it.
    """
    with_alb = ingress_of(policies(resolved())[0])
    without = ingress_of(policies()[0])
    assert len(with_alb) == len(without) + 1
    # The in-cluster rule is untouched, and still admits nothing by ipBlock.
    assert with_alb[0] == without[0]
    assert all("ipBlock" not in peer for peer in with_alb[0]["from"])
    # The new rule admits ONLY by ipBlock -- no namespaceSelector smuggled in.
    assert all(set(peer) == {"ipBlock"} for peer in with_alb[1]["from"])


def test_no_source_renders_the_policy_unchanged() -> None:
    """The gateway stage runs BEFORE the edge exists, so this is the normal case.

    It is also why admitting the ALB is a second rendering rather than a parameter
    the first rendering could have taken: the addresses belong to a load balancer
    that does not exist yet.
    """
    assert policies(None) == policies()
    assert len(ingress_of(policies(None)[0])) == 1


def test_only_the_fixture_gateway_policy_gains_the_rule() -> None:
    """The worker policy is byte-identical, and neither policy's egress moves.

    The ordinary gateway's policy is never rendered by this tooling at all, so the
    strongest available statement is that nothing beyond the one fixture ingress
    rule differs.
    """
    plain_gw, plain_worker = policies()
    edge_gw, edge_worker = policies(resolved())
    assert edge_worker == plain_worker
    assert edge_gw["spec"]["egress"] == plain_gw["spec"]["egress"]
    assert edge_gw["spec"]["podSelector"] == plain_gw["spec"]["podSelector"]
    assert edge_gw["metadata"] == plain_gw["metadata"]
    # And the fixture policy still selects the FIXTURE gateway, not the ordinary one.
    assert edge_gw["spec"]["podSelector"]["matchLabels"]["app"] == GW_LABEL


def test_the_rule_is_not_rendered_from_an_unvalidated_document() -> None:
    """The renderer refuses too, so the two sides cannot silently widen each other.

    `render_policies` is reachable directly, so if it trusted whatever dict it was
    handed, a caller that skipped `resolve_alb_policy_source` would render a rule
    from an unbound document. It renders; it does not decide -- but it does refuse
    what it cannot render safely.
    """
    with pytest.raises(rf.RenderError) as exc:
        policies({"cidrs": [], "container_port": 8080})
    assert "admits nothing" in str(exc.value)
    # Any port that is not the one this function renders, including values that are
    # not ports at all. `True` is an int in Python, and `8080.0 == 8080` compares
    # equal, so both are caught by requiring the exact rendered int.
    for port in (80.5, True, 8081, None, "8080"):
        with pytest.raises(rf.RenderError) as exc:
            policies({"cidrs": CIDRS, "container_port": port})
        assert "renders the fixture Service" in str(exc.value)


def test_the_rendered_policy_is_valid_kubernetes_yaml_shaped_json() -> None:
    """Round-trips, and the rule sits where the API expects it."""
    gw = policies(resolved())[0]
    assert json.loads(json.dumps(gw)) == gw
    assert gw["apiVersion"] == "networking.k8s.io/v1"
    assert "Ingress" in gw["spec"]["policyTypes"]


# ---------------------------------------------------------------------------
# the CLI seam, because a library nothing reaches is a placeholder
#
# render_fixture.py is invoked as a subprocess by 10-create-fixture.sh, so the
# resolver being correct is only half of it: if the flag did not exist, or accepted
# the receipt without its nonce, the checks above would all pass while the rendered
# policy still denied the edge.
# ---------------------------------------------------------------------------
def edge_flags(tmp_path: Path, **over) -> list[str]:
    """The complete set of --edge-* flags, so a test overrides exactly one.

    A helper rather than a literal list in each test because root's fourth finding
    was precisely a flag that could disagree with another (`--run-nonce-expect`
    against `--nonce`); building them from one place is what keeps "all of them
    supplied" the default rather than something each test remembers.
    """
    flags = {
        "--edge-receipt": str(tmp_path / "edge-outputs.json"),
        "--edge-account-id": ACCOUNT,
        "--edge-region": REGION,
        "--edge-environment": ENVIRONMENT,
        "--edge-alb-arn": FIXTURE_ALB_ARN,
    }
    flags.update(over)
    return [part for flag, value in flags.items() if value is not None
            for part in (flag, value)]


def _render(tmp_path: Path, *extra: str, receipt: dict | None = ...) -> tuple[int, str, Path]:
    """Invoke render_fixture's main() the way the shell does, in-process."""
    live = tmp_path / "live.json"
    if not live.exists():
        from conftest import live_deployment  # noqa: PLC0415
        live.write_text(json.dumps(live_deployment()))
    if receipt is not ...:
        (tmp_path / "edge-outputs.json").write_text(json.dumps(receipt))
    out = tmp_path / f"out{len(list(tmp_path.iterdir()))}"
    digest = "sha256:" + "ab" * 32
    argv = [
        "--live-deployment", str(live), "--run-id", RUN_ID, "--nonce", NONCE,
        "--name", GW_LABEL, "--namespace", GW_NS, "--agent-namespace", AGENT_NS,
        "--image", f"1.dkr.ecr.{REGION}.amazonaws.com/x@{digest}",
        "--queue-url", f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/q.fifo",
        "--policy-name", POLICY_NAME, "--out-dir", str(out), *extra,
    ]
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = rf.main(argv)
    return rc, err.getvalue(), out


def test_the_cli_renders_the_alb_rule_from_the_receipt(tmp_path) -> None:
    """The whole outputs document in, one ipBlock rule out.

    A path of addresses rather than a `--alb-cidr` flag, deliberately: an operator
    typing addresses bypasses the freshness and width checks entirely, and a stale
    typed address produces a denial that reads as the finding under test.
    """
    rc, err, out = _render(tmp_path, *edge_flags(tmp_path), receipt=outputs_doc())
    assert rc == 0, err
    rendered = json.loads((out / "00-policies.json").read_text())
    ingress = rendered["items"][0]["spec"]["ingress"]
    assert len(ingress) == 2
    assert ingress[1]["from"] == [{"ipBlock": {"cidr": c}} for c in CIDRS]
    assert ingress[1]["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_the_cli_without_a_receipt_renders_the_in_cluster_policy(tmp_path) -> None:
    """The gateway stage's normal invocation, unchanged by this work."""
    rc, err, out = _render(tmp_path)
    assert rc == 0, err
    rendered = json.loads((out / "00-policies.json").read_text())
    assert len(rendered["items"][0]["spec"]["ingress"]) == 1


@pytest.mark.parametrize("omit", [
    "--edge-receipt", "--edge-account-id", "--edge-region",
    "--edge-environment", "--edge-alb-arn",
])
def test_the_cli_refuses_a_partial_edge_binding(tmp_path, omit) -> None:
    """All-or-nothing, like the worker inputs, and for the same reason.

    Each flag dropped ALONE, so none is satisfied by another's presence. Root's
    review of 8a8d81b8 found this path taking only a receipt and a nonce, which meant
    a document from another account's edge rendered cleanly: the account, region and
    ARN comparisons simply did not exist on the path that writes the manifest.
    """
    rc, err, out = _render(
        tmp_path, *edge_flags(tmp_path, **{omit: None}), receipt=outputs_doc())
    assert rc == 1, f"{omit} was not required"
    assert "needs all of" in err
    assert not out.exists(), "manifests were written despite the refusal"


def test_the_cli_refuses_a_stale_receipt_and_writes_nothing(tmp_path) -> None:
    """The refusal must land BEFORE any manifest exists.

    A rendered policy directory is what the shell creates objects from, so a refusal
    that still wrote manifests would leave a correct-looking input for a later step
    to apply.
    """
    rc, err, out = _render(
        tmp_path, *edge_flags(tmp_path),
        receipt=outputs_doc(source=alb_source(run_nonce="ffffffffffffffff")))
    assert rc == 1
    assert "belongs to run" in err
    assert not out.exists(), "manifests were written despite the refusal"


def test_the_cli_binds_the_source_to_the_nonce_it_actually_renders(tmp_path) -> None:
    """The rendered policy's nonce and its ipBlock rule must be ONE run's.

    Root's fourth finding on 8a8d81b8: the CLI took a separate `--run-nonce-expect`,
    so `--nonce=ffff… --run-nonce-expect=a1b2…` with a receipt for a1b2… was ACCEPTED
    -- stamping the policy's ownership labels with one run's nonce while its ALB rule
    came from another run's load balancer. The labels are what `90-cleanup-ledger.sh`
    gates on, so that object would be torn down by a run whose edge it never admitted.

    The flag is gone: the expectation IS `--nonce`. This test pins that there is no
    second name for that fact, by rendering with a nonce the receipt does not carry
    and requiring a refusal.
    """
    rc, err, out = _render(
        tmp_path, *edge_flags(tmp_path), "--nonce", "ffffffffffffffff",
        receipt=outputs_doc())
    assert rc == 1, "a policy was rendered whose labels and ALB rule belong to different runs"
    # Refused by the RECEIPT binding rather than by the inner source check -- one layer
    # earlier than the stale-source case, because with `--nonce` as the single
    # expectation the whole document is now visibly not this run's, not just its ALB
    # block. Asserted on the run_nonce line specifically so this stays a statement
    # about the nonce and not about any binding field happening to mismatch.
    assert "does not belong to this run" in err
    assert "records run_nonce" in err
    assert not out.exists()


def test_there_is_no_second_name_for_this_runs_nonce(tmp_path) -> None:
    """The flag that allowed the disagreement must not come back.

    Asserted behaviourally -- by trying to USE it -- rather than by grepping the
    source, which would also match the comment explaining why it was removed (it did,
    on the first attempt at this test). argparse refuses an unknown flag with
    SystemExit(2), and that is the property worth pinning: no argument can supply a
    nonce expectation independent of the one the policy is labelled with.
    """
    with pytest.raises(SystemExit) as exc:
        _render(tmp_path, *edge_flags(tmp_path),
                "--run-nonce-expect", NONCE, receipt=outputs_doc())
    assert exc.value.code == 2


def test_the_cli_refuses_an_unreadable_receipt(tmp_path) -> None:
    """"Could not read" is not "no edge" -- it must not fall back to the plain policy.

    Falling back would be the silent denial this whole seam exists to prevent, and
    it would look identical to a successful render.
    """
    (tmp_path / "edge-outputs.json").write_text("{not json")
    rc, err, out = _render(tmp_path, *edge_flags(tmp_path))
    assert rc == 1
    assert not out.exists()


@pytest.mark.parametrize("cidrs,why", [
    (["not-an-ip/32"], "suffix matching is not CIDR parsing"),
    (["10.0.10.41/24"], "a subnet admits the ordinary gateway's ALB"),
])
def test_the_cli_refuses_an_unparseable_or_wide_address(tmp_path, cidrs, why) -> None:
    """The two cases root drove through this exact seam and got rc=0 for.

    Asserted at the CLI rather than only at the resolver because that is where they
    were reproduced: the resolver's checks are only worth what the caller's use of
    them is worth.
    """
    rc, err, out = _render(
        tmp_path, *edge_flags(tmp_path),
        receipt=outputs_doc(source=alb_source(source_cidrs=cidrs)))
    assert rc == 1, why
    assert not out.exists()


def test_the_cli_refuses_the_wrong_container_port(tmp_path) -> None:
    """8081 with a listener of 80: two different integers, and still wrong.

    Root's second finding. The fixture Service targets 8080, so this rule admits the
    load balancer to a port nothing serves -- a denial indistinguishable from the
    protected worker failing its bootstrap.
    """
    rc, err, out = _render(
        tmp_path, *edge_flags(tmp_path),
        receipt=outputs_doc(source=alb_source(container_port=8081)))
    assert rc == 1
    assert "8080" in err
    assert not out.exists()


@pytest.mark.parametrize("arn,why", [
    (None, "a null ARN does not say which load balancer was observed"),
    ("arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/other/zzz999",
     "a foreign ALB in the same account passes on the nonce alone"),
])
def test_the_cli_refuses_addresses_from_another_load_balancer(tmp_path, arn, why) -> None:
    """Root's third finding, both halves, at the seam where it was reproduced."""
    rc, err, out = _render(
        tmp_path, *edge_flags(tmp_path),
        receipt=outputs_doc(source=alb_source(alb_arn=arn)))
    assert rc == 1, why
    assert not out.exists()


# ---------------------------------------------------------------------------
# freshness at EXECUTION time, not just at render time
#
# Root, twice (5810972205, 5810972553): "Require a fresh observation and matching
# policy before execution; stale allowances can refer to reassigned addresses."
#
# `resolve_alb_policy_source` proves the document is this run's and this ALB's. It
# cannot prove the addresses are STILL the ALB's -- a Terraform output records what
# was true when it ran. So the rendered rule is compared against a live re-read
# before the experiment executes.
# ---------------------------------------------------------------------------
def current(**over) -> dict:
    kw = {"observed_cidrs": list(CIDRS), "observed_arn": FIXTURE_ALB_ARN,
          "expected_alb_arn": FIXTURE_ALB_ARN}
    kw.update(over)
    return er.check_alb_source_is_current(resolved(), **kw)


def test_a_policy_matching_the_current_addresses_is_confirmed() -> None:
    """The passing case, so a check tightened to refuse everything is not mistaken
    for a working one."""
    result = current()
    assert result["matches_rendered_policy"] is True
    assert result["cidrs"] == sorted(CIDRS)
    assert result["alb_arn"] == FIXTURE_ALB_ARN


def test_an_address_the_load_balancer_no_longer_holds_is_refused() -> None:
    """The direction an earlier revision of this module called safe, wrongly.

    It claimed staleness was "a denial, never an admission" -- copied from #5836's
    output description, which root asked them to correct too. An interface can be
    RELEASED and its address REASSIGNED in this same VPC, so a stale /32 is a live
    admission of whatever holds it now.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_cidrs=[CIDRS[0]])
    message = str(exc.value)
    assert "no longer holds" in message
    assert "REASSIGNED" in message
    assert "not a harmless leftover" in message


def test_an_address_the_load_balancer_has_gained_is_refused() -> None:
    """Containment is not enough, so the comparison is set EQUALITY.

    An unadmitted interface drops some connections and not others, by which one the
    ALB happened to pick -- an intermittent failure that reads as flakiness in the
    software under test.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_cidrs=[*CIDRS, "10.0.12.7/32"])
    message = str(exc.value)
    assert "does NOT admit" in message
    assert "intermittent" in message


def test_both_directions_are_reported_together() -> None:
    """A recreated ALB changes every address, and the operator needs both halves."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_cidrs=["10.0.20.1/32", "10.0.21.2/32"])
    message = str(exc.value)
    assert "no longer holds" in message and "does NOT admit" in message


@pytest.mark.parametrize("observed", [None, "", "10.0.10.41/32", {}, 0])
def test_an_observation_that_could_not_be_made_is_refused(observed) -> None:
    """"Could not observe" is not "unchanged" -- the same rule the stage gate applies.

    `None` is the case that matters: it must not compare equal to "no addresses" and
    report agreement between two absences.
    """
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_cidrs=observed)
    assert "not a list" in str(exc.value)


def test_an_empty_observation_is_refused_rather_than_matching_nothing() -> None:
    """#5836's own precondition treats an empty ENI result as a refusal."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_cidrs=[])
    assert "found no addresses" in str(exc.value)


@pytest.mark.parametrize("entry", ["not-an-ip/32", "10.0.10.41", "10.0.10.0/24", None, 41])
def test_an_unparseable_or_wide_observation_entry_is_refused(entry) -> None:
    """An entry that cannot be parsed would drop out of the set comparison and read
    as agreement, which is the failure mode this whole function exists to prevent."""
    with pytest.raises(er.EdgeReceiptError):
        current(observed_cidrs=[CIDRS[0], entry])


def test_addresses_observed_on_a_different_load_balancer_prove_nothing() -> None:
    """The observation must be attributed to the ALB the policy was rendered for.

    Otherwise a re-read of the ordinary gateway's ALB -- whose addresses would of
    course differ -- would be reported as staleness in the fixture's policy, or worse,
    a coincidental match would be reported as freshness.
    """
    other = ("arn:aws:elasticloadbalancing:us-east-1:879318057152:"
             "loadbalancer/app/k8s-adpgatew-ordinary/deadbeef")
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_arn=other)
    assert "proves nothing about either" in str(exc.value)


@pytest.mark.parametrize("arn", [None, "", "   "])
def test_an_unattributed_observation_is_refused(arn) -> None:
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(observed_arn=arn)
    assert "names no load balancer" in str(exc.value)


def test_the_expected_arn_cannot_be_omitted_to_waive_the_freshness_check() -> None:
    """Without it, a re-observation of the WRONG load balancer would satisfy this."""
    with pytest.raises(er.EdgeReceiptError) as exc:
        current(expected_alb_arn="", observed_arn=FIXTURE_ALB_ARN)
    assert "must not waive the check it exists for" in str(exc.value)


def test_the_freshness_check_makes_no_cloud_call() -> None:
    """The caller observes; this decides -- the same split `stage_gate` uses.

    Pinned because a version of this that shelled out to `aws` could not be tested
    without an account, and would then not be tested.
    """
    source = Path(er.__file__).read_text()
    start = source.index("def check_alb_source_is_current")
    body = source[start:source.index("def endpoint_provenance")]
    for forbidden in ("subprocess", "boto3", "os.system", "kubectl"):
        assert forbidden not in body, f"{forbidden} in a decision function"


# ---------------------------------------------------------------------------
# the CLI verbs 10-create-fixture.sh actually calls
#
# The two functions above are only reachable from the shell through
# `edge_receipt.py resolve-alb-source` and `edge_receipt.py check-alb-current`.
# Everything asserted above holds of the FUNCTIONS; these tests cover the seam,
# where three things can go wrong that no library test can see:
#
#   * the verb could not exist, or take a different flag name, so the stage would
#     fail open on a shell `||` or a `set -e` that is not set
#   * the comma-separated `--observed-cidrs` string is split HERE, not in the
#     library, so an empty element (`"a,,b"`, or the trailing comma a `paste -sd,`
#     over an empty line produces) is one fewer address required of the policy --
#     one interface of this run's load balancer the fixture silently does not
#     admit, which is the intermittent-bootstrap shape
#   * `--out` is the artifact the next shell step reads; if a refusal wrote it, the
#     existence of the file would be taken for a passed check
#
# Mutation-tested: deleting the empty-element guard, and silently filtering empty
# elements out of the split, both left the whole suite green before these existed.
# ---------------------------------------------------------------------------
def _run_verb(*argv: str, capsys) -> tuple[int, str, str]:
    """Invoke the module CLI the way the shell does, in-process."""
    rc = er.main(list(argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _receipt_file(tmp_path: Path, doc=...) -> Path:
    path = tmp_path / "edge-outputs.json"
    path.write_text(json.dumps(outputs_doc() if doc is ... else doc))
    return path


def _resolve_argv(tmp_path: Path, **over) -> list[str]:
    """Every binding supplied, so a test varies exactly one -- as `resolved` does."""
    flags = {
        "--receipt": str(_receipt_file(tmp_path)),
        "--run-nonce": NONCE,
        "--account-id": ACCOUNT,
        "--region": REGION,
        "--environment": ENVIRONMENT,
        "--expected-alb-arn": FIXTURE_ALB_ARN,
    }
    flags.update(over)
    return ["resolve-alb-source"] + [part for flag, value in flags.items()
                                     if value is not None for part in (flag, value)]


def _rendered_file(tmp_path: Path, text: str | None = None) -> Path:
    """The document `resolve-alb-source` writes, as `check-alb-current` reads it."""
    path = tmp_path / "alb-source.json"
    path.write_text(json.dumps(resolved()) if text is None else text)
    return path


def _current_argv(tmp_path: Path, **over) -> list[str]:
    """Only writes the default rendered document when the caller did NOT override it.

    Writing it unconditionally is the defect this file already hit twice: the good
    document landed at the same path a test had just filled with a truncated one, so
    the unreadable-document case exercised the happy path and its refusal test passed
    by testing the opposite of what it said.
    """
    flags = {
        "--rendered": (over["--rendered"] if "--rendered" in over
                       else str(_rendered_file(tmp_path))),
        "--observed-cidrs": ",".join(CIDRS),
        "--observed-arn": FIXTURE_ALB_ARN,
        "--expected-alb-arn": FIXTURE_ALB_ARN,
    }
    flags.update(over)
    return ["check-alb-current"] + [part for flag, value in flags.items()
                                    if value is not None for part in (flag, value)]


def test_the_existing_flat_endpoint_cli_still_works_beside_the_verbs(tmp_path, capsys) -> None:
    """The reason the verbs are dispatched by leading positional, not by subparsers.

    `10-create-fixture.sh` already calls this module flat (`--receipt ...`) to resolve
    the worker control endpoint. An argparse subparser conversion would have made that
    existing, relied-upon call a usage error -- a break in the one path already in use,
    for tidier help output. This pins that it did not happen.
    """
    rc, out, err = _run_verb(
        "--receipt", str(_receipt_file(tmp_path)), "--run-nonce", NONCE,
        "--account-id", ACCOUNT, "--region", REGION, "--environment", ENVIRONMENT,
        capsys=capsys)
    assert rc == 0, err
    assert out.strip().endswith("/internal/v1/agent")


def test_the_resolve_verb_prints_the_bound_source(tmp_path, capsys) -> None:
    """The passing case, so a verb refusing everything is not mistaken for a working one."""
    rc, out, err = _run_verb(*_resolve_argv(tmp_path), capsys=capsys)
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["cidrs"] == CIDRS
    assert payload["container_port"] == 8080
    assert payload["alb_arn"] == FIXTURE_ALB_ARN


def test_the_resolve_verb_writes_its_out_file_only_when_it_accepted(tmp_path, capsys) -> None:
    """The artifact IS the evidence the shell branches on.

    `10-create-fixture.sh` reads the fields out of this file to compose the policy
    mutation. If a refused resolve still wrote one, the next step would compose from a
    document that was rejected -- and the file's existence would read as a passed check.
    """
    good = tmp_path / "nested" / "source.json"
    rc, _, err = _run_verb(*_resolve_argv(tmp_path, **{"--out": str(good)}), capsys=capsys)
    assert rc == 0, err
    assert json.loads(good.read_text())["cidrs"] == CIDRS

    refused = tmp_path / "nested" / "refused.json"
    rc, _, err = _run_verb(
        *_resolve_argv(tmp_path, **{"--out": str(refused), "--run-nonce": "f" * 16}),
        capsys=capsys)
    assert rc == 1
    assert not refused.exists(), "a refusal left the next step an artifact to read as a pass"
    assert "does not belong to this run" in err


def test_the_resolve_verb_refuses_a_source_from_another_load_balancer(tmp_path, capsys) -> None:
    """The binding root added after review of 8a8d81b8, reachable from the shell."""
    other = FIXTURE_ALB_ARN.replace("w2-fixture/abc123", "some-other/def456")
    rc, out, err = _run_verb(
        *_resolve_argv(tmp_path, **{"--expected-alb-arn": other}), capsys=capsys)
    assert rc == 1
    assert out.strip() == "", "nothing may reach stdout: the shell captures it"
    assert "load balancer" in err


def test_the_current_verb_confirms_a_matching_observation(tmp_path, capsys) -> None:
    """The passing case for the freshness verb."""
    rc, out, err = _run_verb(*_current_argv(tmp_path), capsys=capsys)
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["matches_rendered_policy"] is True
    assert payload["cidrs"] == sorted(CIDRS)


@pytest.mark.parametrize("observed,why", [
    (f"{CIDRS[0]},,{CIDRS[1]}", "an element dropped from the middle"),
    (f"{CIDRS[0]},{CIDRS[1]},", "the trailing comma `paste -sd,` leaves on an empty line"),
    (f",{CIDRS[0]},{CIDRS[1]}", "a leading comma, i.e. the first interface unread"),
    ("", "no observation at all, spelled as an empty string"),
    (f"{CIDRS[0]},   ,{CIDRS[1]}", "an element that is only whitespace"),
])
def test_an_empty_element_in_the_observation_is_refused_not_dropped(
        tmp_path, capsys, observed, why) -> None:
    """The split happens at this seam, so this is the only place it can be caught.

    Every spelling below is what the shell's own pipeline produces when one interface
    goes unread: `describe-network-interfaces` returning a blank line, `tr` on empty
    input, a `paste -sd,` over a short list. Dropping the empty element would leave a
    SHORTER required set that still compares equal to the policy -- the policy then
    admits every address it was checked against, while the load balancer holds one
    more. Connections succeed or fail by which interface the ALB picks, and the run
    records an intermittent bootstrap failure as a finding about the feature.

    Refusing with rc 2 rather than 1 keeps it distinguishable from a genuine mismatch:
    the operator's next action is to fix the query, not to re-render the policy.
    """
    rc, out, err = _run_verb(
        *_current_argv(tmp_path, **{"--observed-cidrs": observed}), capsys=capsys)
    assert rc == 2, why
    assert out.strip() == "", "nothing may reach stdout: the shell captures it"
    assert "contains an empty entry" in err
    assert "Refusing rather than dropping it" in err


def test_a_shorter_observation_without_empty_elements_is_still_refused(
        tmp_path, capsys) -> None:
    """The guard above must not be the ONLY thing standing between a short observation
    and a pass: one address genuinely missing is a mismatch, reported as one."""
    rc, out, err = _run_verb(
        *_current_argv(tmp_path, **{"--observed-cidrs": CIDRS[0]}), capsys=capsys)
    assert rc == 1
    assert out.strip() == ""
    assert "no longer holds" in err
    assert "REASSIGNED" in err


def test_the_current_verb_refuses_an_unreadable_rendered_document(tmp_path, capsys) -> None:
    """"Could not read it" is not "it is fine".

    The rendered document is written by the previous step; if that step was skipped, or
    wrote a truncated file, an exception swallowed into a zero exit would run the
    experiment against a policy nothing corroborates.
    """
    missing = tmp_path / "never-written.json"
    rc, _, err = _run_verb(
        *_current_argv(tmp_path, **{"--rendered": str(missing)}), capsys=capsys)
    assert rc == 1
    assert "'Could not read it' is not 'it is fine'." in err

    truncated = _rendered_file(tmp_path, text='{"cidrs": ["10.0.10.41/32"')
    rc, _, err = _run_verb(
        *_current_argv(tmp_path, **{"--rendered": str(truncated)}), capsys=capsys)
    assert rc == 1
    assert "could not read" in err


def test_the_current_verb_refuses_an_observation_of_another_load_balancer(
        tmp_path, capsys) -> None:
    """Root's requirement at the seam: the fresh observation must be attributable."""
    other = FIXTURE_ALB_ARN.replace("w2-fixture/abc123", "some-other/def456")
    rc, out, err = _run_verb(
        *_current_argv(tmp_path, **{"--observed-arn": other}), capsys=capsys)
    assert rc == 1
    assert out.strip() == ""
    assert "proves nothing about either" in err


@pytest.mark.parametrize("verb,flag", [
    ("resolve-alb-source", "--expected-alb-arn"),
    ("resolve-alb-source", "--run-nonce"),
    ("check-alb-current", "--expected-alb-arn"),
    ("check-alb-current", "--observed-arn"),
    ("check-alb-current", "--observed-cidrs"),
])
def test_no_binding_flag_can_be_omitted_at_the_cli(tmp_path, verb, flag) -> None:
    """An expectation that is missing must refuse, never widen -- including by argparse.

    `required=True` rather than a default, so omitting the flag is a usage error (rc 2
    via SystemExit) instead of a check that silently compares against nothing.
    """
    argv = (_resolve_argv(tmp_path, **{flag: None}) if verb == "resolve-alb-source"
            else _current_argv(tmp_path, **{flag: None}))
    with pytest.raises(SystemExit) as exc:
        er.main(argv)
    assert exc.value.code != 0


def test_the_verbs_make_no_cloud_call_either(tmp_path) -> None:
    """The shell observes; these decide. Pinned at the CLI too, because a verb that
    shelled out to `aws` would make the whole seam untestable without an account --
    and would then not be tested."""
    source = Path(er.__file__).read_text()
    body = source[source.index("def _main_alb"):source.index("_ALB_VERBS = (")]
    for forbidden in ("subprocess", "boto3", "os.system", "kubectl", "describe-network"):
        assert forbidden not in body, f"{forbidden} in the decision CLI"
