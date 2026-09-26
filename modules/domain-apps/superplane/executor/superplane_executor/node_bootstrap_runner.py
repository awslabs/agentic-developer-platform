"""Fixed native bootstrap: bounded preflight retry, then exactly one latched init."""

import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

if __package__ in {None, ""}:
    # -I removes script-directory imports. The fixed root-owned launch path is
    # the only additional import root; the full tree is checked before effects.
    sys.path.insert(0, "/opt/superplane/node-runtime")
    import node_runner as runner
else:
    from . import node_runner as runner

STATE_ROOT = Path("/var/lib/superplane/node-bootstrap")
CONFIG_ROOT = Path("/etc/eks/superplane-native")
PROC_ROOT = Path("/proc")


def verify_bootstrap_exclusive():
    for name in ("nodeadm-config.service", "nodeadm-run.service"):
        state = runner.fixed_command(
            ["/usr/bin/systemctl", "show", name, "--property=LoadState", "--value"]
        ).strip()
        active = runner.fixed_command(
            ["/usr/bin/systemctl", "show", name, "--property=ActiveState", "--value"]
        ).strip()
        if state != "masked" or active != "inactive":
            raise runner.RunnerRefused("competing automatic bootstrap is not masked")
    # These are upstream run-start/cache/config indicators, never success proof.
    # No deletion or takeover of a partly bootstrapped machine is permitted.
    if any(
        os.path.lexists(p)
        for p in (
            "/run/nodeadm/init",
            "/run/eks/nodeadm/config.json",
            "/etc/eks/kubelet/environment",
            "/etc/kubernetes/kubelet/config.json",
            "/etc/kubernetes/kubelet/config.json.d",
            "/var/lib/kubelet/pki",
            "/var/lib/kubelet/kubeconfig",
        )
    ):
        raise runner.RunnerRefused("preexisting bootstrap state")
    dropins = Path("/etc/eks/nodeadm.d")
    if dropins.exists() and (dropins.is_symlink() or any(dropins.iterdir())):
        raise runner.RunnerRefused("ambient NodeConfig source refused")
    jobs = runner.fixed_command(
        ["/usr/bin/systemctl", "list-jobs", "--no-legend", "--no-pager"]
    )
    if len(jobs) > 8192 or any(
        any(
            unit in line.split()
            for unit in (
                "nodeadm-config.service",
                "nodeadm-run.service",
                "nodeadm-boot-hook.service",
                "containerd.service",
                "kubelet.service",
            )
        )
        for line in jobs.splitlines()
    ):
        raise runner.RunnerRefused("competing native bootstrap job is queued")
    for directory in PROC_ROOT.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            executable = (directory / "exe").readlink()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if executable.name in {"nodeadm", "nodeadm-internal"}:
            raise runner.RunnerRefused("another nodeadm invocation is running")


def _directory(path):
    # Prepared parent directories are checked rather than followed through
    # symlinks. Only the fixed application state/config child may be created.
    runner.secure_path(path.parent, directory=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    runner.secure_path(path, directory=True)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _exclusive_file(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        # A partial/empty file is still a permanent refusal. Never unlink it.
        raise


def latch_init(contract):
    _directory(STATE_ROOT)
    path = STATE_ROOT / (contract["instance_id"] + ".started")
    try:
        _exclusive_file(
            path,
            runner.canonical(
                {
                    "version": 1,
                    "contract_sha256": runner.contract_digest(contract),
                    "operation_id": contract["operation_id"],
                    "attempt_id": contract["attempt_id"],
                    "fence_token": contract["fence_token"],
                    "runtime_deadline": contract["runtime_deadline"],
                }
            ).encode(),
        )
    except FileExistsError as exc:
        raise runner.RunnerRefused("init was already started or uncertain") from exc


def execute(contract):
    runner.verify_installation(contract, __file__)
    runner.verify_native_runtime(contract)
    runner.validate_node_config(contract["node_config"], contract)
    preflight_end = min(
        time.monotonic() + 60,
        time.monotonic() + runner.deadline(contract) - time.time(),
    )
    for attempt in range(3):
        if time.monotonic() >= preflight_end:
            raise runner.RunnerRefused("native preflight exhausted")
        try:
            runner.verify_instance(contract)
            verify_bootstrap_exclusive()
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2 or time.monotonic() + 2 >= preflight_end:
                raise runner.RunnerRefused("native preflight unavailable") from None
            time.sleep(2)
    runner.verify_instance(contract)
    latch_init(contract)
    _directory(CONFIG_ROOT)
    config = CONFIG_ROOT / (contract["instance_id"] + ".json")
    _exclusive_file(config, runner.canonical(contract["node_config"]).encode())
    remaining = min(runner.BOOTSTRAP_LIMIT, runner.deadline(contract) - time.time())
    if remaining <= 0:
        raise runner.RunnerRefused("original deadline expired after init latch")
    # No shell, flags from the caller, cache, extra source, daemon-only mode,
    # output forwarding, or retry of this mutation after it starts.
    subprocess.run(
        ["/usr/bin/nodeadm", "init", "--config-source", "file://" + str(config)],
        check=True,
        timeout=remaining,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=runner.clean_environment(),
    )
    runner.verify_instance(contract)
    return runner.success_receipt(contract)


def main(argv=None):
    return runner.entrypoint(
        "node-bootstrap", execute, sys.argv[1:] if argv is None else argv
    )


if __name__ == "__main__":
    raise SystemExit(main())
