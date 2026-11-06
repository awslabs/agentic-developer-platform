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
    PACKAGED_MANIFEST_ANCHOR,
    PACKAGED_MANIFEST_NAME,
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
    load_packaged_manifest,
    parse_manifest,
    resolve_manifest_entry,
)
from src.orchestration.deployment_workflows import components_for_paths

# A syntactically valid 40-character SHA. Not a real commit: these tests never
# resolve it against the repository, and using a recognisable filler makes it
# obvious in a failure message that the value is a fixture rather than a pin.
SHA_A = "a" * 40
SHA_B = "b" * 40
# A distinct third SHA so the artifact pin cannot accidentally be satisfied by the
# workflow revision fixture — the two pin different things and must not be aliased.
SHA_ARTIFACT = "c" * 40

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
        "artifact_revision": SHA_ARTIFACT,
    }
    if overrides.get("release_group"):
        defaults["mutation_role_arn"] = "arn:aws:iam::000000000000:role/adp-dev-trusted-deployment"
    defaults.update(overrides)
    return ManifestEntry(**defaults)


def _resolve(entry: ManifestEntry, **kwargs):
    manifest = DeploymentManifest(schema_version=MANIFEST_SCHEMA_VERSION, entries=(entry,))
    params = {
        "entry_id": entry.entry_id,
        "component": "gateway-backend",
        "policy_connection_ids": frozenset({"conn-1"}),
        "target_lookup": lambda _cid: _target(),
        # The happy path resolves to exactly the revision the entry approves. Made
        # explicit here rather than defaulted inside the resolver, because a
        # resolver that supplied its own "matching" revision would never refuse.
        "resolved_workflow_revision": SHA_A,
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


class TestWorkflowRevisionEnforcement:
    """The pinned revision must be *compared*, not merely stored.

    `WorkflowRef` already refuses an abbreviation, so the approval records a full
    immutable SHA — but a pin nothing checks approves the file at that path, not the
    definition. The workflow file is editable by anyone with write access after the
    review, so an unchecked dispatch runs the edited pipeline under the old approval.
    These tests are the difference between a declared guarantee and an enforced one.
    """

    def test_mismatched_revision_is_refused(self):
        """THE revision test: the definition resolved is not the one approved."""
        resolution = _resolve(_enabled_entry(), resolved_workflow_revision=SHA_B)
        assert not resolution.resolved
        assert resolution.block.code is TargetBlockCode.WORKFLOW_REVISION_MISMATCH
        assert resolution.block.detail == "workflow_revision_mismatch"
        assert resolution.block.owner == "plan approver"

    def test_unresolved_revision_blocks_rather_than_passing(self):
        """`None` is the dangerous case, and it must not read as "matches".

        A caller that cannot resolve the definition has not established anything
        about it. Defaulting the parameter to `None` and skipping the check on `None`
        would make every existing caller silently exempt — which is how a gate gets
        added and enforces nothing.
        """
        resolution = _resolve(_enabled_entry(), resolved_workflow_revision=None)
        assert not resolution.resolved
        assert resolution.block.code is TargetBlockCode.WORKFLOW_REVISION_MISMATCH
        assert resolution.block.detail == "workflow_revision_unresolved"
        assert resolution.block.owner == "platform operator"

    def test_the_two_revision_failures_are_distinguishable(self):
        """Different owners fix them, so one code with one detail would misroute.

        "You did not tell us what you resolved" is the dispatcher's bug; "what you
        resolved is not approved" needs a human to re-review. Collapsing them sends
        both to the same person.
        """
        unresolved = _resolve(_enabled_entry(), resolved_workflow_revision=None)
        mismatch = _resolve(_enabled_entry(), resolved_workflow_revision=SHA_B)
        assert unresolved.block.detail != mismatch.block.detail
        assert unresolved.block.owner != mismatch.block.owner

    def test_refusal_does_not_echo_either_revision(self):
        """A refusal that named the approved SHA is an enumeration oracle.

        The caller supplied its own revision and is not owed the approved one; a
        caller that could read it back by guessing could discover exactly which build
        an environment is approved to run.
        """
        block = _resolve(_enabled_entry(), resolved_workflow_revision=SHA_B).block
        text = f"{block.required_input} {block.detail}"
        assert SHA_A not in text
        assert SHA_B not in text

    def test_case_differing_revision_is_accepted_not_refused(self):
        """An uppercase SHA is the same immutable commit.

        Some provider APIs return uppercase. Refusing it would be a shape complaint
        about a perfectly valid pin, and the operator's only workaround would be to
        edit the reviewed manifest — a worse outcome than comparing case-insensitively.
        """
        assert _resolve(_enabled_entry(), resolved_workflow_revision=SHA_A.upper()).resolved

    def test_a_prefix_of_the_approved_revision_is_refused(self):
        """No prefix matching, in either direction.

        A prefix names a set of commits. Accepting one here would reintroduce exactly
        the ambiguity `WorkflowRef` refuses at construction time.
        """
        resolution = _resolve(_enabled_entry(), resolved_workflow_revision=SHA_A[:12])
        assert resolution.block.code is TargetBlockCode.WORKFLOW_REVISION_MISMATCH

    def test_revision_is_checked_before_the_target_is_resolved(self):
        """Ordering: an unapproved definition must not cause a provider call.

        Resolving the target first would let a refused dispatch still probe the
        connection service, and the block would be reported against a target the
        caller was never permitted to reach.
        """

        def exploding_lookup(_cid):
            raise AssertionError("target_lookup must not be called for an unapproved definition")

        resolution = _resolve(_enabled_entry(), resolved_workflow_revision=SHA_B, target_lookup=exploding_lookup)
        assert resolution.block.code is TargetBlockCode.WORKFLOW_REVISION_MISMATCH


class TestArtifactRevisionPinning:
    """An enabled entry that ships code must name WHICH build it ships.

    Without this, the entry approves "whatever the workflow resolves at dispatch
    time" — so the same reviewed approval ships a different build tomorrow, and an
    incident has no answer to "what was deployed?". The `docs_only` carve-out is
    asserted too: an exemption nothing tests is indistinguishable from a hole.
    """

    def test_enabled_entry_without_an_artifact_revision_is_refused(self):
        with pytest.raises(ManifestError) as exc:
            _enabled_entry(artifact_revision=None)
        assert exc.value.code == "incomplete_manifest_entry"
        assert "artifact_revision" in str(exc.value)

    @pytest.mark.parametrize("value", ["latest", "main", "v1.2.3", "HEAD", SHA_A[:12], "z" * 40])
    def test_a_mutable_name_is_not_a_pin(self, value):
        """Every one of these is resolved at *use* time by someone other than the reviewer.

        `latest` and `main` move; a tag can be re-pointed; an abbreviation names a
        prefix; a 40-character non-hex string is not a SHA at all. Storing any of
        them records approval of a name rather than of a build.
        """
        with pytest.raises(ManifestError) as exc:
            _enabled_entry(artifact_revision=value)
        assert exc.value.code == "invalid_artifact_revision"

    def test_a_mutable_pin_is_refused_even_on_an_unresolved_entry(self):
        """Shape is enforced at every status, deliberately.

        Otherwise `latest` can be parked in an unresolved entry and go live via a
        one-line `status:` edit that changes no value a reviewer would re-read.
        """
        with pytest.raises(ManifestError) as exc:
            ManifestEntry(
                entry_id="env-2",
                status=EntryStatus.UNRESOLVED,
                connection_id=None,
                component_selectors=(),
                workflow=None,
                artifact_revision="latest",
                unresolved_reason="connection not yet verified",
            )
        assert exc.value.code == "invalid_artifact_revision"

    def test_an_unresolved_entry_may_carry_no_pin_at_all(self):
        """The honest state for an entry whose build does not exist yet.

        This is why the requirement is scoped to ENABLED: forcing a SHA onto an
        unresolved entry would make inventing one the path of least resistance.
        """
        entry = ManifestEntry(
            entry_id="env-2",
            status=EntryStatus.UNRESOLVED,
            connection_id=None,
            component_selectors=(),
            workflow=None,
            artifact_revision=None,
            unresolved_reason="connection not yet verified",
        )
        assert entry.artifact_revision is None

    def test_docs_only_entry_is_exempt_and_still_enabled(self):
        """The carve-out is deliberate, and asserted so a later "tighten it" is informed.

        A docs-only entry produces no deployable artifact, so demanding a build SHA
        would force a reviewer to invent one — the exact failure this check prevents.
        """
        entry = _enabled_entry(docs_only=True, artifact_revision=None)
        assert entry.status is EntryStatus.ENABLED
        assert entry.docs_only is True

    def test_an_uppercase_pin_is_accepted(self):
        assert _enabled_entry(artifact_revision=SHA_ARTIFACT.upper()).artifact_revision == SHA_ARTIFACT.upper()


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
            "artifact_revision": SHA_ARTIFACT,
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
        assert entry.artifact_revision == SHA_ARTIFACT

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


class TestPackagedManifestLoading:
    """The manifest must be loadable *the way the image loads it*.

    Every other test in this file reads the YAML through a filesystem path derived
    from `__file__`, which works in a source checkout and proves nothing about the
    installed distribution the pod actually runs. The gap between those two is the
    whole class of "documentation asserting an invariant the code does not enforce":
    a loader that only works from a checkout would pass this suite and raise in the
    image. These tests exercise the packaged path and assert the packaging
    declarations that make it work.
    """

    def test_manifest_loads_through_the_packaged_anchor(self):
        """THE loadability test: `importlib.resources`, not a relative path walk.

        This is the call the deployed process makes. If the YAML were not packaged
        as package-data, or the anchor were not an importable package, this is where
        it fails — rather than at 3am in a pod.
        """
        manifest = load_packaged_manifest()
        assert manifest.schema_version == MANIFEST_SCHEMA_VERSION
        assert manifest.entries

    def test_the_packaged_copy_is_the_reviewed_file_itself(self):
        """One manifest, not a copy that can drift from the reviewed one.

        A second file synchronized by hand would let the image ship an approval
        nobody reviewed, which is worse than shipping none.
        """
        from_path = load_manifest_document(
            (Path(__file__).resolve().parents[1].parent / "src" / "orchestration" / "manifests" / PACKAGED_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        assert load_packaged_manifest() == from_path

    def test_the_anchor_is_an_importable_package(self):
        """`importlib.resources.files()` needs an importable anchor.

        Without `__init__.py` the anchor is a namespace package, and
        `setuptools.packages.find` does not discover it — which also makes any
        `package-data` key naming it silently inert.
        """
        import importlib  # noqa: PLC0415

        module = importlib.import_module(PACKAGED_MANIFEST_ANCHOR)
        assert Path(module.__file__).name == "__init__.py"

    def test_yaml_is_a_runtime_dependency_not_a_dev_extra(self):
        """The image installs with bare `pip install .`, which resolves no extras.

        Asserted against pyproject.toml rather than trusted to a comment: a
        well-meaning cleanup that moved `pyyaml` back under `dev` would break the
        deployed loader while leaving every test green, because the dev extra is
        exactly what the test environment installs.
        """
        text = (Path(__file__).resolve().parents[1].parent / "pyproject.toml").read_text(encoding="utf-8")
        runtime = text.split("[project.optional-dependencies]")[0]
        assert "pyyaml" in runtime.lower(), "pyyaml must be a runtime dependency; the deployed manifest loader parses YAML"

    def test_the_manifest_is_declared_as_package_data(self):
        """A declaration of intent, honestly scoped.

        Measured, not assumed: under the current backend (`setuptools>=75`,
        `include-package-data` at its pyproject default of true) a built wheel
        contains the YAML *even without* this entry — so it is a guard against that
        default changing and a marker for anyone building a sdist/wheel for a
        consumer, NOT the reason the file reaches the image. That reason is
        `COPY src/ src/`, asserted separately below. Kept as a test so the entry is
        not deleted as noise, and worded so nobody mistakes it for the mechanism.
        """
        text = (Path(__file__).resolve().parents[1].parent / "pyproject.toml").read_text(encoding="utf-8")
        section = text.split("[tool.setuptools.package-data]")[1]
        assert PACKAGED_MANIFEST_ANCHOR in section

    def test_the_manifest_is_inside_the_docker_build_context(self):
        """The context is `modules/gateway`, so a repo-root `config/` is unreachable.

        This is why the file moved. Asserted because the constraint is invisible from
        Python: nothing in the import graph reveals that `COPY` cannot see a sibling
        directory of the build context.
        """
        gateway_root = Path(__file__).resolve().parents[1].parent
        packaged = gateway_root / "src" / "orchestration" / "manifests" / PACKAGED_MANIFEST_NAME
        assert packaged.is_file()
        assert packaged.is_relative_to(gateway_root / "src"), "must be under src/, which the Dockerfile already COPYs"
        repo_root = Path(__file__).resolve().parents[4]
        assert not (repo_root / "config" / PACKAGED_MANIFEST_NAME).exists(), (
            "the repo-root copy must not come back: it is outside the gateway docker build context"
        )

    def test_the_dockerfile_copies_the_tree_that_contains_the_manifest(self):
        """The actual mechanism, asserted against the image definition.

        `COPY src/ src/` is what puts the YAML in the container; everything else in
        this class is a guard. Checked here because the alternative is a comment
        claiming it, and a comment does not fail when somebody narrows that COPY to
        specific subdirectories.
        """
        gateway_root = Path(__file__).resolve().parents[1].parent
        dockerfile = (gateway_root / "Dockerfile").read_text(encoding="utf-8")
        assert "COPY src/ src/" in dockerfile, "the manifest reaches the image only because the whole src/ tree is copied"

    def test_a_missing_packaged_manifest_fails_closed_and_names_the_cause(self, monkeypatch):
        """An absent manifest must not degrade into an empty one.

        An empty manifest refuses every deploy with `ENTRY_UNKNOWN`, which sends the
        operator looking for a missing *review* rather than a missing *file* — a
        diagnosis that can cost hours. The typed error names packaging instead.
        """
        import src.orchestration.deployment_manifest as module  # noqa: PLC0415

        monkeypatch.setattr(module, "PACKAGED_MANIFEST_NAME", "no-such-manifest.yaml")
        with pytest.raises(ManifestError) as exc:
            module.load_packaged_manifest()
        assert exc.value.code == "packaged_manifest_missing"


class TestShippedManifest:
    """The reviewed file in `config/` must parse, and must not claim false approvals."""

    @property
    def _path(self) -> Path:
        """The manifest inside the package, not at the repository root.

        It moved there so the gateway image can actually read it: the image is built
        with `modules/gateway` as its docker context, and a repo-root `config/`
        directory is not in that context.
        """
        return Path(__file__).resolve().parents[1].parent / "src" / "orchestration" / "manifests" / PACKAGED_MANIFEST_NAME

    def test_shipped_manifest_parses(self):
        """A manifest that does not parse is an approval nobody can act on."""
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
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        for entry in manifest.entries:
            if entry.status is EntryStatus.ENABLED:
                assert entry.connection_id, entry.entry_id
                assert entry.resource_kind and entry.resource_id, entry.entry_id
            else:
                assert entry.unresolved_reason, entry.entry_id

    def test_enabled_workflows_are_local_or_explicit_external_pins(self):
        """Only explicitly reviewed external workflows may be absent locally.

        Resolving external revisions needs provider access. The scheduled
        dispatcher independently checks repository identity and workflow bytes;
        offline CI preserves the reviewed exception list and source pins.
        """
        # Derived from this test file, not from the manifest's own path: the
        # manifest now lives several levels inside the package, so a relative walk
        # from it would silently point somewhere plausible-but-wrong.
        # tests/orchestration/ -> tests/ -> modules/gateway/ -> modules/ -> repo root
        repo_root = Path(__file__).resolve().parents[4]
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        # These reviewed workflows belong to the two Product fixture repositories.
        # Live provider preflight verifies their repository IDs and content at
        # these exact merges; an arbitrary missing local workflow is still refused.
        external = {
            "qualification-api-8394": (
                ".github/workflows/qualification-api-deploy.yml",
                "8b72cfe13fdb4ae94c5112473286fc9f3515ef87",
            ),
            "qualification-client-8394": (
                ".github/workflows/qualification-client-deploy.yml",
                "f99487960b2cb169f9d0f6a6b9237def4176f374",
            ),
        }
        for entry in manifest.entries:
            if entry.workflow is None:
                continue
            if entry.entry_id in external:
                path, revision = external[entry.entry_id]
                assert entry.workflow.path == path
                assert entry.workflow.definition_revision == entry.artifact_revision == revision
                continue
            if (repo_root / entry.workflow.path).is_file():
                continue
            assert entry.status is EntryStatus.UNRESOLVED, entry.entry_id
            assert entry.workflow.definition_revision is None, entry.entry_id
            assert entry.workflow.path.rsplit("/", 1)[-1] in entry.unresolved_reason, entry.entry_id

    def test_no_shipped_entry_approves_a_non_dev_environment(self):
        """The pilot manifest approves dev only.

        Stated as a test rather than trusted to review because adding `prod` to an
        `allowed_inputs` list is a one-token diff that reads as configuration and
        lands as a permission grant.
        """
        manifest = load_manifest_document(self._path.read_text(encoding="utf-8"))
        for entry in manifest.entries:
            if entry.workflow is None:
                continue
            assert entry.workflow.allowed_inputs.get("environment", frozenset()) <= frozenset({"dev"}), entry.entry_id

    # test_new_dev_component_has_one_safe_entry was retired on 2026-10-15 when its last
    # case, knowledge-door, moved to the enabled set (see
    # test_knowledge_door_entry_is_enabled_and_pinned_to_distinct_reviewed_revisions).

    @pytest.mark.parametrize(
        ("component", "resource_kind", "resource_id", "workflow_path", "adapter", "revision"),
        [
            (
                "tools-platform",
                "agentcore-gateway",
                "arn:aws:bedrock-agentcore:us-east-1:879318057152:gateway/adp-tools-ftgob4rcuu",
                ".github/workflows/tools-platform-deploy.yml",
                "agentcore-gateway-verification",
                "ba28822e50a6bde22d4805835dcadda22b41f92e",
            ),
            # The interceptor and façade workflows changed in #7740 (pinned-definition
            # guard); their pins must be at or after that merge.
            (
                "tools-interceptor",
                "lambda-function",
                "arn:aws:lambda:us-east-1:879318057152:function:adp-tools-dev-interceptor",
                ".github/workflows/tools-interceptor-deploy.yml",
                "lambda-version-verification",
                "c9ca5556031aa23149f6689be846608aa6bca6bc",
            ),
            (
                "tools-facade",
                "eks-deployment",
                "adp-dev-eks-cluster/adp-gateway/tools-facade",
                ".github/workflows/tools-facade-deploy.yml",
                "tools-facade-health-verification",
                "c9ca5556031aa23149f6689be846608aa6bca6bc",
            ),
        ],
    )
    def test_dev_tools_entries_are_enabled_and_fully_pinned(self, component, resource_kind, resource_id, workflow_path, adapter, revision):
        """The enabled dev tools entries: each approves one workflow revision, one build and one target.

        Enabling them is a permission grant (owner decision 2026-10-14 for the tools
        flow's dev deployments), so the exact values are pinned here rather than
        trusted to review. All three share the dev connection, principal and revision.
        """
        manifest = load_packaged_manifest()
        matches = [entry for entry in manifest.entries if entry.covers(component)]
        assert len(matches) == 1
        entry = matches[0]
        assert entry.status is EntryStatus.ENABLED
        assert entry.connection_id == "3cac43fc-f877-4cb0-9b36-9eb68244266c"
        assert entry.resource_kind == resource_kind
        assert entry.resource_id == resource_id
        assert entry.workflow.path == workflow_path
        assert entry.workflow.definition_revision == entry.artifact_revision == revision
        assert entry.workflow.allowed_inputs["environment"] == frozenset({"dev"})
        assert entry.workflow.allowed_inputs["customer_account_id"] == frozenset({"879318057152"})
        assert entry.workflow.allowed_inputs["customer_user_id"] == frozenset({"1fb07e6f-8108-41a2-964d-5f76d63895fe"})
        assert entry.verification_adapter == adapter

    def test_knowledge_door_entry_is_enabled_and_pinned_to_distinct_reviewed_revisions(self):
        """The Door entry (tools flow L2, 2026-10-15) pins one reviewed main commit for workflow and image.

        The Door image is rebuilt at the same commit that fixed the deploy guard
        (#7794, #7802), so the workflow's imageTag lookup, the deploy scripts it calls
        and the identity verifier all name one revision.
        """
        manifest = load_packaged_manifest()
        (entry,) = [entry for entry in manifest.entries if entry.covers("knowledge-door")]
        assert entry.status is EntryStatus.ENABLED
        assert entry.connection_id == "3cac43fc-f877-4cb0-9b36-9eb68244266c"
        # cluster/namespace, as the engine's target model expects for every EKS component
        # except tools-facade and as door_release_evidence.py publishes (#7829).
        assert (entry.resource_kind, entry.resource_id) == ("eks-namespace", "adp-dev-eks-cluster/agent-context")
        assert entry.workflow.path == ".github/workflows/agent-context-deploy.yml"
        assert entry.workflow.definition_revision == "0fe14cc01be22a3fbe72f00086bc25e4ed72da8c"
        assert entry.artifact_revision == "0fe14cc01be22a3fbe72f00086bc25e4ed72da8c"
        assert entry.workflow.allowed_inputs["environment"] == frozenset({"dev"})
        assert entry.workflow.allowed_inputs["customer_account_id"] == frozenset({"879318057152"})
        assert entry.verification_adapter == "door-identity-verification"

    def test_product_dev_gateway_binding_uses_verified_target_and_exact_pins(self):
        manifest = load_packaged_manifest()
        entry = manifest.entry("adp-dev-gateway")
        assert entry.status is EntryStatus.ENABLED
        assert entry.component_selectors == ("gateway-backend",)
        assert entry.connection_id == "3cac43fc-f877-4cb0-9b36-9eb68244266c"
        assert (entry.resource_kind, entry.resource_id) == ("eks-namespace", "adp-dev-eks-cluster/adp-gateway")
        assert entry.workflow.path == ".github/workflows/gateway-deploy.yml"
        assert entry.workflow.definition_revision == entry.artifact_revision == "8ccec3882c6ab9e9f4bf18cf148e5e0855aa0db4"
        assert entry.workflow.allowed_inputs == {"environment": frozenset({"dev"})}
        assert entry.verification_adapter == "gateway-health-verification"
        assert manifest.entry("adp-dev-embark1-gateway").status is EntryStatus.UNRESOLVED
        assert entry.workflow.correlation_input == "adp_correlation"

        def resolve(**overrides):
            arguments = dict(
                entry_id=entry.entry_id,
                component="gateway-backend",
                policy_connection_ids=frozenset({entry.connection_id}),
                target_lookup=lambda _: _target(resource_id=entry.resource_id),
                requested_inputs={"environment": "dev"},
                resolved_workflow_revision=entry.workflow.definition_revision,
            )
            arguments.update(overrides)
            return resolve_manifest_entry(manifest, **arguments)

        assert resolve().resolved
        for overrides in (
            {"policy_connection_ids": frozenset()},
            {"component": "gateway-frontend"},
            {"requested_inputs": {"environment": "prod"}},
            {"requested_inputs": {"manual_source_revision": entry.artifact_revision}},
            {"resolved_workflow_revision": "f" * 40},
        ):
            assert not resolve(**overrides).resolved

    def test_product_frontend_binding_resolves_only_the_reviewed_dev_surface(self):
        from src.orchestration.deployment_controller import ADAPTERS

        manifest = load_packaged_manifest()
        entry = manifest.entry("adp-dev-gateway-frontend")
        backend = manifest.entry("adp-dev-gateway")
        assert entry.status is EntryStatus.ENABLED
        assert entry.component_selectors == ("gateway-frontend",)
        assert entry.connection_id == backend.connection_id
        assert (entry.resource_kind, entry.resource_id) == (backend.resource_kind, backend.resource_id)
        assert entry.workflow.path == ".github/workflows/gateway-frontend-deploy.yml"
        assert entry.workflow.definition_revision == entry.artifact_revision == "18435f88cbe77c500c676747bf1a677015478d9d"
        assert entry.workflow.allowed_inputs == {"environment": frozenset({"dev"})}
        assert entry.workflow.correlation_input == "adp_correlation"
        assert "gateway-frontend" in ADAPTERS[entry.verification_adapter]
        assert entry.rollback_adapter is None and not entry.docs_only

        def resolve(**overrides):
            arguments = dict(
                entry_id=entry.entry_id,
                component="gateway-frontend",
                policy_connection_ids=frozenset({entry.connection_id}),
                target_lookup=lambda _: _target(resource_id=entry.resource_id),
                requested_inputs={"environment": "dev"},
                resolved_workflow_revision=entry.workflow.definition_revision,
            )
            arguments.update(overrides)
            return resolve_manifest_entry(manifest, **arguments)

        assert resolve().resolved
        for overrides in (
            {"policy_connection_ids": frozenset()},
            {"component": "gateway-backend"},
            {"target_lookup": lambda _: _target(resource_id="another-cluster/adp-gateway")},
            {"target_lookup": lambda _: _target(resource_kind="cloudfront-distribution", resource_id=entry.resource_id)},
            {"requested_inputs": {"environment": "prod"}},
            {"requested_inputs": {"account_id": "000000000000"}},
            {"requested_inputs": {"adp_source_revision": entry.artifact_revision}},
            {"resolved_workflow_revision": "f" * 40},
        ):
            assert not resolve(**overrides).resolved

    def test_no_other_runtime_entry_is_enabled(self):
        """Runtime grants remain explicit after temporary flow-read verification is retired."""
        manifest = load_packaged_manifest()
        enabled = sorted(e.entry_id for e in manifest.entries if e.status is EntryStatus.ENABLED and not e.docs_only)
        assert enabled == [
            "adp-dev-atlassian-foundation-gateway",
            "adp-dev-gateway",
            "adp-dev-gateway-frontend",
            "adp-dev-knowledge-door",
            "adp-dev-tool-adapters",
            "adp-dev-tools-facade",
            "adp-dev-tools-interceptor",
            "adp-dev-tools-platform",
            "qualification-api-8394",
            "qualification-client-8394",
        ]

    def test_unpinned_workflow_can_only_be_recorded_as_unresolved(self):
        pending = WorkflowRef(path=".github/workflows/tools-platform-deploy.yml", definition_revision=None)
        with pytest.raises(ManifestError, match="workflow.definition_revision"):
            _enabled_entry(workflow=pending)


def test_adapter_foundation_paths_resolve_to_the_dedicated_approved_group():
    manifest = load_packaged_manifest()
    for path in (
        "modules/tools/adapters/deploy.py",
        "modules/tools/adapters/infra/main.tf",
        "modules/tools/adp_tools/adapter_contracts.py",
        "codebuild/bs-tools-adapters-build.yml",
        ".github/workflows/tools-adapters-deploy.yml",
    ):
        assert manifest.path_rule(path).component == "tool-adapters"
    for path in (
        "modules/tools/adapters/tests/test_runtime_deploy.py",
        "modules/tools/adapters/infra/README.md",
        ".github/workflows/tools-adapters-ci.yml",
    ):
        assert manifest.path_rule(path).component == "ignore"
    assert manifest.path_rule("modules/tools/platform/infra/main.tf").component == "tools-platform"
    entry = manifest.entry("adp-dev-tool-adapters")
    assert entry.status is EntryStatus.ENABLED
    assert entry.component_selectors == ("tool-adapters",)
    assert entry.release_group == "atlassian-foundation"
    assert entry.connection_id == manifest.entry("adp-dev-gateway").connection_id
    assert entry.resource_kind == "eks-namespace"
    assert entry.resource_id == "adp-dev-eks-cluster/adp-tools-adapters"
    assert entry.workflow.path == ".github/workflows/tools-platform-deploy.yml"
    assert entry.workflow.allowed_inputs == {"environment": frozenset({"dev"}), "mode": frozenset({"release"}), "stage": frozenset({"tool-adapters"})}
    assert entry.artifact_revision == entry.workflow.definition_revision == "166dfa700e2b10fcfa419ffafa6a1461d8c519a7"
    assert entry.mutation_role_arn.endswith(":role/adp-dev-tools-trusted-deployment")
    gateway = manifest.entry("adp-dev-atlassian-foundation-gateway")
    ordinary = manifest.entry("adp-dev-gateway")
    assert gateway.status is EntryStatus.ENABLED
    assert gateway.connection_id == entry.connection_id
    assert gateway.resource_id == ordinary.resource_id
    assert gateway.component_selectors == ("gateway-backend",)
    assert gateway.release_group == entry.release_group
    assert gateway.workflow.path == ordinary.workflow.path
    assert gateway.workflow.allowed_inputs == {"environment": frozenset({"dev"}), "release_group": frozenset({"atlassian-foundation"})}
    assert gateway.workflow.definition_revision == gateway.artifact_revision == entry.artifact_revision
    assert gateway.mutation_role_arn.endswith(":role/adp-dev-gateway-trusted-deployment")
    assert ordinary.release_group is None
    assert ordinary.workflow.allowed_inputs == {"environment": frozenset({"dev"})}
    assert ordinary.artifact_revision == ordinary.workflow.definition_revision == "8ccec3882c6ab9e9f4bf18cf148e5e0855aa0db4"


def test_chat_worker_crew_paths_resolve_to_one_blocked_deployment():
    paths = (
        "modules/agent-factory/agent/k8s/chat-crew-pod-admission.yaml",
        "modules/agent-factory/agent/k8s/chat-supervisor-job.yaml",
        "modules/agent-factory/agent/k8s/chat-scaledjob.yaml",
        ".github/workflows/chat-agent-deploy.yml",
    )
    assert components_for_paths(paths) == ("agent-worker",)
    manifest = load_packaged_manifest()
    entries = [entry for entry in manifest.entries if "agent-worker" in entry.component_selectors]
    assert len(entries) == 1
    entry = entries[0]
    assert "chat-worker" in entry.component_selectors
    assert entry.workflow.path == ".github/workflows/chat-agent-deploy.yml"
    assert entry.workflow.correlation_input == "adp_correlation"
    assert entry.verification_adapter == "crew-pod-verification"
    resolution = resolve_manifest_entry(
        manifest,
        entry_id=entry.entry_id,
        component="agent-worker",
        policy_connection_ids=frozenset(),
        target_lookup=lambda _: None,
        resolved_workflow_revision=entry.workflow.definition_revision,
    )
    assert resolution.block.code == TargetBlockCode.TARGET_UNRESOLVED


def test_shared_release_group_requires_reviewed_slug_and_runtime_entry():
    assert _enabled_entry(release_group="tool-connectors").release_group == "tool-connectors"
    for invalid in ("", "foo/bar", "UPPER", "too-long-" * 10):
        with pytest.raises(ManifestError, match="reviewed, nonempty slug"):
            _enabled_entry(release_group=invalid)
    with pytest.raises(ManifestError, match="reviewed, nonempty slug"):
        _enabled_entry(release_group="tool-connectors", docs_only=True)


def test_enabled_group_requires_reviewed_mutation_role():
    with pytest.raises(ManifestError, match="mutation_role_arn"):
        _enabled_entry(release_group="atlassian-foundation", mutation_role_arn=None)
    for invalid in ("arn:aws:iam::000000000000:role/not-approved", "arn:aws:iam::000000000000:role/adp-dev-trusted-deployment?"):
        with pytest.raises(ManifestError, match="trusted deployment role"):
            _enabled_entry(release_group="atlassian-foundation", mutation_role_arn=invalid)
    entry = _enabled_entry(release_group="atlassian-foundation")
    assert entry.mutation_role_arn == "arn:aws:iam::000000000000:role/adp-dev-trusted-deployment"


def test_foundation_release_scope_is_reviewed_and_excludes_other_deployments():
    from types import SimpleNamespace

    from src.orchestration.environment_leases import LeaseError, foundation_release_scope

    flow = SimpleNamespace(id="accepted-flow", org_id="tenant", slug="atlassian-adapters-8445-parallel")
    node = SimpleNamespace(id="stage-one", flow_id=flow.id, org_id=flow.org_id, wave_ref="w03b-foundation-release")
    backend = _enabled_entry(component_selectors=("gateway-backend",), release_group="atlassian-foundation")
    adapter = _enabled_entry(component_selectors=("tool-adapters", "tools-platform"), release_group="atlassian-foundation")
    first, owner = foundation_release_scope(
        node=node, flow=flow, entry=backend, component="gateway-backend", target=_target(), environment="dev", source_revision="a" * 40
    )
    other, second_owner = foundation_release_scope(
        node=node,
        flow=flow,
        entry=adapter,
        component="tool-adapters",
        target=_target(resource_kind="agentcore-gateway", resource_id="different"),
        environment="dev",
        source_revision="a" * 40,
    )
    assert first.canonical_key == other.canonical_key
    assert owner != second_owner
    assert (
        owner
        != foundation_release_scope(
            node=node, flow=flow, entry=backend, component="gateway-backend", target=_target(), environment="dev", source_revision="b" * 40
        )[1]
    )
    for wave in ("w04-identity", "w05b-connections", "w05-reads-ui", "w06b-clients", "w06-writes-clients"):
        scoped, _ = foundation_release_scope(
            node=SimpleNamespace(id=wave, flow_id=flow.id, org_id=flow.org_id, wave_ref=wave),
            flow=flow,
            entry=backend,
            component="gateway-backend",
            target=_target(),
            environment="dev",
            source_revision="a" * 40,
        )
        assert scoped.canonical_key == first.canonical_key
    assert (
        foundation_release_scope(
            node=node, flow=flow, entry=_enabled_entry(), component="gateway-backend", target=_target(), environment="dev", source_revision="a" * 40
        )
        is None
    )
    with pytest.raises(LeaseError, match="accepted flow and node"):
        foundation_release_scope(
            node=SimpleNamespace(flow_id="other", org_id="tenant", wave_ref=node.wave_ref),
            flow=flow,
            entry=backend,
            component="gateway-backend",
            target=_target(),
            environment="dev",
            source_revision="a" * 40,
        )
    with pytest.raises(LeaseError, match="reviewed entry does not cover"):
        foundation_release_scope(
            node=node, flow=flow, entry=backend, component="tool-adapters", target=_target(), environment="dev", source_revision="a" * 40
        )
    with pytest.raises(LeaseError, match="pinned node and source"):
        foundation_release_scope(
            node=node, flow=flow, entry=backend, component="gateway-backend", target=_target(), environment="dev", source_revision="invalid"
        )


def test_security_operations_registration_is_scoped_and_unresolved():
    manifest = load_packaged_manifest()
    entry = manifest.entry("l5-security-target-pending")
    assert entry.status is EntryStatus.UNRESOLVED
    assert entry.connection_id is None
    assert entry.resource_id is None
    assert entry.artifact_revision is None
    assert entry.verification_adapter is None
    assert entry.rollback_adapter is None
    assert entry.workflow.path == ".github/workflows/l5-services-worker-lifecycle.yml"
    assert entry.workflow.allowed_inputs == {"operation": frozenset({"status"}), "environment": frozenset({"dev"})}
    assert components_for_paths(("modules/L5-services/infra/iam.tf",)) == ("l5-security",)
    assert components_for_paths((".github/workflows/l5-services-worker-lifecycle.yml",)) == ("l5-security",)
    assert components_for_paths(("modules/L5-services/tests/deployment/test_scope_policy.py",)) == ("documentation",)
    assert components_for_paths((".github/workflows/l5-services-ci.yml",)) == ("documentation",)


def test_qualification_components_select_only_their_reviewed_entries():
    manifest = load_packaged_manifest()
    api = (
        "qualification-api/qualification/q-fixture/pricing.py",
        "app.py",
        "qualification-api/Dockerfile",
        ".github/workflows/qualification-api-deploy.yml",
    )
    client = (
        "qualification-client/qualification/q-fixture/quote.py",
        "client.py",
        "qualification-client/Dockerfile",
        ".github/workflows/qualification-client-deploy.yml",
    )
    assert components_for_paths(api) == ("qualification-api",)
    assert components_for_paths(client) == ("qualification-client",)
    assert components_for_paths(api + client) == ("qualification-api", "qualification-client")
    for component in ("qualification-api", "qualification-client"):
        entries = [entry for entry in manifest.entries if entry.covers(component)]
        assert [entry.entry_id for entry in entries] == [f"{component}-8394"]
        assert entries[0].status is EntryStatus.ENABLED
        assert entries[0].workflow.path == f".github/workflows/{component}-deploy.yml"
    patterns = [rule.pattern for rule in manifest.path_rules]
    assert len(patterns) == len(set(patterns))
