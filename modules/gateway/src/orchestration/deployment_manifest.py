"""Deployment manifest: which pipeline may deploy what, to which proven target.

Issue #5150 (ENGINE-D1, parent #5131). This module is the **frozen contract** two
sibling stories build against — [#5151](https://github.com/aws-e/adp/issues/5151)
(ENGINE-D2) and [#5152](https://github.com/aws-e/adp/issues/5152) (ENGINE-D3). It
declares the manifest shapes and the canonical physical-target identity, and it
resolves a manifest entry against an in-force policy. It has **no database access
and makes no network calls**, so the downstream authors can import it and build
against a checked-in shape rather than against a guess. The lease store that
serializes access to a resolved target is `environment_leases.py`.

## The problem this module solves

"Deploy" is the one engine action whose blast radius is somebody else's
infrastructure, and today it is unbounded in two independent ways.

**First, nothing says what a deploy may consist of.** `ExecutionPolicy` already
carries `environment_connection_ids` — the deployment targets an accepted plan
permits — but those ids resolve to nothing: there is no registry behind them. So
an agent choosing a workflow path, a set of inputs and a release to ship is
choosing all three freely, and the only thing preventing that today is that no
caller reaches the path yet. A manifest entry is what makes the choice a
*lookup of something a human reviewed* instead of a decision an agent makes at
run time.

**Second, and more subtly, a connection id is an alias rather than an identity.**
Nothing stops one real AWS account being connected twice — two credential rows,
two labels, possibly in two different tenants, all pointing at one cluster. Code
that serialized deployments by connection id would let two runs holding two
different ids for the same cluster both believe they held it exclusively, and they
would deploy incompatible releases on top of each other. Serialization has to
happen on the *physical target*, which is why `PhysicalTarget` exists and why its
`canonical_key` is derived rather than supplied.

## Why the workflow *revision* is pinned and not just the path

Pinning `.github/workflows/gateway-deploy.yml` alone approves a *filename*. The
file's contents change with every merge, so an approval recorded against the path
silently transfers to whatever that path contains later — including a rewritten
job that deploys somewhere else entirely. `workflow_ref` therefore carries an
immutable commit SHA alongside the path, and a dispatch whose resolved definition
does not match it is refused by #5151 rather than run. The SHA is the approval's
subject; the path only says where to find it.

## Why a missing target is an explicit entry rather than an absent one

The temptation with an environment we cannot yet prove is to leave it out of the
manifest, or to fill in a plausible account id so the example is executable.
Both are worse than an honest record. An absent entry is indistinguishable from
an entry nobody has written yet, and an invented account id is a *fabricated
authorization* — it would name a real deployment surface that nobody verified.
So `ManifestEntry.status` has `DISABLED` and `UNRESOLVED` members, an unresolved
entry must state what is missing in `unresolved_reason`, and resolution of one
returns a typed block naming the owner instead of a target.

## What this module deliberately does not do

It dispatches no workflow, reads back no running deployment, executes no
rollback, and mints no credentials. `verification_adapter` and
`rollback_adapter` hold **adapter names**, not callables: this module records
which adapter an entry is approved to use, and #5152 owns what those adapters
do. The rollback name being present is a *permission*, not an instruction. Nor
does this module decide policy — `execution_policy.authorize_action` already owns
whether a `DEPLOY` is permitted for a connection, and `resolve_manifest_entry`
consults that answer rather than forming a second opinion about it.

## Unknown-equivalence is a block, never an optimistic pass

If two targets cannot be *proven* to be the same physical target, this module
refuses to produce a key rather than assuming they differ. "Probably a different
cluster, go ahead" is precisely the reasoning that produces the double-deploy
this story exists to prevent, so an unverifiable equivalence becomes
`TargetBlockCode.EQUIVALENCE_UNVERIFIABLE`. The asymmetry is deliberate and it
fails closed: a false collision costs a caller a wait, while a false distinction
costs a corrupted deployment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "CANONICAL_KEY_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "DeploymentManifest",
    "ManifestEntry",
    "ManifestError",
    "EntryStatus",
    "PhysicalTarget",
    "TargetBlock",
    "TargetBlockCode",
    "TargetEvidence",
    "TargetResolution",
    "WorkflowRef",
    "PACKAGED_MANIFEST_ANCHOR",
    "PACKAGED_MANIFEST_NAME",
    "canonical_target_key",
    "load_manifest_document",
    "load_packaged_manifest",
    "parse_manifest",
    "resolve_manifest_entry",
]

# The manifest schema version this build understands. A document declaring
# anything else is refused rather than best-effort parsed: a newer document may
# carry a field whose *absence of enforcement* is the vulnerability (a narrower
# input allow-list, say), and silently ignoring it would apply an approval that
# was never granted. Bumped when a field changes meaning, not when one is added.
MANIFEST_SCHEMA_VERSION = 1

# Version tag embedded in every canonical physical-target key. The key is a
# stored uniqueness value, so the day its derivation changes, previously stored
# keys would silently stop colliding with newly derived ones for the same
# cluster — two holders of one target, which is the exact failure the key
# prevents. Embedding the version means a derivation change produces visibly
# different keys instead of a silent split, and a migration becomes a decision
# somebody has to make rather than an accident.
CANONICAL_KEY_VERSION = "v1"

# A workflow definition revision must be a full 40-hex-character git commit SHA.
# Abbreviated SHAs are refused: an abbreviation is a *prefix match*, so it can
# become ambiguous as the repository grows, and "the approved revision" must not
# be a value that can later denote two different files.
_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
# Artifact revisions are compared, not just stored, so the same shape rule applies:
# a full immutable SHA or nothing. Case-insensitive because a caller may hand us an
# uppercase SHA from a provider API, and rejecting that would be a shape complaint
# about a perfectly immutable revision. Comparison casefolds both sides.
_ARTIFACT_SHA = re.compile(r"\A[0-9a-fA-F]{40}\Z")

# Workflow paths must be repository-relative and inside the workflows directory.
# Anchored rather than merely checked for a prefix so that neither an absolute
# path nor a traversal segment can name a file outside it.
_WORKFLOW_PATH = re.compile(r"\A\.github/workflows/[A-Za-z0-9._-]+\.ya?ml\Z")

# An AWS account id is exactly 12 digits. Validated for *shape* only — this
# module never treats a well-formed account id as a verified one, which is what
# `TargetEvidence` is for.
_ACCOUNT_ID = re.compile(r"\A[0-9]{12}\Z")


class ManifestError(RuntimeError):
    """The manifest document, or a request against it, was malformed.

    Distinct from a `TargetBlock` for the same reason `ExecutionStoreError` is
    distinct from a `CONFLICT` outcome in the merged execution store: a block is
    an *answer* about a target ("this is not resolvable, here is who fixes it"),
    while this is "the question could not be asked". Callers fail closed on it and
    must not treat it as an absent entry, because a manifest that fails to parse
    means the reviewed approvals are unreadable — which is not the same as there
    being no approvals.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EntryStatus(StrEnum):
    """Whether a manifest entry can be acted on, and if not, why not.

    `DISABLED` and `UNRESOLVED` are separate because they route to different
    people. A disabled entry is a *decision* — somebody reviewed this target and
    deliberately withheld it, so the fix is another review. An unresolved entry is
    a *gap* — the live target or its configuration does not exist or has not been
    verified yet, so the fix is a deployment or a connection step. Collapsing them
    into one "not usable" value would send both to the same wrong owner.
    """

    ENABLED = "enabled"  # Reviewed and actionable
    DISABLED = "disabled"  # Reviewed and deliberately withheld
    UNRESOLVED = "unresolved"  # Live target/configuration missing or unverified


class TargetBlockCode(StrEnum):
    """Why a manifest entry could not be resolved to a lockable physical target.

    Each member names something a *different* party resolves, which is why they do
    not collapse into a single failure. These values are surfaced to operators and
    (via #5151) recorded, so they are part of the contract rather than log text.

    There is deliberately no catch-all member: a block must name a resolvable
    condition and an owner, and an unclassified failure is a `ManifestError`
    (the question could not be asked) rather than a block with no route.
    """

    ENTRY_UNKNOWN = "entry_unknown"  # No manifest entry for the requested id
    ENTRY_DISABLED = "entry_disabled"  # Reviewed and withheld; another review reopens it
    TARGET_UNRESOLVED = "target_unresolved"  # Live target/config absent or unverified
    CONNECTION_UNVERIFIED = "connection_unverified"  # Connection readback has not proven the account
    EQUIVALENCE_UNVERIFIABLE = "equivalence_unverifiable"  # Cannot prove same/different physical target
    POLICY_TARGET_MISMATCH = "policy_target_mismatch"  # In-force policy does not permit this connection
    COMPONENT_NOT_COVERED = "component_not_covered"  # Requested component outside the entry's selectors
    WORKFLOW_REVISION_MISMATCH = "workflow_revision_mismatch"  # Definition is not the approved revision
    INPUT_NOT_PERMITTED = "input_not_permitted"  # An input or value outside the approved set


@dataclass(frozen=True)
class WorkflowRef:
    """An approved pipeline, pinned by path **and** immutable definition revision.

    Both halves are required and they do different jobs. `path` says where the
    definition lives; `definition_revision` says *which version of it* was
    approved. Only the second is immutable, and it is the one that makes the
    approval meaningful — see the module docstring on why a path alone approves a
    filename rather than a pipeline.

    `allowed_inputs` maps an input name to the exact set of values permitted for
    it. An empty value set is meaningful and is **not** "any value": it means the
    input is permitted only at the workflow's own default, which is how an entry
    approves passing an input through without approving a caller choosing its
    value. An input absent from the mapping entirely may not be passed at all.
    The distinction matters because the coarse alternative — "these input names
    are allowed" — would approve `environment: prod` wherever it approved
    `environment: dev`.
    """

    path: str
    definition_revision: str
    allowed_inputs: dict[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _WORKFLOW_PATH.match(str(self.path or "")):
            raise ManifestError(
                "invalid_workflow_path",
                f"A workflow path must be a repository-relative .github/workflows/*.yml file; got {self.path!r}.",
            )
        if not _FULL_SHA.match(str(self.definition_revision or "")):
            # Refused rather than normalized. An abbreviation cannot be expanded
            # here without reading the repository, and this module makes no such
            # call — so accepting one would store an approval whose subject this
            # process never determined.
            raise ManifestError(
                "invalid_workflow_revision",
                f"A workflow definition revision must be a full 40-character commit SHA; got {self.definition_revision!r}.",
            )
        for name, values in self.allowed_inputs.items():
            if not str(name or "").strip():
                raise ManifestError("invalid_workflow_input", "A workflow input name cannot be blank.")
            if not isinstance(values, frozenset):
                raise ManifestError(
                    "invalid_workflow_input",
                    f"Allowed values for input {name!r} must be a frozenset so the approved set cannot be mutated after review.",
                )

    def check_inputs(self, requested: dict[str, str]) -> str | None:
        """Check requested inputs against the approved set. Returns a reason, or None.

        Returns a *stable machine-readable reason* rather than raising, because the
        caller (#5151) records this alongside a refusal and must not have to parse
        an exception message. The returned string deliberately names the offending
        input but **not** the permitted values: a refusal that enumerated what would
        be accepted is a probe oracle for an approval set the caller has no standing
        to read.
        """
        for name, value in requested.items():
            permitted = self.allowed_inputs.get(name)
            if permitted is None:
                return f"input_not_permitted:{name}"
            if value not in permitted:
                # Covers the empty-set case too: an input approved only at its
                # workflow default has no permitted caller-supplied value, so any
                # value supplied here is refused.
                return f"input_value_not_permitted:{name}"
        return None


@dataclass(frozen=True)
class TargetEvidence:
    """What proved this physical target's identity, and when.

    Stored with the lease so that "why did the engine think these two connections
    were the same place?" is answerable later from the record rather than from logs
    that expire. The story requires the source of canonicalization be kept, and
    this is that field — an operator can go and re-check the named source.

    `source` names the mechanism that read the identity back from the provider
    (for example a verified AWS connection whose cross-account role assumption
    succeeded), and `verified_at` is an ISO-8601 timestamp of that readback.
    `detail` carries small non-sensitive context only: never a credential, never a
    token, never a role's session output. These values are read by operators, so a
    secret written here would be a disclosure with no revocation path.
    """

    source: str
    verified_at: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if not str(self.source or "").strip():
            raise ManifestError(
                "invalid_target_evidence",
                "Target evidence must name the source that proved the target; unevidenced canonicalization is a guess.",
            )
        if not str(self.verified_at or "").strip():
            raise ManifestError(
                "invalid_target_evidence",
                "Target evidence must record when the identity was read back; an undated readback cannot be judged stale.",
            )


@dataclass(frozen=True)
class PhysicalTarget:
    """One concrete deployment surface, as proven by provider readback.

    ## Why an account is not a target

    An AWS account is too coarse to serialize on. A dev cluster and a staging
    cluster in one account are independent surfaces, and a lease over "the
    account" would make two unrelated deployments block each other — which trains
    operators to bypass the lease, and a lease people bypass protects nothing. So
    the identity is provider + account + region **plus** the concrete resource
    boundary (`resource_kind`/`resource_id`, e.g. an EKS cluster and namespace).

    ## Why the caller's account string is never trusted

    `account_id` must come from a connection the platform has *verified* by
    actually assuming into it, not from a value a caller passed. A caller-supplied
    account string is an assertion about somebody else's infrastructure, and a run
    that could choose its own target could choose one it was never granted. That is
    why `evidence` is a required field rather than an optional annotation: a target
    with no readback behind it cannot be constructed at all, so there is no code
    path in which one is used by accident.

    ## Why the key is derived rather than stored as given

    `canonical_key` is a hash over the normalized identity tuple. Two aliases for
    one cluster — different connection ids, different labels, different tenants —
    normalize to the same tuple and therefore to the same key, which is what makes
    them collide in the lease table. It is opaque on purpose: it is returned in
    refusals, and a structured key would disclose another tenant's account id and
    cluster name to whoever asked for a busy target.
    """

    provider: str
    account_id: str
    region: str
    resource_kind: str
    resource_id: str
    evidence: TargetEvidence

    def __post_init__(self) -> None:
        for name in ("provider", "region", "resource_kind", "resource_id"):
            if not str(getattr(self, name) or "").strip():
                raise ManifestError(
                    "invalid_physical_target",
                    f"A physical target must name its {name}; an underspecified target cannot be serialized on.",
                )
        if not _ACCOUNT_ID.match(str(self.account_id or "")):
            raise ManifestError(
                "invalid_physical_target",
                "A physical target must carry a 12-digit AWS account id resolved from a verified connection.",
            )

    @property
    def canonical_key(self) -> str:
        """The opaque key the lease table enforces uniqueness on."""
        return canonical_target_key(
            provider=self.provider,
            account_id=self.account_id,
            region=self.region,
            resource_kind=self.resource_kind,
            resource_id=self.resource_id,
        )

    @property
    def describe(self) -> str:
        """A non-opaque description **for the target's own owner only**.

        Never put this in a refusal handed to a caller that does not hold the
        target: it names the account and cluster, which is exactly what
        `canonical_key`'s opacity exists to withhold. It is here for the holder's
        own diagnostics and for operator tooling that has already established
        standing.
        """
        return f"{self.provider}:{self.account_id}:{self.region}:{self.resource_kind}/{self.resource_id}"


def canonical_target_key(
    *,
    provider: str,
    account_id: str,
    region: str,
    resource_kind: str,
    resource_id: str,
) -> str:
    """Derive the opaque canonical key for one physical deployment surface.

    Normalization is **case-folding and whitespace-stripping only**, and that
    restraint is deliberate. Any cleverer equivalence rule — treating an ARN and a
    bare cluster name as the same thing, resolving an alias to its target — would
    be *guessing* at equivalence, and this module's rule is that unproven
    equivalence blocks rather than resolves. Case folding is safe because the
    identifiers involved are case-insensitive in the provider; anything beyond that
    belongs to whatever service can actually prove it, and its verdict arrives here
    as an already-resolved `resource_id`.

    The components are joined with a separator that cannot occur inside a
    normalized component, so `("a", "bc")` and `("ab", "c")` cannot hash alike —
    a collision there would merge two unrelated targets into one lease, silently
    serializing deployments that should be independent.

    Returns:
        A version-tagged hex digest. Opaque by construction: it is returned in
        conflict responses to callers with no standing to learn the account id or
        cluster name behind it.
    """
    parts = [provider, account_id, region, resource_kind, resource_id]
    normalized = []
    for index, part in enumerate(parts):
        text = str(part or "").strip().casefold()
        if not text:
            raise ManifestError(
                "invalid_target_key",
                "Every component of a canonical target key must be non-empty; a blank component would collide unrelated targets.",
            )
        if "\x1f" in text:
            # The separator must not be forgeable from within a component, or a
            # caller-influenced value could impersonate a different tuple.
            raise ManifestError(
                "invalid_target_key",
                f"Target key component {index} contains a reserved separator character.",
            )
        normalized.append(text)
    digest = hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()
    return f"{CANONICAL_KEY_VERSION}:{digest}"


@dataclass(frozen=True)
class TargetBlock:
    """A typed refusal to resolve a manifest entry, naming who resolves it.

    Every field exists because an operator reading only this must be able to act.
    A refusal that said "unresolved" and nothing else sends somebody to logs that
    expire. `owner` names the party who can clear it and `required_input` says what
    they must supply, in plain words.

    `detail` must stay free of another tenant's specifics. The story requires
    conflict information be scoped and generic, and a detail string naming the
    other holder's org or account would defeat that while looking like helpfulness.
    """

    code: TargetBlockCode
    owner: str
    required_input: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if not str(self.owner or "").strip():
            raise ManifestError("invalid_target_block", "A block must name who resolves it; an unowned block is a stall.")
        if not str(self.required_input or "").strip():
            raise ManifestError("invalid_target_block", "A block must state what input is required to clear it.")


@dataclass(frozen=True)
class ManifestEntry:
    """One reviewed deployment target and everything a deploy to it may consist of.

    This is the unit of human approval. Each field narrows something an agent
    would otherwise choose for itself at run time:

    - `connection_id` — the registered environment connection to deploy through,
      which must also appear in the in-force policy's permitted list. Two
      independent checks (reviewed manifest, in-force policy) rather than one,
      because they can legitimately disagree: a manifest reviewed last week must
      not outlive a policy amendment that withdrew the target.
    - `component_selectors` — which components this entry covers. A deploy naming
      a component outside them is refused, so "deploy the gateway" cannot quietly
      become "deploy everything".
    - `workflow` — the pinned pipeline and its permitted inputs.
    - `artifact_revision` — the immutable build this entry is approved to ship.
      **Required** for an enabled entry unless `docs_only`, and validated as a
      full 40-character SHA whatever the status. Deferring to "the workflow's own
      resolution" sounds honest but is not a pin: the same reviewed approval would
      ship a different build tomorrow, and an incident would have no answer to
      "what was deployed?". An entry that has no SHA to pin yet stays
      `unresolved` with `artifact_revision: ~` — which is the honest state —
      rather than being enabled without one.
    - `verification_adapter` / `rollback_adapter` — *names* of adapters #5152 owns.
      A present rollback name is a permission, not an instruction; nothing here
      executes either.
    - `resource_kind` / `resource_id` — the concrete deployment surface, which is
      what makes the physical target finer-grained than an account.

    An entry whose status is not `ENABLED` must carry `unresolved_reason`. That is
    enforced in `__post_init__` rather than trusted to reviewers because the whole
    value of an explicit disabled entry over an absent one is that it *says why*.
    """

    entry_id: str
    status: EntryStatus
    connection_id: str | None
    component_selectors: tuple[str, ...]
    workflow: WorkflowRef | None
    resource_kind: str | None = None
    resource_id: str | None = None
    artifact_revision: str | None = None
    verification_adapter: str | None = None
    rollback_adapter: str | None = None
    unresolved_reason: str | None = None
    docs_only: bool = False

    def __post_init__(self) -> None:
        if not str(self.entry_id or "").strip():
            raise ManifestError("invalid_manifest_entry", "A manifest entry must have an entry_id.")
        if self.artifact_revision is not None and not _ARTIFACT_SHA.match(str(self.artifact_revision)):
            # Refuses "latest", "main", a tag, or an abbreviation. Every one of
            # those is resolved at *use* time by something other than the
            # reviewer, so storing one records an approval of a name rather than
            # of a build. Enforced for every status, not only enabled ones, so a
            # mutable pin cannot be parked in an unresolved entry and then enabled
            # by a one-line status edit that changes no value a reviewer re-reads.
            raise ManifestError(
                "invalid_artifact_revision",
                f"Entry {self.entry_id!r} pins artifact_revision {self.artifact_revision!r}; "
                "a pin must be a full 40-character commit SHA, because any mutable name is resolved by something other than the reviewer.",
            )
        if self.status is EntryStatus.ENABLED:
            # An enabled entry is the one an agent can act on, so every field a
            # dispatch needs must be present *at review time*. Discovering a
            # missing connection id at dispatch time would turn a reviewed
            # approval into a runtime failure.
            required = ["connection_id", "workflow", "resource_kind", "resource_id", "verification_adapter"]
            if not self.docs_only:
                # An enabled entry that ships code must name WHICH build it ships.
                # Without this the entry approves "whatever the workflow resolves
                # at dispatch time", which is a moving target: the same reviewed
                # approval would ship a different artifact tomorrow, and an
                # incident would have no answer to "what was deployed?".
                #
                # `docs_only` entries are carved out deliberately, not by
                # oversight: they produce no deployable artifact, so requiring a
                # build SHA from them would force a reviewer to invent one — the
                # exact failure this check exists to prevent.
                required.append("artifact_revision")
            missing = [name for name in required if not getattr(self, name)]
            if missing:
                raise ManifestError(
                    "incomplete_manifest_entry",
                    f"Enabled entry {self.entry_id!r} is missing {', '.join(missing)}; "
                    "an enabled entry must be fully specified or recorded as unresolved.",
                )
            if not self.component_selectors:
                raise ManifestError(
                    "incomplete_manifest_entry",
                    f"Enabled entry {self.entry_id!r} must name at least one component selector; an entry covering nothing can only ever refuse.",
                )
        elif not str(self.unresolved_reason or "").strip():
            raise ManifestError(
                "unexplained_manifest_entry",
                f"Entry {self.entry_id!r} is {self.status.value} and must state why in unresolved_reason; "
                "an unexplained non-enabled entry is indistinguishable from an oversight.",
            )

    def covers(self, component: str) -> bool:
        """Whether this entry's selectors cover one component.

        Exact match on a normalized name. Prefix or glob matching is deliberately
        absent: a selector meant to approve one component would silently widen to
        every component sharing its prefix, which is the kind of over-approval a
        reviewer cannot see when reading the manifest.
        """
        wanted = str(component or "").strip().casefold()
        return any(wanted == selector.strip().casefold() for selector in self.component_selectors)


@dataclass(frozen=True)
class DeploymentManifest:
    """The reviewed set of deployment entries, keyed by entry id."""

    schema_version: int
    entries: tuple[ManifestEntry, ...]

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestError(
                "unsupported_schema_version",
                f"This build understands manifest schema version {MANIFEST_SCHEMA_VERSION}, but the document declares {self.schema_version}.",
            )
        seen: set[str] = set()
        for entry in self.entries:
            key = entry.entry_id.strip().casefold()
            if key in seen:
                # Two entries for one id would make "the approval for X" depend on
                # iteration order, so one reviewed entry could be shadowed by
                # another that a reviewer believed applied elsewhere.
                raise ManifestError("duplicate_manifest_entry", f"Manifest declares entry {entry.entry_id!r} more than once.")
            seen.add(key)

    def entry(self, entry_id: str) -> ManifestEntry | None:
        wanted = str(entry_id or "").strip().casefold()
        return next((e for e in self.entries if e.entry_id.strip().casefold() == wanted), None)


@dataclass(frozen=True)
class TargetResolution:
    """The answer to "may this deploy proceed, and against which target?".

    Exactly one of `target` / `block` is set, and `resolved` is the property
    consumers branch on. Typed rather than a bare `PhysicalTarget | None` because
    a `None` return would make "refused" and "nothing configured" the same value
    at the call site — and those route to different owners.
    """

    entry: ManifestEntry | None
    target: PhysicalTarget | None = None
    block: TargetBlock | None = None

    def __post_init__(self) -> None:
        if (self.target is None) == (self.block is None):
            raise ManifestError(
                "invalid_target_resolution",
                "A resolution must carry exactly one of target/block; anything else is an ambiguous answer about a deploy.",
            )

    @property
    def resolved(self) -> bool:
        return self.target is not None


def resolve_manifest_entry(
    manifest: DeploymentManifest,
    *,
    entry_id: str,
    component: str,
    policy_connection_ids: frozenset[str],
    target_lookup,
    requested_inputs: dict[str, str] | None = None,
    resolved_workflow_revision: str | None = None,
) -> TargetResolution:
    """Resolve one manifest entry to a lockable physical target, or a typed block.

    This is the function #5151 calls before it dispatches anything, and it is
    deliberately the *only* place the several independent checks are sequenced, so
    a second caller cannot implement a weaker version of the same gate.

    Checks run cheapest-first and each refuses something specific: the entry
    exists; it is enabled; the requested component is covered; the in-force policy
    still permits the entry's connection; the requested inputs are within the
    approved set; and only then is the physical target resolved from trusted
    readback.

    The policy check is here even though the manifest already names the connection
    because the two can legitimately disagree — a manifest reviewed last week must
    not outlive a policy amendment that withdrew the target. Checking only the
    manifest would let a withdrawn target stay deployable until somebody
    remembered to edit a YAML file.

    Args:
        policy_connection_ids: The connection ids the in-force `ExecutionPolicy`
            permits (`ExecutionPolicy.environment_connection_ids`). Passed in
            rather than loaded here because this module performs no I/O and must
            not form a second opinion about policy — `execution_policy` owns that.
        target_lookup: A callable taking the connection id and returning a
            `PhysicalTarget` proven by provider readback, or `None` when the
            connection is not verified. Injected so this module stays free of
            database access and so the trusted-readback path remains the caller's
            existing connection service rather than a second implementation here.
            An exception from it is **not** converted into a pass: it becomes an
            `EQUIVALENCE_UNVERIFIABLE` block, because a lookup that failed has not
            established that the target is free to use.
        resolved_workflow_revision: The commit SHA the caller actually resolved the
            workflow definition to, read from the repository or the provider at
            dispatch time. Checked against the entry's approved
            `definition_revision`.

            Passed in, and required whenever the entry pins a workflow, because
            this module reads nothing: it cannot resolve a revision itself, so the
            only alternative to injection is to skip the check — which would leave
            `WORKFLOW_REVISION_MISMATCH` declared and never raised, i.e. a
            documented guarantee the code does not enforce.

            `None` is a **block, not a pass**. "The caller did not tell us which
            definition it resolved" and "the definition matches" are different
            facts, and treating the first as the second is precisely how a
            pinned-revision approval degrades into a pinned-*filename* approval:
            the workflow file at that path can be edited after review, and an
            unchecked dispatch would run the edited version under the old
            approval. The two cases carry distinct `detail` values so the operator
            is told whether to supply a revision or to re-review the entry.

    Returns:
        A `TargetResolution` carrying either the physical target to lease or a
        `TargetBlock` naming the owner and the required input.
    """
    entry = manifest.entry(entry_id)
    if entry is None:
        return TargetResolution(
            entry=None,
            block=TargetBlock(
                code=TargetBlockCode.ENTRY_UNKNOWN,
                owner="platform operator",
                required_input=f"Add a reviewed manifest entry for {entry_id!r} to src/orchestration/manifests/orchestration-deployments.yaml.",
            ),
        )

    if entry.status is not EntryStatus.ENABLED:
        # Disabled and unresolved are reported distinctly: the first needs another
        # review, the second needs the live target or connection to exist.
        code = TargetBlockCode.ENTRY_DISABLED if entry.status is EntryStatus.DISABLED else TargetBlockCode.TARGET_UNRESOLVED
        owner = "platform operator" if entry.status is EntryStatus.DISABLED else "environment owner"
        return TargetResolution(
            entry=entry,
            block=TargetBlock(
                code=code,
                owner=owner,
                required_input=entry.unresolved_reason or "Resolve the entry and re-review it.",
                detail=entry.unresolved_reason,
            ),
        )

    if not entry.covers(component):
        return TargetResolution(
            entry=entry,
            block=TargetBlock(
                code=TargetBlockCode.COMPONENT_NOT_COVERED,
                owner="platform operator",
                required_input=(
                    f"Component {component!r} is not covered by entry {entry.entry_id!r}; widen the reviewed selectors or pick the right entry."
                ),
            ),
        )

    if entry.connection_id not in policy_connection_ids:
        return TargetResolution(
            entry=entry,
            block=TargetBlock(
                code=TargetBlockCode.POLICY_TARGET_MISMATCH,
                owner="plan approver",
                # Deliberately does not echo the policy's permitted ids: the
                # caller asked about one target and is not owed the list of
                # everything the plan permits.
                required_input="The in-force accepted plan does not permit this deployment target; amend the plan or choose a permitted target.",
            ),
        )

    if entry.workflow is not None:
        # The approval is of a *definition*, not of a path. See the module
        # docstring: a path names a file that anyone with write access can change
        # after review, so the pinned revision is the only half of the pin that
        # makes the approval mean anything — and a pin that is never compared is
        # not a pin.
        if resolved_workflow_revision is None:
            return TargetResolution(
                entry=entry,
                block=TargetBlock(
                    code=TargetBlockCode.WORKFLOW_REVISION_MISMATCH,
                    owner="platform operator",
                    required_input=(
                        "Resolve the workflow definition to a commit SHA and pass it as resolved_workflow_revision; "
                        "an unchecked definition cannot be dispatched under a pinned approval."
                    ),
                    detail="workflow_revision_unresolved",
                ),
            )
        if str(resolved_workflow_revision).strip().casefold() != entry.workflow.definition_revision.strip().casefold():
            return TargetResolution(
                entry=entry,
                block=TargetBlock(
                    code=TargetBlockCode.WORKFLOW_REVISION_MISMATCH,
                    owner="plan approver",
                    # Neither SHA is echoed. The caller supplied one and does not
                    # need it back; the approved one belongs to the review record,
                    # and repeating it here would let a caller enumerate approved
                    # revisions by guessing.
                    required_input=(
                        "The workflow definition resolved for this dispatch is not the revision this entry approves; "
                        "re-review the entry against the current definition or dispatch the approved revision."
                    ),
                    detail="workflow_revision_mismatch",
                ),
            )

    if entry.workflow is not None and requested_inputs:
        reason = entry.workflow.check_inputs(requested_inputs)
        if reason is not None:
            return TargetResolution(
                entry=entry,
                block=TargetBlock(
                    code=TargetBlockCode.INPUT_NOT_PERMITTED,
                    owner="platform operator",
                    required_input="A requested workflow input is outside the reviewed allow-list; remove it or have the entry re-reviewed.",
                    detail=reason,
                ),
            )

    try:
        target = target_lookup(entry.connection_id)
    except Exception as exc:  # noqa: BLE001 - see below; any failure must fail closed
        # A failed lookup is not permission to proceed. Letting the exception
        # propagate would be acceptable, but converting it to a typed block keeps
        # the caller's handling uniform and — more importantly — makes it
        # impossible for a caller's broad `except` around this call to turn a
        # failure to verify into an unverified deploy.
        return TargetResolution(
            entry=entry,
            block=TargetBlock(
                code=TargetBlockCode.EQUIVALENCE_UNVERIFIABLE,
                owner="platform operator",
                required_input=(
                    "The deployment target could not be resolved from its registered connection; retry once the connection service is reachable."
                ),
                detail=type(exc).__name__,
            ),
        )

    if target is None:
        return TargetResolution(
            entry=entry,
            block=TargetBlock(
                code=TargetBlockCode.CONNECTION_UNVERIFIED,
                owner="environment owner",
                required_input="Verify the registered AWS connection for this environment so its account can be proven by readback.",
            ),
        )

    if not isinstance(target, PhysicalTarget):
        # A lookup that returned something else has not proven a target, and
        # coercing it here would be inventing the identity we refuse to invent.
        raise ManifestError(
            "invalid_target_lookup",
            "target_lookup must return a PhysicalTarget or None; a different shape cannot carry canonicalization evidence.",
        )

    if entry.resource_kind and entry.resource_id:
        # The manifest names the surface a human approved; the connection proves
        # the account. If the resolved target's surface is not the approved one,
        # the two disagree about *where* this deploys — which is unknown
        # equivalence, and the story requires a typed block rather than a choice
        # between them.
        approved = (entry.resource_kind.strip().casefold(), entry.resource_id.strip().casefold())
        resolved = (target.resource_kind.strip().casefold(), target.resource_id.strip().casefold())
        if approved != resolved:
            return TargetResolution(
                entry=entry,
                block=TargetBlock(
                    code=TargetBlockCode.EQUIVALENCE_UNVERIFIABLE,
                    owner="platform operator",
                    required_input=(
                        "The reviewed deployment surface and the surface resolved from the connection "
                        "do not match; re-review the entry against the live target."
                    ),
                ),
            )

    return TargetResolution(entry=entry, target=target)


def parse_manifest(document: dict) -> DeploymentManifest:
    """Build a `DeploymentManifest` from an already-parsed mapping.

    Takes a `dict` rather than a path or a YAML string so that this module needs
    no YAML dependency: `pyyaml` is a **dev-only** dependency of this package, so
    importing it at module scope here would work in tests and fail at runtime in
    the deployed image. `load_manifest_document` isolates the parse for callers
    that do have it.

    Unknown top-level or per-entry keys are refused rather than ignored. Ignoring
    an unknown key is how a narrowing constraint gets silently dropped — a
    reviewer adds a field intending to restrict something, an older build does not
    implement it, and the deploy proceeds with an approval nobody granted.
    """
    if not isinstance(document, dict):
        raise ManifestError("invalid_manifest", "A manifest document must be a mapping.")

    unknown = set(document) - {"schema_version", "entries"}
    if unknown:
        raise ManifestError("unknown_manifest_key", f"Manifest declares unsupported top-level key(s): {sorted(unknown)}.")

    raw_version = document.get("schema_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise ManifestError("invalid_manifest", "A manifest must declare an integer schema_version.")

    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ManifestError("invalid_manifest", "A manifest must declare an entries list.")

    entries = tuple(_parse_entry(raw) for raw in raw_entries)
    return DeploymentManifest(schema_version=raw_version, entries=entries)


_ENTRY_KEYS = frozenset(
    {
        "entry_id",
        "status",
        "connection_id",
        "component_selectors",
        "workflow",
        "resource_kind",
        "resource_id",
        "artifact_revision",
        "verification_adapter",
        "rollback_adapter",
        "unresolved_reason",
        "docs_only",
    }
)

_WORKFLOW_KEYS = frozenset({"path", "definition_revision", "allowed_inputs"})


def _parse_entry(raw: object) -> ManifestEntry:
    if not isinstance(raw, dict):
        raise ManifestError("invalid_manifest_entry", "Each manifest entry must be a mapping.")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise ManifestError("unknown_manifest_key", f"Manifest entry declares unsupported key(s): {sorted(unknown)}.")

    raw_status = raw.get("status")
    try:
        status = EntryStatus(str(raw_status))
    except ValueError as exc:
        raise ManifestError(
            "invalid_manifest_entry",
            f"Entry status must be one of {sorted(s.value for s in EntryStatus)}; got {raw_status!r}.",
        ) from exc

    selectors_raw = raw.get("component_selectors") or []
    if not isinstance(selectors_raw, list) or any(not isinstance(s, str) for s in selectors_raw):
        raise ManifestError("invalid_manifest_entry", "component_selectors must be a list of strings.")

    docs_only = raw.get("docs_only", False)
    if not isinstance(docs_only, bool):
        # Explicit rather than truthy-coerced: `docs_only: "no"` is truthy in
        # Python and would classify a real deploy as documentation-only.
        raise ManifestError("invalid_manifest_entry", "docs_only must be an explicit boolean.")

    return ManifestEntry(
        entry_id=str(raw.get("entry_id") or ""),
        status=status,
        connection_id=raw.get("connection_id") or None,
        component_selectors=tuple(selectors_raw),
        workflow=_parse_workflow(raw.get("workflow")),
        resource_kind=raw.get("resource_kind") or None,
        resource_id=raw.get("resource_id") or None,
        artifact_revision=raw.get("artifact_revision") or None,
        verification_adapter=raw.get("verification_adapter") or None,
        rollback_adapter=raw.get("rollback_adapter") or None,
        unresolved_reason=raw.get("unresolved_reason") or None,
        docs_only=docs_only,
    )


def _parse_workflow(raw: object) -> WorkflowRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("invalid_manifest_entry", "An entry's workflow must be a mapping.")
    unknown = set(raw) - _WORKFLOW_KEYS
    if unknown:
        raise ManifestError("unknown_manifest_key", f"Workflow declares unsupported key(s): {sorted(unknown)}.")

    allowed_raw = raw.get("allowed_inputs") or {}
    if not isinstance(allowed_raw, dict):
        raise ManifestError("invalid_manifest_entry", "allowed_inputs must be a mapping of input name to permitted values.")
    allowed: dict[str, frozenset[str]] = {}
    for name, values in allowed_raw.items():
        if values is None:
            # An explicitly empty allow-list: the input may be passed only at the
            # workflow's own default. See WorkflowRef.allowed_inputs.
            allowed[str(name)] = frozenset()
            continue
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ManifestError(
                "invalid_manifest_entry",
                f"Permitted values for input {name!r} must be a list of strings (or empty for default-only).",
            )
        allowed[str(name)] = frozenset(values)

    return WorkflowRef(
        path=str(raw.get("path") or ""),
        definition_revision=str(raw.get("definition_revision") or ""),
        allowed_inputs=allowed,
    )


PACKAGED_MANIFEST_ANCHOR = "src.orchestration.manifests"
PACKAGED_MANIFEST_NAME = "orchestration-deployments.yaml"


def load_manifest_document(text: str) -> DeploymentManifest:
    """Parse manifest YAML text into a `DeploymentManifest`.

    `yaml.safe_load` rather than `yaml.load`: this file is reviewed, but a loader
    that can construct arbitrary Python objects should never be pointed at
    configuration, because the day that assumption changes is not announced.

    Takes text rather than a path so the caller owns the I/O — a test can pass a
    literal document, and `load_packaged_manifest` below supplies the one the image
    actually ships.
    """
    import yaml  # noqa: PLC0415 - kept function-local; see load_packaged_manifest

    return parse_manifest(yaml.safe_load(text) or {})


def load_packaged_manifest() -> DeploymentManifest:
    """Load the reviewed manifest that ships inside this distribution.

    THE DEPLOYED LOADING CONTRACT
    -----------------------------
    This is the only supported way for the running gateway to obtain the manifest.
    Two things make it work in the image, and both were verified by building a wheel
    and by reading the image definition rather than assumed:

    1. **The YAML lives inside `src/`**, which the Dockerfile already carries with
       `COPY src/ src/`, and the gateway image is built with `modules/gateway` as its
       docker context (`codebuild/bs-gateway-build.yml`: `cd modules/gateway && docker
       build .`). This is the load-bearing mechanism: the container runs
       `uvicorn src.app:create_app` from `/app`, so it imports the copied tree. A
       repository-root `config/` directory is not in that build context at all, so no
       `COPY` could reach it — which is why this file is not there.
    2. **`pyyaml` is a runtime dependency**, not a `dev` extra. The image installs with
       bare `pip install .`, which resolves no extras — so a manifest loader relying on
       a dev-only parser would pass every test and raise `ModuleNotFoundError` in the
       pod.

    `[tool.setuptools.package-data]` also names this package, mirroring the
    `pricing_policy/snapshots` precedent (#4969). Measured honestly: under the
    current backend (`setuptools>=75`, `include-package-data` left at its pyproject
    default of true) the YAML is included in a built wheel *without* that entry, so
    it is a declaration of intent and a guard against that default changing — **not**
    the reason the file is present. It is recorded that way here so nobody later
    treats removing it as safe *because* of a comment, or as sufficient *on its own*
    for a consumer installing from a wheel.

    `importlib.resources.files()` rather than `Path(__file__).parents[n]`: the
    latter resolves relative to the source tree and breaks once the package is
    installed rather than run from a checkout — exactly the difference between a
    test environment and the image.

    Raises:
        ManifestError: if the packaged manifest is missing. Fails closed and names
            the packaging cause, because a gateway that silently substituted an
            empty manifest would refuse every deployment with `ENTRY_UNKNOWN` and
            send an operator looking for a missing review rather than a missing
            file.
    """
    from importlib.resources import files  # noqa: PLC0415 - local, mirrors pricing_policy's loader

    try:
        text = files(PACKAGED_MANIFEST_ANCHOR).joinpath(PACKAGED_MANIFEST_NAME).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ManifestError(
            "packaged_manifest_missing",
            f"The reviewed deployment manifest {PACKAGED_MANIFEST_NAME!r} is not present in this build "
            f"(anchor {PACKAGED_MANIFEST_ANCHOR!r}); it must be packaged as package-data, not read from the repository.",
        ) from exc
    return load_manifest_document(text)
