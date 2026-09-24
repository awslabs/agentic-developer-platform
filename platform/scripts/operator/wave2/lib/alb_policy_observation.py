#!/usr/bin/env python3
"""Decide whether the LIVE fixture gateway policy actually admits this run's ALB.

Root's item-5 instruction (5810972553): "compose/observe the fixture gateway policy
before admitting the worker stage. Keep the ordinary policy unchanged and retain both
policy UID gates you are currently implementing."

WHY COMPOSING IT IS NOT OBSERVING IT
------------------------------------
``render_fixture.render_policies`` composes the ``ipBlock`` rule, and
``edge_receipt.resolve_alb_policy_source`` establishes that the addresses it renders
from belong to this run's load balancer. Both act on a MANIFEST. Neither establishes
that the object now in the cluster carries the rule:

  * the gateway stage necessarily renders the policy BEFORE #5836's edge exists, so
    the object created then has no ALB rule at all. The rule arrives by a later
    mutation, and a mutation that silently did not apply leaves a policy that reads
    as configured in the operator's local manifest and denies the edge in the cluster;
  * a replacement policy of the same name -- another run, a re-apply, a human -- is a
    different object with different content and the same name. Which is why the uid is
    checked HERE too rather than only by the stage gate: the gate proves the recorded
    object still exists, and this proves the object whose content was just read is
    that same one;
  * ``kubectl`` reports success for an apply that changed nothing.

And the consequence of getting it wrong is the one this whole fixture is built to
avoid: a denied edge produces a worker whose bootstrap handshake never completes,
which reads as "the protected worker failed", which is the very question Wave 2
exists to answer. A missing admission must therefore be a refusal BEFORE the worker
is created, not a timeout after it.

WHAT AN OBSERVATION CANNOT BE
-----------------------------
Absent. Every refusal below distinguishes "this policy does not admit the ALB" from
"I could not read the policy", and both refuse -- but they refuse with different
messages, because the operator's next action differs. What must never happen is a
missing readback scoring as a satisfied check, which is the defect class recorded in
``GRADER-VACUOUS-PASS.md`` and repaired throughout this branch.

THE FOUR WAYS A PRESENT RULE STILL DENIES THE EDGE
--------------------------------------------------
Each is checked explicitly because each produces a policy that looks configured:

1. an ``ipBlock.except`` covering the ALB's address. ``except`` subtracts from the
   very CIDR the rule names, so ``cidr: 10.0.10.41/32`` with
   ``except: [10.0.10.41/32]`` admits nothing while listing the right address. It is
   the sharpest case here: a reader checking that the address "is in the policy"
   finds it.
2. the rule naming a port the pod does not serve. #5836 publishes the listener port
   alongside the container port precisely because they are different numbers, and the
   ALB connects to the pod on the container port.
3. the rule naming NO ports, which admits the ALB to EVERY port -- including the
   worker control port. That is a widening, not a permissive success.
4. a prefix wider than /32. Root, in #5836's own output description: both ordinary
   gateway ALBs share a subnet with the fixture's, so a subnet rule admits
   PRODUCTION's edge to the fixture. A wider rule passes a containment check and
   fails the isolation the fixture is for.

Pure functions: live object in, verdict out. No cloud call and no cluster call, so the
refusals are tested against real object shapes without an account -- the same
observe/decide split ``stage_gate`` and ``worker_observation`` use.
"""

from __future__ import annotations

import ipaddress
from typing import Any

NONCE_LABEL = "adp.io/w2-nonce"
FIXTURE_LABEL = "adp.io/w2-fixture"


class AlbPolicyObservationError(Exception):
    """The live policy cannot be shown to admit exactly this run's ALB."""


def _parse_single_address(cidr: Any, *, where: str) -> str:
    """Canonical /32 string, or raise. Suffix matching is not CIDR parsing.

    ``cidr.endswith("/32")`` accepts ``"not-an-ip/32"``; a bare ``"10.0.0.1"`` is a
    valid /32 to ``ipaddress`` but is not what a NetworkPolicy ipBlock may carry.
    Both are refused, because both reach a comparison that would then silently not
    match and read as a policy admitting something else.
    """
    if not isinstance(cidr, str) or "/" not in cidr:
        raise AlbPolicyObservationError(
            f"{where} carries {cidr!r}, which is not a CIDR. It is compared against the "
            "addresses the load balancer actually holds, so a value that cannot be parsed "
            "would drop out of the comparison rather than fail it."
        )
    try:
        network = ipaddress.IPv4Network(cidr.strip(), strict=True)
    except ValueError as exc:
        raise AlbPolicyObservationError(
            f"{where} carries {cidr!r}, which is not a valid IPv4 CIDR ({exc})."
        ) from exc
    if network.prefixlen != 32:
        raise AlbPolicyObservationError(
            f"{where} carries {cidr!r}, which is WIDER than a single address. Both ordinary "
            "gateway ALBs share a subnet with the fixture's, so a rule at this width admits "
            "production's edge to the fixture gateway -- the isolation this fixture exists "
            "to have. A wider rule satisfies a containment check and fails the property."
        )
    return str(network)


def _rule_admits_port(rule: dict, *, expected_port: int) -> tuple[bool, str | None]:
    """Whether this ingress rule admits TCP/expected_port, and why not if it does not."""
    ports = rule.get("ports")
    if ports is None or ports == []:
        # Not a permissive success. A rule with no ports admits its peers to every
        # port on the pod, including the worker control listener.
        return False, (
            "the rule names NO ports, which admits the load balancer to EVERY port on the "
            "fixture pod -- including the worker control listener. That is a widening of the "
            f"fixture's isolation, not a superset of the intended TCP/{expected_port} rule, "
            "so it is refused rather than accepted as sufficient"
        )
    if not isinstance(ports, list):
        return False, f"the rule's ports field is {ports!r}, not a list"
    named: list[Any] = []
    for entry in ports:
        if not isinstance(entry, dict):
            return False, f"the rule contains a port entry {entry!r} that is not an object"
        protocol = entry.get("protocol", "TCP")
        port = entry.get("port")
        named.append((protocol, port))
        # `8080.0 == 8080` and `True == 1` are both true in Python, so the type is
        # checked as well as the value: the API server rejects a float port, and it
        # would do so mid-run, after the gateway already exists.
        if (
            protocol == "TCP"
            and isinstance(port, int)
            and not isinstance(port, bool)
            and port == expected_port
        ):
            if entry.get("endPort") is not None:
                return False, (
                    f"the rule admits TCP/{expected_port} as the start of a RANGE ending at "
                    f"{entry['endPort']!r}. A range admits ports nothing in this fixture "
                    "serves, so it is a widening rather than the single-port rule intended"
                )
            return True, None
    return False, (
        f"the rule admits {named!r}, which does not include TCP/{expected_port} -- the port "
        "the fixture pod actually serves and the port the load balancer connects to. #5836 "
        "publishes the LISTENER port alongside it because they are different numbers, and "
        "naming the listener port here denies the flow under test while reading as configured"
    )


def _ipblock_cidrs(rule: dict, *, index: int) -> list[str]:
    """Canonical /32s this rule admits by ipBlock, refusing any `except` subtraction."""
    peers = rule.get("from")
    if peers is None:
        # `from` absent admits ALL sources. Reported by the caller as a widening; here
        # it simply contributes no ipBlock addresses.
        return []
    if not isinstance(peers, list):
        raise AlbPolicyObservationError(
            f"ingress rule {index}'s `from` is {peers!r}, not a list, so the sources it "
            "admits cannot be determined. Refusing rather than treating it as empty."
        )
    found: list[str] = []
    for peer in peers:
        if not isinstance(peer, dict):
            raise AlbPolicyObservationError(
                f"ingress rule {index} contains a peer {peer!r} that is not an object."
            )
        block = peer.get("ipBlock")
        if block is None:
            continue
        if not isinstance(block, dict):
            raise AlbPolicyObservationError(
                f"ingress rule {index} contains an ipBlock {block!r} that is not an object."
            )
        excepted = block.get("except")
        if excepted:
            # `except` subtracts from the cidr on the SAME peer. A reader checking that
            # the ALB's address appears in the policy finds it here and concludes the
            # edge is admitted, while the rule admits nothing at all.
            raise AlbPolicyObservationError(
                f"ingress rule {index} admits {block.get('cidr')!r} but EXCEPTS {excepted!r}. "
                "`except` subtracts from the CIDR on the same peer, so a rule naming exactly "
                "the right address can admit nothing -- and the address is still present for "
                "anyone checking that it is listed. Refusing: this fixture's rule is a plain "
                "set of /32s with no subtraction, and an unexpected `except` means the policy "
                "was composed by something other than this tooling."
            )
        found.append(_parse_single_address(
            block.get("cidr"), where=f"ingress rule {index}'s ipBlock"))
    return found


def policy_admits_alb(
    live_policy: Any,
    *,
    expected_uid: str,
    expected_name: str,
    expected_namespace: str,
    expected_cidrs: Any,
    expected_port: int,
    expected_nonce: str,
) -> dict[str, Any]:
    """Confirm the policy AS IT EXISTS admits exactly this run's ALB, or raise.

    ``live_policy`` is the object read back from the API server -- ``kubectl get
    networkpolicy <name> -n <ns> -o json`` -- not a rendered manifest. Reading the
    manifest back would only confirm this tooling agrees with itself.

    ``expected_cidrs`` and ``expected_port`` come from
    ``edge_receipt.check_alb_source_is_current``, so the addresses this is compared
    against have themselves been re-observed on the load balancer whose ARN this run
    recorded. Chaining them matters: comparing the live policy against the RENDERED
    document would confirm the mutation applied while leaving both sides free to
    describe an ALB that has since changed.

    ``expected_port`` is passed rather than defaulted so there is one constant in the
    chain (``#5836's container_port`` -> the resolver -> the renderer -> here) instead
    of a fourth place independently naming a number they must all agree on.

    Returns the observation record on success. Raises on every other outcome,
    including every way of failing to read the policy at all.
    """
    if not expected_uid or not str(expected_uid).strip():
        raise AlbPolicyObservationError(
            "no expected policy uid was supplied, so a REPLACEMENT policy of the same name "
            "would satisfy this check. The uid is the whole ownership proof; an expectation "
            "that is missing must not waive the check it exists for."
        )
    if not isinstance(expected_cidrs, list) or not expected_cidrs:
        raise AlbPolicyObservationError(
            f"the expected ALB addresses are {expected_cidrs!r}, so there is nothing to "
            "require of the policy. An empty expectation is satisfied by a policy that "
            "admits nothing, which denies the edge -- and that denial is indistinguishable "
            "from the protected worker failing its bootstrap."
        )
    expected = {
        _parse_single_address(cidr, where="the expected ALB address set")
        for cidr in expected_cidrs
    }

    if not isinstance(live_policy, dict) or not live_policy:
        raise AlbPolicyObservationError(
            f"the fixture gateway policy read back as {live_policy!r}. 'Could not read the "
            "policy' is not 'the policy is correct': without the live object there is no "
            "evidence the ALB rule was ever applied, and the worker must not be created on "
            "the strength of a manifest."
        )
    kind = live_policy.get("kind")
    if kind != "NetworkPolicy":
        raise AlbPolicyObservationError(
            f"the object read back is a {kind!r}, not a NetworkPolicy. A `kubectl get` that "
            "returned a List (no object matched) or a Status (an error) deserialises fine "
            "and carries no ingress rules, which would otherwise read as a policy admitting "
            "nothing."
        )

    metadata = live_policy.get("metadata")
    if not isinstance(metadata, dict):
        raise AlbPolicyObservationError(
            f"the policy's metadata is {metadata!r}, so its identity cannot be established."
        )
    live_uid = metadata.get("uid") or ""
    if not live_uid:
        raise AlbPolicyObservationError(
            "the policy was read back with no metadata.uid, so it cannot be shown to be the "
            "object this run created. An object with no uid must not read as a clean match."
        )
    if live_uid != expected_uid:
        raise AlbPolicyObservationError(
            f"the live policy {expected_namespace}/{expected_name} has uid {live_uid!r}, but "
            f"this run created uid {expected_uid!r}. A same-named REPLACEMENT is a different "
            "object with different content: its rules were composed by something else, and "
            "this run's ledger neither authorises deleting it nor can vouch for what it "
            "admits. Refusing to read its ALB rule as evidence about this run's fixture."
        )
    if metadata.get("name") != expected_name or metadata.get("namespace") != expected_namespace:
        raise AlbPolicyObservationError(
            f"the policy read back is {metadata.get('namespace')!r}/{metadata.get('name')!r}, "
            f"not {expected_namespace!r}/{expected_name!r}. The uid matched, which means the "
            "expectation itself is inconsistent -- resolve by hand rather than proceeding."
        )
    if metadata.get("deletionTimestamp"):
        raise AlbPolicyObservationError(
            f"the policy is terminating (deletionTimestamp "
            f"{metadata.get('deletionTimestamp')!r}). It still reads as present and its rules "
            "still parse, but it is about to stop enforcing anything -- so an admission "
            "observed now says nothing about the window the experiment runs in."
        )
    labels = metadata.get("labels")
    # Read through a local rather than through the value itself: `kubectl -o jsonpath`
    # yields labels as a STRING, and an earlier revision of this refusal formatted
    # `labels.get(...)` into its own message -- so the guard fired correctly and then
    # died with AttributeError instead of the refusal it had already decided on. A
    # diagnostic that crashes is not a diagnostic.
    found = labels.get(NONCE_LABEL) if isinstance(labels, dict) else labels
    if not isinstance(labels, dict) or found != expected_nonce:
        raise AlbPolicyObservationError(
            f"the policy's {NONCE_LABEL} label is {found!r}, not "
            f"this run's nonce. Those labels are what teardown selects on, so a policy "
            "carrying another run's nonce is both unverifiable here and outside this run's "
            "ledger. Refusing rather than mutating or trusting it."
        )

    spec = live_policy.get("spec")
    if not isinstance(spec, dict):
        raise AlbPolicyObservationError(
            f"the policy's spec is {spec!r}, so the sources it admits cannot be determined."
        )
    policy_types = spec.get("policyTypes") or []
    if "Ingress" not in policy_types:
        raise AlbPolicyObservationError(
            f"the policy's policyTypes are {policy_types!r} and do not include Ingress. A "
            "NetworkPolicy that does not declare Ingress does not restrict ingress AT ALL, so "
            "the edge would reach the fixture -- and so would everything else. The ALB rule "
            "would pass a reachability probe while the fixture was never isolated, which "
            "makes the measurement one of an unconfined pod."
        )
    ingress = spec.get("ingress")
    if not isinstance(ingress, list) or not ingress:
        raise AlbPolicyObservationError(
            f"the policy's ingress rules are {ingress!r}. With Ingress declared and no rules, "
            "the policy denies ALL inbound traffic including the edge's -- which is the "
            "gateway-stage policy before the ALB mutation, i.e. the mutation did not apply."
        )

    admitted_on_port: set[str] = set()
    all_ipblocks: set[str] = set()
    port_problems: list[str] = []
    for index, rule in enumerate(ingress):
        if not isinstance(rule, dict):
            raise AlbPolicyObservationError(
                f"ingress rule {index} is {rule!r}, not an object."
            )
        cidrs = set(_ipblock_cidrs(rule, index=index))
        all_ipblocks |= cidrs
        if not cidrs:
            continue
        ok, why = _rule_admits_port(rule, expected_port=expected_port)
        if ok:
            admitted_on_port |= cidrs
        else:
            port_problems.append(f"ingress rule {index} admits {sorted(cidrs)} but {why}")

    if not all_ipblocks:
        raise AlbPolicyObservationError(
            f"the live policy {expected_namespace}/{expected_name} has no ipBlock rule at all, "
            f"so it does not admit the fixture load balancer ({sorted(expected)}). With "
            "`target-type: ip` the ALB connects from its own network interfaces, which belong "
            "to no pod and no namespace, so the namespaceSelector rules do not admit it at any "
            "width. This is the gateway-stage policy unmodified: the ALB mutation was not "
            "applied, or was applied to a different object. Creating the worker now would "
            "produce a bootstrap that never completes, reported as the protected worker "
            "failing -- the conclusion this fixture exists to establish or refute."
        )
    if port_problems:
        raise AlbPolicyObservationError(
            "the live policy names the load balancer's addresses but does not admit them to "
            f"the port the fixture pod serves (TCP/{expected_port}):\n  - "
            + "\n  - ".join(port_problems)
            + "\nAn address admitted to the wrong port is denied traffic exactly as if it "
              "were absent, while reading as configured to anyone checking the address list."
        )

    missing = sorted(expected - admitted_on_port)
    extra = sorted(admitted_on_port - expected)
    if missing or extra:
        problems = []
        if missing:
            problems.append(
                f"the policy does not admit {missing}, which the load balancer currently "
                "holds. Connections would succeed or fail by which interface it happened to "
                "use: an intermittent bootstrap failure, the shape most easily recorded as "
                "flakiness in the software under test"
            )
        if extra:
            problems.append(
                f"the policy admits {extra}, which is not an address of this run's load "
                "balancer. An address the ALB does not hold may have been released and "
                "REASSIGNED in this same VPC, so this is a live admission of something else "
                "reaching the fixture gateway, not a harmless leftover"
            )
        raise AlbPolicyObservationError(
            f"the live policy {expected_namespace}/{expected_name} does not match this run's "
            "load balancer:\n  - " + "\n  - ".join(problems)
            + "\nRe-observe the ALB and re-apply the policy before creating the worker. Both "
              "directions refuse: admitting more than the ALB holds is an opening, and "
              "admitting less measures the fixture's own networking rather than the feature."
        )

    return {
        "policy": f"{expected_namespace}/{expected_name}",
        "uid": live_uid,
        "run_nonce": expected_nonce,
        "admits_cidrs": sorted(admitted_on_port),
        "on_port": expected_port,
        "observed_from": "live NetworkPolicy object, read back from the API server",
        "admits_alb": True,
    }


def compose_alb_rule_update(
    live_policy: Any,
    *,
    expected_uid: str,
    expected_name: str,
    expected_namespace: str,
    expected_nonce: str,
    alb_cidrs: Any,
    alb_port: int,
) -> dict[str, Any]:
    """Build the PUT body that adds this run's ALB rule, or raise.

    Returns the full object to replace the live one with, ready for a
    ``resourceVersion``-preconditioned PUT. Does not perform the update: the caller
    sends it, the same observe/decide split as everything else here.

    WHY A REPLACE, AND WHY THE RESOURCE VERSION IS THE PRECONDITION
    ---------------------------------------------------------------
    ``kubectl apply`` would work and is not used, for the reason ``create`` is used
    instead of ``apply`` everywhere in this tooling: apply ADOPTS whatever object of
    that name is present, and would therefore mutate a replacement this run does not
    own. A PUT carrying the live object's ``metadata.resourceVersion`` is rejected with
    409 Conflict if the object changed at all since the read -- so the uid check below
    and the content this update preserves are decided against the same version of the
    object the server still holds. Without it, the interval between the read and the
    write is a window in which the policy can be replaced, and the write would then
    overwrite the replacement's rules with ours.

    This mirrors ``cleanup.delete_k8s_with_uid_precondition``: the comparison belongs
    on the server, where it is atomic with the mutation, not in this process where it
    is a check followed by a hope.

    WHAT IT REFUSES TO TOUCH
    -----------------------
    The ordinary gateway's policy, by three independent properties -- the uid this run
    recorded, the run nonce label, and the name/namespace pair. Root: "Keep the
    ordinary policy unchanged." A widening applied to the ordinary policy would put
    production's blast radius on the line for a measurement, and it is the one mistake
    here that this tooling could not undo.

    Every existing rule is preserved and ONE rule is appended. The existing ingress is
    not rewritten, filtered or normalised: a composer that rebuilt the rule list could
    silently drop the in-cluster namespaceSelector rule the harness itself reaches the
    fixture through, and the resulting fixture would fail its own probes for a reason
    that has nothing to do with the edge.
    """
    if not isinstance(alb_port, int) or isinstance(alb_port, bool):
        raise AlbPolicyObservationError(
            f"the ALB port to admit is {alb_port!r}, which is not an integer. The API server "
            "rejects a non-integer port, and it would do so mid-run, after the gateway "
            "already exists."
        )
    if not isinstance(alb_cidrs, list) or not alb_cidrs:
        raise AlbPolicyObservationError(
            f"the ALB addresses to admit are {alb_cidrs!r}. An empty rule admits nothing, "
            "which denies the edge -- and that denial is indistinguishable from the protected "
            "worker failing its bootstrap. Refusing to compose it."
        )
    cidrs = sorted({
        _parse_single_address(cidr, where="the ALB addresses to admit")
        for cidr in alb_cidrs
    })

    # The identity checks are the observation's, reused rather than restated: a second
    # implementation of "is this the right object" is a second thing to keep in sync,
    # and the copy that drifts is the one guarding the mutation. This raises on every
    # identity failure -- wrong uid, missing uid, wrong nonce, terminating, wrong kind,
    # unreadable -- before anything is composed.
    #
    # It is called with the ALB expectation the update is ABOUT to satisfy, so the
    # "already admits it" case returns cleanly and every other case raises. That is why
    # the result is inspected rather than simply awaited.
    already = None
    try:
        already = policy_admits_alb(
            live_policy,
            expected_uid=expected_uid,
            expected_name=expected_name,
            expected_namespace=expected_namespace,
            expected_cidrs=cidrs,
            expected_port=alb_port,
            expected_nonce=expected_nonce,
        )
    except AlbPolicyObservationError as exc:
        # Only the "no ipBlock rule at all" outcome is a policy this may update: it is
        # the gateway-stage policy, whose identity has just been fully established by
        # the checks that ran before this one. Every other refusal describes a policy
        # whose ALB rule was composed by something else, is the wrong width, names the
        # wrong port, or carries an `except` -- and mutating any of those would mean
        # appending our rule beside a rule we have just refused to vouch for, leaving
        # the policy a union of ours and something unreviewed.
        if "no ipBlock rule at all" not in str(exc):
            raise AlbPolicyObservationError(
                "refusing to add the ALB rule to this policy: " + str(exc)
            ) from exc

    resource_version = ((live_policy.get("metadata") or {}).get("resourceVersion") or "")
    if not resource_version:
        raise AlbPolicyObservationError(
            "the live policy was read back with no metadata.resourceVersion, so the update "
            "cannot be made conditional on the object not having changed since the read. "
            "Refusing to send an unconditional replace: it would overwrite whatever the "
            "policy has become, including a replacement this run does not own."
        )

    if already is not None:
        # Idempotent, and reported as such rather than as an update. A second
        # invocation of the stage must not report a mutation it did not make, because
        # "the rule was applied" and "the rule was already there" are different
        # evidence about whether the mutation path works at all.
        return {
            "action": "none",
            "reason": "the live policy already admits exactly this run's ALB on the pod port",
            "observation": already,
            "resource_version": resource_version,
            "body": None,
        }

    import copy  # noqa: PLC0415 -- deep-copied only on the mutating path

    updated = copy.deepcopy(live_policy)
    updated["spec"]["ingress"] = list(updated["spec"]["ingress"]) + [{
        # One rule, separate from the namespaceSelector rule. Mixing an ipBlock into
        # that peer list would pair the ALB's addresses with the existing selectors
        # under one `ports` clause -- wider than it reads, and silently following any
        # future change to that rule's ports. Same reasoning as render_policies.
        "from": [{"ipBlock": {"cidr": cidr}} for cidr in cidrs],
        "ports": [{"protocol": "TCP", "port": alb_port}],
    }]
    return {
        "action": "replace",
        "reason": "the gateway-stage policy has no ipBlock rule; appending this run's ALB",
        "observation": None,
        "resource_version": resource_version,
        "body": updated,
    }


# ---------------------------------------------------------------------------
# CLI seam: the shell observes, this decides and prints
# ---------------------------------------------------------------------------
# Both subcommands take the live object as a FILE the caller has already read back
# with `kubectl get -o json`, and neither contacts the cluster. That is the boundary
# the no-import test pins: the shell owns the reads and the single mutating PUT, and
# this owns the decisions, so every refusal here is reachable in a test without an
# account and without credentials on any host.
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (
        ("admits", "does the live policy admit exactly this run's ALB?"),
        ("compose-update", "build the resourceVersion-preconditioned replace body"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--live-policy", required=True,
                       help="the object from `kubectl get networkpolicy <name> -n <ns> -o json`. "
                            "A FILE, not a rendered manifest: reading the manifest back would "
                            "only confirm this tooling agrees with itself")
        p.add_argument("--uid", required=True,
                       help="the uid THIS RUN's ledger records for the policy. Required: "
                            "without it a same-named replacement satisfies the check")
        p.add_argument("--name", required=True)
        p.add_argument("--namespace", required=True)
        p.add_argument("--nonce", required=True, help="this run's nonce, from the ledger")
        p.add_argument("--cidrs", required=True,
                       help="comma-separated /32s, from edge_receipt's FRESH observation -- "
                            "not from the rendered policy, which would compare this tooling "
                            "against itself")
        p.add_argument("--port", required=True, type=int,
                       help="the container port #5836 publishes. Passed rather than defaulted "
                            "so the chain has one constant instead of a copy that can disagree")
        p.add_argument("--out", default=None, help="write the result JSON here as well")

    args = parser.parse_args(argv)

    # Split before parsing so an empty element is a refusal rather than silently
    # dropping out: `"a/32,,b/32".split(",")` yields "", which would then be one fewer
    # address required of the policy than the operator supplied.
    cidrs = [part.strip() for part in args.cidrs.split(",")]
    if not all(cidrs):
        print(f"FAIL: --cidrs {args.cidrs!r} contains an empty entry. Refusing rather than "
              "dropping it: one fewer address required of the policy is one address of this "
              "run's load balancer the fixture silently does not admit.", file=sys.stderr)
        return 2

    try:
        with open(args.live_policy, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"FAIL: could not read the live policy from {args.live_policy}: {exc}\n"
              "'Could not read the policy' is not 'the policy is correct'.", file=sys.stderr)
        return 1
    if not text.strip():
        # `kubectl get ... > file` on a failed lookup leaves an EMPTY file and the
        # shell's own error check may have been bypassed by a pipeline. An empty file
        # must not parse as an absent policy and read as a refusal with the wrong
        # diagnosis, nor -- worse -- as `{}`.
        print(f"FAIL: {args.live_policy} is empty. A failed `kubectl get` leaves an empty "
              "file, so this is an unmade observation, not a policy with no rules.",
              file=sys.stderr)
        return 1
    try:
        live = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"FAIL: {args.live_policy} is not valid JSON ({exc}). Refusing: a partial read "
              "of the policy cannot establish what it admits.", file=sys.stderr)
        return 1

    try:
        if args.cmd == "admits":
            result = policy_admits_alb(
                live,
                expected_uid=args.uid, expected_name=args.name,
                expected_namespace=args.namespace, expected_cidrs=cidrs,
                expected_port=args.port, expected_nonce=args.nonce,
            )
        else:
            result = compose_alb_rule_update(
                live,
                expected_uid=args.uid, expected_name=args.name,
                expected_namespace=args.namespace, expected_nonce=args.nonce,
                alb_cidrs=cidrs, alb_port=args.port,
            )
    except AlbPolicyObservationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        # Written only after the decision SUCCEEDED, so the file's existence is itself
        # evidence: a refusal leaves no artifact for a later step to mistake for a
        # passed check.
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            print(f"FAIL: decided, but could not write {args.out}: {exc}\n"
                  "Refusing to report success: the next step reads that file.", file=sys.stderr)
            return 1
    print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
