"""Agreement with #5524's published contract and with the consumer's Protocol.

Issue #5525 (w6-02), EPIC #4910, Wave 6.

## Why these assertions exist rather than an import

`harness_jobs` duplicates three things the domain side also spells out:
`REQUIRED_PERMISSION`, the `OperationState` values, and the forbidden-parameter set.
Duplicated rather than imported because this package is installed independently of
`superplane-contracts` and must not require it on `sys.path` -- the same reasoning
`superplane_contracts.provisioning` gives for its own duplication of
`superplane_auth.policy.Permission.PROVISION`.

Duplication without a test is drift waiting to happen: the two spellings agree today
and nothing notices when one changes. These tests are what notices. They skip when the
contracts package is not importable, which is honest -- a skip says "not checked here",
whereas a silent pass would say "checked and agreed".
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

import harness_jobs

# The contracts package and the domain app live in the same repository but are separate
# installables. Added to `sys.path` for this test only, and only if present.
_REPO = Path(__file__).resolve().parents[4]
_CONTRACTS = _REPO / "modules" / "domain-apps" / "superplane" / "contracts"
_DOMAIN_APP = (
    _REPO / "modules" / "domain-apps" / "superplane" / "src" / "superplane-api"
)

for candidate in (_CONTRACTS, _DOMAIN_APP):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

provisioning = pytest.importorskip(
    "superplane_contracts.provisioning",
    reason=(
        "superplane-contracts is not importable here; the agreement between the two "
        "spellings is therefore not checked in this run rather than assumed"
    ),
)


# ---------------------------------------------------------------------------
# The shared constants
# ---------------------------------------------------------------------------


def test_the_required_permission_is_the_same_string():
    """A drifted permission string is an operation checked against nothing.

    If the store demanded `workspace:provision` and the domain contract demanded
    `workspace.provision`, a principal carrying one would be refused by the other --
    or, worse, a check written against the unused spelling would pass vacuously.
    """
    assert harness_jobs.REQUIRED_PERMISSION == provisioning.REQUIRED_PERMISSION


def test_the_operation_states_agree_by_value():
    """Values, because the stored and wire form is the string.

    Compared as sets of values rather than as enum members: they are deliberately
    different types (one is B's, one is the domain's), and what has to match is what
    crosses the boundary.
    """
    ours = {member.value for member in harness_jobs.OperationState}
    theirs = {member.value for member in provisioning.OperationState}
    assert ours == theirs


def test_the_terminal_states_agree():
    """Including UNKNOWN on both sides.

    If one side treated UNKNOWN as non-terminal, a poll loop across the boundary would
    never finish; if one side treated it as a failure, a provision that succeeded would
    be retried.
    """
    ours = {member.value for member in harness_jobs.TERMINAL_STATES}
    theirs = {member.value for member in provisioning.TERMINAL_STATES}
    assert ours == theirs
    assert "unknown" in ours


def _domain_refuses(key: str) -> bool:
    """Whether the domain contract refuses `key`.

    Wrapped because the two functions take different shapes -- the domain's takes a
    `ProvisioningIntent` whose parameters are a tuple of pairs, ours takes the dict the
    facade is handed. The *shapes* differing is fine; the *answers* differing is not,
    which is what these tests are about.
    """
    intent = provisioning.ProvisioningIntent(
        action="provision", parameters=((key, "x"),)
    )
    return bool(provisioning.forbidden_parameters(intent))


def test_the_forbidden_parameter_refusal_agrees_on_every_domain_key():
    """Whatever the domain contract refuses, this store refuses too.

    Asserted directionally on purpose: this store may refuse *more* keys than the
    domain contract does (a stricter store is safe), but it must never refuse fewer --
    a key the domain refuses and the store accepts is a smuggling route that the
    domain's own tests would report as closed, which is the worst kind of gap because
    it is covered by a passing test somewhere else.

    Driven from the domain's own constant rather than a hand-written probe list, so a
    key added there is checked here without anyone remembering to add it.
    """
    probes = set(provisioning.FORBIDDEN_PARAMETER_KEYS)
    probes |= {f"{prefix}thing" for prefix in provisioning.FORBIDDEN_PARAMETER_PREFIXES}
    assert probes, "the domain contract published no forbidden keys to compare against"

    missing = sorted(
        key
        for key in probes
        if _domain_refuses(key) and not harness_jobs.forbidden_parameters({key: "x"})
    )
    assert not missing, (
        f"refused by the domain contract but accepted by harness_jobs: {missing}. "
        "The stricter side must be the store."
    )


def test_a_benign_parameter_is_accepted_by_both():
    """The blocklist must not have become a denial of everything.

    Worth asserting because the cheap way to pass the containment test above is to
    refuse every key, and that would break provisioning entirely while looking like
    maximum safety.
    """
    for key in ("instance_size", "region", "accelerator", "node_count"):
        assert harness_jobs.forbidden_parameters({key: "a100"}) == ()
        assert not _domain_refuses(key)


def test_every_declared_shape_key_is_one_the_domain_contract_also_accepts():
    """The prefix-family exemption may not outrun the domain's own judgement.

    `_DECLARED_SHAPE_KEYS` exists because the `workspace_` family refused
    `workspace_name`, which the maintained caller sends as provisioning shape. That is a
    false positive worth fixing -- but the exemption is still a hole punched in a
    deliberately broad rule, so it has to stay inside the containment direction the test
    above asserts: the store may refuse more than the domain contract, never fewer.

    Driven from the constant rather than a copy of it, so adding a key there without the
    domain accepting it fails here instead of being discovered by a reviewer.
    """
    from harness_jobs.identity import _DECLARED_SHAPE_KEYS

    assert _DECLARED_SHAPE_KEYS, "the exemption set is empty; this asserts nothing"
    over_permissive = sorted(
        key for key in _DECLARED_SHAPE_KEYS if _domain_refuses(key)
    )
    assert not over_permissive, (
        f"exempted from the prefix families but refused by the domain contract: "
        f"{over_permissive}. The store must not accept a key the domain refuses."
    )


def test_no_declared_shape_key_collides_with_an_exact_identity_refusal():
    """The exemption must not be able to un-refuse an identity claim.

    `forbidden_parameters` applies the exact-name set first and only then consults the
    exemption, so this cannot happen through that function today. Asserted on the sets
    themselves anyway, because the ordering is one edit away from being reversed and the
    consequence would be silent: `workspace_id` accepted, tenant chosen by the caller.
    """
    from harness_jobs.identity import _DECLARED_SHAPE_KEYS, _FORBIDDEN_COMPACT

    collisions = sorted(
        key
        for key in _DECLARED_SHAPE_KEYS
        if key.replace("_", "") in _FORBIDDEN_COMPACT
    )
    assert not collisions, (
        f"these keys are both exempted and exactly refused: {collisions}. An exemption "
        "that can override an exact identity refusal is a caller-chosen tenant."
    )


# ---------------------------------------------------------------------------
# The port entry
# ---------------------------------------------------------------------------


def test_the_port_entry_names_this_package_as_the_owner():
    """The registry says who implements this; this test says we are that.

    Protects against the quieter failure: implementing a facade that satisfies nobody's
    declared port because the port was reassigned or renamed.
    """
    integration = pytest.importorskip("superplane_contracts.integration")
    entry = next(
        (
            port
            for port in integration.PRODUCTION_PORTS
            if port.name == "operation_facade"
        ),
        None,
    )
    assert entry is not None, "the operation_facade port is no longer published"
    assert entry.owner is integration.PortOwner.HARNESS_JOBS
    assert entry.required_permission == harness_jobs.REQUIRED_PERMISSION
    for bound in entry.bound_identifiers:
        assert bound in harness_jobs.OperationRecord.__dataclass_fields__, (
            f"the port requires an operation to be bound to {bound!r}, and the store "
            "does not persist it; an unbound operation is the duplicate-spend and "
            "cross-tenant-access failure class the registry names"
        )


def test_every_declared_refusal_name_has_a_translation():
    """The port's unknown answer is RAISE_UNAVAILABLE, and the names are the domain's.

    The registry matches declared names against the raised exception's MRO
    (`conformance.py:962`), and the declared names -- `ProvisioningUnavailable`,
    `ProvisioningRefused` -- belong to `app.services.provisioning`, which this package
    must not import. So this package's own exceptions cannot match by name, and the
    composition seam has to translate.

    `PORT_REFUSAL_NAMES` is that translation, published as data. This test is what
    makes it a checked obligation rather than a comment: if the registry adds a third
    declared refusal, this fails here instead of in the conformance lane, where an
    un-translated exception is read as "an adapter with a typo raised AttributeError"
    -- a crash rather than this port's answer, which is precisely the manufactured
    evidence the registry exists to prevent.
    """
    integration = pytest.importorskip("superplane_contracts.integration")
    entry = integration.PORTS_BY_NAME["operation_facade"]

    assert entry.unknown_outcome is integration.UnknownOutcome.RAISE_UNAVAILABLE

    declared = set(entry.refusal_exceptions)
    assert declared, "a RAISE_UNAVAILABLE port must name its refusal types"

    from harness_jobs.facade import PORT_REFUSAL_NAMES

    assert set(PORT_REFUSAL_NAMES.values()) == declared, (
        "the published translation does not cover exactly the port's declared "
        f"refusals {sorted(declared)}; an untranslated refusal reaches the "
        "conformance suite as an undeclared exception"
    )
    for ours in PORT_REFUSAL_NAMES:
        assert hasattr(harness_jobs, ours), (
            f"{ours} is named in the translation but not exported by this package"
        )


def test_this_package_does_not_import_the_domain_app():
    """The reason the translation is data and not behaviour.

    Asserted rather than trusted: an import of `app.*` here would couple a shared
    package to one consumer, and it is the change that would make the translation
    above look unnecessary right before it made this package uninstallable without
    the domain app.
    """
    import ast

    for path in sorted(Path(harness_jobs.__path__[0]).glob("*.py")):
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        for name in imported:
            root = name.split(".")[0]
            assert root not in ("app", "superplane_contracts", "superplane_auth"), (
                f"{path.name} imports {name!r}; this package is installed "
                "independently of the domain app and its contracts, and coupling it "
                "to one consumer is what makes a shared package unshippable"
            )

    # Parsed imports rather than a substring scan on purpose: every module here
    # *mentions* `superplane_contracts` in prose, because naming the contract a
    # duplicated constant agrees with is the whole reason the duplication is
    # defensible. A text search cannot tell a citation from a dependency.


def test_the_facade_matches_the_consumers_protocol():
    """Signature-for-signature, so composing this changes no call site.

    Checked by comparing signatures rather than by `isinstance` against a
    `runtime_checkable` Protocol, because the latter only checks that the attributes
    exist -- it would pass for a method taking entirely different arguments, which is
    precisely the drift that would force the consumer to change to suit its supplier.
    """
    services = pytest.importorskip(
        "app.services.provisioning",
        reason="the domain app is not importable here",
    )
    declared = services.OperationFacade
    implementation = harness_jobs.OperationFacadeService

    for name in ("open_operation", "report_progress"):
        assert hasattr(implementation, name), f"the facade is missing {name}"
        expected = inspect.signature(getattr(declared, name))
        actual = inspect.signature(getattr(implementation, name))
        assert list(expected.parameters) == list(actual.parameters), (
            f"{name} takes {list(actual.parameters)} but the consumer declares "
            f"{list(expected.parameters)}; a consumer changed to suit its supplier "
            "is how a port stops being a port"
        )
        for parameter in expected.parameters.values():
            if parameter.name == "self":
                continue
            assert actual.parameters[parameter.name].kind == parameter.kind, (
                f"{name}'s {parameter.name} changed calling convention"
            )


def test_the_progress_shape_matches_what_the_consumer_reads():
    """The consumer's three derived properties exist and mean the same thing."""
    services = pytest.importorskip(
        "app.services.provisioning",
        reason="the domain app is not importable here",
    )
    theirs = services.OperationProgress
    ours = harness_jobs.OperationProgress

    assert set(theirs.__dataclass_fields__) <= set(ours.__dataclass_fields__)

    succeeded = ours(operation_id="op", state="succeeded")
    unknown = ours(operation_id="op", state="unknown")
    failed = ours(operation_id="op", state="failed")

    assert succeeded.is_conclusive_success and succeeded.is_terminal
    assert failed.is_conclusive_failure and failed.is_terminal
    # The one that must not be collapsed: terminal, but neither a success nor a
    # failure. A consumer reading it as failure retries a provision that may have
    # happened.
    assert unknown.is_terminal
    assert not unknown.is_conclusive_success
    assert not unknown.is_conclusive_failure


def test_the_contract_version_is_the_published_one():
    version = pytest.importorskip("superplane_contracts.version")
    published = getattr(version, "CONTRACT_VERSION", None)
    if published is None:
        pytest.skip("the contracts package publishes no CONTRACT_VERSION constant")
    assert harness_jobs.CONTRACT_VERSION == published


def test_the_facade_reads_no_configuration():
    """The package cannot be a second composition root.

    Asserted by source inspection rather than by trusting the docstring: a facade that
    could build its own pool would make "is this port installed" a property of that
    file rather than of the reviewed startup sequence that installs it.

    Lives in the offline suite, not beside the facade's database tests. It was written
    there, where the module-level `requires_postgres` mark meant it skipped on any
    machine without a database -- so the one check that this package never grows a
    credential path was silent in precisely the setting where nobody would notice.
    """
    from harness_jobs import facade as module

    source = inspect.getsource(module)
    for smell in ("os.environ", "getenv", "create_pool", "DATABASE_URL"):
        # `connect(` is deliberately absent from this list: `self.connect()` is the
        # injected factory, which is the arrangement under test rather than a breach of
        # it.
        assert smell not in source, (
            f"{smell!r} appears in facade.py; connection and credential handling "
            "belongs to the composer, not to this package"
        )
