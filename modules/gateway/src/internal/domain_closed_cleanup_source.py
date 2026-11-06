"""Portable source binding and build-time native cleanup import check."""

import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def native_paths():
    import sys

    here = Path(__file__).resolve()
    staged = here.parents[2] / "contracts"
    repo = here.parents[4] if len(here.parents) > 4 else None
    if repo and (repo / "modules/domain-apps/superplane/workspace_bootstrap/superplane_bootstrap").is_dir():
        paths = [
            repo / "modules/domain-apps/superplane/workspace_bootstrap",
            repo / "modules/domain-apps/superplane/contracts",
            repo / "modules/harness/jobs",
        ]
    else:
        paths = [staged / "superplane-bootstrap", staged / "superplane-contracts", staged / "harness-jobs"]
    for path, package in zip(paths, ("superplane_bootstrap", "superplane_contracts", "harness_jobs")):
        if not (path / package / "__init__.py").is_file():
            raise RuntimeError("closed cleanup package missing")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return paths


def source_digest():
    paths = native_paths()
    root = Path(__file__).resolve().parents[1]
    # Cover the command, native implementation and the shared authorization code.
    # Keys are portable between repository and staged image layouts.
    sources = {}
    for label, directory in [
        ("native", paths[0] / "superplane_bootstrap"),
        ("contracts", paths[1] / "superplane_contracts"),
        ("harness", paths[2] / "harness_jobs"),
        ("internal", root / "internal"),
        ("shared", root / "shared"),
    ]:
        for path in sorted(directory.rglob("*.py")):
            sources[label + "/" + str(path.relative_to(directory))] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    return digest(sources)


def selfcheck():
    native_paths()
    from harness_jobs import settled_bootstrap_repeat
    from superplane_bootstrap.closed_cleanup_access import VerifierAccess
    from superplane_bootstrap.closed_cleanup_kube import ScopedKubernetes
    from superplane_bootstrap.closed_cleanup_run import run_closed_cleanup
    from superplane_contracts.provisioning import OperationBinding

    assert all((run_closed_cleanup, VerifierAccess, ScopedKubernetes, OperationBinding, settled_bootstrap_repeat.validate))
    return source_digest()


if __name__ == "__main__":
    print(json.dumps({"service_source_sha256": selfcheck()}))
