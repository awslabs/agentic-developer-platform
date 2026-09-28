"""The API's deployable manifests carry secret *references*, not credentials — #5683 (A04).

## What was wrong

`src/superplane-api/deploy/` shipped usable credentials inline: a connection URI with
the username and password in `config.env`, `db-migrate-job.yaml` and
`db-seed-job.yaml`, and a plaintext token signing key in a `kind: Secret` inside
`integration-test.yaml`. These are *appliable* files, so each value would come to exist
in a cluster, in shell history and in CI logs — and rotating it would mean rotating
every one of those copies rather than one secret.

## Why a test and not just the fix

Removing a literal is a one-line change to undo, and the obvious "fix" when a pod will
not start for want of a secret is to put the value back or to set `optional: true`.
These tests make either of those a failing check rather than a passing review, which is
the only thing that keeps the repair from decaying. They assert on the manifests a lane
would actually apply, not on documentation: a test that searched prose for the right
sentence would pass on a repo that says the right thing and does the wrong one.

## The one deliberate exception

`integration-test.yaml` is a throwaway local fixture — an `emptyDir` Postgres destroyed
with its pod — and it keeps trivial inline database credentials on purpose. Its dedicated
namespace, test-only resource identities, PostgreSQL ingress policy and local-cluster apply
guard are what make that safe. That exemption is **named and justified per-file** below
rather than applied by a wildcard, and it is paired with assertions that the file cannot
quietly become deployment-shaped or reintroduce a committed signing key.

No test here reproduces the removed placeholder or any other usable credential. The
assertions are on credential *shape*, for the reason the acceptance criterion gives:
proving a literal is gone by restating it would ship the thing being removed.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
API_DEPLOY = MODULE_ROOT / "src" / "superplane-api" / "deploy"
ROTATION_RUNBOOK = (
    MODULE_ROOT.parents[2]
    / "docs"
    / "runbooks"
    / "superplane-jwt-and-db-credential-rotation.md"
)

# `scheme://user:password@host` — the shape of an embedded credential, matched without
# naming any particular value. `[^/@\s'"]+` for the password so the match cannot run
# past the authority section into the rest of a URI or a surrounding quote.
CREDENTIAL_URI = re.compile(r"[a-z0-9+]+://[A-Za-z0-9._%-]+:[^/@\s'\"]+@")

# Files that a lane could apply to a shared cluster. These must hold zero credentials.
DEPLOYED_MANIFESTS = (
    "config.env",
    "db-migrate-job.yaml",
    "db-seed-job.yaml",
    "deployment.yaml",
)

# The throwaway fixture, exempted from the credential-URI scan with its reason stated.
LOCAL_FIXTURE = "integration-test.yaml"
LOCAL_APPLIER = "apply-integration-test-fixture.sh"
LOCAL_NAMESPACE = "superplane-integration-test"


def read(name: str) -> str:
    return (API_DEPLOY / name).read_text(encoding="utf-8")


def documents(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(read(name)) if isinstance(d, dict)]


def env_entries(doc: dict) -> list[dict]:
    """Every container `env` entry in a Deployment/Job document, across all containers."""
    spec = doc.get("spec", {})
    # A Job nests its pod template one level deeper than a Deployment does not — both
    # land on spec.template.spec, so this handles either without branching on `kind`.
    pod = spec.get("template", {}).get("spec", spec)
    entries = []
    for container in (*pod.get("containers", []), *pod.get("initContainers", [])):
        entries.extend(container.get("env", []) or [])
    return entries


def fake_kubectl(tmp_path: Path, monkeypatch, context: str, server: str) -> Path:
    command = tmp_path / "kubectl"
    command.write_text(
        """#!/bin/sh
if [ "$1" = "config" ] && [ "$2" = "current-context" ]; then
  printf '%s\n' "$FAKE_CONTEXT"
elif [ "$1" = "config" ] && [ "$2" = "view" ]; then
  printf '%s\n' "$FAKE_SERVER"
elif [ "$1" = "--context" ]; then
  printf '%s\n' "$*" >> "$APPLY_RECORD"
else
  exit 3
fi
""",
        encoding="utf-8",
    )
    command.chmod(0o755)
    record = tmp_path / "apply-record"
    monkeypatch.setenv("KUBECTL", str(command))
    monkeypatch.setenv("FAKE_CONTEXT", context)
    monkeypatch.setenv("FAKE_SERVER", server)
    monkeypatch.setenv("APPLY_RECORD", str(record))
    return record


class TestNoDeployedManifestCarriesACredential:
    """The value-level claim: nothing appliable holds material."""

    @pytest.mark.parametrize("name", DEPLOYED_MANIFESTS)
    def test_no_embedded_credential_uri(self, name: str) -> None:
        """No `scheme://user:password@host` anywhere in an appliable file.

        This is the assertion that fails if someone "fixes" a non-starting pod by
        pasting the URI back in. Verified to catch the real pre-fix content: the same
        pattern matches the URIs these three files carried before #5683.
        """
        found = CREDENTIAL_URI.findall(read(name))
        assert not found, (
            f"{name} embeds a credential in a URI. Deployed manifests must reference a "
            f"Secret (valueFrom.secretKeyRef) instead; see "
            f"docs/runbooks/superplane-jwt-and-db-credential-rotation.md"
        )

    def test_the_scan_detects_the_shape_it_exists_to_catch(self) -> None:
        """A guard that cannot detect its own defect is not a guard.

        Uses an obviously-synthetic string, not the removed value. Without this, a
        broken regex would make every assertion above pass vacuously — which is the
        failure mode of a scan-based test, and the reason it is checked explicitly.
        """
        assert CREDENTIAL_URI.search(
            "postgresql+asyncpg://exampleuser:examplepassword@db.invalid:5432/example"
        )
        # And does not fire on the reference form that replaced it.
        assert not CREDENTIAL_URI.search("postgresql+asyncpg://")

    def test_no_config_template_field_assigns_a_credential(self) -> None:
        """`config.env` is copied to `.env` *and* mounted as a ConfigMap.

        A ConfigMap is not a Secret — it is readable by anything in the namespace and
        appears in `kubectl get -o yaml` — so a value here is exposed twice over. Both
        credential-bearing keys must ship empty.
        """
        for line in read("config.env").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in ("DATABASE_URL", "JWT_SECRET_KEY"):
                assert value.strip() == "", (
                    f"{key.strip()} in config.env must ship empty; deployments inject it "
                    f"by reference and developers set it in an uncommitted .env"
                )


class TestTheReferencesFailClosed:
    """A reference that tolerates a missing secret is the old default wearing a hat.

    `optional: true` lets the pod start with the variable simply unset. For the signing
    key that used to mean falling back to the committed placeholder; now it would mean
    starting a pod that refuses every login. Either way the operator-visible symptom is
    an outage of unknown cause, so the pod must not start at all.
    """

    @pytest.mark.parametrize(
        "name", ("db-migrate-job.yaml", "db-seed-job.yaml", "deployment.yaml")
    )
    def test_every_secret_reference_is_required(self, name: str) -> None:
        checked = 0
        for doc in documents(name):
            for entry in env_entries(doc):
                ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
                if ref is None:
                    continue
                checked += 1
                assert ref.get("optional") is False, (
                    f"{name}: {entry['name']} uses optional={ref.get('optional')!r}. "
                    f"A missing secret must stop the pod, not start it unset."
                )
        assert checked, (
            f"{name} declares no secretKeyRef — the credential wiring is gone"
        )

    def test_the_api_deployment_takes_both_credentials_by_reference(self) -> None:
        """The database URL *and* the signing key, since the key is the new requirement.

        The API refuses to start without `JWT_SECRET_KEY` (see
        src/superplane-api/tests/test_jwt_secret_required.py), so this manifest has to
        supply it — otherwise the fail-closed check turns into a failed rollout.
        """
        referenced = {
            entry["name"]
            for doc in documents("deployment.yaml")
            if doc.get("kind") == "Deployment"
            for entry in env_entries(doc)
            if (entry.get("valueFrom") or {}).get("secretKeyRef")
        }
        assert {"DATABASE_URL", "JWT_SECRET_KEY"} <= referenced, (
            f"deployment.yaml must inject both by secretKeyRef; got {sorted(referenced)}"
        )

    def test_no_deployed_manifest_sets_a_signing_key_inline(self) -> None:
        """`JWT_SECRET_KEY` may be referenced, never given a literal `value`."""
        for name in DEPLOYED_MANIFESTS[1:]:  # config.env is checked as text above
            for doc in documents(name):
                for entry in env_entries(doc):
                    if entry.get("name") == "JWT_SECRET_KEY":
                        assert "value" not in entry, (
                            f"{name} assigns JWT_SECRET_KEY inline; it must come from a Secret"
                        )


class TestSigningKeyRunbook:
    """Both signing-key sources have safe update and no-overlap cutover procedures."""

    def test_installer_secret_update_is_bound_to_environment_region(self) -> None:
        runbook = ROTATION_RUNBOOK.read_text(encoding="utf-8")
        installer_section = runbook.split(
            "## Installer-backed signing-key source update", maxsplit=1
        )[1].split("## Initial remediation from the removed fallback", maxsplit=1)[0]

        assert (
            "export AWS_REGION='<value of top-level region from the environment file>'"
            in installer_section
        )
        for operation in ("get-secret-value", "put-secret-value"):
            command = rf"aws secretsmanager {operation} \\\n[ \t]+--profile \"\$AWS_PROFILE\" \\\n[ \t]+--region \"\$AWS_REGION\" \\"
            assert re.search(command, installer_section), (
                f"Secrets Manager {operation} must use the selected environment region"
            )

    def test_runbook_covers_the_exact_standalone_reference_and_cutover(self) -> None:
        deployment = next(
            doc
            for doc in documents("deployment.yaml")
            if doc.get("kind") == "Deployment"
        )
        signing_key = next(
            entry
            for entry in env_entries(deployment)
            if entry.get("name") == "JWT_SECRET_KEY"
        )
        reference = signing_key["valueFrom"]["secretKeyRef"]
        runbook = ROTATION_RUNBOOK.read_text(encoding="utf-8")

        assert reference["name"] in runbook
        assert reference["key"] in runbook
        assert "managed secret-sync" in runbook
        assert f"--from-file={reference['key']}=/dev/stdin" in runbook
        assert f"patch secret {reference['name']}" in runbook
        assert "scale deployment/superplane-api --replicas=0" in runbook
        assert "replicas: 2" in runbook
        assert "fresh sign-in succeeds" in runbook
        assert "rejected with 401" in runbook

    def test_disclosed_signing_key_has_no_rollback_path(self) -> None:
        runbook = ROTATION_RUNBOOK.read_text(encoding="utf-8")
        rollback = runbook.split("## Failure and rollback", maxsplit=1)[1].split(
            "## Evidence to record", maxsplit=1
        )[0]

        assert re.search(r"restoring the previous\s+key is prohibited", rollback)
        assert "keep API traffic stopped" in rollback
        assert "Availability does not justify re-enabling" in rollback
        assert "deliberate availability trade-off" not in rollback


class TestTheLocalFixtureStaysUnmistakablyLocal:
    """The exemption is bounded: isolated fixtures are fine, deployment-shaped ones are not."""

    def test_namespace_and_resource_names_cannot_collide_with_deployment(self) -> None:
        docs = documents(LOCAL_FIXTURE)
        namespace = next(doc for doc in docs if doc.get("kind") == "Namespace")
        assert namespace["metadata"]["name"] == LOCAL_NAMESPACE
        assert namespace["metadata"]["labels"]["superplane.aws/test-fixture"] == "true"

        namespaced = [doc for doc in docs if doc.get("kind") != "Namespace"]
        assert namespaced
        assert {doc["metadata"]["namespace"] for doc in namespaced} == {LOCAL_NAMESPACE}
        assert all(
            doc["metadata"]["name"].startswith("superplane-integration-test-")
            for doc in namespaced
        )

    def test_postgres_accepts_ingress_only_from_fixture_consumers(self) -> None:
        policies = [
            doc
            for doc in documents(LOCAL_FIXTURE)
            if doc.get("kind") == "NetworkPolicy"
        ]
        assert len(policies) == 1
        assert policies[0]["spec"] == {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "superplane-integration-test-postgres"
                }
            },
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/part-of": LOCAL_NAMESPACE
                                }
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 5432}],
                }
            ],
        }

    @pytest.mark.parametrize(
        ("context", "server"),
        [
            ("production-admin", "https://cluster.example.test"),
            ("kind-misleading-name", "https://cluster.example.test"),
        ],
    )
    def test_apply_helper_refuses_a_shared_cluster(
        self, tmp_path: Path, monkeypatch, context: str, server: str
    ) -> None:
        record = fake_kubectl(tmp_path, monkeypatch, context, server)
        result = subprocess.run(
            ["bash", str(API_DEPLOY / LOCAL_APPLIER)],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 2
        assert "Refusing integration fixture" in result.stderr
        assert not record.exists()

    def test_apply_helper_targets_an_explicit_local_cluster(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        context = "kind-superplane-integration-test"
        record = fake_kubectl(
            tmp_path,
            monkeypatch,
            context,
            "https://127.0.0.1:5443",
        )
        result = subprocess.run(
            ["bash", str(API_DEPLOY / LOCAL_APPLIER)],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        commands = record.read_text(encoding="utf-8").splitlines()
        assert commands[0].startswith(f"--context {context} apply -f")
        assert commands[0].endswith(LOCAL_FIXTURE)
        assert commands[1:] == [
            (
                f"--context {context} --namespace {LOCAL_NAMESPACE} wait "
                "--for=condition=complete --timeout=180s "
                "job/superplane-integration-test-secret-init"
            ),
            (
                f"--context {context} --namespace {LOCAL_NAMESPACE} wait "
                "--for=condition=complete --timeout=300s "
                "job/superplane-integration-test-db-migrate"
            ),
            (
                f"--context {context} --namespace {LOCAL_NAMESPACE} wait "
                "--for=condition=complete --timeout=180s "
                "job/superplane-integration-test-db-seed"
            ),
            (
                f"--context {context} --namespace {LOCAL_NAMESPACE} rollout status "
                "--timeout=300s deployment/superplane-integration-test-api"
            ),
        ]

    def test_database_consumers_share_fixture_database_and_secret(self) -> None:
        docs = documents(LOCAL_FIXTURE)
        consumers = {
            doc["metadata"]["name"]: doc
            for doc in docs
            if doc.get("kind") in {"Deployment", "Job"}
            and doc["metadata"]["name"]
            in {
                "superplane-integration-test-api",
                "superplane-integration-test-db-migrate",
                "superplane-integration-test-db-seed",
            }
        }
        assert set(consumers) == {
            "superplane-integration-test-api",
            "superplane-integration-test-db-migrate",
            "superplane-integration-test-db-seed",
        }

        database_host = (
            "superplane-integration-test-postgres."
            "superplane-integration-test.svc.cluster.local"
        )
        for name, consumer in consumers.items():
            assert consumer["metadata"]["namespace"] == LOCAL_NAMESPACE
            pod = consumer["spec"]["template"]
            assert pod["metadata"]["labels"]["app.kubernetes.io/part-of"] == (
                LOCAL_NAMESPACE
            )
            references = [
                entry["valueFrom"]["secretKeyRef"]
                for entry in env_entries(consumer)
                if entry.get("name") == "DATABASE_URL"
            ]
            assert references
            assert all(
                reference
                == {
                    "name": "superplane-integration-test-api-secrets",
                    "key": "DATABASE_URL",
                    "optional": False,
                }
                for reference in references
            ), f"{name} must use the fixture database Secret"

        manifest = read(LOCAL_FIXTURE)
        generated_url = next(
            line
            for line in manifest.splitlines()
            if "--from-literal=DATABASE_URL=" in line
        )
        assert database_host in generated_url
        assert all(
            database_host in str(consumer)
            or any(
                "alembic check" in str(container.get("command", ""))
                for container in consumer["spec"]["template"]["spec"].get(
                    "initContainers", []
                )
            )
            for consumer in consumers.values()
        )

    def test_it_declares_that_it_is_not_a_deployment(self) -> None:
        """Stated in the file, because the person about to apply it reads the file.

        Asserted on the header rather than on a filename convention: the hazard was that
        the file was indistinguishable from a production manifest, and a name alone did
        not distinguish it.
        """
        header = read(LOCAL_FIXTURE)[:2000].upper()
        assert "DO NOT APPLY TO A SHARED CLUSTER" in header
        assert "NOT A DEPLOYMENT" in header

    def test_it_commits_no_signing_key(self) -> None:
        """The part of this file that #5683 actually changed.

        It used to ship `stringData: JWT_SECRET_KEY: <literal>`. The key is now generated
        inside the fixture namespace, so no Secret document may carry committed data.
        """
        for doc in documents(LOCAL_FIXTURE):
            if doc.get("kind") != "Secret":
                continue
            for block in ("stringData", "data"):
                assert "JWT_SECRET_KEY" not in (doc.get(block) or {}), (
                    f"{LOCAL_FIXTURE} commits a signing key in {block}; generate it at apply time"
                )

    def test_secret_initialization_uses_executable_named_resource_rbac(self) -> None:
        """The generator may patch its one precreated Secret, never create Secrets.

        Kubernetes authorizes object creation against the collection endpoint, where
        there is no resource name for a `resourceNames` restriction to match. The
        bounded design therefore applies an empty named Secret first, grants named
        get/patch only, and makes both keys required so the API cannot start before
        the patch lands.
        """
        docs = documents(LOCAL_FIXTURE)

        def find(kind: str, name: str) -> tuple[int, dict]:
            return next(
                (index, doc)
                for index, doc in enumerate(docs)
                if doc.get("kind") == kind
                and (doc.get("metadata") or {}).get("name") == name
            )

        secret_index, secret = find("Secret", "superplane-integration-test-api-secrets")
        job_index, job = find("Job", "superplane-integration-test-secret-init")
        deployment_index, deployment = find(
            "Deployment", "superplane-integration-test-api"
        )
        assert secret_index < job_index < deployment_index
        assert secret.get("data") == {}
        assert "stringData" not in secret
        assert "ttlSecondsAfterFinished" not in job["spec"]

        _, role = find("Role", "superplane-integration-test-secret-init")
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "verbs": ["get", "patch"],
                "resourceNames": ["superplane-integration-test-api-secrets"],
            }
        ]

        command = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
        assert (
            "kubectl create secret generic superplane-integration-test-api-secrets"
            in command
        )
        assert "--dry-run=client" in command
        assert (
            "| kubectl patch secret superplane-integration-test-api-secrets" in command
        )
        assert "--patch-file=/dev/stdin" in command

        references = {
            entry["name"]: entry["valueFrom"]["secretKeyRef"]
            for entry in env_entries(deployment)
            if "secretKeyRef" in (entry.get("valueFrom") or {})
        }
        assert references == {
            "JWT_SECRET_KEY": {
                "name": "superplane-integration-test-api-secrets",
                "key": "JWT_SECRET_KEY",
                "optional": False,
            },
            "DATABASE_URL": {
                "name": "superplane-integration-test-api-secrets",
                "key": "DATABASE_URL",
                "optional": False,
            },
        }

    def test_its_database_is_disposable(self) -> None:
        """What justifies the exemption, asserted rather than assumed.

        The inline credentials are safe *because* the database is destroyed with its pod.
        If this ever gained a PersistentVolumeClaim it would be holding real data with a
        committed password, and the exemption above would no longer be honest.
        """
        text = read(LOCAL_FIXTURE)
        assert "emptyDir" in text
        assert "PersistentVolumeClaim" not in text, (
            "the fixture gained persistent storage; its inline credentials are only "
            "defensible while the database is throwaway"
        )
