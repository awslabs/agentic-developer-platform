"""The integration registry describes the ports this repository actually declares.

Issue #5524 (w6-01), EPIC #4910, Wave 6. AC-01 and AC-02.

## What these tests are defending

The registry's value depends entirely on it staying true. A registry that has
drifted from the code is worse than no registry, because sixteen Wave 6 stories
would be implementing against a description of a system that no longer exists,
and every one of their suites would still pass.

So the tests below are mostly *drift* tests rather than behaviour tests. They open
each ``declared_at`` citation and confirm the port is still declared there; they
compare the registry's capability subset against the keys the API server actually
reports; and they assert the structural rules (every port binds an identifier,
every port declares an unknown answer, no externally-owned port is marked as
locally composable) that would otherwise decay silently.

The version-mismatch and identity-forgery cases are AC-01's negative evidence:
a registry entry written against a version this package does not serve, and a
probe set that must reject an adapter which admits a forged workspace.
"""

from __future__ import annotations

import ast
from pathlib import Path

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from superplane_contracts import (
    API_CAPABILITY_PORTS,
    CONTRACT_VERSION,
    PORTS_BY_NAME,
    PRODUCTION_PORTS,
    ContractViolation,
    PortContract,
    PortOwner,
    UnknownOutcome,
    check_port_version,
    externally_owned_ports,
    port,
    ports_owned_by,
)

# The module root, used to resolve `declared_at` citations. `tests/` sits directly
# under it, matching `_migration_path`/`_release_path`'s convention.
MODULE_ROOT = Path(__file__).resolve().parent.parent

# Protocol declarations the registry deliberately does not cite.
#
# Pinned as an exact set rather than asserted empty, because the one entry is a
# real finding this story surfaced but does not own: `OperationFacade` is declared
# twice, in the contracts package and again in the API service, with different and
# incompatible surfaces. The registry cites the API-side one because that is the
# declaration a production composition installs against
# (`provisioning.py:set_operation_facade`).
#
# An exact set makes both drift directions fail: a newly added port that nobody
# described shows up here, and a resolution of the duplication makes this stale.
# Asserting "empty" would have forced this story to reconcile two owners'
# declarations, which is outside it; asserting "no more than N" would let the next
# undescribed port hide in the allowance.
KNOWN_UNCITED_PROTOCOLS = frozenset(
    {"contracts/superplane_contracts/provisioning_adapter.py:83"}
)


class TestRegistryMatchesTheCode:
    """Every citation resolves, so a moved or renamed port cannot leave a stale entry."""

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_declared_at_file_exists(self, contract: PortContract) -> None:
        path, _, line = contract.declared_at.rpartition(":")
        resolved = MODULE_ROOT / path
        assert resolved.is_file(), (
            f"port {contract.name!r} cites {contract.declared_at}, but "
            f"{resolved} does not exist — the port moved and the registry did not"
        )
        assert line.isdigit(), (
            f"declared_at must end in a line number: {contract.declared_at}"
        )

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_declared_line_is_a_protocol_or_class_declaration(
        self, contract: PortContract
    ) -> None:
        """The cited line still declares something.

        Deliberately a weak assertion — it checks that the line declares a class,
        not that the class has a particular name — because line numbers shift by a
        few lines routinely and a brittle exact-name-at-exact-line check would fail
        on unrelated edits and get deleted. What it does catch is the case that
        matters: a citation pointing into the middle of a docstring or past the end
        of a file, which means the port is no longer where the registry says.
        """
        path, _, line_number = contract.declared_at.rpartition(":")
        lines = (MODULE_ROOT / path).read_text().splitlines()
        index = int(line_number) - 1
        assert 0 <= index < len(lines), (
            f"port {contract.name!r} cites line {line_number} of {path}, which has "
            f"only {len(lines)} lines"
        )
        # Search a small window: an edit above the declaration shifts it slightly,
        # and failing on that would make this test noise rather than signal.
        window = "\n".join(lines[max(0, index - 5) : index + 6])
        assert "class " in window, (
            f"port {contract.name!r} cites {contract.declared_at}, but no class "
            "declaration is within five lines of it"
        )

    def test_every_declared_protocol_has_a_registry_entry(self) -> None:
        """A port declared in this repository must be described in the registry.

        This is the drift direction that would otherwise go unnoticed: a new
        ``Protocol`` is a new thing a production composition must supply, and if the
        registry does not mention it, no Wave 6 story is told to build it and the
        readiness check does not probe it.

        Matched on ``declared_at`` citations rather than on names. Port names are
        snake_case capability keys and Protocol names are CamelCase, and no
        normalization between them is faithful — ``trusted_delivery`` is
        ``TrustedDeliveryChannel``, ``provider_authority`` is
        ``ProviderAuthorityValidator``. A fuzzy name match would have to be loose
        enough to pair those, and once it is that loose it silently pairs a new
        Protocol with an unrelated entry and reports full coverage. The citation is
        exact, and it is already the thing a reviewer opens.
        """
        uncited = self._declared_protocol_sites() - {
            contract.declared_at for contract in PRODUCTION_PORTS
        }
        assert uncited == KNOWN_UNCITED_PROTOCOLS, (
            "the set of Protocol declarations with no registry entry changed.\n"
            f"  uncited now: {sorted(uncited)}\n"
            f"  expected:    {sorted(KNOWN_UNCITED_PROTOCOLS)}\n"
            "A port nobody describes is a port no Wave 6 story is told to build. "
            "If a port was added, give it a registry entry; if one of the known "
            "duplicates was resolved, shrink KNOWN_UNCITED_PROTOCOLS."
        )

    def test_the_two_operation_facade_protocols_have_different_surfaces(self) -> None:
        """The one uncited declaration is a genuine hazard, so it is pinned here.

        ``OperationFacade`` is declared twice under the same name with different
        method sets: the contracts-package one
        (``provisioning_adapter.py:83``) is synchronous and reports progress plus
        attempt start/finish, while the API-side one (``provisioning.py:226``) is
        async and opens the operation. The registry cites the API-side declaration
        because that is the one a production composition installs, via
        ``set_operation_facade``.

        This test exists because the duality is the kind of thing that reads as a
        harmless redundancy and is not one: an implementer told to "implement
        ``operation_facade``" has two incompatible targets with the same name, and
        satisfying the wrong one produces an object that passes a
        ``runtime_checkable`` ``isinstance`` against the other while implementing
        none of the methods that other actually calls. Asserting the surfaces differ
        keeps the hazard visible until the declarations are reconciled, rather than
        leaving it as a comment nobody reads.
        """
        adapter_side = self._method_names(
            MODULE_ROOT
            / "contracts"
            / "superplane_contracts"
            / "provisioning_adapter.py",
            after_line=83,
        )
        api_side = self._method_names(
            MODULE_ROOT
            / "src"
            / "superplane-api"
            / "app"
            / "services"
            / "provisioning.py",
            after_line=int(port("operation_facade").declared_at.rsplit(":", 1)[1]),
        )
        assert adapter_side and api_side
        assert adapter_side != api_side, (
            "the two OperationFacade Protocols now declare the same surface; if they "
            "were reconciled into one declaration, drop this test and the "
            "KNOWN_UNCITED_PROTOCOLS entry"
        )
        # `report_progress` is the overlap, and it is the reason a wrong
        # implementation is not immediately obvious: the shared name makes the two
        # look interchangeable at a glance.
        assert "report_progress" in adapter_side & api_side

    @staticmethod
    def _declared_protocol_sites() -> set[str]:
        """Every ``path:line`` in this module that declares a ``Protocol``."""
        sites: set[str] = set()
        roots = (
            MODULE_ROOT / "contracts" / "superplane_contracts",
            MODULE_ROOT / "src" / "superplane-api" / "app",
        )
        for root in roots:
            for source in sorted(root.rglob("*.py")):
                if source.name in ("integration.py", "conformance.py"):
                    # The registry and the probes describe ports; they declare none.
                    continue
                for number, line in enumerate(source.read_text().splitlines(), start=1):
                    stripped = line.strip()
                    if stripped.startswith("class ") and "Protocol" in stripped:
                        relative = source.relative_to(MODULE_ROOT).as_posix()
                        sites.add(f"{relative}:{number}")
        return sites

    @staticmethod
    def _method_names(source: Path, *, after_line: int) -> set[str]:
        """The method names declared in the class beginning at ``after_line``."""
        names: set[str] = set()
        for line in source.read_text().splitlines()[after_line:]:
            if line and not line[0].isspace():
                break  # left the class body
            stripped = line.strip()
            for prefix in ("def ", "async def "):
                if stripped.startswith(prefix):
                    names.add(stripped[len(prefix) :].split("(", 1)[0])
        return names

    def test_api_capability_subset_matches_the_servers_readout(self) -> None:
        """The capability keys the registry names are the ones the API probes.

        Read as source rather than by importing ``app.capability_probes``: this suite
        runs in the module lane, where ``app.*`` is not importable (the module
        ``conftest.py`` sets ``collect_ignore = ["src"]`` and the API package is
        installed only in the transferred lane). Reading the source keeps the
        assertion in the lane that gates every domain PR instead of only the one
        that gates API changes.

        ``capability_probes.py`` carries its own import-time guard on the same
        invariant, which is the stronger check — but it only fires where the module
        imports, so a PR that edits the registry alone would reach review with the
        two out of step and this lane green. Both exist for that reason.

        Parsed with ``ast`` rather than scanned for quoted lines: the probe table's
        values are multi-line tuples, so a line-shaped reader picks up whichever
        string literals happen to sit at the right indent and would keep passing
        while reading the wrong thing.
        """
        source = (
            MODULE_ROOT / "src" / "superplane-api" / "app" / "capability_probes.py"
        ).read_text()
        probed: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            target = getattr(node, "target", None)
            if not isinstance(node, ast.AnnAssign) or not isinstance(target, ast.Name):
                continue
            if target.id != "_PROBES" or not isinstance(node.value, ast.Dict):
                continue
            probed = {
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            break
        assert probed, "no `_PROBES` table found in app/capability_probes.py"
        assert probed == set(API_CAPABILITY_PORTS), (
            "the registry's API capability set and the probe table disagree: "
            f"registry={sorted(API_CAPABILITY_PORTS)} probed={sorted(probed)}"
        )

    def test_the_api_readout_is_not_a_presence_check(self) -> None:
        """`capabilities()` establishes each port by probing, not by `is not None`.

        The defect this story fixes, pinned at the place it would return. Four
        ``is not None`` tests are a plausible-looking capability check — a later edit
        that inlines them back for simplicity reads as a cleanup, and every existing
        test still passes because an uncomposed image answers False either way.
        Only a composed-looking placeholder tells the two apart, and this lane has
        no adapters to install; asserting on the source is what it can do.
        """
        source = (
            MODULE_ROOT / "src" / "superplane-api" / "app" / "installation.py"
        ).read_text()
        body = source.split("def capabilities(", 1)[1].split("\ndef ", 1)[0]
        for getter in (
            "get_credential_evidence_reader",
            "get_provider_authority_validator",
            "get_allocation_inventory_reader",
            "get_operation_facade",
        ):
            assert f"{getter}() is not None" not in body, (
                f"`capabilities()` tests `{getter}()` for presence again; a capability "
                "is established by calling the adapter and requiring it to refuse "
                "(see app/capability_probes.py)"
            )


class TestStructuralRules:
    """Properties that hold by construction, asserted so an edit cannot relax them."""

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_every_port_binds_an_identifier(self, contract: PortContract) -> None:
        """An unbound operation cannot be deduplicated or tenant-scoped.

        This is the duplicate-spend and cross-tenant-access failure class the
        story's impact analysis names: without an operation identity a repeat is
        indistinguishable from a new request.
        """
        assert contract.bound_identifiers

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_every_port_declares_an_unknown_answer(
        self, contract: PortContract
    ) -> None:
        """A port with no declared unknown answer is one whose silence reads as success."""
        assert isinstance(contract.unknown_outcome, UnknownOutcome)

    @pytest.mark.parametrize("contract", PRODUCTION_PORTS, ids=lambda c: c.name)
    def test_every_unimplemented_port_names_a_live_verifier(
        self, contract: PortContract
    ) -> None:
        """AC-02: no criterion ends at a mocked adapter.

        ``submitter_resolver`` is the one exemption and it is exempt for a stated
        reason: it is already implemented by ``ConfiguredSubmitterResolver`` and
        covered by the existing observation-auth suites, so no Wave 6 live verifier
        owes anything for it. Naming a verifier who owes nothing would be worse than
        the blank, because the matrix would then claim coverage that no one is
        actually going to produce.
        """
        if contract.name == "submitter_resolver":
            assert contract.owner is PortOwner.DOMAIN
            return
        assert contract.live_verifier.strip(), (
            f"port {contract.name!r} has no live verifier; an offline-only "
            "criterion cannot close a capability that must work against a provider"
        )

    def test_externally_owned_ports_are_not_locally_composable(self) -> None:
        """The domain app must not be able to supply its own authority.

        If it could satisfy ``operation_facade`` locally it would be authorizing its
        own operations, which is the separation the facade exists to create.
        """
        external = externally_owned_ports()
        assert external, "the registry describes no externally-owned port"
        for contract in external:
            assert not contract.is_composed_locally
            assert contract.owner is not PortOwner.DOMAIN

    def test_most_ports_are_owned_outside_the_domain(self) -> None:
        """A registry where the domain owns everything would describe nothing useful."""
        domain_owned = ports_owned_by(PortOwner.DOMAIN)
        assert len(domain_owned) < len(PRODUCTION_PORTS) / 2

    def test_read_only_ports_demand_no_write_permission(self) -> None:
        """A port that confers no authority must not require one.

        A read that demands a write permission invites every caller to hold the
        write, which is the broad-authority substitution the design forbids.
        """
        for contract in PRODUCTION_PORTS:
            if contract.required_permission is None:
                continue
            assert contract.required_permission.startswith("workspace:"), (
                f"port {contract.name!r} demands {contract.required_permission!r}, "
                "which is not a workspace-scoped domain permission"
            )


class TestRegistryRefusesMalformedEntries:
    """Negative cases: the constructor is the validator, so these must not construct."""

    def _valid_kwargs(self) -> dict[str, object]:
        return {
            "name": "probe_port",
            "owner": PortOwner.HARNESS_JOBS,
            "declared_at": "contracts/superplane_contracts/adapter.py:88",
            "purpose": "a port used only by these negative tests",
            "bound_identifiers": ("operation_id",),
            "required_permission": "workspace:provision",
            "acts_as": "the test",
            "unknown_outcome": UnknownOutcome.NONE_MEANS_UNVERIFIED,
        }

    def test_port_with_no_bound_identifier_is_refused(self) -> None:
        with pytest.raises(
            ContractViolation, match="must bind at least one identifier"
        ):
            PortContract(**{**self._valid_kwargs(), "bound_identifiers": ()})

    def test_duplicate_bound_identifier_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="duplicate bound identifier"):
            PortContract(
                **{
                    **self._valid_kwargs(),
                    "bound_identifiers": ("operation_id", "operation_id"),
                }
            )

    def test_blank_permission_is_refused_while_none_is_accepted(self) -> None:
        """Blank is worse than absent.

        Absent says "this port confers no authority". Blank reads as a permission
        that happens to be unnamed — and an unnamed permission compares equal to
        nothing, so every check against it silently passes.
        """
        with pytest.raises(ContractViolation, match="non-empty string or None"):
            PortContract(**{**self._valid_kwargs(), "required_permission": "  "})
        assert (
            PortContract(
                **{**self._valid_kwargs(), "required_permission": None}
            ).required_permission
            is None
        )

    def test_unsupported_contract_version_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="unsupported contract version"):
            PortContract(**{**self._valid_kwargs(), "contract_version": "v0"})

    def test_declared_at_without_a_line_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="'path:line' evidence"):
            PortContract(**{**self._valid_kwargs(), "declared_at": "adapter.py"})

    def test_unknown_outcome_must_be_the_enum(self) -> None:
        """A string is refused, so an entry cannot declare an unknown answer nobody models."""
        with pytest.raises(ContractViolation, match="must be an UnknownOutcome"):
            PortContract(**{**self._valid_kwargs(), "unknown_outcome": "unknown"})

    def test_owner_must_be_the_enum(self) -> None:
        with pytest.raises(ContractViolation, match="must be a PortOwner"):
            PortContract(**{**self._valid_kwargs(), "owner": "harness_jobs"})

    @pytest.mark.parametrize("field_name", ["name", "purpose", "acts_as"])
    def test_required_text_fields_reject_blanks(self, field_name: str) -> None:
        with pytest.raises(ContractViolation, match="is required"):
            PortContract(**{**self._valid_kwargs(), field_name: "   "})


class TestLookupAndVersioning:
    """AC-01: stale versions are rejected, and an unknown port is an error not a None."""

    def test_unknown_port_raises_rather_than_returning_none(self) -> None:
        """A ``None`` flowing onward becomes an AttributeError somewhere less obvious."""
        with pytest.raises(ContractViolation, match="unknown production port"):
            port("no_such_port")

    def test_current_version_is_accepted(self) -> None:
        result = check_port_version("operation_facade", CONTRACT_VERSION)
        assert result.accepted
        assert result.version == CONTRACT_VERSION

    @pytest.mark.parametrize("stale", ["v0", "v2", "V1", "1", "v1 "])
    def test_stale_or_unserved_versions_are_refused(self, stale: str) -> None:
        """Including the near-misses.

        ``"V1"`` and ``"1"`` are the interesting ones: a lenient comparison would
        accept them, and accepting a version whose exact form this package never
        agreed to is how two ends end up interpreting the same bytes differently.
        ``"v1 "`` is accepted after stripping, matching ``version.check_version``'s
        documented behaviour, so it is listed here only to pin which of the two it is.
        """
        result = check_port_version("operation_facade", stale)
        if stale.strip() == CONTRACT_VERSION:
            assert result.accepted
        else:
            assert not result.accepted
            assert result.reason

    @pytest.mark.parametrize("absent", [None, "", "   ", 1, object()])
    def test_missing_or_non_string_versions_are_refused(self, absent: object) -> None:
        """Fail-closed: an absent version is never defaulted to the current one."""
        assert not check_port_version("operation_facade", absent).accepted

    def test_ports_owned_by_requires_the_enum(self) -> None:
        with pytest.raises(ContractViolation, match="must be a PortOwner"):
            ports_owned_by("harness_jobs")

    def test_every_owner_with_ports_is_reachable_by_lookup(self) -> None:
        for owner in PortOwner:
            for contract in ports_owned_by(owner):
                assert port(contract.name) is contract

    def test_the_name_index_covers_every_port_exactly_once(self) -> None:
        """A dropped entry would make ``port()`` raise for a port that exists.

        ``PORTS_BY_NAME`` is a dict comprehension over ``PRODUCTION_PORTS``, so a
        duplicate name silently collapses two entries into one — last one wins, and
        the losing port's obligations vanish without an error. The registry has an
        import-time guard for exactly that; this asserts the guard's postcondition
        holds rather than trusting that it ran.
        """
        assert len(PORTS_BY_NAME) == len(PRODUCTION_PORTS)
        assert set(PORTS_BY_NAME) == {item.name for item in PRODUCTION_PORTS}
        for name, contract in PORTS_BY_NAME.items():
            assert contract.name == name


class TestRegistryConfersNothing:
    """The registry describes obligations and hands back no implementation."""

    def test_registry_module_imports_no_application_code(self) -> None:
        """A registry that could import an adapter would be a second composition root.

        Asserted as an absence because absences decay: nothing stops a future edit
        from importing ``app.services`` to "check whether the port is wired", and
        that import is what would let this package answer a question only a running
        process can answer.
        """
        source = (
            MODULE_ROOT / "contracts" / "superplane_contracts" / "integration.py"
        ).read_text()
        for forbidden in ("import app", "from app", "httpx", "boto3", "sqlalchemy"):
            assert forbidden not in source, (
                f"integration.py references {forbidden!r}; the registry must not be "
                "able to reach an implementation or a provider"
            )

    def test_no_port_carries_an_implemented_or_ready_flag(self) -> None:
        """Whether a port is composed is a property of a process, not of this file.

        A boolean here would be a claim about the world that no observation
        supports, kept truthful only by someone remembering to edit it.
        """
        for contract in PRODUCTION_PORTS:
            for forbidden in ("implemented", "ready", "available", "composed", "wired"):
                assert not hasattr(contract, forbidden), (
                    f"PortContract exposes {forbidden!r}; composition state belongs "
                    "to `app.installation.capabilities()`, not to the registry"
                )

    def test_entries_are_immutable(self) -> None:
        """A mutable entry is a relaxable obligation."""
        contract = port("operation_facade")
        with pytest.raises(Exception):  # noqa: B017 - dataclasses raise FrozenInstanceError
            contract.required_permission = "workspace:read"  # type: ignore[misc]
