"""Observe ordinary Actions deployments and produce the shared-release inputs.

No deployment is performed here. Gateway/frontend publish through their existing
workflows; this adapter verifies their signed receipts against the live runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from functools import partial
from pathlib import Path
from urllib.request import Request, urlopen

from tests.e2e.product_onboarding.product_completion_release import COMPONENTS, _write

SOURCE_ROOT = Path(__file__).resolve().parents[3]

PATHS = {
    "gateway-backend": (
        "contracts",
        "modules/gateway/src",
        "modules/gateway/deployment_evidence",
        "modules/gateway/cli",
        "modules/gateway/alembic",
        "modules/gateway/pricing_policy",
        "modules/gateway/Dockerfile",
        "modules/gateway/pyproject.toml",
        "modules/gateway/alembic.ini",
        "modules/gateway/security",
        "modules/gateway/.dockerignore",
        "modules/gateway/scripts/stage-contracts.sh",
        "codebuild/bs-gateway-build.yml",
        "modules/insights/projection.py",
        "modules/insights/results.py",
        "modules/harness/jobs/harness_jobs",
        "modules/agent-factory/rules/personas",
        "modules/tools/adapters/runtime_evidence.py",
    ),
    "gateway-frontend": (
        "modules/gateway/frontend/src",
        "modules/gateway/frontend/package.json",
        "modules/gateway/frontend/package-lock.json",
        "modules/gateway/frontend/vite.config.ts",
        "modules/gateway/frontend/index.html",
        "modules/gateway/frontend/public",
        "modules/domain-apps/superplane/ui",
        "modules/gateway/frontend/tsconfig.json",
        "modules/gateway/frontend/tsconfig.node.json",
        "modules/gateway/frontend/tailwind.config.js",
        "modules/gateway/frontend/postcss.config.js",
        ".github/workflows/gateway-frontend-deploy.yml",
    ),
    "installed-cli": ("modules/gateway/cli",),
    "schema": ("modules/gateway/alembic",),
}


def fingerprint(revision, component):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Component source must be an immutable commit")
    try:
        tree = subprocess.check_output(
            ["git", "ls-tree", "-rz", revision, "--", *PATHS[component]],
            cwd=SOURCE_ROOT, stderr=subprocess.PIPE, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as error:
        raise ValueError("Component source cannot be verified from the selected checkout") from error
    if not tree:
        raise ValueError("Component source is absent")
    return hashlib.sha256(tree).hexdigest()


def compatible_release(release, source, component, *, compare=None):
    compare = compare or fingerprint
    expected = compare(source, component)
    if (
        release.component != component
        or compare(release.source_revision, component) != expected
    ):
        raise ValueError(f"{component} has not deployed the selected source")
    return expected


def compatible_source(actual, expected, component):
    """Retain deployment provenance while allowing commits with identical runtime inputs."""
    if not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
               for value in (actual, expected)):
        raise ValueError("Component revisions must be immutable commits")
    if actual == expected:
        return
    if fingerprint(actual, component) != fingerprint(expected, component):
        raise ValueError(f"{component} source differs from the selected release")


def github(path):
    request = Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("Provider response exceeds observation bounds")
        return json.loads(body)


def latest_release(
    component, *, source, account, region, environment, repository, repository_id, store
):
    from src.orchestration.deployment_runtime_contract import ReleaseArtifact

    workflow = (
        "gateway-deploy.yml"
        if component == "gateway-backend"
        else "gateway-frontend-deploy.yml"
    )
    runs = github(
        f"/repos/{repository}/actions/workflows/{workflow}/runs?branch=main&status=success&per_page=50"
    )["workflow_runs"]
    for run in runs:
        if (
            run.get("path") != ".github/workflows/" + workflow
            or run.get("repository", {}).get("id") != repository_id
            or run.get("head_branch") != "main"
            or run.get("conclusion") != "success"
        ):
            continue
        stored = store.read(
            account=account,
            environment=environment,
            region=region,
            repository=repository,
            repository_id=repository_id,
            run_id=run["id"],
            attempt=run["run_attempt"],
            kind="release",
            name=component,
            workflow_path=run["path"],
            workflow_revision=run["head_sha"],
        )
        if stored is None:
            continue
        payload, digest, reference = stored
        release = ReleaseArtifact.model_validate_json(payload)
        expected = {
            "run_id": run["id"],
            "run_attempt": run["run_attempt"],
            "repository_id": repository_id,
            "workflow_path": run["path"],
            "workflow_revision": run["head_sha"],
            "account_id": account,
            "region": region,
        }
        if any(getattr(release, name) != value for name, value in expected.items()):
            raise ValueError("Signed component receipt differs from its workflow")
        try:
            compatible_release(release, source, component)
        except ValueError:
            continue
        return release, digest, reference
    raise ValueError(
        f"No successful compatible {workflow} release is available; finish that deployment first"
    )


def install_cli(root, origin, source):
    """Use the shipped installer in isolated configuration, then hash its files."""
    binary = root / "cli"
    config = root / "cli-config"
    # Run the reviewed installer without adjacent CLI files so it must fetch the
    # artifacts served by this deployment, then compare every installed byte.
    installer = root / "install.sh"
    shutil.copyfile("modules/gateway/cli/install.sh", installer)
    subprocess.run(
        [
            "bash",
            str(installer),
            "--gateway-url",
            origin,
            "--prefix",
            str(binary),
            "--no-path-edit",
        ],
        env={**os.environ, "BG_CONFIG_DIR": str(config)},
        check=True,
        timeout=180,
        capture_output=True,
    )
    hashes = {}
    inventory = re.search(r'^CLI_FILES="([^"]+)"$', installer.read_text(), re.MULTILINE)
    if inventory is None:
        raise ValueError("Reviewed CLI installer has no file inventory")
    for name in inventory.group(1).split():
        path = Path("modules/gateway/cli") / name
        installed = binary / name
        if not installed.is_file():
            raise ValueError(f"Installed CLI file is missing: {name}")
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        actual = hashlib.sha256(installed.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(
                f"Installed CLI file differs from the selected source: {name}"
            )
        hashes[name] = actual
    if not {"adp", "adp-product.py", "adp-onboard.py", "adp_common.py"} <= set(hashes):
        raise ValueError("Installed Product CLI inventory is incomplete")
    commands = []
    for args in (["--version"], ["product", "--help"], ["onboard", "--help"]):
        completed = subprocess.run(
            [str(binary / "adp"), *args],
            env={**os.environ, "BG_CONFIG_DIR": str(config)},
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        commands.append(
            {
                "args": args,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
            }
        )
    return {
        "source_revision": source,
        "files": hashes,
        "commands": commands,
        "sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
    }


def observe(root, environment=os.environ):
    import boto3
    from deployment_evidence.store import EvidenceStore
    from src.orchestration.deployment_runtime_reader import (
        KubernetesRuntime,
        PilotRuntimeReader,
    )

    root = Path(root)
    if (
        not root.is_absolute()
        or root.is_symlink()
        or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o777 != 0o700
    ):
        raise ValueError("Shared release needs a private attempt directory")
    source = environment["ADP_SOURCE_REVISION"]
    repository = environment["GITHUB_REPOSITORY"]
    region = environment["AWS_REGION"]
    account = environment["ACCOUNT_ID"]
    session = boto3.Session(region_name=region)
    if session.client("sts").get_caller_identity()["Account"] != account:
        raise ValueError("Observer credentials differ from the authorized account")
    store = EvidenceStore(s3=session.client("s3"))
    reader = PilotRuntimeReader(
        kubernetes=partial(
            KubernetesRuntime,
            proof_invoke_url=environment["ADP_RUNTIME_PROOF_INVOKE_URL"],
        )
    )
    desired, current, receipts = {}, {}, {}
    evidence = root / "evidence"
    evidence.mkdir(mode=0o700)
    backend = None
    for component in ("gateway-backend", "gateway-frontend"):
        release, digest, reference = latest_release(
            component,
            source=source,
            account=account,
            region=region,
            environment="dev",
            repository=repository,
            repository_id=int(environment["GITHUB_REPOSITORY_ID"]),
            store=store,
        )
        matching = compatible_release(release, source, component)
        if component == "gateway-backend":
            cluster, namespace = release.resource_id.split("/", 1)
            description = session.client("eks").describe_cluster(name=cluster)[
                "cluster"
            ]
            runtime = reader.inspect(
                session,
                description,
                namespace,
                artifact=release,
                artifact_hash=digest,
                evidence_ref=reference,
                environment="dev",
            ).model_dump(mode="json")
            if (
                runtime.get("product_profile_enabled") is not True
                or runtime.get("product_onboarding_enabled") is not True
            ):
                raise ValueError("Product admissions are not enabled")
            artifact_hash = release.image_digest.removeprefix("sha256:")
            backend = release, runtime
        else:
            count = reader.frontend(session, release, "dev")
            runtime = {"asset_count": count, "assets": release.assets}
            artifact_hash = hashlib.sha256(
                json.dumps(release.assets, sort_keys=True).encode()
            ).hexdigest()
        _write(
            {
                "release": release.model_dump(mode="json"),
                "release_ref": reference,
                "runtime": runtime,
            },
            evidence / (component + ".json"),
        )
        desired[component] = {
            "sha256": artifact_hash,
            "fingerprint": matching,
            "source_revision": source,
        }
        current[component] = {
            **desired[component],
            "source_revision": release.source_revision,
            "verified": True,
            "evidence_ref": f"evidence/{component}.json",
        }
    release, runtime = backend
    if runtime["migration_head"] != release.migration_head:
        raise ValueError("Live schema differs from the signed deployment")
    schema_hash = hashlib.sha256(release.migration_head.encode()).hexdigest()
    schema_fingerprint = fingerprint(source, "schema")
    if schema_fingerprint != fingerprint(release.source_revision, "schema"):
        raise ValueError("Selected migrations have not deployed")
    _write(
        {
            "migration_head": runtime["migration_head"],
            "pod_uids": runtime["pod_uids"],
            "release_ref": current["gateway-backend"]["evidence_ref"],
        },
        evidence / "schema.json",
    )
    desired["schema"] = {
        "sha256": schema_hash,
        "fingerprint": schema_fingerprint,
        "source_revision": source,
    }
    current["schema"] = {
        **desired["schema"],
        "source_revision": release.source_revision,
        "verified": True,
        "evidence_ref": "evidence/schema.json",
    }
    installed = install_cli(root, environment["ADP_GATEWAY_BASE_URL"], source)
    _write(installed, evidence / "installed-cli.json")
    desired["installed-cli"] = {
        "sha256": installed["sha256"],
        "fingerprint": fingerprint(source, "installed-cli"),
        "source_revision": source,
    }
    current["installed-cli"] = {
        **desired["installed-cli"],
        "verified": True,
        "evidence_ref": "evidence/installed-cli.json",
    }
    for component in COMPONENTS:
        target, observed = desired[component], current[component]
        receipts[component] = {
            "desired_sha256": target["sha256"],
            "observed_sha256": observed["sha256"],
            "source_fingerprint": target["fingerprint"],
            "observed_fingerprint": observed["fingerprint"],
            "observed_source_revision": observed["source_revision"],
            "evidence_ref": observed["evidence_ref"],
            "verified": True,
            "action": "reused",
        }
    for name, document in (
        ("desired", desired),
        ("current", current),
        ("component-receipts", receipts),
    ):
        _write(document, root / (name + ".json"))


if __name__ == "__main__":
    observe(Path(os.environ["RUNNER_TEMP"]) / "product-profile-release")
