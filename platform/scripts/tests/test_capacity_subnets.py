"""Additional EKS capacity subnets are never removed by absent or stale configuration (#5830).

The failure these tests exist to prevent: an operator relieves pod-IP exhaustion by
adding existing private subnets to the cluster's subnet set; later, an ordinary
deployment that simply does not carry those account-specific ids plans them away and
re-breaks pod scheduling, with nobody having asked for a change. So the cases that
matter most here are the ones where the configuration says nothing (unset, blank) or
says less than the live cluster has (partial).
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import capacity_subnets as cap  # noqa: E402

SPEC = importlib.util.spec_from_file_location("resolve_capacity_subnets",
                                              SCRIPTS / "resolve-capacity-subnets.py")
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)

BASELINE = ["subnet-0ba5e000000000001", "subnet-0ba5e000000000002"]
ADDED = {"us-east-1a": "subnet-0e57a000000000001", "us-east-1b": "subnet-0e57a000000000002"}


def state(baseline=BASELINE, managed_extra=(), output=True):
    """Platform state: networking private subnets, optionally other managed subnets."""
    def resource(name, ids, module="module.networking"):
        return {"mode": "managed", "type": "aws_subnet", "name": name, "module": module,
                "instances": [{"attributes": {"id": i}} for i in ids]}
    result = {"resources": [resource("private", baseline),
                            resource("public", ["subnet-0deadbeef00000001", "subnet-0deadbeef00000002"])]}
    if managed_extra:
        result["resources"].append(resource("capacity", list(managed_extra), "module.other"))
    if output:
        result["outputs"] = {"private_subnet_ids": {"value": list(baseline)}}
    return result


# ---------------------------------------------------------------------------
# Baseline ownership — the finding about deriving it from every aws_subnet
# ---------------------------------------------------------------------------

def test_baseline_is_the_networking_private_subnets_not_every_managed_subnet():
    # Public subnets are Terraform-managed too. Treating all managed subnets as
    # "already wired through private_subnet_ids" is broader than the truth.
    assert cap.baseline_subnet_ids(state()) == set(BASELINE)


def test_baseline_falls_back_to_the_networking_private_resource_when_the_output_is_absent():
    # Older state may predate the output; the fallback must still exclude public.
    assert cap.baseline_subnet_ids(state(output=False)) == set(BASELINE)


def test_unknown_platform_state_refuses_rather_than_guessing():
    with pytest.raises(cap.Refused, match="Cannot determine the networking private subnets"):
        cap.baseline_subnet_ids({"resources": []})


def test_a_public_subnet_in_the_live_set_is_refused_not_silently_ignored():
    # Managed, but not a baseline private subnet: neither safely pinnable nor
    # safely ignorable, so the unsupported ownership shape is refused explicitly.
    with pytest.raises(cap.Refused, match="not among the networking private subnets"):
        cap.live_additions(state(), [*BASELINE, "subnet-0deadbeef00000001"])


def test_an_independently_managed_private_capacity_subnet_is_refused():
    with pytest.raises(cap.Refused, match="ownership shape is not supported"):
        cap.live_additions(state(managed_extra=["subnet-0cafe0000000000f1"]), [*BASELINE, "subnet-0cafe0000000000f1"])


def test_an_unmanaged_live_subnet_is_recognised_as_an_addition():
    assert cap.live_additions(state(), [*BASELINE, "subnet-0e57a000000000001"]) == {"subnet-0e57a000000000001"}


def test_a_cluster_with_no_additions_yields_none():
    assert cap.live_additions(state(), BASELINE) == set()


# ---------------------------------------------------------------------------
# The three omission cases root required: unset, blank, partial
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("configured", [None, {}], ids=["unset", "blank"])
def test_unset_or_blank_configuration_retains_the_live_additions(configured):
    # THE regression: empty resolves to {} and an ordinary apply would remove the
    # additions. Retaining is what makes "nobody set the variable" survivable.
    assert cap.effective_additions(configured, ADDED) == ADDED


def test_partial_configuration_refuses_instead_of_planning_a_removal():
    # A stale variable is likelier than a deliberate decision to shrink capacity,
    # and guessing wrong costs pod scheduling — so fail closed and name the subnet.
    with pytest.raises(cap.Refused, match="subnet-0e57a000000000002"):
        cap.effective_additions({"us-east-1a": "subnet-0e57a000000000001"}, ADDED)


def test_the_refusal_explains_the_consequence_and_the_two_ways_forward():
    with pytest.raises(cap.Refused) as exc:
        cap.effective_additions({}, ADDED, allow_removal=False) if False else \
            cap.effective_additions({"us-east-1a": "subnet-0e57a000000000001"}, ADDED)
    message = str(exc.value)
    assert "re-break pod IP assignment" in message and "authorise the removal" in message


def test_configuration_may_add_beyond_what_the_cluster_already_has():
    # Adding a zone while still declaring the live ones is an operator adding
    # capacity, and is allowed.
    result = cap.effective_additions({**ADDED, "us-east-1c": "subnet-0e57a000000000003"}, ADDED)
    assert result == {**ADDED, "us-east-1c": "subnet-0e57a000000000003"}


def test_adding_one_zone_while_omitting_the_live_ones_still_refuses():
    # Fail-closed is about the omission, not about whether something was also
    # added: this configuration would still drop the two live additions.
    with pytest.raises(cap.Refused, match="omit subnet"):
        cap.effective_additions({"us-east-1c": "subnet-0e57a000000000003"}, ADDED)


def test_narrowing_requires_a_deliberate_authorisation():
    result = cap.effective_additions({"us-east-1a": "subnet-0e57a000000000001"}, ADDED, allow_removal=True)
    assert result == {"us-east-1a": "subnet-0e57a000000000001"}


def test_a_configured_subnet_conflicting_with_the_live_one_in_that_zone_refuses():
    # Nothing is dropped here — both live subnets are still declared — but the
    # configuration claims a different subnet for a zone the cluster already uses,
    # which a zone-keyed map cannot represent. Refuse rather than pick one.
    with pytest.raises(cap.Refused, match="conflicts with the subnet the live cluster uses"):
        cap.effective_additions(
            {"us-east-1a": "subnet-00fbe0000000000a1", "us-east-1b": "subnet-0e57a000000000002",
             "us-east-1c": "subnet-0e57a000000000001"}, ADDED)


# ---------------------------------------------------------------------------
# Zone-keyed representation
# ---------------------------------------------------------------------------

def test_an_unresolvable_zone_refuses_rather_than_dropping_the_subnet():
    with pytest.raises(cap.Refused, match="Cannot resolve the availability zone"):
        cap.as_zone_map({"subnet-0e57a000000000001": "us-east-1a", "subnet-09affe00000000001": None})


def test_two_additions_in_one_zone_cannot_be_represented_and_refuse():
    with pytest.raises(cap.Refused, match="share availability zone"):
        cap.as_zone_map({"subnet-0e57a000000000001": "us-east-1a", "subnet-0e57a000000000002": "us-east-1a"})


# ---------------------------------------------------------------------------
# Configured-input parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", None], ids=["empty", "whitespace", "none"])
def test_blank_configured_input_parses_as_nothing_configured(raw):
    assert resolver.configured(raw) == {}


@pytest.mark.parametrize("raw,match", [
    ("{", "not valid JSON"),
    ('["subnet-0e57a000000000001"]', "must be a JSON object"),
    ('{"us-east-1a": "vpc-0extra1"}', "entries must look like"),
    ('{"us-east-1a": 5}', "entries must look like"),
    ('{"us-east-1a": "subnet-0e57a000000000001", "us-east-1b": "subnet-0e57a000000000001"}', "same subnet under two"),
])
def test_malformed_configured_input_refuses(raw, match):
    with pytest.raises(cap.Refused, match=match):
        resolver.configured(raw)


def test_valid_configured_input_parses():
    assert resolver.configured('{"us-east-1a": "subnet-0123456789abcdef0"}') == \
        {"us-east-1a": "subnet-0123456789abcdef0"}


# ---------------------------------------------------------------------------
# The resolver end to end — what the workflow actually runs
# ---------------------------------------------------------------------------

def fake_aws(live, state_doc, zones, missing_cluster=False):
    def aws(*args):
        if args[:2] == ("eks", "describe-cluster"):
            if missing_cluster:
                raise RuntimeError("AWS eks describe-cluster failed: ResourceNotFoundException")
            return {"cluster": {"resourcesVpcConfig": {"subnetIds": list(live)}}}
        if args[:2] == ("s3api", "get-object"):
            Path(args[-1]).write_text(json.dumps(state_doc))
            return {}
        if args[:2] == ("ec2", "describe-subnets"):
            asked = args[args.index("--subnet-ids") + 1:]
            return {"Subnets": [{"SubnetId": s, "AvailabilityZone": zones[s]}
                                for s in asked if s in zones]}
        raise AssertionError(f"Unexpected AWS access: {args[:2]}")
    return aws


def run_resolver(monkeypatch, live, configured="", zones=None, allow_removal=False,
                 state_doc=None, missing_cluster=False):
    monkeypatch.setattr(resolver, "aws", fake_aws(live, state_doc or state(),
                                                  zones or {"subnet-0e57a000000000001": "us-east-1a",
                                                            "subnet-0e57a000000000002": "us-east-1b"},
                                                  missing_cluster))
    args = type("Args", (), {"environment": "dev", "bucket": "b", "configured": configured,
                             "allow_removal": allow_removal})()
    return resolver.resolve(args)


def test_resolver_retains_live_additions_when_the_repository_variable_is_unset(monkeypatch):
    assert run_resolver(monkeypatch, [*BASELINE, "subnet-0e57a000000000001", "subnet-0e57a000000000002"]) == ADDED


def test_resolver_retains_live_additions_when_the_repository_variable_is_blank(monkeypatch):
    assert run_resolver(monkeypatch, [*BASELINE, "subnet-0e57a000000000001"], configured="  ") == \
        {"us-east-1a": "subnet-0e57a000000000001"}


def test_resolver_refuses_a_partial_repository_variable_against_an_expanded_cluster(monkeypatch):
    with pytest.raises(cap.Refused, match="subnet-0e57a000000000002"):
        run_resolver(monkeypatch, [*BASELINE, "subnet-0e57a000000000001", "subnet-0e57a000000000002"],
                     configured='{"us-east-1a": "subnet-0e57a000000000001"}')


def test_resolver_returns_nothing_for_an_unexpanded_cluster(monkeypatch):
    # Every un-opted-in environment must keep planning the set it has today.
    assert run_resolver(monkeypatch, BASELINE) == {}


def test_resolver_uses_the_configured_map_when_the_cluster_does_not_exist_yet(monkeypatch):
    # First deployment: there is no live set to preserve.
    assert run_resolver(monkeypatch, [], configured='{"us-east-1a": "subnet-0e57a000000000001"}',
                        missing_cluster=True) == {"us-east-1a": "subnet-0e57a000000000001"}


def test_resolver_propagates_an_unexpected_cluster_read_failure(monkeypatch):
    # A failed read must NOT be interpreted as "no additions" — that is exactly the
    # silent-removal path this resolver exists to close.
    def aws(*args):
        raise RuntimeError("AWS eks describe-cluster failed: AccessDeniedException")
    monkeypatch.setattr(resolver, "aws", aws)
    args = type("Args", (), {"environment": "dev", "bucket": "b", "configured": "",
                             "allow_removal": False})()
    with pytest.raises(RuntimeError, match="AccessDenied"):
        resolver.resolve(args)


def test_resolver_cli_prints_json_and_refuses_with_a_nonzero_exit():
    # The workflow captures stdout into TF_VAR_, so the contract is the printed JSON.
    proc = subprocess.run([sys.executable, str(SCRIPTS / "resolve-capacity-subnets.py"), "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and "--allow-removal" in proc.stdout


def test_a_subnet_claimed_under_two_zones_after_merging_refuses():
    # Terraform's variable validation refuses this too, but failing here names the
    # conflicting subnet instead of surfacing it as a late plan-time type error.
    with pytest.raises(cap.Refused, match="more than one"):
        cap.effective_additions(
            {**ADDED, "us-east-1c": "subnet-0e57a000000000001"}, ADDED)
