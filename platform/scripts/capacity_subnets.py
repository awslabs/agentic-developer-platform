#!/usr/bin/env python3
"""Decide the effective additional EKS capacity subnets, without ever narrowing the live set (#5830).

An operator relieves pod-IP exhaustion by adding already-existing private subnets
to the EKS cluster's own subnet set, which is what Auto Mode's managed `default`
NodeClass reads. Those subnet ids belong to one account, so they are deliberately
not committed to this repository and are supplied per-deployment instead.

That portability has a sharp edge, and it is the failure this module exists to
prevent: "nobody configured additions" and "there are deliberately none" look
identical, and both resolve to the empty default. So an ordinary deployment that
simply does not carry the ids plans the cluster back down to its original subnets
and re-breaks pod scheduling, with nobody having asked for a change.

The rule here is therefore: NEVER narrow the live subnet set as a side effect of
configuration being absent or stale.

  - configuration absent  -> retain what the live cluster already has
  - configuration omits a live addition -> REFUSE (fail closed); a partial list is
    far more likely to be a stale variable than a considered decision to shrink
    capacity, and guessing wrong costs pod scheduling
  - configuration names more -> allowed; that is an operator adding capacity
  - narrowing at all -> only via an explicit, deliberate removal

Both paths that could drop the subnets use this one module: the CI apply path
(platform-infra-apply.yml) and upgrade discovery (upgrade-state.py). The failure
being prevented is identical in both, so the rules are implemented and tested once.

BASELINE OWNERSHIP. An "addition" is a live cluster subnet that is not one of the
networking module's PRIVATE subnets -- those are what reach the cluster through
var.private_subnet_ids, so they need no pinning. It is deliberately not "every
subnet Terraform manages": Terraform also manages public subnets, and could manage
other private capacity, so treating all managed subnets as already-wired would let
a managed-but-not-baseline subnet be neither retained nor recognised, and it would
disappear from the cluster's set unannounced. An ownership shape this module cannot
account for is refused rather than assumed benign.
"""


class Refused(Exception):
    """A configuration that would narrow the live subnet set, or cannot be represented."""


def baseline_subnet_ids(state):
    """The networking private subnets that reach the cluster via var.private_subnet_ids.

    Prefers the platform output, which is literally the value wired into the EKS
    module, so the baseline is the same set the cluster is configured from rather
    than a re-derivation that could drift from it.
    """
    ids = state.get("outputs", {}).get("private_subnet_ids", {}).get("value")
    if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
        return set(ids)
    # Older state may predate the output. Fall back to the networking module's
    # `private` subnets specifically -- never to every aws_subnet, which would
    # sweep in public subnets.
    fallback = {attrs["id"] for resource, attrs in _managed(state, "aws_subnet")
                if resource.get("module") == "module.networking" and resource["name"] == "private"}
    if not fallback:
        raise Refused("Cannot determine the networking private subnets from platform state; "
                      "refusing rather than guessing which live cluster subnets are additions")
    return fallback


def _managed(state, kind):
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed" or resource["type"] != kind:
            continue
        for instance in resource.get("instances", []):
            yield resource, instance["attributes"]


def live_additions(state, live_subnet_ids):
    """Live cluster subnets that are additions rather than the Terraform baseline.

    Refuses an ownership shape it cannot account for: a subnet Terraform manages
    that is NOT in the baseline is neither safely retainable (pinning an id
    Terraform may replace) nor safely ignorable (it would silently leave the set).
    """
    if not live_subnet_ids:
        # Nothing live means nothing to preserve, so the baseline does not need to be
        # resolvable. Refusing here would block a deployment over a question whose
        # answer cannot change the outcome.
        return set()
    baseline = baseline_subnet_ids(state)
    additions = {s for s in live_subnet_ids if s not in baseline}
    managed_elsewhere = sorted(additions & {attrs["id"] for _, attrs in _managed(state, "aws_subnet")})
    if managed_elsewhere:
        raise Refused(
            "Live cluster subnet(s) are managed by platform Terraform but are not among the "
            f"networking private subnets wired into the cluster: {', '.join(managed_elsewhere)}. "
            "This ownership shape is not supported by capacity-subnet retention; resolve it "
            "explicitly rather than letting the subnet drop out of the cluster's set")
    return additions


def as_zone_map(subnet_zones):
    """Zone-keyed map for additional_private_subnet_ids_by_az, or a refusal.

    The variable is keyed by availability zone to make one-subnet-per-zone
    structural. Two additions in one zone cannot be represented, and a zone that
    cannot be resolved cannot be re-supplied -- either one, dropped silently, would
    shrink the live subnet set, which is the failure this module prevents.
    """
    result = {}
    for subnet, zone in sorted(subnet_zones.items()):
        if not zone:
            raise Refused(f"Cannot resolve the availability zone of live cluster subnet {subnet}; "
                          "refusing rather than dropping it from the cluster's subnet set")
        if result.setdefault(zone, subnet) != subnet:
            raise Refused(f"Two additional cluster subnets share availability zone {zone} "
                          f"({result[zone]}, {subnet}); this cannot be retained as a zone-keyed map")
    return result


def effective_additions(configured, retained, allow_removal=False):
    """Merge the configured map with the live additions, failing closed on omissions.

    `configured` is what this deployment was given (repository variable / TF_VAR_);
    `retained` is what the live cluster already has. Returns the map to plan AND
    apply with -- the same value for both, so the reviewed plan is the applied one.
    """
    configured, retained = dict(configured or {}), dict(retained or {})
    if not configured:
        # NOTHING CONFIGURED is not "remove everything". This is the whole point:
        # unset and blank are indistinguishable from a stale deployment that never
        # carried the account-specific ids, so they retain what the cluster has.
        # Narrowing to nothing requires an explicit authorised removal below.
        return dict(retained) if not allow_removal else {}
    dropped = sorted(s for s in retained.values() if s not in set(configured.values()))
    if dropped and not allow_removal:
        raise Refused(
            "Configured additional capacity subnets omit subnet(s) the live cluster is using: "
            f"{', '.join(dropped)}. Applying this would remove them from the cluster's subnet set "
            "and re-break pod IP assignment for every node launched afterwards. Add them to the "
            "configuration (they are retained automatically when it is unset), or authorise the "
            "removal deliberately if narrowing the set is genuinely intended.")
    if dropped:
        # Deliberate narrowing: the operator's configuration wins, as asked.
        return dict(configured)
    merged = dict(retained)
    for zone, subnet in configured.items():
        if merged.get(zone, subnet) != subnet:
            raise Refused(
                f"Configured additional subnet for {zone} ({subnet}) conflicts with the subnet the "
                f"live cluster uses there ({merged[zone]}); one zone cannot hold two entries in a "
                "zone-keyed map. Resolve which subnet that zone should use.")
        merged[zone] = subnet
    if len(set(merged.values())) != len(merged):
        # The Terraform variable refuses this too, but failing here names the
        # conflict instead of surfacing it as a late plan-time validation error.
        duplicated = sorted({s for s in merged.values() if list(merged.values()).count(s) > 1})
        raise Refused(f"Subnet(s) {', '.join(duplicated)} would be named under more than one "
                      "availability zone; a subnet belongs to exactly one zone, so resolve which "
                      "zone each is meant to serve.")
    return merged
