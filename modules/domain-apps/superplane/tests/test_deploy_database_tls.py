"""Every shipped artefact that opens a Superplane database connection verifies it — #5676 (A22).

## What was wrong

Verified transport was conditional on an optional variable. When
`SUPERPLANE_DATABASE_CA` was absent — which was the case for every manifest under
`src/superplane-api/deploy/`, none of which referenced the CA secret at all — the
code handed the driver no SSL setting and the driver applied its own default.

That default is the part worth stating precisely, because the intuitive reading is
wrong in both directions. asyncpg with no `ssl` argument resolves to
`sslmode=prefer`, which *does* attempt encryption (so "TLS was absent" is false),
accepts any certificate without checking the chain or the hostname, and silently
retries **in plaintext** if the encrypted attempt fails. So the wire format was
non-deterministic per connection and nothing recorded which outcome occurred.

## Why this test enumerates rather than lists

The issue's failure mode is "incomplete coverage of the fix": the database is
opened from the service, the migration Job and the seed Job, and hardening some
while leaving others means the finding reads as closed while an equivalent gap
stays open. So the artefacts are **discovered** by scanning the deploy directory
for anything that consumes `DATABASE_URL`, not written down in a list here. A Job
added later is picked up automatically and must satisfy the same requirement.

## The two supply mechanisms, and why they differ

`deployment.yaml` and `db-migrate-job.yaml` run our Python image (asyncpg) and
receive the bundle as `SUPERPLANE_DATABASE_CA`. `db-seed-job.yaml` runs `psql`,
which is libpq: it cannot take a CA from an environment variable at all, so it
gets the same `ca-pem` key mounted as a file plus `PGSSLMODE=verify-full`. One
secret key feeding both shapes, so a rotated bundle reaches every consumer.

## The one deliberate exception

`integration-test.yaml` connects to a throwaway in-cluster Postgres on an
`emptyDir` that serves no certificate — there is nothing to verify and no CA that
would make it verifiable. It therefore sets the explicit local-only exception.
That is asserted **positively** below (it must be present, named, and confined to
that file) rather than waved through, and paired with an assertion that the
exception never appears in a deployable manifest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
API_DEPLOY = MODULE_ROOT / "src" / "superplane-api" / "deploy"

CA_VARIABLE = "SUPERPLANE_DATABASE_CA"
LOCAL_EXCEPTION_VARIABLE = "SUPERPLANE_DATABASE_ALLOW_UNVERIFIED_LOCAL_TLS"
CA_SECRET_KEY = "ca-pem"

# The throwaway fixture. Named once, with its reason in the module docstring.
LOCAL_FIXTURE = "integration-test.yaml"

# `scheme://user:password@host` — an embedded credential's shape, matched without
# naming any particular value, so this file never reproduces the thing it forbids.
CREDENTIAL_URI = re.compile(r"[a-z0-9+]+://[A-Za-z0-9._%-]+:[^/@\s'\"]+@")


def documents(path: Path) -> list[dict]:
    return [
        d
        for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(d, dict)
    ]


def containers(doc: dict) -> list[dict]:
    """Every container in a Deployment/Job document, init containers included.

    Init containers matter here and are easy to miss: `integration-test.yaml`
    runs `alembic check` from one, which opens a real database connection.
    """
    spec = doc.get("spec", {})
    pod = spec.get("template", {}).get("spec", spec)
    return [*(pod.get("containers") or []), *(pod.get("initContainers") or [])]


def env_names(container: dict) -> set[str]:
    return {e.get("name") for e in (container.get("env") or [])}


def database_consumers() -> list[tuple[Path, dict, dict]]:
    """Discover every (file, document, container) that opens a database connection.

    Discovery, not a hardcoded list — that is the point of the test. A container
    counts as a consumer if it takes `DATABASE_URL`, which is the only way any of
    these reach the database.
    """
    found = []
    for path in sorted(API_DEPLOY.glob("*.yaml")):
        for doc in documents(path):
            for container in containers(doc):
                if "DATABASE_URL" in env_names(container):
                    found.append((path, doc, container))
    return found


def deployable_consumers() -> list[tuple[Path, dict, dict]]:
    return [c for c in database_consumers() if c[0].name != LOCAL_FIXTURE]


def describe(path: Path, doc: dict, container: dict) -> str:
    kind = doc.get("kind", "?")
    name = (doc.get("metadata") or {}).get("name", "?")
    return f"{path.name} {kind}/{name} container={container.get('name', '?')}"


def test_discovery_finds_the_known_connection_paths() -> None:
    """Guard the guard: if the scan silently found nothing, every check below
    would pass vacuously, which is the classic way an enumerating test rots."""
    consumers = database_consumers()
    assert len(consumers) >= 4, f"discovery found too few consumers: {consumers}"

    files = {path.name for path, _, _ in consumers}
    for expected in ("deployment.yaml", "db-migrate-job.yaml", "db-seed-job.yaml"):
        assert expected in files, (
            f"{expected} opens a database connection but discovery missed it; the "
            f"scan is broken, not the manifest"
        )


class TestEveryDeployableConsumerVerifiesTheServer:
    """The core acceptance: certificate AND hostname verification on every path."""

    def test_each_consumer_receives_trust_material(self) -> None:
        offenders = []
        for path, doc, container in deployable_consumers():
            names = env_names(container)
            # asyncpg path: the bundle arrives as an env var. libpq path: the
            # bundle is a mounted file and the mode is set explicitly, because
            # libpq cannot read a CA from an environment variable.
            asyncpg_supplied = CA_VARIABLE in names
            libpq_supplied = {"PGSSLROOTCERT", "PGSSLMODE"} <= names
            if not (asyncpg_supplied or libpq_supplied):
                offenders.append(describe(path, doc, container))
        assert not offenders, (
            f"these shipped containers open a database connection without trust "
            f"material, so the driver default (opportunistic, unauthenticated, "
            f"silent plaintext fallback) applies: {offenders}"
        )

    def test_trust_material_comes_from_a_secret_reference(self) -> None:
        """Never a literal PEM, and never `optional: true`.

        `optional: true` is the specific regression to block: it is the obvious
        "fix" for a pod that will not start, and it restores exactly the unset-CA
        state this story removes.
        """
        offenders = []
        for path, doc, container in deployable_consumers():
            for entry in container.get("env") or []:
                if entry.get("name") != CA_VARIABLE:
                    continue
                ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
                if not ref:
                    offenders.append(f"{describe(path, doc, container)}: inline value")
                elif ref.get("key") != CA_SECRET_KEY:
                    offenders.append(
                        f"{describe(path, doc, container)}: key={ref.get('key')!r}"
                    )
                elif ref.get("optional") is not False:
                    offenders.append(
                        f"{describe(path, doc, container)}: optional is not False"
                    )
        assert not offenders, (
            f"trust material must be a required secretKeyRef on key {CA_SECRET_KEY!r}: "
            f"{offenders}"
        )

    def test_libpq_consumers_verify_both_chain_and_hostname(self) -> None:
        """`require` encrypts while verifying neither; only `verify-full` does both.

        This is the distinction the finding turns on, so it is asserted rather
        than assumed from the presence of a TLS-looking setting.
        """
        for path, doc, container in deployable_consumers():
            for entry in container.get("env") or []:
                if entry.get("name") == "PGSSLMODE":
                    assert entry.get("value") == "verify-full", (
                        f"{describe(path, doc, container)}: PGSSLMODE="
                        f"{entry.get('value')!r}. Only 'verify-full' checks the "
                        f"certificate chain AND the hostname."
                    )

    def test_mounted_bundles_come_from_the_same_secret_key(self) -> None:
        """One source of truth, so a rotated bundle reaches every consumer."""
        for path in sorted(API_DEPLOY.glob("*.yaml")):
            if path.name == LOCAL_FIXTURE:
                continue
            for doc in documents(path):
                spec = doc.get("spec", {})
                pod = spec.get("template", {}).get("spec", spec)
                mounted_keys = {
                    item.get("key")
                    for volume in (pod.get("volumes") or [])
                    for item in ((volume.get("secret") or {}).get("items") or [])
                    if "ca" in (item.get("path") or "")
                }
                for key in mounted_keys:
                    assert key == CA_SECRET_KEY, (
                        f"{path.name}: CA mounted from key {key!r}; every consumer "
                        f"must read {CA_SECRET_KEY!r} so one rotation covers all"
                    )

    def test_no_deployable_consumer_takes_the_local_exception(self) -> None:
        """The escape hatch becoming the default is the named rollout risk."""
        offenders = [
            describe(path, doc, container)
            for path, doc, container in deployable_consumers()
            if LOCAL_EXCEPTION_VARIABLE in env_names(container)
        ]
        assert not offenders, (
            f"{LOCAL_EXCEPTION_VARIABLE} disables certificate and hostname "
            f"verification. It is for a local throwaway database only and must "
            f"never appear in a deployable manifest: {offenders}"
        )

    def test_connection_strings_stay_secret_references(self) -> None:
        """A22 keeps A04's property; the TLS work must not reintroduce a literal."""
        offenders = []
        for path, doc, container in deployable_consumers():
            for entry in container.get("env") or []:
                if entry.get("name") == "DATABASE_URL" and "valueFrom" not in entry:
                    offenders.append(describe(path, doc, container))
        assert not offenders, f"DATABASE_URL must be a secret reference: {offenders}"

    @pytest.mark.parametrize(
        "name",
        sorted(p.name for p in API_DEPLOY.glob("*.yaml") if p.name != LOCAL_FIXTURE),
    )
    def test_no_deployable_manifest_embeds_a_credential(self, name: str) -> None:
        found = CREDENTIAL_URI.findall((API_DEPLOY / name).read_text(encoding="utf-8"))
        assert not found, (
            f"{name} embeds a credential in a URI; use a secretKeyRef instead"
        )

    def test_the_credential_scan_detects_the_shape_it_targets(self) -> None:
        """An obviously-synthetic sample, so this file never carries a real one."""
        assert CREDENTIAL_URI.search(
            "postgresql+asyncpg://sample-user:sample-not-a-real-password@db.invalid:5432/x"
        )


class TestTheLocalExceptionIsExplicitAndConfined:
    """The exception must be visible and justified, not implied by omission."""

    def test_the_fixture_declares_the_exception_on_every_connection(self) -> None:
        """Including init containers — `alembic check` runs in one and connects."""
        offenders = []
        for path, doc, container in database_consumers():
            if path.name != LOCAL_FIXTURE:
                continue
            if LOCAL_EXCEPTION_VARIABLE not in env_names(container):
                offenders.append(describe(path, doc, container))
        assert not offenders, (
            f"the fixture connects to a certificate-less throwaway Postgres, so it "
            f"must set {LOCAL_EXCEPTION_VARIABLE} explicitly rather than rely on a "
            f"permissive default: {offenders}"
        )

    def test_the_exception_is_set_to_the_exact_literal(self) -> None:
        """The settings code matches "true" exactly; a mismatch here would be a
        Job that fails to start for a reason nothing in the manifest explains."""
        for path, doc, container in database_consumers():
            if path.name != LOCAL_FIXTURE:
                continue
            for entry in container.get("env") or []:
                if entry.get("name") == LOCAL_EXCEPTION_VARIABLE:
                    assert entry.get("value") == "true", (
                        f"{describe(path, doc, container)}: value="
                        f"{entry.get('value')!r}, expected 'true'"
                    )

    def test_the_fixture_keeps_its_local_apply_guard(self) -> None:
        """What makes the exception safe is that the fixture cannot reach a shared
        cluster. If that guard goes, the exception stops being justified."""
        applier = API_DEPLOY / "apply-integration-test-fixture.sh"
        text = applier.read_text(encoding="utf-8")
        assert "kind-*|k3d-*|minikube" in text
        assert "https://127.0.0.1:*" in text
