"""Qualification config loading for the reusable orchestration harness (#5156).

The config file describes WHERE a bounded qualification may run and WHAT it may
spend. It never contains a credential: secrets are named by reference and
resolved at runtime by :func:`resolve_secret_ref`.

Loading is deliberately unforgiving. A qualification provisions real resources
in a real account, so an ambiguous config is refused rather than defaulted:
unknown keys, unknown targets, unpinned versions, missing or non-positive
bounds and anything that looks like an embedded secret are all errors. Every
problem found is reported at once so an operator fixes the file in one pass.

Standard library only, and no AWS call happens at import or during
:func:`load_config` — the offline tests import this module directly.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

CONFIG_VERSION = 1

# Targets an operator may name. Production is refused outright: this harness
# provisions fixtures, and nothing in #5156 authorizes it against production.
ALLOWED_ENVIRONMENTS = ("dev", "staging")
REFUSED_ENVIRONMENTS = ("prod", "production")

# Ceilings applied on top of whatever the operator wrote. A config may ask for
# less than these; it may never ask for more. This bounds the blast radius of a
# typo (max_usd: 10000) without pretending to replace the real policy/budget
# authority in #5128, which still applies at run time.
BOUND_CEILINGS: dict[str, float] = {
    "max_resources": 50,
    "max_runs": 20,
    "max_usd": 50.0,
    "max_duration_seconds": 7200,
}

_SECRET_REF = re.compile(r"^(secretsmanager|ssm|env):[A-Za-z0-9_./-]{1,256}$")
# A pinned version is an exact semver or a 40-character commit SHA. Floating
# refs are rejected by name below so the error says why.
_PINNED_VERSION = re.compile(r"^(?:\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?|[0-9a-f]{40})$")
_FLOATING_VERSIONS = frozenset({"latest", "main", "master", "head", "stable", "edge", "*", ""})
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SCENARIO_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# An AWS account id is exactly 12 digits. Declared in the config so the harness
# can compare it against the account the credentials ACTUALLY resolve to.
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
_ORG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

# Keys that must never hold a literal value in a config file. A secret belongs
# in `secret_refs` as a reference; seeing one of these names anywhere else means
# somebody pasted a credential in.
_SECRET_LIKE_KEY = re.compile(
    r"(password|passwd|secret|token|credential|private_key|access_key|api_key)",
    re.IGNORECASE,
)

# Shapes that are credentials regardless of the key they sit under.
_EMBEDDED_SECRET_VALUES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS temporary access key id"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub fine-grained token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "Slack token"),
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "config_version",
        "environment",
        "connection",
        "identity",
        "versions",
        "bounds",
        "artifacts",
        "secret_refs",
        "scenarios",
    }
)
_REQUIRED_TOP_LEVEL = (
    "config_version",
    "environment",
    "connection",
    "identity",
    "versions",
    "bounds",
    "artifacts",
)
_SECTION_KEYS: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    # section -> (allowed keys, required keys)
    # `expected_account_id` and `expected_org` are what the selected connection
    # MUST resolve to. They are required: without them there is nothing to check
    # the real identity against, and reporting whichever account the credentials
    # happen to reach is not target verification.
    "connection": (
        frozenset({"connection_ref", "repository", "expected_account_id", "expected_org"}),
        ("connection_ref", "repository", "expected_account_id", "expected_org"),
    ),
    "identity": (
        frozenset({"org_ref", "team_ref", "identity_ref"}),
        ("org_ref", "team_ref", "identity_ref"),
    ),
    "versions": (frozenset({"engine", "worker", "harness"}), ("engine", "worker", "harness")),
    "bounds": (
        frozenset(BOUND_CEILINGS),
        ("max_resources", "max_runs", "max_usd", "max_duration_seconds"),
    ),
    "artifacts": (frozenset({"directory"}), ("directory",)),
}
_INTEGER_BOUNDS = ("max_resources", "max_runs", "max_duration_seconds")


class ConfigError(ValueError):
    """A config was refused. Carries every problem found, not just the first."""

    def __init__(self, problems: list[str], source: Path | str | None = None):
        self.problems = list(problems)
        self.source = str(source) if source is not None else None
        where = f" in {self.source}" if self.source else ""
        detail = "".join(f"\n  - {p}" for p in self.problems)
        super().__init__(f"Refused qualification config{where}:{detail}")


@dataclass(frozen=True)
class QualificationConfig:
    """A validated, secret-free qualification config."""

    environment: str
    connection_ref: str
    repository: str
    expected_account_id: str
    expected_org: str
    org_ref: str
    team_ref: str
    identity_ref: str
    versions: dict[str, str]
    bounds: dict[str, float]
    artifact_directory: Path
    secret_refs: dict[str, str] = field(default_factory=dict)
    scenarios: tuple[str, ...] = ()
    source: str | None = None

    @property
    def max_resources(self) -> int:
        return int(self.bounds["max_resources"])

    @property
    def max_runs(self) -> int:
        return int(self.bounds["max_runs"])

    @property
    def max_usd(self) -> float:
        return float(self.bounds["max_usd"])

    @property
    def max_duration_seconds(self) -> int:
        return int(self.bounds["max_duration_seconds"])

    def ownership_tags(self, qualification_id: str) -> dict[str, str]:
        """Tags stamped on every fixture so cleanup can prove ownership.

        `inventory.py` records these and `fixtures.py` refuses to delete
        anything whose observed tags do not match.
        """
        return {
            "adp:qualification-id": qualification_id,
            "adp:qualification-environment": self.environment,
            "adp:managed-by": "tests.e2e.orchestration",
        }


def load_config(path: str | Path) -> QualificationConfig:
    """Parse and validate a qualification config file.

    Raises :class:`ConfigError` listing every problem. Performs no network or
    AWS call — secret references are validated for shape only and resolved
    later by :func:`resolve_secret_ref`.
    """
    source = Path(path)
    try:
        raw_text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError([f"config file does not exist: {source}"]) from None
    except OSError as exc:
        raise ConfigError([f"config file could not be read: {exc}"]) from None

    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError([f"config is not valid JSON: {exc}"], source) from None

    if not isinstance(document, dict):
        raise ConfigError(["config root must be a JSON object"], source)

    problems: list[str] = []
    _check_embedded_secrets(document, problems)
    _check_shape(document, problems)

    if problems:
        raise ConfigError(problems, source)

    config = QualificationConfig(
        environment=document["environment"],
        connection_ref=document["connection"]["connection_ref"],
        repository=document["connection"]["repository"],
        expected_account_id=document["connection"]["expected_account_id"],
        expected_org=document["connection"]["expected_org"],
        org_ref=document["identity"]["org_ref"],
        team_ref=document["identity"]["team_ref"],
        identity_ref=document["identity"]["identity_ref"],
        versions=dict(document["versions"]),
        bounds={k: document["bounds"][k] for k in _SECTION_KEYS["bounds"][1]},
        artifact_directory=Path(document["artifacts"]["directory"]),
        secret_refs=dict(document.get("secret_refs") or {}),
        scenarios=tuple(document.get("scenarios") or ()),
        source=str(source),
    )
    return config


def _check_embedded_secrets(document: dict[str, Any], problems: list[str]) -> None:
    """Refuse a config that carries a credential instead of a reference."""

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{trail}.{key}" if trail else str(key)
                # `secret_refs` is the one place a secret-like NAME is expected,
                # including the section key itself; its values are still checked
                # for credential shapes below.
                in_secret_refs = child == "secret_refs" or child.startswith("secret_refs.")
                if not in_secret_refs and _SECRET_LIKE_KEY.search(str(key)):
                    problems.append(
                        f"{child}: secret-like key is not allowed in a config file; "
                        f"name it under 'secret_refs' as a reference "
                        f"(e.g. 'secretsmanager:adp/dev/...') instead"
                    )
                walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")
        elif isinstance(node, str):
            for pattern, label in _EMBEDDED_SECRET_VALUES:
                if pattern.search(node):
                    # Never echo the matched value.
                    problems.append(f"{trail}: value looks like an embedded {label}; use a secret reference")
                    break

    walk(document, "")


def _check_shape(document: dict[str, Any], problems: list[str]) -> None:
    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"unknown top-level key(s): {', '.join(unknown)}")

    for key in _REQUIRED_TOP_LEVEL:
        if key not in document:
            problems.append(f"missing required key: {key}")

    if document.get("config_version") != CONFIG_VERSION:
        problems.append(f"config_version must be {CONFIG_VERSION}, got {document.get('config_version')!r}")

    _check_environment(document.get("environment"), problems)

    for section, (allowed, required) in _SECTION_KEYS.items():
        body = document.get(section)
        if body is None:
            continue  # already reported as missing when required
        if not isinstance(body, dict):
            problems.append(f"{section} must be an object")
            continue
        extra = sorted(set(body) - allowed)
        if extra:
            problems.append(f"unknown key(s) in {section}: {', '.join(extra)}")
        for key in required:
            if key not in body:
                problems.append(f"missing required key: {section}.{key}")

    connection = document.get("connection")
    if isinstance(connection, dict):
        _check_ref(connection.get("connection_ref"), "connection.connection_ref", problems)
        repository = connection.get("repository")
        if repository is not None and not (isinstance(repository, str) and _REPOSITORY.match(repository)):
            problems.append(f"connection.repository must be 'owner/repo', got {repository!r}")
        account = connection.get("expected_account_id")
        if account is not None and not (isinstance(account, str) and _ACCOUNT_ID.match(account)):
            # A string, not an int: a 12-digit account id with a leading zero
            # loses it in JSON number form.
            problems.append(
                f"connection.expected_account_id must be a 12-digit AWS account id as a string, got {account!r}"
            )
        org = connection.get("expected_org")
        if org is not None and not (isinstance(org, str) and _ORG.match(org)):
            problems.append(f"connection.expected_org must be a GitHub org name, got {org!r}")

    identity = document.get("identity")
    if isinstance(identity, dict):
        for key in ("org_ref", "team_ref", "identity_ref"):
            _check_ref(identity.get(key), f"identity.{key}", problems)

    _check_versions(document.get("versions"), problems)
    _check_bounds(document.get("bounds"), problems)
    _check_artifacts(document.get("artifacts"), problems)
    _check_secret_refs(document.get("secret_refs"), problems)
    _check_scenarios(document.get("scenarios"), problems)


def _check_environment(environment: Any, problems: list[str]) -> None:
    if environment is None:
        return
    if not isinstance(environment, str):
        problems.append("environment must be a string")
        return
    if environment.lower() in REFUSED_ENVIRONMENTS:
        problems.append(
            f"environment {environment!r} is refused: this harness provisions fixtures and is "
            f"not authorized against production"
        )
        return
    if environment not in ALLOWED_ENVIRONMENTS:
        problems.append(
            f"unknown target environment {environment!r}; expected one of {', '.join(ALLOWED_ENVIRONMENTS)}"
        )


def _check_ref(value: Any, label: str, problems: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _REF.match(value):
        problems.append(f"{label} must be a reference matching {_REF.pattern}, got {value!r}")


def _check_versions(versions: Any, problems: list[str]) -> None:
    if not isinstance(versions, dict):
        return
    for key in ("engine", "worker", "harness"):
        value = versions.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            problems.append(f"versions.{key} must be a string")
            continue
        if value.strip().lower() in _FLOATING_VERSIONS:
            problems.append(
                f"versions.{key} must be pinned, got the floating ref {value!r}; "
                f"use an exact semver or a 40-character commit SHA"
            )
            continue
        if not _PINNED_VERSION.match(value):
            problems.append(
                f"versions.{key} is malformed: {value!r} is neither an exact semver "
                f"(1.2.3) nor a 40-character commit SHA"
            )


def _check_bounds(bounds: Any, problems: list[str]) -> None:
    if not isinstance(bounds, dict):
        return
    for key, ceiling in BOUND_CEILINGS.items():
        if key not in bounds:
            continue
        value = bounds[key]
        # bool is an int subclass; `max_runs: true` is a mistake, not a bound.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                f"bounds.{key} must be a positive number bounding the run, got {value!r} "
                f"(an unbounded or non-numeric limit is refused)"
            )
            continue
        if math.isnan(value) or math.isinf(value):
            problems.append(f"bounds.{key} must be finite, got {value!r}")
            continue
        if value <= 0:
            problems.append(f"bounds.{key} must be greater than zero, got {value!r}")
            continue
        if key in _INTEGER_BOUNDS and float(value) != int(value):
            problems.append(f"bounds.{key} must be a whole number, got {value!r}")
            continue
        if value > ceiling:
            problems.append(f"bounds.{key}={value} exceeds the harness ceiling of {ceiling}")


def _check_artifacts(artifacts: Any, problems: list[str]) -> None:
    if not isinstance(artifacts, dict):
        return
    directory = artifacts.get("directory")
    if directory is None:
        return
    if not isinstance(directory, str) or not directory.strip():
        problems.append("artifacts.directory must be a non-empty path")
        return
    if ".." in Path(directory).parts:
        problems.append(f"artifacts.directory must not contain '..' segments, got {directory!r}")


def _check_secret_refs(secret_refs: Any, problems: list[str]) -> None:
    if secret_refs is None:
        return
    if not isinstance(secret_refs, dict):
        problems.append("secret_refs must be an object mapping a name to a reference")
        return
    for key, value in secret_refs.items():
        if not isinstance(value, str) or not _SECRET_REF.match(value):
            # Do not echo the value: if it is malformed it may be a literal secret.
            problems.append(
                f"secret_refs.{key} must be a reference of the form "
                f"'secretsmanager:<name>', 'ssm:<name>' or 'env:<VAR>'"
            )


def _check_scenarios(scenarios: Any, problems: list[str]) -> None:
    if scenarios is None:
        return
    if not isinstance(scenarios, list):
        problems.append("scenarios must be a list of scenario adapter ids")
        return
    for index, value in enumerate(scenarios):
        if not isinstance(value, str) or not _SCENARIO_ID.match(value):
            problems.append(f"scenarios[{index}] must be a scenario adapter id matching {_SCENARIO_ID.pattern}")


def resolve_secret_ref(reference: str) -> str:
    """Resolve one secret reference at run time.

    Called only on the live path, never during :func:`load_config`, so offline
    tests and `--preflight` never need AWS. The returned value is a credential:
    never log it, never write it to the inventory or to an artifact.
    """
    if not _SECRET_REF.match(reference):
        raise ConfigError([f"not a valid secret reference: {reference!r}"])
    scheme, _, name = reference.partition(":")

    if scheme == "env":
        import os

        try:
            return os.environ[name]
        except KeyError:
            raise ConfigError([f"secret reference {reference!r} is not set in the environment"]) from None

    import boto3  # imported lazily: the offline test path must not need boto3

    if scheme == "secretsmanager":
        client = boto3.client("secretsmanager")
        return client.get_secret_value(SecretId=name)["SecretString"]
    client = boto3.client("ssm")
    return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def resolve_secrets(config: QualificationConfig) -> dict[str, str]:
    """Resolve every secret reference in a config. Live path only."""
    return {name: resolve_secret_ref(ref) for name, ref in config.secret_refs.items()}


@dataclass(frozen=True)
class ResolvedConnection:
    """What the connection registry says a ``connection_ref`` actually is.

    This is the *authority* on a qualification's target. The config only
    *declares* an account; the registry is what establishes that the named
    connection exists, is still active, and is authorized for that account and
    org. Without it a config would be checked against itself.

    ``active`` is separate from resolving at all, so a revoked connection is
    refused with an accurate reason instead of being reported as unknown.
    """

    connection_ref: str
    account_id: str
    org: str
    active: bool = True
    detail: str | None = None


class ConnectionResolutionError(RuntimeError):
    """The registry could not answer whether a connection is authorized.

    Distinct from "resolved and says no": a registry that is unreachable or
    malformed leaves the target *unknown*, and unknown must refuse rather than
    fall through to the config's own claim.
    """


@runtime_checkable
class ConnectionResolver(Protocol):
    """The registry lookup the harness needs, injected rather than imported.

    Scenario adapters (#5157) supply the real implementation, exactly as they do
    for :class:`~tests.e2e.orchestration.fixtures.FixtureProvider`; the offline
    tests supply a protocol fixture. This slice deliberately defines only the
    contract — it does not implement or duplicate a production identity API.
    """

    def resolve_connection(self, connection_ref: str) -> ResolvedConnection | None:
        """Return the registered connection, or ``None`` if there is no such ref.

        Raise :class:`ConnectionResolutionError` for "could not determine" — do
        not return ``None``, which means "positively not registered".
        """


@dataclass(frozen=True)
class TargetVerification:
    """The result of checking the config's declared target against reality.

    ``verified`` is the only value that may precede a mutation. Everything else —
    an unreadable identity, a mismatched account, a missing connection — is a
    refusal, because acting on an unverified target is how a qualification
    provisions fixtures in somebody else's account.
    """

    verified: bool
    reason: str
    expected_account_id: str
    expected_org: str
    observed_account_id: str | None = None
    observed_arn: str | None = None
    resolved_connection: ResolvedConnection | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verified": self.verified,
            "reason": self.reason,
            "expected_account_id": self.expected_account_id,
            "expected_org": self.expected_org,
            "observed_account_id": self.observed_account_id,
            "observed_arn": self.observed_arn,
        }
        resolved = self.resolved_connection
        payload["resolved_connection"] = (
            None
            if resolved is None
            else {
                "connection_ref": resolved.connection_ref,
                "account_id": resolved.account_id,
                "org": resolved.org,
                "active": resolved.active,
            }
        )
        return payload


def verify_target(
    config: QualificationConfig,
    identity: dict[str, str] | None,
    identity_error: str | None = None,
    resolver: ConnectionResolver | None = None,
) -> TargetVerification:
    """Check the *registered* connection against the identity actually in effect.

    The comparison — not the report — is the point. ``preflight`` shows the
    result read-only; ``run``, ``resume`` and ``cleanup`` refuse to mutate unless
    :attr:`TargetVerification.verified` is true.

    Three things must agree before a mutation is allowed:

    1. ``connection_ref`` resolves, through ``resolver``, to a registered and
       still-active connection;
    2. that connection's authoritative account/org match what the config
       declares — a drift means the config is stale or was edited;
    3. the credentials in effect resolve to that same account.

    Checking only (3) against the config's own ``expected_account_id`` would be
    self-referential: both values come from the same file, so ``connection_ref``
    would be decorative and an unregistered ref could still provision fixtures.
    Resolution is therefore required, and an absent resolver refuses.

    The identity is passed in rather than read here so this stays network-free
    and testable; the caller does the one AWS read.
    """
    expected_account = config.expected_account_id
    expected_org = config.expected_org

    resolved, resolution_error = _resolve_connection(config.connection_ref, resolver)
    if resolution_error is not None:
        return TargetVerification(
            verified=False,
            reason=resolution_error,
            expected_account_id=expected_account,
            expected_org=expected_org,
            resolved_connection=resolved,
        )
    assert resolved is not None  # _resolve_connection returns one or the other

    # The registry is the authority, so a config that disagrees with it is
    # refused rather than silently preferred in either direction.
    if resolved.account_id != expected_account:
        return TargetVerification(
            verified=False,
            reason=(
                f"target mismatch: registered connection {config.connection_ref!r} is authorized for "
                f"account {resolved.account_id} but the config declares {expected_account}; "
                f"refusing a config that disagrees with the connection registry"
            ),
            expected_account_id=expected_account,
            expected_org=expected_org,
            resolved_connection=resolved,
        )
    if resolved.org != expected_org:
        return TargetVerification(
            verified=False,
            reason=(
                f"target mismatch: registered connection {config.connection_ref!r} belongs to org "
                f"{resolved.org!r} but the config declares {expected_org!r}"
            ),
            expected_account_id=expected_account,
            expected_org=expected_org,
            resolved_connection=resolved,
        )

    if identity_error or identity is None:
        return TargetVerification(
            verified=False,
            reason=(
                "the target account could not be verified, so no mutation is allowed: "
                f"{identity_error or 'no caller identity was available'}"
            ),
            expected_account_id=expected_account,
            expected_org=expected_org,
            resolved_connection=resolved,
        )

    observed_account = str(identity.get("account") or "")
    observed_arn = str(identity.get("arn") or "")

    if not observed_account:
        return TargetVerification(
            verified=False,
            reason="the caller identity carried no account id, so the target cannot be confirmed",
            expected_account_id=expected_account,
            expected_org=expected_org,
            observed_arn=observed_arn or None,
            resolved_connection=resolved,
        )

    # Compared against the REGISTRY's account, not the config's. They are equal
    # by the check above, but naming the authoritative source here is what makes
    # this a verification rather than a config agreeing with itself.
    if observed_account != resolved.account_id:
        return TargetVerification(
            verified=False,
            reason=(
                f"target mismatch: registered connection {config.connection_ref!r} is authorized for "
                f"account {resolved.account_id} but the active credentials resolve to {observed_account}; "
                f"refusing to mutate an account this qualification was not authorized against"
            ),
            expected_account_id=expected_account,
            expected_org=expected_org,
            observed_account_id=observed_account,
            observed_arn=observed_arn or None,
            resolved_connection=resolved,
        )

    # The repository the config authorizes must belong to the registered
    # connection's org; otherwise a config could point at one org's account
    # while acting on another org's repository.
    repository_org = config.repository.split("/", 1)[0]
    if repository_org != resolved.org:
        return TargetVerification(
            verified=False,
            reason=(
                f"target mismatch: connection.repository {config.repository!r} belongs to "
                f"{repository_org!r} but registered connection {config.connection_ref!r} "
                f"belongs to {resolved.org!r}"
            ),
            expected_account_id=expected_account,
            expected_org=expected_org,
            observed_account_id=observed_account,
            observed_arn=observed_arn or None,
            resolved_connection=resolved,
        )

    return TargetVerification(
        verified=True,
        reason=(
            f"verified: connection {config.connection_ref!r} is registered and active for account "
            f"{resolved.account_id}, the active credentials resolve to that account, and the "
            f"repository belongs to {resolved.org}"
        ),
        expected_account_id=expected_account,
        expected_org=expected_org,
        observed_account_id=observed_account,
        observed_arn=observed_arn,
        resolved_connection=resolved,
    )


def _resolve_connection(
    connection_ref: str,
    resolver: ConnectionResolver | None,
) -> tuple[ResolvedConnection | None, str | None]:
    """Resolve a connection ref through the registry, fail-closed.

    Returns ``(resolved, None)`` on success or ``(partial_or_none, reason)`` on
    refusal. Every branch that is not "registered and active" refuses: an absent
    resolver, a resolver that raises, a ref with no registration, a revoked
    connection, and a resolver returning something malformed. None of these are
    allowed to fall through to the config's own declaration, which is the bug
    this function exists to close.
    """
    if resolver is None:
        return None, (
            f"connection {connection_ref!r} could not be resolved: no connection resolver is "
            f"available, so there is no authority for this target and no mutation is allowed. "
            f"A resolver is supplied by the scenario adapters (#5157)."
        )

    try:
        resolved = resolver.resolve_connection(connection_ref)
    except ConnectionResolutionError as exc:
        return None, (
            f"connection {connection_ref!r} could not be resolved: {exc}; the target is unknown "
            f"and no mutation is allowed"
        )
    except Exception as exc:  # noqa: BLE001 - a registry is third-party code; any failure is "unknown"
        return None, (
            f"connection {connection_ref!r} could not be resolved: the connection registry failed "
            f"with {type(exc).__name__}: {exc}; the target is unknown and no mutation is allowed"
        )

    if resolved is None:
        return None, (
            f"connection {connection_ref!r} is not a registered connection; refusing to qualify "
            f"against a target that nothing authorizes"
        )
    if not isinstance(resolved, ResolvedConnection):
        return None, (
            f"connection {connection_ref!r} resolved to {type(resolved).__name__}, not a "
            f"ResolvedConnection; the target cannot be trusted and no mutation is allowed"
        )
    if resolved.connection_ref != connection_ref:
        # A resolver answering about a different connection is a wiring bug, and
        # trusting it would verify the wrong target entirely.
        return resolved, (
            f"the connection registry was asked about {connection_ref!r} but answered for "
            f"{resolved.connection_ref!r}; refusing an inconsistent resolution"
        )
    if not resolved.active:
        detail = f": {resolved.detail}" if resolved.detail else ""
        return resolved, (
            f"connection {connection_ref!r} is registered but not active{detail}; a revoked "
            f"connection authorizes nothing and no mutation is allowed"
        )
    if not _ACCOUNT_ID.match(resolved.account_id or ""):
        return resolved, (
            f"connection {connection_ref!r} resolved to {resolved.account_id!r}, which is not a "
            f"12-digit AWS account id; the target cannot be confirmed"
        )
    return resolved, None
