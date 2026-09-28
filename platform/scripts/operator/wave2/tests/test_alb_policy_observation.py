#!/usr/bin/env python3
"""The LIVE fixture gateway policy must admit this run's ALB (issue #3968, item 5).

Root's instruction: "compose/observe the fixture gateway policy before admitting the
worker stage. Keep the ordinary policy unchanged and retain both policy UID gates."

WHY A SEPARATE FILE FROM test_alb_policy_source.py, WHICH IS ALSO ABOUT THE ALB RULE

Those tests are about a DOCUMENT: #5836's Terraform output, and whether it belongs to
this run. These are about an OBJECT: the NetworkPolicy now in the cluster, and whether
it admits the load balancer. The distinction is the whole point of this module --
composing a rule and applying it are different events, and everything between them
(the gateway stage creating the policy before the edge exists, the later mutation, a
same-named replacement, an apply that silently changed nothing) can leave a manifest
that is right and a cluster that denies the edge.

WHAT MAKES THIS WORTH TESTING RATHER THAN OBVIOUS

Every refusal below describes a policy that reads as configured. The `except`
subtraction is the sharpest: the ALB's address is present, at the right width, in an
ipBlock, on the right port -- and the rule admits nothing. A reviewer checking "is the
address in the policy?" finds it. So does a grep.

And the failure they all produce is the same one, which is why they must be caught
BEFORE the worker is created rather than diagnosed after: a denied edge means the
worker's bootstrap handshake never completes, which is recorded as "the protected
worker failed its bootstrap" -- the very conclusion Wave 2 exists to establish or
refute.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/test_alb_policy_observation.py -q
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import alb_policy_observation as apo
import render_fixture as rf

RUN_ID = "w2-20260924-0130"
NONCE = "deadbeefcafe0123"
POLICY_UID = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
POLICY_NAME = f"w2-fixture-policy-{RUN_ID[3:]}"
GW_NS = "adp-gateway"
AGENT_NS = "adp-agents"
ALB_CIDRS = ["10.0.10.41/32", "10.0.11.87/32"]
PORT = 8080


def live_policy(**over) -> dict:
    """A policy that admits exactly this run's ALB, correct in every respect.

    Shaped like what the API server returns rather than like a rendered manifest --
    with a uid, a resourceVersion and the namespaceSelector rule the gateway stage
    created -- so a test that varies one field is varying exactly that field.
    """
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": POLICY_NAME,
            "namespace": GW_NS,
            "uid": POLICY_UID,
            "resourceVersion": "80431",
            "labels": {apo.FIXTURE_LABEL: RUN_ID, apo.NONCE_LABEL: NONCE},
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": "w2-fixture-gateway-20260924-0130"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": GW_NS}}},
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": AGENT_NS}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": PORT}],
                },
                {
                    "from": [{"ipBlock": {"cidr": cidr}} for cidr in ALB_CIDRS],
                    "ports": [{"protocol": "TCP", "port": PORT}],
                },
            ],
            "egress": [{"ports": [{"protocol": "TCP", "port": 443}]}],
        },
    }
    policy = copy.deepcopy(policy)
    for key, value in over.items():
        policy[key] = value
    return policy


_DEFAULT = object()


def admits(policy=_DEFAULT, **over) -> dict:
    """Call the decision with every expectation named, so none is defaulted here.

    An omitted expectation is itself a refusal in this module, and a helper that
    quietly supplied one would hide that.

    The sentinel is not fussiness. The first revision used `policy=None` with
    `live_policy() if policy is None else policy`, so `admits(None)` -- the case
    asserting that an unreadable policy is refused -- silently substituted the GOOD
    policy and reported DID NOT RAISE. A default that collides with a value under
    test makes the test pass for the wrong reason, which here would have been a test
    claiming to pin the vacuous-pass defect while exercising the happy path.
    """
    kwargs = {
        "expected_uid": POLICY_UID,
        "expected_name": POLICY_NAME,
        "expected_namespace": GW_NS,
        "expected_cidrs": list(ALB_CIDRS),
        "expected_port": PORT,
        "expected_nonce": NONCE,
    }
    kwargs.update(over)
    return apo.policy_admits_alb(
        live_policy() if policy is _DEFAULT else policy, **kwargs)


def alb_rule(policy: dict) -> dict:
    """The ipBlock rule, located by content so reordering the rules is not a failure."""
    for rule in policy["spec"]["ingress"]:
        if any("ipBlock" in peer for peer in rule.get("from") or []):
            return rule
    raise AssertionError("fixture has no ipBlock rule")


# ---------------------------------------------------------------------------
# the passing case, first and deliberately
# ---------------------------------------------------------------------------
def test_a_policy_that_admits_this_runs_alb_passes() -> None:
    """Without this, a check tightened to refuse everything would look like a fix.

    That is not a hypothetical: most of this file asserts refusals, so the one test
    that can distinguish "correctly strict" from "broken" is this one.
    """
    record = apo.policy_admits_alb(
        live_policy(),
        expected_uid=POLICY_UID, expected_name=POLICY_NAME, expected_namespace=GW_NS,
        expected_cidrs=list(ALB_CIDRS), expected_port=PORT, expected_nonce=NONCE,
    )
    assert record["admits_alb"] is True
    assert record["admits_cidrs"] == sorted(ALB_CIDRS)
    assert record["on_port"] == PORT
    assert record["uid"] == POLICY_UID
    # The record states where the evidence came from, so a later reader can tell an
    # observation from an intention. This is the distinction the whole module rests on.
    assert "read back from the API server" in record["observed_from"]


def test_the_rules_may_be_in_any_order() -> None:
    """The ALB rule is found by content, not by position.

    Pinned because the renderer appends it last today; a future revision that
    prepends it must not turn into a spurious refusal, which is the kind of failure
    that gets "fixed" by relaxing the check.
    """
    policy = live_policy()
    policy["spec"]["ingress"].reverse()
    assert admits(policy)["admits_alb"] is True


# ---------------------------------------------------------------------------
# the unmutated gateway-stage policy: the case this module exists for
# ---------------------------------------------------------------------------
def test_the_gateway_stage_policy_without_the_alb_rule_is_refused() -> None:
    """The literal output of `render_policies(alb_source=None)`, plus a uid.

    This is not a contrived input: it is exactly what the cluster holds after the
    gateway stage, which necessarily runs before #5836's edge exists. If the later
    mutation does not apply, THIS is what the worker would be created against, and
    the refusal must name the consequence rather than just the absence.
    """
    rendered = rf.render_policies(
        run_id=RUN_ID, nonce=NONCE, policy_name=POLICY_NAME,
        gateway_namespace=GW_NS, agent_namespace=AGENT_NS,
        gateway_label="w2-fixture-gateway-20260924-0130",
        worker_label="w2-fixture-gateway-20260924-0130",
        alb_source=None,
    )
    gateway_policy = next(p for p in rendered if p["metadata"]["namespace"] == GW_NS)
    gateway_policy["metadata"]["uid"] = POLICY_UID
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(gateway_policy)
    message = str(exc.value)
    assert "no ipBlock rule at all" in message
    assert "belong to no pod and no namespace" in message
    # The consequence, not just the fact: an operator who reads only the first line
    # must still learn that proceeding produces a false negative about the feature.
    assert "establish or refute" in message


def test_an_ingress_list_that_is_empty_is_refused_as_a_total_denial() -> None:
    """Ingress declared with no rules denies everything, including the edge."""
    policy = live_policy()
    policy["spec"]["ingress"] = []
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "denies ALL inbound traffic" in str(exc.value)


def test_a_policy_that_does_not_declare_ingress_is_refused_even_though_traffic_flows() -> None:
    """The one case where the edge WOULD reach the fixture, and is still refused.

    A NetworkPolicy without Ingress in policyTypes restricts no ingress at all, so a
    reachability probe against it passes -- and the fixture is not isolated from
    anything. Accepting it would make the experiment a measurement of an unconfined
    pod, so this cannot be a "permissive success".
    """
    policy = live_policy()
    policy["spec"]["policyTypes"] = ["Egress"]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "does not restrict ingress AT ALL" in str(exc.value)


# ---------------------------------------------------------------------------
# present, correct-looking, and admits nothing
# ---------------------------------------------------------------------------
def test_an_except_subtraction_is_refused_although_the_address_is_listed() -> None:
    """The sharpest case in this file.

    Every surface check passes: the address is present, it is a /32, it is in an
    ipBlock, the port is right. `except` subtracts from the cidr on the SAME peer, so
    the rule admits nothing -- and a reviewer, or a grep, looking for the ALB's
    address in the policy finds it.
    """
    policy = live_policy()
    rule = alb_rule(policy)
    rule["from"][0]["ipBlock"]["except"] = [ALB_CIDRS[0]]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "EXCEPTS" in message
    assert "subtracts from the CIDR on the same peer" in message


def test_an_except_naming_some_other_address_is_also_refused() -> None:
    """Not only the self-subtracting case.

    An `except` this tooling did not render means the policy was composed by
    something else, which is itself the finding -- so the refusal does not depend on
    proving the subtraction covers the ALB.
    """
    policy = live_policy()
    alb_rule(policy)["from"][0]["ipBlock"]["except"] = ["10.0.10.0/28"]
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy)


def test_the_listener_port_instead_of_the_container_port_is_refused() -> None:
    """#5836 publishes both because they are different numbers.

    The ALB listens on 443 and connects to the pod on 8080. A rule naming 443 denies
    the flow under test while looking like a correctly configured HTTPS allowance,
    which is why the resolver, the renderer and this module all check the same
    constant rather than each accepting a plausible port.
    """
    policy = live_policy()
    alb_rule(policy)["ports"] = [{"protocol": "TCP", "port": 443}]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "does not include TCP/8080" in message
    assert "reading as configured" in message


def test_udp_on_the_right_port_does_not_admit_the_edge() -> None:
    """The protocol is checked, not only the number."""
    policy = live_policy()
    alb_rule(policy)["ports"] = [{"protocol": "UDP", "port": PORT}]
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy)


def test_a_rule_with_no_ports_is_refused_rather_than_accepted_as_a_superset() -> None:
    """It admits the ALB to EVERY port, including the worker control listener.

    Superset-of-intended is the tempting reading and the wrong one: the fixture's
    isolation is the property under measurement, so a widening is a refusal even
    though the edge would reach the gateway.
    """
    policy = live_policy()
    del alb_rule(policy)["ports"]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "EVERY port" in message
    assert "worker control listener" in message


def test_a_port_range_starting_at_the_right_port_is_refused() -> None:
    """`endPort` turns one allowance into many, including ports nothing serves."""
    policy = live_policy()
    alb_rule(policy)["ports"] = [{"protocol": "TCP", "port": PORT, "endPort": 8090}]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "RANGE" in str(exc.value)


@pytest.mark.parametrize("port", [8080.0, True, "8080", None])
def test_a_port_that_is_not_the_integer_8080_does_not_admit_the_edge(port) -> None:
    """`8080.0 == 8080` and `True == 1` are both true in Python.

    The API server rejects a float port -- mid-run, after the gateway already exists
    -- so a value that compares equal here would pass this gate and fail the apply.
    """
    policy = live_policy()
    alb_rule(policy)["ports"] = [{"protocol": "TCP", "port": port}]
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy)


# ---------------------------------------------------------------------------
# width: a rule that passes containment and loses the isolation
# ---------------------------------------------------------------------------
def test_a_subnet_wide_rule_is_refused_although_it_admits_the_alb() -> None:
    """Root, in #5836's own output description: do not widen it to the subnet.

    Both ordinary gateway ALBs share a subnet with the fixture's, so a /24 admits
    PRODUCTION's edge to the fixture gateway. It contains every address the fixture
    ALB holds, so a containment check passes and the isolation is gone.
    """
    policy = live_policy()
    alb_rule(policy)["from"] = [{"ipBlock": {"cidr": "10.0.10.0/24"}}]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "WIDER than a single address" in message
    assert "production's edge" in message


def test_a_bare_address_without_a_prefix_is_refused() -> None:
    """`ipaddress.IPv4Network("10.0.10.41")` is a valid /32; an ipBlock cidr is not.

    Refused by an explicit prefix check rather than by parsing, because parsing
    ACCEPTS it -- the same hole root found in the resolver, where #5836 publishes both
    `source_cidrs` and `source_ips` and reading the wrong field is the likeliest way
    in.
    """
    policy = live_policy()
    alb_rule(policy)["from"] = [{"ipBlock": {"cidr": "10.0.10.41"}}]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "not a CIDR" in str(exc.value)


@pytest.mark.parametrize("cidr", ["not-an-ip/32", "10.0.10.999/32", "/32", "10.0.10.41/33"])
def test_an_unparseable_cidr_in_the_live_policy_is_refused(cidr) -> None:
    """Suffix matching is not parsing: `"not-an-ip/32".endswith("/32")` is True.

    These reach a set comparison, where an entry that cannot be parsed would simply
    not match and read as a policy admitting some other address.
    """
    policy = live_policy()
    alb_rule(policy)["from"] = [{"ipBlock": {"cidr": cidr}}]
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy)


# ---------------------------------------------------------------------------
# both drift directions, refused with distinguishable messages
# ---------------------------------------------------------------------------
def test_an_address_the_alb_holds_but_the_policy_omits_is_refused() -> None:
    """Some connections succeed and others do not, by which interface the ALB picks.

    The intermittent shape, which is the one most easily written off as flakiness in
    the software under test rather than as the fixture's own networking.
    """
    policy = live_policy()
    alb_rule(policy)["from"] = [{"ipBlock": {"cidr": ALB_CIDRS[0]}}]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "does not admit ['10.0.11.87/32']" in message
    assert "intermittent" in message


def test_an_address_the_policy_admits_but_the_alb_no_longer_holds_is_refused() -> None:
    """Not a harmless leftover: it may since have been REASSIGNED in this VPC.

    This is the claim an earlier revision got wrong in the other direction, copied
    from the producer's output description ("a missing address is a denial, not an
    admission") and corrected by root. A stale /32 is a live admission of whatever
    holds it now.
    """
    policy = live_policy()
    alb_rule(policy)["from"].append({"ipBlock": {"cidr": "10.0.12.9/32"}})
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "admits ['10.0.12.9/32']" in message
    assert "REASSIGNED" in message


def test_both_directions_are_reported_together() -> None:
    """A recreated ALB changes every address; the operator needs both halves at once."""
    policy = live_policy()
    alb_rule(policy)["from"] = [{"ipBlock": {"cidr": "10.0.20.1/32"}}]
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "does not admit" in message and "REASSIGNED" in message


def test_the_alb_addresses_may_be_split_across_several_rules() -> None:
    """Equality is over the admitted SET, not over one rule's contents.

    A policy assembled by two applies is unusual but not wrong, and refusing it would
    be a false refusal -- which trains an operator to bypass the gate.
    """
    policy = live_policy()
    rule = alb_rule(policy)
    rule["from"] = [{"ipBlock": {"cidr": ALB_CIDRS[0]}}]
    policy["spec"]["ingress"].append({
        "from": [{"ipBlock": {"cidr": ALB_CIDRS[1]}}],
        "ports": [{"protocol": "TCP", "port": PORT}],
    })
    assert admits(policy)["admits_alb"] is True


def test_an_address_admitted_only_on_the_wrong_port_does_not_count_as_admitted() -> None:
    """The two checks compose rather than substituting for each other.

    Half the addresses on the right port and half on the wrong one is the shape a
    partial re-apply leaves, and the port complaint must fire rather than the set
    comparison silently counting them all.
    """
    policy = live_policy()
    rule = alb_rule(policy)
    rule["from"] = [{"ipBlock": {"cidr": ALB_CIDRS[0]}}]
    policy["spec"]["ingress"].append({
        "from": [{"ipBlock": {"cidr": ALB_CIDRS[1]}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    })
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "does not admit them to the port the fixture pod serves" in str(exc.value)


# ---------------------------------------------------------------------------
# identity: the second uid gate root asked to retain
# ---------------------------------------------------------------------------
def test_a_same_named_replacement_policy_is_refused_on_uid() -> None:
    """The object whose content was just read must be the one this run created.

    The stage gate proves the recorded object still EXISTS. That is a different
    question from whether the object this readback describes is it, and the gap
    between the two reads is where a replacement lands. A replacement's rules were
    composed by something else and this run's ledger cannot vouch for them.
    """
    policy = live_policy()
    policy["metadata"]["uid"] = "99999999-0000-1111-2222-333333333333"
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    message = str(exc.value)
    assert "REPLACEMENT" in message
    assert "neither authorises deleting it" in message


@pytest.mark.parametrize("uid", ["", None])
def test_a_policy_read_back_with_no_uid_is_refused(uid) -> None:
    """No uid is not a clean match: there is nothing to compare ownership against."""
    policy = live_policy()
    policy["metadata"]["uid"] = uid
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "no metadata.uid" in str(exc.value)


@pytest.mark.parametrize("expected", ["", None, "   "])
def test_an_omitted_uid_expectation_does_not_waive_the_uid_check(expected) -> None:
    """Otherwise a caller with no recorded uid gets a pass on the identity gate.

    An expectation that is missing must refuse, never widen -- the same rule the
    freshness check follows for a missing ALB ARN.
    """
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(expected_uid=expected)
    assert "would satisfy this check" in str(exc.value)


def test_a_terminating_policy_is_refused_although_its_rules_still_parse() -> None:
    """It reads as present and about to stop enforcing anything.

    An admission observed now says nothing about the window the experiment runs in,
    which is the same reasoning `worker_observation` applies to a pod carrying a
    deletionTimestamp.
    """
    policy = live_policy()
    policy["metadata"]["deletionTimestamp"] = "2026-09-24T09:15:00Z"
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "terminating" in str(exc.value)


def test_a_policy_carrying_another_runs_nonce_is_refused() -> None:
    """Those labels are what teardown selects on.

    So a policy stamped with another run's nonce is both unverifiable here and
    outside this run's ledger -- refused rather than mutated or trusted.
    """
    policy = live_policy()
    policy["metadata"]["labels"][apo.NONCE_LABEL] = "0000000000000000"
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    # Whitespace-normalised: the message wraps, so a literal substring match would be
    # asserting the line breaks rather than the sentence.
    message = " ".join(str(exc.value).split())
    assert "not this run's nonce" in message
    assert "what teardown selects on" in message


@pytest.mark.parametrize("labels", [{}, None, "adp.io/w2-nonce=deadbeefcafe0123"])
def test_missing_or_malformed_labels_are_refused(labels) -> None:
    """A string of labels is what `kubectl -o jsonpath` yields, and it is not a map."""
    policy = live_policy()
    policy["metadata"]["labels"] = labels
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy)


# ---------------------------------------------------------------------------
# a readback that did not happen is a refusal, never an empty policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("unreadable", [None, {}, "", [], "NotFound", 0])
def test_a_policy_that_could_not_be_read_is_refused(unreadable) -> None:
    """"Could not read the policy" is not "the policy is correct".

    This is the vacuous-pass defect class in its own right: an absent observation
    treated as a satisfied one. Without the object there is no evidence the ALB
    mutation was ever applied, and the worker must not be created on a manifest.
    """
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(unreadable)
    assert "not" in str(exc.value)


@pytest.mark.parametrize("kind", ["List", "Status", "Pod", None])
def test_an_object_of_the_wrong_kind_is_refused(kind) -> None:
    """A `kubectl get` that matched nothing returns a List; an error returns a Status.

    Both deserialise cleanly and carry no ingress rules, so without this they would
    read as a policy admitting nothing -- a refusal with the wrong diagnosis, which
    sends the operator to re-apply a policy that was never the problem.
    """
    policy = live_policy()
    policy["kind"] = kind
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(policy)
    assert "not a NetworkPolicy" in str(exc.value)


@pytest.mark.parametrize("spec", [None, {}, "spec", []])
def test_a_policy_with_no_usable_spec_is_refused(spec) -> None:
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(live_policy(spec=spec))


@pytest.mark.parametrize("metadata", [None, "meta", []])
def test_a_policy_with_no_usable_metadata_is_refused(metadata) -> None:
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(live_policy(metadata=metadata))


@pytest.mark.parametrize("peers", ["all", 5, {"ipBlock": {"cidr": ALB_CIDRS[0]}}])
def test_a_from_field_that_is_not_a_list_is_refused(peers) -> None:
    """A dict where a list belongs is the JSON-shape mistake that iterates as keys.

    Refused rather than treated as empty: "the sources cannot be determined" is not
    "there are no sources".
    """
    policy = live_policy()
    alb_rule(policy)["from"] = peers
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy)


@pytest.mark.parametrize("expected", [[], None, "10.0.10.41/32", {}, 0])
def test_an_empty_or_malformed_expectation_is_refused(expected) -> None:
    """An empty expectation is satisfied by a policy that admits nothing.

    Which denies the edge -- and that denial is indistinguishable from the protected
    worker failing its bootstrap, so it must not be reachable by passing no
    addresses.
    """
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        admits(expected_cidrs=expected)
    assert "nothing to require of the policy" in str(exc.value) or \
        "not a CIDR" in str(exc.value)


# ---------------------------------------------------------------------------
# the constants in the chain must be ONE constant
# ---------------------------------------------------------------------------
def test_this_module_takes_the_port_rather_than_naming_a_fourth_copy_of_it() -> None:
    """#5836's container_port -> the resolver -> the renderer -> here.

    A fourth independent literal is a fourth thing that can disagree, and this
    particular disagreement produces a policy that admits the ALB to a port nothing
    serves. Asserted behaviourally -- the function must honour a port it is GIVEN --
    rather than by grepping for the absence of a constant, because a grep also
    matches the comment explaining the absence.
    """
    policy = live_policy()
    alb_rule(policy)["ports"] = [{"protocol": "TCP", "port": 9090}]
    assert admits(policy, expected_port=9090)["on_port"] == 9090
    with pytest.raises(apo.AlbPolicyObservationError):
        admits(policy, expected_port=rf.FIXTURE_POD_PORT)


def test_the_observation_makes_no_cluster_or_cloud_call() -> None:
    """Caller observes, this decides -- the split `stage_gate` uses.

    It is what makes these refusals testable against real object shapes without an
    account, and it is what keeps a decision function from acquiring the authority to
    act on what it decided.

    Asserted on the module's IMPORTS rather than on its text. The first revision
    scanned the source for the strings "kubectl", "subprocess", "urllib" and failed on
    its own docstring, which says `kubectl` reports success for an apply that changed
    nothing -- the same defect as the source-grep assertion removed from
    test_alb_policy_source.py: a scan for the absence of code also matches the prose
    explaining why the code is absent. Imports are the actual capability boundary; a
    module that cannot import a client cannot call one.
    """
    import ast

    tree = ast.parse(Path(apo.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"subprocess", "boto3", "botocore", "os", "urllib", "http", "socket", "requests"}
    assert not (imported & forbidden), (
        f"{sorted(imported & forbidden)} imported into a decision module: the caller "
        "observes, this decides. A module that can reach the cluster can act on what it "
        "decided, and its refusals stop being testable without an account."
    )


# ---------------------------------------------------------------------------
# composing the mutation: what it appends, and what it refuses to touch
# ---------------------------------------------------------------------------
def unmutated(**over) -> dict:
    """The gateway-stage policy: real renderer output, plus the server's fields.

    Rendered by `render_policies(alb_source=None)` rather than hand-written, because
    this is the object the mutation is actually applied to and a hand-written
    lookalike would let the composer's expectations drift from what the gateway stage
    really creates.
    """
    rendered = rf.render_policies(
        run_id=RUN_ID, nonce=NONCE, policy_name=POLICY_NAME,
        gateway_namespace=GW_NS, agent_namespace=AGENT_NS,
        gateway_label="w2-fixture-gateway-20260924-0130",
        worker_label="w2-fixture-gateway-20260924-0130",
        alb_source=None,
    )
    policy = next(p for p in rendered if p["metadata"]["namespace"] == GW_NS)
    policy["metadata"].update({"uid": POLICY_UID, "resourceVersion": "80431"})
    for key, value in over.items():
        policy[key] = value
    return policy


def compose(policy=_DEFAULT, **over) -> dict:
    kwargs = {
        "expected_uid": POLICY_UID,
        "expected_name": POLICY_NAME,
        "expected_namespace": GW_NS,
        "expected_nonce": NONCE,
        "alb_cidrs": list(ALB_CIDRS),
        "alb_port": PORT,
    }
    kwargs.update(over)
    return apo.compose_alb_rule_update(
        unmutated() if policy is _DEFAULT else policy, **kwargs)


def test_the_composed_update_appends_the_alb_rule_and_changes_nothing_else() -> None:
    """The whole contract, asserted as a diff rather than as a shape.

    Comparing against the pre-update object field by field is what makes "changes
    nothing else" a real claim: an assertion that the ALB rule is PRESENT would pass
    just as well if the composer had rewritten the egress rules or dropped the
    in-cluster ingress rule on the way.
    """
    before = unmutated()
    result = compose(copy.deepcopy(before))
    assert result["action"] == "replace"
    body = result["body"]

    # Everything except spec.ingress is byte-identical.
    stripped_before = copy.deepcopy(before)
    stripped_after = copy.deepcopy(body)
    del stripped_before["spec"]["ingress"], stripped_after["spec"]["ingress"]
    assert stripped_after == stripped_before

    # The existing rules are preserved in order, and exactly one is appended.
    assert body["spec"]["ingress"][:len(before["spec"]["ingress"])] == before["spec"]["ingress"]
    assert len(body["spec"]["ingress"]) == len(before["spec"]["ingress"]) + 1

    appended = body["spec"]["ingress"][-1]
    assert appended["ports"] == [{"protocol": "TCP", "port": PORT}]
    assert appended["from"] == [{"ipBlock": {"cidr": c}} for c in sorted(ALB_CIDRS)]


def test_the_in_cluster_rule_survives_the_update() -> None:
    """Named separately because losing it breaks the harness's OWN access.

    The harness reaches the fixture through the namespaceSelector rule. A composer
    that rebuilt the rule list rather than appending to it would produce a fixture
    that fails its own probes for a reason having nothing to do with the edge -- and
    the ALB rule would be right, so the diagnosis would start in the wrong place.
    """
    body = compose()["body"]
    selectors = [
        peer for rule in body["spec"]["ingress"] for peer in rule.get("from") or []
        if "namespaceSelector" in peer
    ]
    names = {p["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
             for p in selectors}
    assert names == {GW_NS, AGENT_NS}


def test_the_composed_update_satisfies_the_observation_it_will_be_checked_by() -> None:
    """The two halves of item 5, closed against each other.

    The mutation and the gate that admits the worker stage must agree, and this is the
    only test that can catch them disagreeing: each is internally consistent, so a
    composer that emitted a rule the observation rejects would pass every other test
    in this file and deadlock the stage in practice.
    """
    body = compose()["body"]
    record = apo.policy_admits_alb(
        body,
        expected_uid=POLICY_UID, expected_name=POLICY_NAME, expected_namespace=GW_NS,
        expected_cidrs=list(ALB_CIDRS), expected_port=PORT, expected_nonce=NONCE,
    )
    assert record["admits_alb"] is True


def test_an_already_correct_policy_composes_no_update() -> None:
    """Idempotent, and reported as `none` rather than as a replace.

    "The rule was applied" and "the rule was already there" are different evidence
    about whether the mutation path works, so a second invocation must not claim a
    mutation it did not make.
    """
    result = compose(live_policy())
    assert result["action"] == "none"
    assert result["body"] is None
    assert result["observation"]["admits_alb"] is True


def test_the_update_carries_the_live_resource_version() -> None:
    """It is the precondition that makes the replace safe.

    Without it the interval between the read and the write is a window in which the
    policy can be replaced, and the write would overwrite the replacement's rules
    with ours -- the same read-then-act hole root reproduced in cleanup's
    delete-by-name.
    """
    assert compose()["resource_version"] == "80431"


@pytest.mark.parametrize("version", ["", None])
def test_a_policy_with_no_resource_version_cannot_be_updated(version) -> None:
    """Refusing beats sending an unconditional replace."""
    policy = unmutated()
    policy["metadata"]["resourceVersion"] = version
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        compose(policy)
    assert "unconditional replace" in str(exc.value)


def test_a_policy_this_run_does_not_own_is_not_mutated() -> None:
    """The ordinary gateway's policy is protected by the uid, not by its name.

    A blocklist of names only ever covers what someone remembered. This is also the
    one mistake in this module that could not be undone, so the refusal must come from
    the identity check the observation already performs rather than from a second
    implementation of it that could drift.
    """
    policy = unmutated()
    policy["metadata"]["uid"] = "99999999-0000-1111-2222-333333333333"
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        compose(policy)
    message = str(exc.value)
    assert "refusing to add the ALB rule" in message
    assert "REPLACEMENT" in message


def test_a_policy_carrying_another_runs_nonce_is_not_mutated() -> None:
    policy = unmutated()
    policy["metadata"]["labels"][apo.NONCE_LABEL] = "0000000000000000"
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        compose(policy)
    assert "refusing to add the ALB rule" in str(exc.value)


def test_a_policy_with_an_ipblock_rule_this_tooling_did_not_render_is_not_mutated() -> None:
    """Appending beside a rule we have just refused to vouch for is not a repair.

    The result would be a policy that is the union of our rule and something
    unreviewed, and it would then PASS the observation gate -- because our rule is
    correct. So the refusal has to happen here, at composition, not at the gate.
    """
    policy = unmutated()
    policy["spec"]["ingress"].append({
        "from": [{"ipBlock": {"cidr": "10.0.10.0/24"}}],
        "ports": [{"protocol": "TCP", "port": PORT}],
    })
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        compose(policy)
    message = str(exc.value)
    assert "refusing to add the ALB rule" in message
    assert "WIDER than a single address" in message


def test_an_existing_rule_with_an_except_blocks_the_mutation() -> None:
    """The `except` case again, from the mutating side."""
    policy = unmutated()
    policy["spec"]["ingress"].append({
        "from": [{"ipBlock": {"cidr": ALB_CIDRS[0], "except": [ALB_CIDRS[0]]}}],
        "ports": [{"protocol": "TCP", "port": PORT}],
    })
    with pytest.raises(apo.AlbPolicyObservationError) as exc:
        compose(policy)
    assert "EXCEPTS" in str(exc.value)


@pytest.mark.parametrize("cidrs", [[], None, "10.0.10.41/32", {}, ["10.0.10.0/24"], ["nope/32"]])
def test_an_unusable_address_set_composes_nothing(cidrs) -> None:
    """Including the subnet-wide case: the composer will not widen it either."""
    with pytest.raises(apo.AlbPolicyObservationError):
        compose(alb_cidrs=cidrs)


@pytest.mark.parametrize("port", [8080.0, True, "8080", None])
def test_a_non_integer_port_composes_nothing(port) -> None:
    """The API server would reject it mid-run, after the gateway already exists."""
    with pytest.raises(apo.AlbPolicyObservationError):
        compose(alb_port=port)


def test_the_composed_rule_deduplicates_and_canonicalises_addresses() -> None:
    """One rule entry per distinct address, in a stable order.

    A duplicate would be harmless to enforcement and not to review: it makes two
    readings of "which addresses does this admit" differ, and the operator comparing
    the policy against the ALB's interfaces is doing that by eye.
    """
    body = compose(alb_cidrs=[ALB_CIDRS[1], ALB_CIDRS[0], ALB_CIDRS[1]])["body"]
    admitted = [peer["ipBlock"]["cidr"] for peer in body["spec"]["ingress"][-1]["from"]]
    assert admitted == sorted(ALB_CIDRS)


def test_composing_does_not_mutate_the_object_it_was_given() -> None:
    """The caller's observation stays valid for its own error reporting.

    An in-place edit would also make the "changes nothing else" diff above compare an
    object against itself, so this test is what stops that one from becoming vacuous.
    """
    policy = unmutated()
    before = copy.deepcopy(policy)
    compose(policy)
    assert policy == before


# ---------------------------------------------------------------------------
# the CLI seam the shell calls: refusals must not leave usable artifacts
# ---------------------------------------------------------------------------
def write_policy(tmp_path: Path, policy, name="live.json") -> Path:
    import json

    path = tmp_path / name
    path.write_text(policy if isinstance(policy, str) else json.dumps(policy))
    return path


def cli_flags(tmp_path: Path, policy=_DEFAULT, **over) -> list[str]:
    """Every flag from one place, so a test varying one varies exactly one.

    The default policy file is written only when the caller did NOT override
    `--live-policy`. The first revision always wrote it, to the same `live.json` the
    override usually points at -- so the truncated-file and empty-file tests had their
    fixture OVERWRITTEN with the good policy while the flag dict was being built, and
    reported rc=0. Second instance in this file of a default colliding with the value
    under test; both made a refusal test pass by exercising the happy path.
    """
    flags = {
        "--uid": POLICY_UID,
        "--name": POLICY_NAME,
        "--namespace": GW_NS,
        "--nonce": NONCE,
        "--cidrs": ",".join(ALB_CIDRS),
        "--port": str(PORT),
    }
    if "--live-policy" not in over:
        flags["--live-policy"] = str(write_policy(
            tmp_path, live_policy() if policy is _DEFAULT else policy))
    flags.update(over)
    return [item for pair in flags.items() for item in pair]


def test_the_cli_admits_a_correct_policy_and_writes_its_record(tmp_path, capsys) -> None:
    import json

    out = tmp_path / "observation.json"
    rc = apo.main(["admits", *cli_flags(tmp_path, **{"--out": str(out)})])
    assert rc == 0
    assert json.loads(out.read_text())["admits_alb"] is True
    assert json.loads(capsys.readouterr().out)["admits_alb"] is True


def test_the_cli_refuses_the_unmutated_policy_and_writes_no_artifact(tmp_path) -> None:
    """The file's absence is the evidence.

    A refusal that still wrote its output would leave a later step a file to read as a
    passed check -- the vacuous-pass shape, one layer out in the pipeline.
    """
    out = tmp_path / "observation.json"
    rc = apo.main(["admits", *cli_flags(tmp_path, unmutated(), **{"--out": str(out)})])
    assert rc == 1
    assert not out.exists()


def test_the_cli_compose_refuses_a_foreign_policy_and_writes_no_artifact(tmp_path) -> None:
    policy = unmutated()
    policy["metadata"]["uid"] = "99999999-0000-1111-2222-333333333333"
    out = tmp_path / "update.json"
    rc = apo.main(["compose-update", *cli_flags(tmp_path, policy, **{"--out": str(out)})])
    assert rc == 1
    assert not out.exists()


def test_the_cli_compose_emits_a_body_the_admits_check_then_accepts(tmp_path) -> None:
    """The full shell path, end to end, without a cluster.

    compose-update's body is fed back through `admits` exactly as the stage will after
    the PUT. This is the seam where a mismatch between the two subcommands would show
    up as a stage that mutates and then refuses its own mutation.
    """
    import json

    update_out = tmp_path / "update.json"
    rc = apo.main(["compose-update", *cli_flags(
        tmp_path, unmutated(), **{"--out": str(update_out)})])
    assert rc == 0
    body = json.loads(update_out.read_text())["body"]

    rc = apo.main(["admits", *cli_flags(tmp_path, body)])
    assert rc == 0


@pytest.mark.parametrize("cidrs", ["10.0.10.41/32,,10.0.11.87/32", ",", "10.0.10.41/32,"])
def test_an_empty_cidr_element_is_refused_rather_than_dropped(tmp_path, cidrs) -> None:
    """`"a,,b".split(",")` yields "", which would be one fewer address required.

    One fewer address required of the policy is one address of this run's load
    balancer the fixture silently does not admit -- an intermittent bootstrap failure
    produced by a stray comma.
    """
    assert apo.main(["admits", *cli_flags(tmp_path, **{"--cidrs": cidrs})]) == 2


def test_an_empty_policy_file_is_not_an_absent_policy(tmp_path, capsys) -> None:
    """`kubectl get > file` on a failed lookup leaves the file EMPTY.

    Which must not parse as a policy with no rules: that would be a refusal with the
    wrong diagnosis, sending the operator to re-apply a policy that was never read.

    The MESSAGE is asserted, not just the exit code. Removing the empty-file guard
    still yields rc=1 -- an empty string is also a JSON decode error -- so an
    exit-code-only test cannot tell "I noticed the observation was never made" from "I
    failed to parse something". Verified by mutation: with the guard neutered this
    test passed, which is how the weakness was found.
    """
    path = tmp_path / "empty.json"
    path.write_text("")
    rc = apo.main(["admits", *cli_flags(tmp_path, **{"--live-policy": str(path)})])
    assert rc == 1
    message = capsys.readouterr().err
    assert "is empty" in message
    assert "unmade observation" in message


def test_a_truncated_policy_file_is_refused(tmp_path) -> None:
    """A partial read cannot establish what the policy admits."""
    path = write_policy(tmp_path, '{"kind": "NetworkPolicy", "metadata": {')
    assert apo.main(["admits", *cli_flags(tmp_path, **{"--live-policy": str(path)})]) == 1


def test_a_missing_policy_file_is_refused(tmp_path) -> None:
    rc = apo.main(["admits", *cli_flags(
        tmp_path, **{"--live-policy": str(tmp_path / "absent.json")})])
    assert rc == 1


@pytest.mark.parametrize("flag", ["--uid", "--name", "--namespace", "--nonce", "--cidrs", "--port"])
def test_every_expectation_is_a_required_flag(tmp_path, flag) -> None:
    """None may be defaulted at the CLI either.

    An expectation this seam supplied for the operator would be an expectation nobody
    checked -- and the uid and nonce are the two that keep this off the ordinary
    gateway's policy.
    """
    flags = cli_flags(tmp_path)
    index = flags.index(flag)
    del flags[index:index + 2]
    with pytest.raises(SystemExit) as exc:
        apo.main(["admits", *flags])
    assert exc.value.code == 2
