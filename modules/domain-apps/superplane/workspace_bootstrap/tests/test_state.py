"""Durable state and the ownership decision it authorizes — findings F3 and F6.

F3 verbatim: "a pre-existing namespace can forge ADP ownership with a label
(`superplane.aws-e/bootstrap-owner == workspace_id` is not secret)."

`namespace_ownership` is the function that fix lives in, and the tests for it are the
most consequential in this file: its answer decides whether cleanup may DELETE a
namespace. Both failure directions are covered, because they harm different people.
Answering `adp-created` for something ADP did not create destroys a BYOC owner's
workloads. Answering `adopted` for something ADP did create leaves a namespace nothing
will clean up, making bootstrap non-idempotent. The first is unrecoverable, so the
default is `adopted` and every test below is written to show it is reached by default
rather than by accident.

The store tests exist because durability is the property the whole F6 repair rests on.
`FileStateStore` is exercised against a real filesystem — an in-memory fake alone could
not show that a record survives the process, which is the entire point of the module.
And a corrupt or unreadable file is a REFUSAL rather than "nothing was created": that
default would re-create F6 directly, an owned namespace with no cleanup plan.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.state import (
    ADOPTED,
    ADP_CREATED,
    STATE_VERSION,
    BootstrapState,
    FileStateStore,
    NamespaceRecord,
    load_state,
    namespace_ownership,
    record,
    state_from_mapping,
)

from .conftest import CLUSTER_ARN, NAMESPACE, WORKSPACE_ID, FakeStateStore

NAMESPACE_UID = "namespace-uid-0001"


def _state(**overrides) -> BootstrapState:
    return BootstrapState(
        **{
            "workspace_id": WORKSPACE_ID,
            "cluster_arn": CLUSTER_ARN,
            **overrides,
        }
    )


def _created(name: str = NAMESPACE, uid: str = NAMESPACE_UID) -> BootstrapState:
    """State recording that this bootstrap created the named namespace."""
    return _state(namespace=NamespaceRecord(name=name, uid=uid))


# --- F3: ownership requires a durable record AND a matching live uid -------------


def test_a_namespace_this_bootstrap_created_is_owned():
    """The positive control. Without it, "always adopted" would pass every test below
    and quietly make bootstrap non-idempotent."""
    assert (
        namespace_ownership(_created(), name=NAMESPACE, observed_uid=NAMESPACE_UID)
        == ADP_CREATED
    )


def test_a_namespace_with_no_durable_record_is_adopted():
    """**The F3 fix, stated as directly as it can be.**

    This is the forgery case. A pre-existing namespace labelled
    `superplane.aws-e/bootstrap-owner: <workspace_id>` reaches this function with no
    creation record, because nothing in this bootstrap created it. Neither the label key
    nor the workspace id is secret, so a BYOC owner or an unrelated prior process can
    produce that exact namespace — and the first revision recorded the observed uid as if
    ADP had created it and planned to delete it.
    """
    assert (
        namespace_ownership(_state(), name=NAMESPACE, observed_uid=NAMESPACE_UID)
        == ADOPTED
    )


def test_a_namespace_recreated_under_a_new_uid_is_adopted():
    """The other direction the uid catches.

    ADP genuinely created this namespace; somebody then deleted and recreated it. The
    NAME still matches the record, so a name-only check would authorize deleting an object
    ADP has never seen — created by whoever recreated it, holding their workloads.
    """
    assert (
        namespace_ownership(_created(), name=NAMESPACE, observed_uid="a-different-uid")
        == ADOPTED
    )


def test_a_record_for_a_different_namespace_name_confers_nothing():
    """A record naming `superplane-workspace` says nothing about `kube-system`.

    Worth its own test because the uid comparison alone would be satisfied by a
    coincidence, and because a bootstrap retried with a different namespace argument is
    the realistic way this input arises.
    """
    assert (
        namespace_ownership(
            _created(name="some-other-namespace"),
            name=NAMESPACE,
            observed_uid=NAMESPACE_UID,
        )
        == ADOPTED
    )


def test_an_unobservable_uid_confers_nothing():
    """A blank observed uid is an unanswered question, not a match.

    Without this, `recorded.uid != ""` would be the only guard, and a read that failed to
    return a uid would compare unequal by luck rather than by rule. `strip()` covers the
    whitespace variant that a shell-parsed value produces.
    """
    for observed in ("", "   "):
        assert (
            namespace_ownership(_created(), name=NAMESPACE, observed_uid=observed)
            == ADOPTED
        )


def test_a_record_explicitly_disclaiming_creation_is_adopted():
    """`created_by_bootstrap=False` is respected even with a matching name and uid.

    The flag exists so a future adoption path can record what it adopted without that
    record becoming delete authority. If this check were dropped, the mere PRESENCE of a
    record would authorize deletion — which is the F3 defect one level up.
    """
    state = _state(
        namespace=NamespaceRecord(
            name=NAMESPACE, uid=NAMESPACE_UID, created_by_bootstrap=False
        )
    )

    assert (
        namespace_ownership(state, name=NAMESPACE, observed_uid=NAMESPACE_UID)
        == ADOPTED
    )


def test_ownership_consults_no_label_at_all():
    """The forged fact must be unreachable, not merely outvoted.

    Asserted against the signature: there is no parameter through which a label could be
    passed. A version that accepted labels and ignored them would be one edit away from
    consulting them again, so the absence is the guarantee.
    """
    import inspect

    parameters = set(inspect.signature(namespace_ownership).parameters)

    assert parameters == {"state", "name", "observed_uid"}
    assert not any("label" in name for name in parameters)


def test_only_two_ownership_answers_exist():
    """A third value would be a state callers must interpret, and the interpretation
    would differ between the cleanup planner and the installer."""
    assert {ADP_CREATED, ADOPTED} == {"adp-created", "adopted"}


# --- The creation record cannot be incomplete -----------------------------------


@pytest.mark.parametrize("field_name", ["name", "uid"])
def test_a_creation_record_missing_a_field_is_refused(field_name):
    """A record without a uid cannot establish ownership on a later attempt, so it must
    not be constructible — otherwise it would silently degrade to adopted and the
    namespace it created would leak."""
    complete = {"name": NAMESPACE, "uid": NAMESPACE_UID}

    with pytest.raises(BootstrapRefused, match=f"NamespaceRecord.{field_name}"):
        NamespaceRecord(**{**complete, field_name: "   "})


# --- Serialization: unreadable is a refusal, not "nothing was created" ----------


def test_state_survives_the_round_trip_with_every_ownership_fact():
    """The facts ownership is computed from have to survive serialization, or the
    guarantee holds only within one process."""
    original = _state(
        namespace=NamespaceRecord(name=NAMESPACE, uid=NAMESPACE_UID),
        crds_established=("nodepools.superplane.ai",),
        controller_installed=True,
        prerequisites_recorded=True,
        registration_reserved=True,
        taint_cleared=True,
        registration_finalized=True,
        taint_restored=False,
    )

    reloaded = state_from_mapping(original.to_mapping())

    assert reloaded == original
    assert (
        namespace_ownership(reloaded, name=NAMESPACE, observed_uid=NAMESPACE_UID)
        == ADP_CREATED
    )


def test_every_field_is_carried_through_serialization():
    """A field silently dropped by `state_from_mapping` would be a durability hole that
    a round-trip of DEFAULT values could not detect — every default survives trivially.
    So this asserts the reader handles every declared field by name."""
    declared = {spec.name for spec in dataclasses.fields(BootstrapState)}
    payload = _state().to_mapping()

    assert set(payload) == declared, (
        "to_mapping and the dataclass have diverged, so a recorded fact may not be "
        "written at all"
    )
    # `state_from_mapping` refuses unknown fields, so a field present in the payload and
    # absent from the reader is caught by the round trip above; this catches the reverse.
    assert state_from_mapping(payload) == _state()


def test_unknown_fields_are_refused_rather_than_ignored():
    """A state file this version cannot fully interpret may carry ownership claims that
    do not mean what they appear to. Ignoring the unknown parts is how a newer writer's
    record gets acted on by an older reader."""
    payload = _state().to_mapping()
    payload["namespace_owned"] = True

    with pytest.raises(BootstrapRefused, match="unknown fields"):
        state_from_mapping(payload)


def test_a_state_version_mismatch_is_refused():
    with pytest.raises(BootstrapRefused, match="written by a different version"):
        _state(version=STATE_VERSION + 1)


def test_a_namespace_record_that_is_not_an_object_is_refused():
    """Refusing to infer ownership from an unreadable record. A string here would
    otherwise be `str.get`-ed and crash, or be coerced into a plausible-looking
    record."""
    payload = _state().to_mapping()
    payload["namespace"] = "superplane-workspace"

    with pytest.raises(BootstrapRefused, match="not an object"):
        state_from_mapping(payload)


def test_a_namespace_record_missing_its_uid_is_refused_on_read():
    """The refusal happens at read time, not at use time.

    A record deserialized with a blank uid would compute `ADOPTED` — the safe answer, but
    arrived at by accident and silently. The namespace ADP created would then leak with no
    diagnostic, so an unreadable record refuses instead.
    """
    payload = _state().to_mapping()
    payload["namespace"] = {"name": NAMESPACE, "created_by_bootstrap": True}

    with pytest.raises(BootstrapRefused, match="NamespaceRecord.uid"):
        state_from_mapping(payload)


def test_a_non_mapping_state_is_refused():
    with pytest.raises(BootstrapRefused, match="must be a mapping"):
        state_from_mapping(["workspace_id", WORKSPACE_ID])  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ["workspace_id", "cluster_arn"])
def test_state_without_an_identity_is_refused(field_name):
    """State that cannot say which workspace and cluster it describes cannot be checked
    against the attempt reading it, which is the guard `load_state` relies on."""
    with pytest.raises(BootstrapRefused, match=f"BootstrapState.{field_name}"):
        _state(**{field_name: "  "})


# --- load_state: a mismatched record needs an operator, not a reset -------------


def test_load_state_returns_a_fresh_state_when_there_is_no_prior_attempt():
    state = load_state(
        FakeStateStore(), workspace_id=WORKSPACE_ID, cluster_arn=CLUSTER_ARN
    )

    assert state.workspace_id == WORKSPACE_ID
    assert state.namespace is None
    assert state.interrupted_after_taint_cleared is False
    assert state.recovery_pending is False


def test_load_state_returns_the_prior_attempt():
    """Resuming is the normal case: bootstrap is retried after a partial failure far
    more often than it is run on a clean slate."""
    store = FakeStateStore()
    store.save(_created())

    state = load_state(store, workspace_id=WORKSPACE_ID, cluster_arn=CLUSTER_ARN)

    assert state.namespace is not None
    assert state.namespace.uid == NAMESPACE_UID


def test_load_state_refuses_a_record_for_another_workspace():
    """Its ownership claims describe another workspace's objects. Overwriting would
    orphan whatever that record owned — nothing else knows those objects exist."""
    store = FakeStateStore()
    store.save(_created())

    with pytest.raises(BootstrapRefused, match="different workspace"):
        load_state(store, workspace_id="another-workspace", cluster_arn=CLUSTER_ARN)


def test_load_state_refuses_a_record_for_another_cluster():
    """A workspace previously bootstrapped against a different cluster is exactly the
    rebinding `registration.py` refuses, seen from the state file."""
    store = FakeStateStore()
    store.save(_created())

    with pytest.raises(BootstrapRefused, match="silent rebind"):
        load_state(
            store,
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN.replace("cluster/", "cluster/other-"),
        )


# --- record(): the write happens before the caller continues -------------------


def test_record_persists_before_returning():
    """One function for update-and-write, so a caller cannot update the in-memory value
    and forget to persist it — an omission invisible until a crash, which is the worst
    time to find it."""
    store = FakeStateStore()

    updated = record(store, _state(), prerequisites_recorded=True)

    assert updated.prerequisites_recorded is True
    assert store.history, "record() returned without writing"
    assert store.history[-1]["prerequisites_recorded"] is True


def test_record_returns_the_persisted_value_not_the_input():
    """The returned object is what was written, so a caller threading it forward cannot
    diverge from the durable record."""
    store = FakeStateStore()
    original = _state()

    updated = record(store, original, taint_cleared=True)

    assert updated is not original
    assert original.taint_cleared is False, "the input state was mutated in place"
    assert updated == state_from_mapping(store.history[-1])


def test_a_failed_durable_write_propagates_rather_than_continuing():
    """If the record cannot be written, the caller must NOT proceed to the next mutation.

    Continuing would produce exactly the F6 state on purpose: an object on the cluster
    with no durable evidence that it exists.
    """
    store = FakeStateStore(fail_on=0)

    with pytest.raises(OSError):
        record(store, _state(), prerequisites_recorded=True)

    assert store.history == []


# --- FileStateStore: durability against a real filesystem ----------------------


def test_the_file_store_round_trips_through_a_real_file(tmp_path):
    """The claim is that the record survives the PROCESS. An in-memory fake cannot show
    that, so the production store is exercised against a real file."""
    store = FileStateStore(tmp_path / "nested" / "state.json")
    original = _created()

    store.save(original)

    assert FileStateStore(tmp_path / "nested" / "state.json").load() == original


def test_the_file_store_reports_no_prior_attempt_for_a_missing_file(tmp_path):
    """Absent is the one case that legitimately means "nothing was created" — and it is
    distinguishable from unreadable, which is why the next tests refuse."""
    assert FileStateStore(tmp_path / "absent.json").load() is None


def test_the_file_store_refuses_a_corrupt_record(tmp_path):
    """**A corrupt file must not read as "nothing was created".**

    That default is F6 exactly: an owned namespace with no cleanup plan. A truncated file
    is also the expected result of a crash mid-write, so this is a case that happens.
    """
    path = tmp_path / "state.json"
    path.write_text('{"workspace_id": "22222222-')

    with pytest.raises(BootstrapRefused, match="not valid JSON"):
        FileStateStore(path).load()


def test_the_file_store_refuses_an_unreadable_record(tmp_path):
    """Refusing to treat an unreadable record as 'nothing was created' — the same rule
    for a permissions failure as for corruption."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_created().to_mapping()))
    os.chmod(path, 0o000)

    try:
        if os.access(path, os.R_OK):  # pragma: no cover - running as root
            pytest.skip(
                "the current user can read a 0o000 file, so this is unassertable"
            )
        with pytest.raises(BootstrapRefused, match="could not be read"):
            FileStateStore(path).load()
    finally:
        os.chmod(path, 0o600)


def test_the_file_store_writes_a_private_file(tmp_path):
    """The record names the cluster and the namespaces ADP owns. Not a credential, but
    not world-readable either — and it matches `installation/runner.py::atomic`, whose
    discipline this store deliberately copies."""
    path = tmp_path / "nested" / "state.json"

    FileStateStore(path).save(_created())

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_file_store_leaves_no_temporary_files_behind(tmp_path):
    """The write is a temp-file-then-rename. A leftover temp file would be a partial
    record sitting next to the real one, and a future reader globbing the directory
    could pick it up."""
    path = tmp_path / "state.json"
    store = FileStateStore(path)

    store.save(_state())
    store.save(_created())

    assert [entry.name for entry in tmp_path.iterdir()] == ["state.json"]


def test_a_second_save_replaces_rather_than_appends(tmp_path):
    """`replace` is atomic, so a crash mid-write leaves the PREVIOUS state rather than a
    truncated file — and a truncated file would refuse on the next read and strand the
    workspace."""
    path = tmp_path / "state.json"
    store = FileStateStore(path)

    store.save(_state())
    store.save(_created(uid="second-uid"))

    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.namespace is not None
    assert reloaded.namespace.uid == "second-uid"
    assert json.loads(path.read_text())["namespace"]["uid"] == "second-uid"


def test_the_written_file_is_readable_by_an_operator(tmp_path):
    """It is the artifact an operator inspects when a bootstrap fails, so it is written
    sorted and indented rather than as one line."""
    path = tmp_path / "state.json"

    FileStateStore(path).save(_created())
    text = path.read_text()

    assert text.endswith("\n")
    assert "\n  " in text, "the state file is not indented, so it is unreadable by hand"
    keys = [line.split('"')[1] for line in text.splitlines() if line.startswith('  "')]
    assert keys == sorted(keys), (
        "the keys are unsorted, so diffs between attempts churn"
    )


@pytest.mark.parametrize(
    "value",
    [
        True,
        [],
        {"workspace_id": WORKSPACE_ID},
        {"workspace_id": "foreign", "prerequisites": []},
        {"workspace_id": WORKSPACE_ID, "prerequisites": [{"identifier": "rule"}]},
    ],
)
def test_malformed_or_foreign_durable_prerequisite_inventory_refuses(value):
    with pytest.raises(BootstrapRefused, match="inventory"):
        state_from_mapping({**_state().to_mapping(), "prerequisite_inventory": value})


def test_legacy_recorded_flag_does_not_invent_ownership():
    value = _state(prerequisites_recorded=True).to_mapping()
    value.pop("prerequisite_inventory")
    assert state_from_mapping(value).prerequisite_inventory is None
