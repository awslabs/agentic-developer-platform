"""Real provider-install probe; run with /tmp limited to 64 MiB and a writable /work.

Uses only public provider downloads and fixture credentials, with backend disabled.
The candidate preserves its lock file and removes private scratch after
installing both pinned providers. Validation must accept the extracted packages;
successful initialization alone does not establish a usable provider lock.
"""

import hashlib
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from workspace_provisioning.process import WorkerProcesses
from workspace_provisioning.terraform import operation_directory, workspace_source


def main():
    credentials = SimpleNamespace(
        _expiry_time=datetime.now(UTC) + timedelta(hours=1),
        get_frozen_credentials=lambda: SimpleNamespace(
            access_key="fixture-access",
            secret_key="fixture-secret",
            token="fixture-token",
        ),
    )
    with tempfile.TemporaryDirectory(dir="/work") as directory:
        # Exercise the real hashed layout, whose path exceeds the Linux Unix
        # socket limit when used directly as the provider's TMPDIR.
        root = operation_directory(
            directory,
            "fixture-org",
            "fixture-workspace",
            "fixture-operation",
            create=True,
        )
        module = root / "module"
        module.mkdir()
        for name in ("versions.tf", ".terraform.lock.hcl"):
            shutil.copyfile(workspace_source() / name, module / name)
        lock = module / ".terraform.lock.hcl"
        before = hashlib.sha256(lock.read_bytes()).hexdigest()
        process = WorkerProcesses(
            binaries={"terraform": "/usr/local/bin/terraform"},
            directory=root,
            session=SimpleNamespace(get_credentials=lambda: credentials),
            region="us-east-1",
            verify=lambda: None,
        )
        result = process.run(
            [
                "terraform",
                "init",
                "-backend=false",
                "-input=false",
                "-lockfile=readonly",
                "-no-color",
            ],
            cwd=module,
            timeout=300,
        )
        print(result.stdout)
        print(result.stderr)
        assert result.returncode == 0, "provider installation failed"
        validation = process.run(
            ["terraform", "validate", "-json"], cwd=module, timeout=120
        )
        print(validation.stdout)
        print(validation.stderr)
        assert validation.returncode == 0, "installed provider validation failed"
        assert hashlib.sha256(lock.read_bytes()).hexdigest() == before
        assert {p.name for p in root.iterdir()} <= {
            "module",
            ".terraform.d",
        }, "command scratch was retained"
        print(
            "Pinned providers installed and validated; lock preserved; private scratch removed."
        )


if __name__ == "__main__":
    main()
