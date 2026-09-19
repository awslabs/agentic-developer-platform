"""The pinned SkyPilot image must actually START an API server — Issue #5042 (U3).

## The reproduction these tests lock down

A checkpoint review of `328080ca` raised this as a P1: the Deployment pinned an image and
wired a Service, probes and a NetworkPolicy around port 46580, but never told the container
what to run.

The pinned image's config blob declares `Cmd: ["python3"]` and no `Entrypoint`. Verified from
the registry by digest, hashing each response body: the image index hashes to the digest
`releases/superplane.lock.yaml` pins, its amd64 manifest to `sha256:ab4e67e7…b318`, and that
manifest's config blob to `sha256:528a1f79…62a1`. Those recorded facts live in
`skypilot_image_facts.json`, which documents how to re-fetch them.

So a container overriding neither `command` nor `args` starts a **bare Python interpreter**.
Nothing listens on 46580, ever. What makes this worse than an obvious crash is the shape of the
symptom: `python3` with no TTY does not exit, so the container stays Running and only the
readiness probe fails — indistinguishable at a glance from a slow or unhealthy server. The
Service, the probes and the NetworkPolicy all look correct, because individually they are.

## Why these tests read the real package rather than upstream documentation

The review's instruction was to "verify against the pinned version's actual CLI/server
behavior rather than inferring from upstream controller constants". Every expectation below is
therefore taken from `skypilot==0.12.0` — the version the image installs — and recorded in
`skypilot_image_facts.json` with its source path:

*   `-m sky.server.server` is the package's OWN launcher (`sky/server/common.py`,
    `API_SERVER_CMD`, invoked as `[sys.executable, *API_SERVER_CMD.split()]`).
*   `--host` defaults to `127.0.0.1`. That is the second half of the defect: even a container
    that ran the right module would bind loopback inside its own network namespace and refuse
    every connection from the Service.
*   uid 1000 does not exist in the image and there is no `/home` entry, so with `HOME` unset
    `os.path.expanduser('~/.sky')` returns the path UNCHANGED (documented CPython behaviour,
    bpo-10496) — a relative path resolved against the working directory. `test_expanduser_…`
    below executes that, so the claim is demonstrated rather than asserted.

## What is NOT claimed here

These are offline manifest and image-level checks. They prove the container is told to start a
server that binds an interface the Service can reach; they do not prove a server came up
against a real database, and nothing here is an isolation, backup, retention or restore claim.
Database hosting (Decision 2) is unresolved and this file decides nothing about it.
"""

from __future__ import annotations

import json
import posixpath
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

MODULE_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = MODULE_ROOT / "k8s"
API_MANIFEST = K8S_DIR / "40-skypilot-api.yaml"
LOCK_FILE = MODULE_ROOT / "releases" / "superplane.lock.yaml"
IMAGE_FACTS = Path(__file__).resolve().parent / "skypilot_image_facts.json"


@pytest.fixture(scope="module")
def facts() -> dict:
    return json.loads(IMAGE_FACTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def package(facts) -> dict:
    return facts["package_facts"]


@pytest.fixture(scope="module")
def api_container() -> dict:
    """The `skypilot-api` container from the unrendered Deployment."""
    for doc in yaml.safe_load_all(API_MANIFEST.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            containers = doc["spec"]["template"]["spec"]["containers"]
            for container in containers:
                if container["name"] == "skypilot-api":
                    return container
    raise AssertionError(f"no skypilot-api container found in {API_MANIFEST}")


@pytest.fixture(scope="module")
def pod_spec() -> dict:
    for doc in yaml.safe_load_all(API_MANIFEST.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            return doc["spec"]["template"]["spec"]
    raise AssertionError("no Deployment in the SkyPilot API manifest")


@pytest.fixture(scope="module")
def api_service() -> dict:
    for doc in yaml.safe_load_all(API_MANIFEST.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind") == "Service":
            return doc
    raise AssertionError("no Service in the SkyPilot API manifest")


def _env(container: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in container.get("env") or []}


def _argv(container: dict) -> list[str]:
    return list(container.get("command") or []) + list(container.get("args") or [])


# ---------------------------------------------------------------------------
# The recorded facts must describe the image the lock actually pins.
# ---------------------------------------------------------------------------


def test_recorded_facts_describe_the_locked_image(facts):
    """Otherwise every test below could be true of some other image.

    The lock's digest and the recorded index digest are compared, so a future U2 re-pin makes
    this file fail loudly instead of the suite silently validating a stale image.
    """
    lock = yaml.safe_load(LOCK_FILE.read_text(encoding="utf-8"))
    assert lock["images"]["skypilot-api"] == facts["image_index_digest"], (
        "the lock pins a different SkyPilot image than skypilot_image_facts.json records. "
        "Re-record the facts (the file documents the commands) before trusting these tests."
    )


def test_the_image_does_not_start_a_server_on_its_own(facts):
    """The premise of this whole module, stated as a test.

    If a future re-pin ships an image with a real entrypoint, this fails — and that is the
    signal to revisit whether the explicit command below is still the right shape, rather
    than leaving a stale override in place.
    """
    config = facts["config"]
    assert config["Entrypoint"] in (None, []), (
        "the pinned image now declares an Entrypoint; the manifest's explicit command may "
        f"no longer be correct: {config['Entrypoint']!r}"
    )
    assert config["Cmd"] == ["python3"], (
        f"the pinned image's Cmd changed from a bare interpreter: {config['Cmd']!r}"
    )


# ---------------------------------------------------------------------------
# The startup contract.
# ---------------------------------------------------------------------------


def test_the_container_overrides_the_images_bare_interpreter(api_container):
    """Without a command, the pod runs `python3` and serves nothing."""
    assert api_container.get("command"), (
        "the skypilot-api container sets no `command`, so it inherits the image's "
        '`Cmd: ["python3"]` and starts an interactive interpreter. The container stays '
        "Running and only the readiness probe fails, so the rollout looks merely slow."
    )


def test_the_server_is_launched_as_a_module_using_the_packages_own_command(
    api_container, package
):
    """`-m sky.server.server`, from `API_SERVER_CMD` — not an invented path.

    Running it as a FILE (`python3 .../server.py`) would take the `__main__` branch but never
    the `__name__ == 'sky.server.server'` branch that loads plugins under uvicorn's re-import,
    so it must be `-m`.
    """
    argv = _argv(api_container)
    module_flag = package["api_server_cmd"].split()  # ['-m', 'sky.server.server']
    assert module_flag[0] in argv and module_flag[1] in argv, (
        f"the container does not launch {package['api_server_cmd']!r} "
        f"(from {package['api_server_cmd_source']}); argv is {argv!r}"
    )
    assert not package["has_server_dunder_main_module"]
    assert not any(arg.endswith("server.py") for arg in argv), (
        "the server is being run as a FILE. `sky/server/server.py` guards on both "
        f"{package['module_name_guards']!r}, and the second only fires when it is imported "
        "as a module, so a file invocation silently skips plugin loading."
    )


def test_the_server_binds_an_address_the_service_can_reach(api_container, package):
    """The half of the defect that survives fixing the command.

    `--host` defaults to loopback, which is per-network-namespace: the Service would get
    connection-refused from a perfectly healthy process.
    """
    argv = _argv(api_container)
    hosts = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--host=")]
    assert hosts, (
        f"no --host is passed, so the server binds {package['default_host']} "
        f"({package['default_host_source']}) and the Service cannot reach it"
    )
    assert hosts[-1] in ("0.0.0.0", "::"), (
        f"--host={hosts[-1]!r} is not an address reachable from outside the pod"
    )


def test_the_served_port_matches_the_service_the_probes_and_the_container_port(
    api_container, api_service, package
):
    """One number in four places. Read from all four rather than restated once."""
    argv = _argv(api_container)
    ports = [int(arg.split("=", 1)[1]) for arg in argv if arg.startswith("--port=")]
    assert ports, (
        "the served port is left implicit, so an upstream default change breaks it"
    )
    served = ports[-1]

    assert served == package["default_port"]
    container_ports = [p["containerPort"] for p in api_container["ports"]]
    assert served in container_ports, (
        f"the server is told to serve {served} but the container declares {container_ports}"
    )
    service_ports = [p["port"] for p in api_service["spec"]["ports"]]
    assert service_ports == [served], (
        f"the Service exposes {service_ports} but the server serves {served}"
    )

    named = {
        p["name"]: p["containerPort"] for p in api_container["ports"] if p.get("name")
    }
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        spec = api_container.get(probe)
        if not spec:
            continue
        port = spec["httpGet"]["port"]
        resolved = named.get(port, port) if isinstance(port, str) else port
        assert resolved == served, (
            f"{probe} targets {port!r}, but the server serves {served}"
        )
        assert spec["httpGet"]["path"] == package["health_endpoint"]


def test_metrics_port_cannot_collide_with_the_api_port(api_container, package):
    """`server.py` raises ValueError when --port == --metrics-port.

    Both are pinned so that collision is unreachable by editing one number.
    """
    argv = _argv(api_container)
    api = [int(a.split("=", 1)[1]) for a in argv if a.startswith("--port=")]
    metrics = [int(a.split("=", 1)[1]) for a in argv if a.startswith("--metrics-port=")]
    assert metrics, (
        "--metrics-port is not pinned, so a --port change could collide with it"
    )
    assert api[-1] != metrics[-1], (
        "--port equals --metrics-port; the server raises ValueError and never starts"
    )
    assert metrics[-1] == package["default_metrics_port"]


def test_deploy_mode_is_not_enabled(api_container, package):
    """A scope constraint, not a tuning choice.

    `--deploy` auto-enables jobs consolidation mode, which runs the jobs controller in this
    pod and provisions compute from it. This unit is authorised to stand up an API server; no
    GPU work or compute launch is in scope.
    """
    assert package["deploy_flag_enables_consolidation_mode"]
    assert "--deploy" not in _argv(api_container), (
        "--deploy auto-enables consolidation mode "
        f"({package['deploy_flag_source']}), which launches compute from this pod"
    )


# ---------------------------------------------------------------------------
# HOME and the paths that depend on it.
# ---------------------------------------------------------------------------


def test_expanduser_returns_an_unusable_path_without_home_or_a_passwd_entry(facts):
    """DEMONSTRATE the mechanism rather than assert it.

    With `HOME` unset and the current uid absent from the password database, CPython returns
    `~/.sky` unchanged (bpo-10496) — relative, so it resolves against the working directory
    and the absolutely-mounted config is never read. Run in a subprocess with a stubbed `pwd`
    so the host's own passwd file cannot make this pass for the wrong reason.
    """
    assert not facts["has_uid_1000"], (
        "uid 1000 now exists in the image, which changes this failure mode"
    )
    assert facts["home_entries"] == []

    script = (
        "import sys, types, os\n"
        "fake = types.ModuleType('pwd')\n"
        "def getpwuid(uid): raise KeyError('uid not found')\n"
        "def getpwnam(n): raise KeyError(n)\n"
        "fake.getpwuid = getpwuid; fake.getpwnam = getpwnam\n"
        "sys.modules['pwd'] = fake\n"
        "os.environ.pop('HOME', None)\n"
        "import posixpath\n"
        "print(posixpath.expanduser('~/.sky'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    expanded = result.stdout.strip()
    assert expanded == "~/.sky", f"expected the path unchanged, got {expanded!r}"
    assert not posixpath.isabs(expanded), (
        "the path is absolute, so this failure mode no longer applies and the HOME "
        "requirement below should be re-examined"
    )


def test_home_is_set_explicitly(api_container, pod_spec, package):
    """Required because the pod runs as a uid the image's passwd file does not contain."""
    security = pod_spec.get("securityContext") or {}
    assert security.get("runAsUser") == 1000, (
        "this test is written for runAsUser 1000; if the uid changed, re-check whether it "
        "exists in the image before relying on passwd-based HOME resolution"
    )
    env = _env(api_container)
    assert "HOME" in env, (
        "HOME is not set. SkyPilot resolves its config, log, lock and generated-file paths "
        f"through expanduser ({package['home_relative_write_paths']}), and with no HOME and "
        "no passwd entry for uid 1000 those stay relative paths."
    )
    assert env["HOME"]["value"].startswith("/"), "HOME must be an absolute path"


def test_config_is_reachable_by_an_absolute_path(api_container, package):
    """The config must not depend on `~` expansion at all."""
    env = _env(api_container)
    assert package["global_config_env"] in env, (
        f"{package['global_config_env']} is not set, so config loading falls back to "
        f"{package['global_config_default_path']} and depends on HOME expansion"
    )
    configured = env[package["global_config_env"]]["value"]
    assert configured.startswith("/"), f"{configured!r} is not absolute"

    mounts = {m["mountPath"]: m for m in api_container.get("volumeMounts") or []}
    assert configured in mounts, (
        f"{package['global_config_env']} points at {configured!r}, which no volumeMount "
        f"provides. Mounted paths: {sorted(mounts)}"
    )


def test_the_config_mount_does_not_make_the_home_directory_read_only(
    api_container, package
):
    """The server WRITES inside `~/.sky`; a ConfigMap over that directory breaks it.

    A ConfigMap volume is read-only regardless of the `readOnly` flag, so mounting one at
    `~/.sky` fails the server's first write — after the container has started, which is the
    hardest failure to attribute from a rollout.
    """
    env = _env(api_container)
    home = env["HOME"]["value"]
    sky_dir = f"{home}/.sky"
    mounts = {m["mountPath"]: m for m in api_container.get("volumeMounts") or []}

    assert sky_dir in mounts, f"nothing writable is mounted at {sky_dir}"
    assert not mounts[sky_dir].get("readOnly"), (
        f"{sky_dir} is mounted read-only, but the server creates "
        f"{package['home_relative_write_paths']} inside it"
    )
    assert not mounts[sky_dir].get("subPath"), (
        f"{sky_dir} is a subPath mount, which would not provide the directory itself"
    )

    config_mount = mounts[env[package["global_config_env"]]["value"]]
    assert config_mount.get("subPath"), (
        "the config is mounted as a whole directory rather than a single file, which makes "
        f"its parent read-only. Use subPath so {sky_dir} stays writable."
    )


def test_the_writable_home_volume_is_not_a_persistent_claim(api_container, pod_spec):
    """Durable state lives in Postgres (U2 pinned `state_backend: postgres`).

    A PVC here would be a second, node-bound copy of state that disagrees with the database
    after a reschedule — and StorageClasses are platform-owned in any case.
    """
    env = _env(api_container)
    sky_dir = f"{env['HOME']['value']}/.sky"
    mounts = {m["mountPath"]: m for m in api_container.get("volumeMounts") or []}
    volume_name = mounts[sky_dir]["name"]
    volume = next(v for v in pod_spec["volumes"] if v["name"] == volume_name)
    assert "persistentVolumeClaim" not in volume, (
        f"{sky_dir} is backed by a PVC; SkyPilot's durable state belongs in Postgres"
    )
    assert "emptyDir" in volume
    assert volume["emptyDir"].get("sizeLimit"), (
        "the writable home volume is unbounded, so a log or lock leak fills the node's disk"
    )


def test_the_database_reference_uses_the_env_var_the_package_reads(
    api_container, package
):
    """A misnamed variable would fall back to SQLite and silently drop state on restart."""
    env = _env(api_container)
    name = package["db_connection_uri_env"]
    assert name in env, (
        f"{name} ({package['db_connection_uri_env_source']}) is not set; the server would "
        "fall back to its local default and lose cluster state on every restart"
    )
    entry = env[name]
    assert "value" not in entry, (
        f"{name} carries a literal value; the connection URI must arrive by reference"
    )
    ref = entry["valueFrom"]["secretKeyRef"]
    assert ref.get("optional") is False, (
        f"{name} is optional, so the pod could start with it empty and fall back to SQLite"
    )


# ---------------------------------------------------------------------------
# Startup timing: a slow start must not be killed as a failed one.
# ---------------------------------------------------------------------------


def test_a_startup_probe_gates_the_liveness_probe(api_container):
    """Before serving, the server initialises its DBs over the network and starts workers.

    On a bare `livenessProbe.initialDelaySeconds`, a startup slower than that fixed window
    gets the container killed and restarted forever, presenting as a crash loop rather than
    as "startup needs longer". A startupProbe suspends the other probes until it passes.
    """
    startup = api_container.get("startupProbe")
    assert startup, (
        "no startupProbe, so a slow first start is indistinguishable from an unhealthy "
        "server and the liveness probe kills it mid-initialisation"
    )
    budget = startup["periodSeconds"] * startup["failureThreshold"]
    assert budget >= 120, (
        f"the startup budget is only {budget}s; database initialisation and worker startup "
        "can exceed that on a cold cluster"
    )
    assert "initialDelaySeconds" not in api_container.get("livenessProbe", {}), (
        "the liveness probe still carries its own initialDelaySeconds. With a startupProbe "
        "present that is redundant, and keeping both invites tuning one and not the other."
    )


def test_no_shell_wrapper_is_used(api_container):
    """`sh -c` would make the server a child process that does not receive SIGTERM.

    Kubernetes signals PID 1. A shell wrapper swallows the signal, so a Recreate rollout waits
    out the full termination grace period on every deploy — and this Deployment is
    single-replica Recreate, so that delay is downtime.
    """
    argv = _argv(api_container)
    assert argv[0] not in ("sh", "bash", "/bin/sh", "/bin/bash"), (
        f"the server runs under a shell wrapper ({argv[0]!r}), which does not forward "
        "SIGTERM to the server process"
    )
    assert "-c" not in argv[:2], f"shell -c invocation: {argv!r}"


def test_the_manifest_declares_no_environment_specific_literals(api_container):
    """Region must stay a render placeholder, so no environment is baked in here."""
    env = _env(api_container)
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        assert env[name]["value"].startswith("REPLACE_WITH_"), (
            f"{name} carries a literal instead of a render placeholder"
        )
