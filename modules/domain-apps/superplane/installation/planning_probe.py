"""Read-only inspection of retained initial-planning state.

This probe is evidence, never permission to retry. The recovery owner must also
verify the original volume, qualified source, expired grants/sessions and exact
admitted phase. In particular, a missing directory alone proves no cloud fact.
"""

import copy
import hashlib
import re
from pathlib import Path


def probe_program(
    org_id, workspace_id, operation_id, *, completion=False, partial_source=None
):
    for value in (org_id, workspace_id, operation_id):
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9-]{0,127}", value
        ):
            raise ValueError("explicit operation identity required")
    identity = [org_id, workspace_id, operation_id]
    if partial_source is not None:
        if completion:
            raise ValueError("partial apply is distinct from saved-plan completion")
        from .partial_apply_probe import program

        return program(identity, partial_source)
    if completion:
        from .saved_plan_probe import saved_plan_program

        return saved_plan_program(identity)
    boundary_source = (
        Path(__file__).resolve().parents[1]
        / "workspace_provisioning/planning_boundary.py"
    ).read_text()
    socket_source = (
        Path(__file__).resolve().parents[1]
        / "workspace_provisioning/provider_socket_boundary.py"
    ).read_text()
    return (
        boundary_source
        + "\n"
        + socket_source
        + "\n"
        + """import hashlib, importlib.util, json, os, stat
from pathlib import Path
identity = """
        + repr(identity)
        + """
root = Path('/run/state')
assert not root.is_symlink() and root.is_dir()
assert os.statvfs(root).f_flag & os.ST_RDONLY, 'state must be mounted read-only'
private = root / ('superplane-worker-' + str(os.getuid()))
assert not private.is_symlink()
if private.exists():
    info = private.stat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
    assert not info.st_mode & 0o007
    assert info.st_gid == 65532 or not info.st_mode & 0o070
key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
target = private / key
assert not target.is_symlink()
entries = snapshot(target) if target.exists() else None
documents = inspection_documents(target) if target.exists() else None
lock_refusal = provider_lock_refusal(target) if target.exists() else None
socket_refusal = provider_socket_refusal(target) if target.exists() else None
package = Path(importlib.util.find_spec('workspace_provisioning').origin).parent
sources = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(package.rglob('*.py')) if 'tests' not in p.parts}
print(json.dumps({'version': 1, 'identity': identity, 'read_only': True,
                 'private_directory_exists': private.exists(), 'retained_entries': entries,
                 'initialization_documents': documents,
                 'provider_lock_refusal': lock_refusal,
                 'provider_socket_refusal': socket_refusal,
                 'operation_directory_exists': target.exists(),
                 'operation_directory_name': key, 'runtime_sources': sources}, sort_keys=True))
"""
    )


def probe_job(
    template,
    *,
    namespace,
    claim_name,
    identity,
    run_id,
    completion=False,
    partial_source=None,
):
    """Reuse only scheduling/image settings; no worker authority or DB mounts."""
    if not re.fullmatch(r"[a-f0-9]{16}", run_id):
        raise ValueError("installation run identity required")
    image = template["containers"][0]["image"]
    if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("qualified digest image required")
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "superplane-planning-probe-" + run_id,
            "namespace": namespace,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 300,
            "template": {
                "spec": {
                    "serviceAccountName": "superplane-state-probe",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "nodeSelector": copy.deepcopy(template["nodeSelector"]),
                    "securityContext": {
                        "supplementalGroups": [65532],
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "planning-probe",
                            "image": image,
                            "command": [
                                "/opt/executor/bin/python",
                                "-B",
                                "-c",
                                probe_program(
                                    *identity,
                                    completion=completion,
                                    partial_source=partial_source,
                                ),
                            ],
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 65531,
                                "runAsGroup": 65532,
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                # Native provider startup plus Terraform and
                                # the collector can exceed 128 MiB before the
                                # expected socket refusal is emitted.
                                "requests": {"cpu": "50m", "memory": "256Mi"},
                                "limits": {"cpu": "250m", "memory": "512Mi"},
                            },
                            "volumeMounts": [
                                {
                                    "name": "state",
                                    "mountPath": "/run/state",
                                    "readOnly": True,
                                },
                                {"name": "temporary", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "state",
                            "persistentVolumeClaim": {
                                "claimName": claim_name,
                            },
                        },
                        {
                            "name": "temporary",
                            "emptyDir": {
                                "medium": "Memory",
                                "sizeLimit": "16Mi" if completion else "1Mi",
                            },
                        },
                    ],
                },
            },
        },
    }


def verify_report(report, identity, source_root):
    expected = {
        str(path.relative_to(source_root)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(source_root.rglob("*.py"))
        if "tests" not in path.parts
    }
    if (
        report.get("version") != 1
        or report.get("identity") != list(identity)
        or report.get("read_only") is not True
        or {
            key: value
            for key, value in report.get("runtime_sources", {}).items()
            if not key.startswith("_data/")
        }
        != expected
    ):
        raise ValueError("planning probe differs from reviewed source/identity")
    return report
