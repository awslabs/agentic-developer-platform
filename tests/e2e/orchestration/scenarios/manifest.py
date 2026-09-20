"""Reviewed environment inputs; acceptance tests and criterion ids stay in code."""

import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tests.e2e.orchestration.config import _EMBEDDED_SECRET_VALUES
from tests.e2e.orchestration.report import Strict
from .definitions import DEFINITION_HASH, digest

ROOT = Path(__file__).resolve().parents[4]


class RuntimeTarget(Strict):
    cluster: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    deployment: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    container: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    ecr_repository: str = Field(pattern=r"^[a-z0-9][a-z0-9/_-]{0,127}$")


class Manifest(Strict):
    schema_version: int = 1
    definition_hash: str
    api_origin: str
    execution_policy: dict
    evaluation: dict
    # The only human graph gate in the primary scenario, before story two.
    planned_gates: list[str]
    runtime: dict[str, RuntimeTarget]
    required_checks: list[str] = Field(min_length=1, max_length=32)
    deployment_workflows: list[str] = Field(min_length=1, max_length=8)
    poll_seconds: int = Field(default=10, ge=1, le=30)
    native_faults: bool = False

    @model_validator(mode="after")
    def fixed_contract(self):
        if self.schema_version != 1 or self.definition_hash != DEFINITION_HASH:
            raise ValueError(
                "manifest does not pin the current reviewed criterion definitions"
            )
        parsed = urlsplit(self.api_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("api_origin must be an HTTPS origin without credentials")
        if self.planned_gates != ["release", "refusal"] or set(self.runtime) != {
            "engine",
            "worker",
        }:
            raise ValueError("manifest must pin release gate and engine/worker targets")
        if any(
            k in self.execution_policy
            for k in ("policy_id", "policy_hash", "principal_id")
        ):
            raise ValueError("policy provenance must be server stamped")
        if any(
            not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", p)
            for p in self.deployment_workflows
        ):
            raise ValueError("deployment workflow must be a repository workflow path")
        return self


def load_manifest(config):
    if not isinstance(config.scenario_manifest, str):
        raise ValueError("Q2 requires a reviewed scenario_manifest path")
    relative = Path(config.scenario_manifest)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("scenario_manifest must be repository relative")
    root = ROOT
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("manifest escapes reviewed checkout")
    raw = json.loads(path.read_text())
    if any(pattern.search(json.dumps(raw)) for pattern, _ in _EMBEDDED_SECRET_VALUES):
        raise ValueError("embedded credential in scenario manifest")
    manifest = Manifest.model_validate(raw)
    policy = manifest.execution_policy
    if (
        policy.get("org_id") != config.org_ref
        or policy.get("repository_ids") != [config.repository]
        or policy.get("environment_connection_ids") != [config.connection_ref]
    ):
        raise ValueError("manifest policy scope differs from qualification config")
    limits = policy.get("limits", {})
    if (
        not 0 < float(limits.get("max_spend_usd", 0)) <= config.max_usd
        or not 0
        < limits.get("max_wall_clock_seconds", 0)
        <= config.max_duration_seconds
        or not 0 < limits.get("max_attempts_per_node", 0) <= config.max_runs
    ):
        raise ValueError(
            "accepted policy must enforce qualification spend/time/attempt bounds"
        )
    if any(not re.fullmatch(r"[0-9a-f]{40}", v) for v in config.versions.values()):
        raise ValueError("live Q2 requires exact source SHAs for all three versions")
    return manifest, digest(raw)


def verify_checkout(config):
    """Refuse untracked or modified code/config before resolving live credentials."""
    package = ROOT / "tests/e2e/orchestration"
    paths = list(package.rglob("*.py"))
    code_paths = set(paths)
    paths.append(ROOT / config.scenario_manifest)
    if config.source is None:
        raise ValueError("live qualification needs its committed config source")
    paths.append(Path(config.source).resolve())
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("qualification source escapes reviewed checkout")
        relative = str(path.resolve().relative_to(ROOT))
        try:
            revision = config.versions["harness"] if path in code_paths else "HEAD"
            committed = subprocess.check_output(
                ["git", "show", revision + ":" + relative],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            raise ValueError(
                "live qualification source/config is not committed"
            ) from None
        if committed != path.read_bytes():
            raise ValueError(
                "live qualification source/config differs from the committed revision"
            )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    expected = subprocess.check_output(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            config.versions["harness"],
            "--",
            "tests/e2e/orchestration",
        ],
        cwd=ROOT,
        text=True,
    ).splitlines()
    if {p for p in expected if p.endswith(".py")} != {
        str(p.relative_to(ROOT)) for p in code_paths
    }:
        raise ValueError("executing harness file set differs from approved source")
    return head
