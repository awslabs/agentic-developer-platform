"""Tests for the deployment manifest contract (#5150, ENGINE-D1).

These assertions are about *refusals*. The manifest's value is entirely in what it
declines to approve, so a test that only proved the happy path would pass against
a module that approved everything. Each test below therefore names the
over-approval it would catch.

No database, no network, no AWS — this module performs no I/O by design, and these
tests run in-process. The concurrency behaviour built on top of it is tested in
`test_environment_leases.py` and, where locking matters, against real PostgreSQL in
`test_environment_leases_postgres.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.orchestration.deployment_manifest import (
    CANONICAL_KEY_VERSION,
    MANIFEST_SCHEMA_VERSION,
    DeploymentManifest,
    EntryStatus,
    ManifestEntry,
    ManifestError,
    PhysicalTarget,
    TargetBlockCode,
    TargetEvidence,
    WorkflowRef,
    canonical_target_key,
    load_manifest_document,
    parse_manifest,
    resolve_manifest_entry,
)

# A syntactically valid 40-character SHA. Not a real commit: these tests never
# resolve it against the repository, and using a recognisable filler makes it
# obvious in a failure message that the value is a fixture rather than a pin.
SHA_A = "a" * 40
SHA_B = "b" * 40

EVIDENCE = TargetEvidence(
    source="verified-aws-connection:conn-1",
    verified_at="2026-09-18T00:00:00+00:00",
    detail="sts:AssumeRole readback",
)


def _target(
    *,
    account_id: str = "000000000000",
    region: str = "us-east-1",
    resource_kind: str = "eks-namespace",
    resource_id: str = "cluster-a/adp-gateway",
) -> PhysicalTarget:
    """A physical target with placeholder identifiers.

    `000000000000` is deliberately not a plausible real AWS account: the story
    forbids inventing account ids to make examples work, and a fixture that looked
    like a real account could be copied into configuration by someone skimming.
    """
    return PhysicalTarget(
        provider="aws",
        account_id=account_id,
        region=region,
        resource_kind=resource_kind,
        resource_id=resource_id,
        evidence=EVIDENCE,
    )


def _enabled_entry(**overrides) -> ManifestEntry:
    defaults = {
        "entry_id": "env-1",
        "status": EntryStatus.ENABLED,
        "connection_id": "conn-1",
        "component_selectors": ("gateway-backend",),
        "workflow": WorkflowRef(
            path=".github/workflows/gateway-deploy.yml",
            definition_revision=SHA_A,
            allowed_inputs={"environment": frozenset({"dev"}), "account_id": frozenset()},
        ),
        "resource_kind": "eks-namespace",
        "resource_id": "cluster-a/adp-gateway",
        "verification_adapter": "gateway-health-verification",
    }
    defaults.update(overrides)
    return ManifestEntry(**defaults)


def _resolve(entry: ManifestEntry, **kwargs):
    manifest = DeploymentManifest(schema_version=MANIFEST_SCHEMA_VERSION, entries=(entry,))
    params = {
        "entry_id": entry.entry_id,
        "component": "gateway-backend",
        "policy_connection_ids": frozenset({"conn-1"}),
        "target_lookup": lambda _cid: _target(),
    }
    params.update(kwargs)
    return resolve_manifest_entry(manifest, **params)


class TestWorkflowPinning:
    """A workflow reference must pin a pipeline, not a filename."""

    def test_abbreviated_revision_is_refused(self):
        """An abbreviation names a prefix, not a commit.

        Expanding it would require reading the repository, which this module never
        does — so accepting one would store an approval whose subject this process
        never actually determined, and a later ambiguous prefix would resolve
        somewhere else.
        """
        with pytest.raises(ManifestError) as exc:
            WorkflowRef(path=".github/workflows/gateway-deploy.yml", definition_revision="a" * 7)
        assert exc.value.code == "invalid_workflow_revision"

    def test_missing_revision_is_refused(self):
        """Path-only would approve whatever that filename contains after the next merge."""
        with pytest.raises(ManifestError):
            WorkflowRef(path=".github/workflows/gateway-deploy.yml", definition_revision="")

    @pytest.mark.parametrize(
        "path",
        [
            "gateway-deploy.yml",  # not under .github/workflows
            "../../.github/workflows/gateway-deploy.yml",  # traversal
            ".github/workflows/../../evil.yml",  # traversal after the prefix
            ".github/workflows/gateway-deploy.sh",  # not a workflow definition
            ".github/workflows/nested/deploy.yml",  # workflows are not nested
        ],
    )
    def test_non_workflow_paths_are_refused(self, path):
        """The path must be anchored under .github/workflows.

        Without anchoring, a traversal or an arbitrary script path would be
        approvable, and the dispatcher would be pointed at something no reviewer
        read as a workflow.
        """
        with pytest.raises(ManifestError) as exc:
            WorkflowRef(path=path, definition_revision=SHA_A)
        assert exc.value.code == "invalid_workflow_path"


class TestAllowedInputs:
    """Approving an input name is not the same as approving its values."""

    def test_unapproved_value_is_refused_for_an_approved_name(self):
        """THE input test: `environment` is approved, but only at `dev`.

        A name-only allow-list would accept `prod` here. That is the exact
        escalation this design exists to prevent, and it would read as correct in
        review because the input name genuinely is approved.
        """
        ref = WorkflowRef(
            path=".github/workflows/gateway-deploy.yml",
            definition_revision=SHA_A,
            allowed_inputs={"environment": frozenset({"dev"})},
        )
        assert ref.check_inputs({"environment": "dev"}) is None
        assert ref.check_inputs({"environment": "prod"}) == "input_value_not_permitted:environment"

    def test_input_absent_from_the_allow_list_may_not_be_passed(self):
        ref = WorkflowRef(
            path=".github/workflows/gateway-deploy.yml",
            definition_revision=SHA_A,
            allowed_inputs={"environment": frozenset({"dev"})},
        )
        assert ref.check_inputs({"ref": "main"}) == "input_not_permitted:ref"

    def test_empty_value_set_permits_the_default_only_not_any_value(self):
        """An empty set means "pass through at the workflow's own default".

        Reading it as "any value allowed" is the plausible misreading, and it would
        let a caller choose the AWS account — the untrusted account string this
        story exists to eliminate.
        """
        ref = WorkflowRef(
            path=".github/workflows/gateway-deploy.yml",
            definition_revision=SHA_A,
            allowed_inputs={"account_id": frozenset()},
        )
        assert ref.check_inputs({}) is None
        assert ref.check_inputs({"account_id": "000000000000"}) == "input_value_not_permitted:account_id"

    def test_refusal_does_not_enumerate_permitted_values(self):
        """The reason names the offending input but not what would be accepted.

        An enumerating refusal is a probe oracle for an approval set the caller has
        no standing to read.
        """
        ref = WorkflowRef(
            path=".github/workflows/gateway-deploy.yml",
            definition_revision=SHA_A,
            allowed_inputs={"environment": frozenset({"dev", "staging"})},
        )
        reason = ref.check_inputs({"environment": "prod"})
        assert "staging" not in reason
        assert "dev" not in reason

    def test_mutable_value_set_is_refused(self):
        """A `set` could be mutated after review, widening an approval in place."""
        with pytest.raises(ManifestError) as exc:
            WorkflowRef(
                path=".github/workflows/gateway-deploy.yml",
                definition_revision=SHA_A,
                allowed_inputs={"environment": {"dev"}},
            )
        assert exc.value.code == "invalid_workflow_input"


class TestPhysicalTargetIdentity:
    """A target is a surface, not an account, and it must be evidenced."""

    def test_target_cannot_be_constructed_without_evidence(self):
        """Evidence is a required field, so there is no unevidenced-target path."""
        with pytest.raises(TypeError):
            PhysicalTarget(
                provider="aws",
                account_id="000000000000",
                region="us-east-1",
                resource_kind="eks-namespace",
                resource_id="cluster-a/adp-gateway",
            )

    def test_evidence_requires_a_source_and_a_time(self):
        with pytest.raises(ManifestError) as exc:
            TargetEvidence(source="", verified_at="2026-09-18T00:00:00+00:00")
        assert exc.value.code == "invalid_target_evidence"
        with pytest.raises(ManifestError):
            TargetEvidence(source="verified-aws-connection:conn-1", verified_at="")

    def test_non_account_shaped_id_is_refused(self):
        with pytest.raises(ManifestError) as exc:
            _target(account_id="not-an-account")
        assert exc.value.code == "invalid_physical_target"

    def test_aliases_for_one_surface_produce_one_key(self):
        """THE collision test.

        Two connections — different ids, different tenants, different labels — that
        name one cluster must produce one key, because that key is what the lease
        table enforces uniqueness on. If the key were derived from the connection
        id, both holders would believe they had exclusive access and would deploy
        incompatible releases on top of each other.
        """
        alias_one = _target()
        alias_two = PhysicalTarget(
            provider="AWS",  # provider casing differs
            account_id="000000000000",
            region="US-EAST-1",  # region casing differs
            resource_kind="EKS-Namespace",
            resource_id="Cluster-A/ADP-Gateway",
            # Different tenant, different connection, different readback moment.
            evidence=TargetEvidence(source="verified-aws-connection:conn-99", verified_at="2026-09-18T12:00:00+00:00"),
        )
        assert alias_one.canonical_key == alias_two.canonical_key

    def test_distinct_surfaces_in_one_account_do_not_collide(self):
        """An account is too coarse to serialize on.

        If the key were account-level, these two independent namespaces would block
        each other. A lease that blocks unrelated work trains operators to bypass
        it, and a lease people bypass protects nothing.
        """
        a = _target(resource_id="cluster-a/adp-gateway")
        b = _target(resource_id="cluster-a/adp-agents")
        c = _target(resource_id="cluster-b/adp-gateway")
        assert len({a.canonical_key, b.canonical_key, c.canonical_key}) == 3

    def test_region_is_part_of_the_identity(self):
        assert _target(region="us-east-1").canonical_key != _target(region="us-west-2").canonical_key

    def test_component_boundary_cannot_be_shifted_to_forge_a_key(self):
        """`("a","bc")` must not hash like `("ab","c")`.

        A boundary-shift collision would merge two unrelated targets into one lease,
        silently serializing deployments that should be independent.
        """
        shifted = canonical_target_key(
            provider="aws",
            account_id="000000000000",
            region="us-east-1",
            resource_kind="eks",
            resource_id="-namespacecluster-a/adp-gateway",
        )
        assert shifted != _target().canonical_key

    def test_separator_inside_a_component_is_refused(self):
        with pytest.raises(ManifestError) as exc:
            canonical_target_key(
                provider="aws",
                account_id="000000000000",
                region="us-east-1",
                resource_kind="eks-namespace",
                resource_id="cluster-a\x1fadp-gateway",
            )
        assert exc.value.code == "invalid_target_key"

    def test_blank_component_is_refused(self):
        with pytest.raises(ManifestError):
            canonical_target_key(
                provider="aws",
                account_id="000000000000",
                region="us-east-1",
                resource_kind="eks-namespace",
                resource_id="   ",
            )

    def test_key_is_opaque_and_version_tagged(self):
        """The key is returned in refusals, so it must disclose nothing.

        Version-tagged so that if the canonicalization rule ever changes, old keys
        are distinguishable rather than silently reinterpreted — a reinterpreted key
        would compare unequal to a live holder's and hand out a held target.
        """
        key = _target().canonical_key
        assert key.startswith(f"{CANONICAL_KEY_VERSION}:")
        assert "000000000000" not in key
        assert "cluster-a" not in key
        assert "us-east-1" not in key
        # Must fit the column the lease table declares (String(128)).
        assert len(key) <= 128


class TestManifestEntryReview:
    """An entry is a unit of human approval, so it must be complete or explained."""

    @pytest.mark.parametrize(
        "missing",
        ["connection_id", "workflow", "resource_kind", "resource_id", "verification_adapter"],
    )
    def test_enabled_entry_must_be_fully_specified(self, missing):
        """Discovering a missing field at dispatch time turns an approval into an outage."""
        with pytest.raises(ManifestError) as exc:
            _enabled_entry(**{missing: None})
        assert exc.value.code == "incomplete_manifest_entry"
        assert missing in str(exc.value)

    def test_enabled_entry_must_cover_at_least_one_component(self):
        with pytest.raises(ManifestError):
            _enabled_entry(component_selectors=())

    def test_non_enabled_entry_must_say_why(self):
        """An unexplained disabled entry is indistinguishable from an oversight.

        The entire value of an explicit disabled entry over an absent one is that it
        states what is missing and who resolves it.
        """
        with pytest.raises(ManifestError) as exc:
            ManifestEntry(
                entry_id="env-1",
                status=EntryStatus.UNRESOLVED,
                connection_id=None,
                component_selectors=(),
                workflow=None,
            )
        assert exc.value.code == "unexplained_manifest_entry"

    def test_selectors_do_not_match_by_prefix(self):
        """Exact match only.

        A selector meant to approve `gateway-backend` must not widen to
        `gateway-backend-admin` — an over-approval a reviewer cannot see when
        reading the manifest.
        """
        entry = _enabled_entry(component_selectors=("gateway-backend",))
        assert entry.covers("gateway-backend")
        assert entry.covers("  Gateway-Backend  ")  # normalized, not widened
        assert not entry.covers("gateway-backend-admin")
        assert not entry.covers("gateway")

    def test_duplicate_entry_ids_are_refused(self):
        """Two entries for one id would make "the approval for X" order-dependent."""
        with pytest.raises(ManifestError) as exc:
            DeploymentManifest(
                schema_version=MANIFEST_SCHEMA_VERSION,
                entries=(_enabled_entry(), _enabled_entry(connection_id="conn-2")),
            )
        assert exc.value.code == "duplicate_manifest_entry"

    def test_future_schema_version_is_refused(self):
        """A newer document may narrow something this build does not implement.

        Reading it with today's rules would drop that narrowing and proceed with an
        approval nobody granted.
        """
        with pytest.raises(ManifestError) as exc:
            DeploymentManifest(schema_version=MANIFEST_SCHEMA_VERSION + 1, entries=())
        assert exc.value.code == "unsupported_schema_version"


class TestResolution:
    """What `resolve_manifest_entry` refuses, and who each refusal routes to."""

    def test_enabled_entry_resolves_to_its_target(self):
        resolution = _resolve(_enabled_entry())
        assert resolution.resolved
        assert resolution.block is None
        assert resolution.target.canonical_key == _target().canonical_key

    def test_unknown_entry_is_blocked_not_defaulted(self):
        manifest = DeploymentManifest(schema_version=MANIFEST_SCHEMA_VERSION, entries=(_enabled_entry(),))
        resolution = resolve_manifest_entry(
            manifest,
            entry_id="no-such-entry",
            component="gateway-backend",
            policy_connection_ids=frozenset({"conn-1"}),
            target_lookup=lambda _cid: _target(),
        )
        assert not resolution.resolved
        assert resolution.block.code is TargetBlockCode.ENTRY_UNKNOWN

    def test_disabled_and_unresolved_route_to_different_owners(self):
        """Collapsing these two would send the wrong person to fix it.

        A disabled entry needs another review; an unresolved one needs the live
        connection or surface to exist. Those are different people and different
        actions.
        """
        disabled = _resolve(
            ManifestEntry(
                entry_id="env-1",
                status=EntryStatus.DISABLED,
                connection_id="conn-1",
                component_selectors=("gateway-backend",),
                workflow=None,
                unresolved_reason="Withdrawn pending re-review.",
            )
        )
        unresolved = _resolve(
            ManifestEntry(
                entry_id="env-1",
                status=EntryStatus.UNRESOLVED,
                connection_id=None,
                component_selectors=("gateway-backend",),
                workflow=None,
                unresolved_reason="No verified connection for this environment yet.",
            )
        )
        assert disabled.block.code is TargetBlockCode.ENTRY_DISABLED
        assert unresolved.block.code is TargetBlockCode.TARGET_UNRESOLVED
        assert disabled.block.owner != unresolved.block.owner

    def test_uncovered_component_is_refused(self):
        resolution = _resolve(_enabled_entry(), component="agent-runtime")
        assert resolution.block.code is TargetBlockCode.COMPONENT_NOT_COVERED

    def test_policy_withdrawal_overrides_a_reviewed_manifest(self):
        """THE independence test.

        The manifest still approves the target, but the in-force plan no longer
        permits it. If only the manifest were consulted, a withdrawn target would
        stay deployable until somebody remembered to edit a YAML file.
        """
        resolution = _resolve(_enabled_entry(), policy_connection_ids=frozenset({"conn-other"}))
        assert resolution.block.code is TargetBlockCode.POLICY_TARGET_MISMATCH
        assert resolution.block.owner == "plan approver"

    def test_policy_refusal_does_not_echo_permitted_targets(self):
        """The caller asked about one target and is not owed the plan's whole list."""
        resolution = _resolve(
            _enabled_entry(),
            policy_connection_ids=frozenset({"conn-secret-a", "conn-secret-b"}),
        )
        rendered = f"{resolution.block.required_input} {resolution.block.detail}"
        assert "conn-secret-a" not in rendered
        assert "conn-secret-b" not in rendered

    def test_disallowed_input_is_refused_before_the_target_is_resolved(self):
        resolution = _resolve(_enabled_entry(), requested_inputs={"environment": "prod"})
        assert resolution.block.code is TargetBlockCode.INPUT_NOT_PERMITTED
        assert resolution.block.detail == "input_value_not_permitted:environment"

    def test_unverified_connection_blocks_rather_than_guessing(self):
        """A lookup returning None means the connection is not verified.

        It must not fall back to anything derivable from the manifest text: the
        manifest is a human's statement of intent, not proof of an account.
        """
        resolution = _resolve(_enabled_entry(), target_lookup=lambda _cid: None)
        assert resolution.block.code is TargetBlockCode.CONNECTION_UNVERIFIED
        assert resolution.block.owner == "environment owner"

    def test_lookup_failure_fails_closed(self):
        """A failed lookup has not established that the target is free to use.

        This is the arm most likely to be "simplified" into a re-raise that some
        caller's broad `except` then swallows into a pass.
        """

        def explode(_cid):
            raise TimeoutError("connection service unreachable")

        resolution = _resolve(_enabled_entry(), target_lookup=explode)
        assert not resolution.resolved
        assert resolution.block.code is TargetBlockCode.EQUIVALENCE_UNVERIFIABLE
        assert resolution.block.detail == "TimeoutError"

    def test_surface_disagreement_blocks_rather_than_choosing_a_side(self):
        """THE unknown-equivalence test.

        The reviewed surface and the surface resolved from the connection disagree
        about *where* this deploys. Picking either one is a guess; the story
        requires a typed block.
        """
        resolution = _resolve(
            _enabled_entry(),
            target_lookup=lambda _cid: _target(resource_id="cluster-b/adp-gateway"),
        )
        assert not resolution.resolved
        assert resolution.block.code is TargetBlockCode.EQUIVALENCE_UNVERIFIABLE

    def test_lookup_returning_a_foreign_shape_is_an_error_not_a_coercion(self):
        """Coercing it would invent the identity this module refuses to invent."""
        with pytest.raises(ManifestError) as exc:
            _resolve(_enabled_entry(), target_lookup=lambda _cid: {"account_id": "000000000000"})
        assert exc.value.code == "invalid_target_lookup"


class TestParsing:
    """What the parser refuses in a document a reviewer edited."""

    def _document(self, **overrides) -> dict:
        entry = {
            "entry_id": "env-1",
            "status": "enabled",
            "connection_id": "conn-1",
            "component_selectors": ["gateway-backend"],
            "resource_kind": "eks-namespace",
            "resource_id": "cluster-a/adp-gateway",
            "verification_adapter": "gateway-health-verification",
            "workflow": {
                "path": ".github/workflows/gateway-deploy.yml",
                "definition_revision": SHA_A,
                "allowed_inputs": {"environment": ["dev"], "account_id": None},
            },
        }
        entry.update(overrides)
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "entries": [entry]}

    def test_round_trips_a_valid_document(self):
        manifest = parse_manifest(self._document())
        entry = manifest.entry("env-1")
        assert entry.status is EntryStatus.ENABLED
        assert entry.workflow.definition_revision == SHA_A
        # `account_id: ~` becomes an empty allow-list: pass-through at the
        # workflow's default, not "any value".
        assert entry.workflow.allowed_inputs["account_id"] == frozenset()

    def test_unknown_top_level_key_is_refused(self):
        """Ignoring it is how a narrowing constraint gets silently dropped."""
        document = self._document()
        document["require_approval"] = True
        with pytest.raises(ManifestError) as exc:
            parse_manifest(document)
        assert exc.value.code == "unknown_manifest_key"

    def test_unknown_entry_key_is_refused(self):
        with pytest.raises(ManifestError) as exc:
            parse_manifest(self._document(max_concurrency=1))
        assert exc.value.code == "unknown_manifest_key"

    def test_unknown_workflow_key_is_refused(self):
        document = self._document()
        document["entries"][0]["workflow"]["required_checks"] = ["build"]
        with pytest.raises(ManifestError) as exc:
            parse_manifest(document)
        assert exc.value.code == "unknown_manifest_key"

    def test_unknown_status_is_refused(self):
        with pytest.raises(ManifestError):
            parse_manifest(self._document(status="probably-fine"))

    def test_docs_only_must_be_an_explicit_boolean(self):
        """`docs_only: "no"` is truthy in Python.

        Coercing it would classify a real deploy as documentation-only, which is
        the classification that skips the checks.
        """
        with pytest.raises(ManifestError) as exc:
            parse_manifest(self._document(docs_only="no"))
        assert exc.value.code == "invalid_manifest_entry"
        assert parse_manifest(self._document(docs_only=True)).entry("env-1").docs_only is True

    def test_schema_version_must_be_an_integer(self):
        document = self._document()
        document["schema_version"] = "1"
        with pytest.raises(ManifestError):
            parse_manifest(document)

    def test_boolean_schema_version_is_not_an_integer(self):
        """`True == 1` in Python, so a bare isinstance check would accept it."""
        document = self._document()
        document["schema_version"] = True
        with pytest.raises(ManifestError):
            parse_manifest(document)


class TestShippedManifest:
    """The reviewed file in `config/` must parse, and must not claim false approvals."""

    @property
    def _path(self) -> Path:
        # tests/orchestration/ -> tests/ -> modules/gateway/ -> modules/ -> repo root
        return Path(__file__).resolve().parents[4] / "config" / "orchestration-deployments.yaml"

    def test_shipped_manifest_parses(self):
        """A manifest that does not parse is an approval nobody can act on.

        Uses `load_manifest_document`, which imports yaml lazily — `pyyaml` is a
        dev-only dependency, so a module-scope import would pass here and raise
        `ModuleNotFoundError` in the deployed image.
        """
        pytest.importorskip("yaml")
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
        assert manifest.entries

    def test_no_shipped_entry_is_enabled_without_a_resolved_surface(self):
        """The honesty check.

        An entry may only be `enabled` once it names a connection AND a concrete
        deployment surface. This test is what would fail if somebody enabled a pilot
        entry by filling in a plausible-looking account or cluster to make the
        example executable — the specific failure the story forbids.
        """
        pytest.importorskip("yaml")
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        for entry in manifest.entries:
            if entry.status is EntryStatus.ENABLED:
                assert entry.connection_id, entry.entry_id
                assert entry.resource_kind and entry.resource_id, entry.entry_id
            else:
                assert entry.unresolved_reason, entry.entry_id

    def test_every_shipped_workflow_path_exists_in_this_repository(self):
        """A pinned path that does not exist is an approval for nothing.

        Only the path is checked here, not the revision: resolving a SHA needs git
        history the test environment may not have, and #5151 owns the
        revision-match refusal at dispatch time.
        """
        pytest.importorskip("yaml")
        repo_root = self._path.parent.parent
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        for entry in manifest.entries:
            if entry.workflow is None:
                continue
            assert (repo_root / entry.workflow.path).is_file(), f"{entry.entry_id} pins a missing workflow: {entry.workflow.path}"

    def test_no_shipped_entry_approves_a_non_dev_environment(self):
        """The pilot manifest approves dev only.

        Stated as a test rather than trusted to review because adding `prod` to an
        `allowed_inputs` list is a one-token diff that reads as configuration and
        lands as a permission grant.
        """
        pytest.importorskip("yaml")
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        for entry in manifest.entries:
            if entry.workflow is None:
                continue
            assert entry.workflow.allowed_inputs.get("environment", frozenset()) <= frozenset({"dev"}), entry.entry_id
