"""Crew bundle producer (#8054): layout, manifest, determinism and pins, all offline.

No network access and no real downloads: the staging tree is synthetic and the
shell script runs in its TEST ONLY --offline-stage mode. The bundle it produces
is accepted by image/install_crew.py's own validators and installer (AC-01).
"""

import importlib.util
import io
import json
import os
import re
import platform
import resource
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MODULE_ROOT.parents[2]
SCRIPTS = MODULE_ROOT / "scripts"
BUILD_SCRIPT = SCRIPTS / "build-crew-bundle.sh"
DOCKERFILE = REPO_ROOT / "modules/agent-factory/agent/Dockerfile.sandbox"

SPEC = importlib.util.spec_from_file_location("dev_box_crew_bundle", SCRIPTS / "crew_bundle.py")
BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUNDLE)
INSTALLER = BUNDLE.load_installer()

SOURCE = "a" * 40
INSTALL_PREFIX = "/opt/adp/crew/current"


def make_stage(root: Path) -> Path:
    """A staging tree shaped like build-crew-bundle.sh --local leaves it, with placeholder bytes."""
    stage = root / "stage"
    for member in BUNDLE.required_members():
        target = stage / member
        target.parent.mkdir(parents=True, exist_ok=True)
        if member.startswith("clients/node_modules/.bin/"):
            continue  # created as relative symlinks below, like npm does
        target.write_bytes(b"#!/bin/sh\necho approved\n" if "/bin/" in member else b"approved-crew-artifact")
        if "/bin/" in member:
            target.chmod(0o755)
    cli = stage / "clients/node_modules/@anthropic-ai/claude-code/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"#!/usr/bin/env node\n")
    cli.chmod(0o755)
    acp = stage / "clients/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js"
    acp.parent.mkdir(parents=True)
    acp.write_bytes(b"#!/usr/bin/env node\n")
    acp.chmod(0o755)
    codex = stage / "clients/node_modules/@openai/codex/bin/codex.js"
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b"#!/usr/bin/env node\n")
    codex.chmod(0o755)
    (stage / "clients/node_modules/.bin").mkdir(parents=True, exist_ok=True)
    (stage / "clients/node_modules/.bin/claude").symlink_to("../@anthropic-ai/claude-code/cli.js")
    (stage / "clients/node_modules/.bin/claude-agent-acp").symlink_to("../@agentclientprotocol/claude-agent-acp/dist/index.js")
    (stage / "clients/node_modules/.bin/codex").symlink_to("../@openai/codex/bin/codex.js")
    # build-crew-bundle.sh exposes the bundle's Node beside the adapter for its "#!/usr/bin/env node" shebang (#8223 cause 31).
    (stage / "clients/node_modules/.bin/node").symlink_to("../../../host/node")
    # Relocatable interpreter and a venv created against it (absolute links, as venv makes them).
    interpreter = stage / "python/bin/python3.12"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"ELF-placeholder")
    interpreter.chmod(0o755)
    (stage / "python/bin/python3").symlink_to("python3.12")
    venv_bin = stage / "venv/bin"
    (venv_bin / "python").symlink_to(interpreter)
    (venv_bin / "python3").symlink_to("python")
    (venv_bin / "python3.12").symlink_to("python")
    (venv_bin / "kirocrew").write_text(f"#!{venv_bin}/python3\nimport kiro_crew.cli\n")
    (venv_bin / "kirocrew").chmod(0o755)
    (venv_bin / "aws").write_text(f"#!{venv_bin}/python\nimport awscli\n")
    (venv_bin / "aws").chmod(0o755)
    (stage / "venv/pyvenv.cfg").write_text(f"home = {stage}/python/bin\ninclude-system-site-packages = false\n"
                                            f"version = 3.12.15\nexecutable = {stage}/python/bin/python3.12\n"
                                            f"command = {stage}/python/bin/python3.12 -m venv {stage}/venv\n")
    dist_info = stage / "venv/lib/python3.12/site-packages/kirocrew-0.11.2.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "direct_url.json").write_text(json.dumps({"url": f"file://{root}/downloads/kirocrew.whl"}))
    (dist_info / "RECORD").write_text("kirocrew-0.11.2.dist-info/direct_url.json,sha256=abc,42\n"
                                      "kirocrew-0.11.2.dist-info/METADATA,sha256=def,100\n")
    cache = stage / "venv/lib/python3.12/site-packages/kiro_crew/__pycache__"
    cache.mkdir(parents=True)
    (cache / "cli.cpython-312.pyc").write_bytes(b"pyc")
    return stage


def runner(calls):
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if command[:2] == ["id", "-u"] else 0)
    return run


@pytest.fixture
def stage(tmp_path):
    return make_stage(tmp_path)


def test_required_members_are_read_from_the_installer():
    constant = next(c for c in INSTALLER._unpack.__code__.co_consts if isinstance(c, tuple) and "host/node" in c)
    assert BUNDLE.required_members() == constant, "crew_bundle.py must track install_crew._unpack's required tuple"
    assert {"venv/bin/kirocrew", "clients/node_modules/.bin/claude", "host/node", "host/crew-host-model.js",
            "models/qwen3-embedding-0.6b-q8_0.gguf"} <= set(BUNDLE.required_members())


def test_pack_writes_an_installable_bundle_and_exact_manifest(stage, tmp_path):
    BUNDLE.normalize_venv(stage / "venv", stage, INSTALL_PREFIX)
    summary = BUNDLE.pack(stage, tmp_path / "out", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2",
                          source_date_epoch=1700000000)
    bundle, manifest_path = tmp_path / "out/crew.tar.gz", tmp_path / "out/profile.json"
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest) == {"schema_version", "revision", "source_revision", "crew_version", "bundle_sha256"}
    assert manifest["schema_version"] == 1 and manifest["crew_version"] == "0.11.2"
    assert manifest["revision"] == "crew-r1" and manifest["source_revision"] == SOURCE
    assert manifest["bundle_sha256"] == BUNDLE._digest(bundle) == summary["bundle_sha256"]
    assert manifest_path.read_text() == json.dumps(manifest, sort_keys=True) + "\n"
    with tarfile.open(bundle, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert set(BUNDLE.required_members()) <= set(members)
    assert members["clients/node_modules/.bin/claude"].issym()
    assert members["clients/node_modules/.bin/claude"].linkname == "../@anthropic-ai/claude-code/cli.js"
    assert members["clients/node_modules/.bin/codex"].issym()
    assert members["clients/node_modules/.bin/codex"].linkname == "../@openai/codex/bin/codex.js"
    assert members["clients/node_modules/.bin/node"].issym()
    assert members["clients/node_modules/.bin/node"].linkname == "../../../host/node"
    assert members["venv/bin/python"].linkname == "../../python/bin/python3.12"
    assert all(member.uid == 0 and member.gid == 0 and member.mtime == 1700000000 for member in members.values())
    assert members["venv/bin/kirocrew"].mode == 0o755 and members["host/crew-host-model.js"].mode == 0o644
    assert "venv/lib/python3.12/site-packages/kirocrew-0.11.2.dist-info/direct_url.json" not in members
    assert not any("__pycache__" in name for name in members)
    # The installer itself accepts the artifact (AC-01).
    calls = []
    result = INSTALLER.install(manifest, bundle, prefix=tmp_path / "opt", unit_dir=tmp_path / "units", run=runner(calls),
                               expected_source_revision=SOURCE, expected_profile_revision="crew-r1")
    assert result["profile_revision"] == "crew-r1" and result["bundle_sha256"] == manifest["bundle_sha256"]
    installed = tmp_path / "opt/crew-r1"
    assert (installed / "venv/bin/kirocrew").read_text().startswith(f"#!{INSTALL_PREFIX}/venv/bin/python3\n")
    assert (installed / "venv/bin/python").readlink() == Path("../../python/bin/python3.12")
    assert f"home = {INSTALL_PREFIX}/python/bin" in (installed / "venv/pyvenv.cfg").read_text()
    assert str(stage) not in (installed / "venv/pyvenv.cfg").read_text()


def test_pack_is_byte_deterministic(stage, tmp_path):
    BUNDLE.normalize_venv(stage / "venv", stage, INSTALL_PREFIX)
    BUNDLE.pack(stage, tmp_path / "one", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2", source_date_epoch=5)
    copied = tmp_path / "copy/stage"
    shutil.copytree(stage, copied, symlinks=True)
    os.utime(copied / "host/node", (1, 1))  # a different build time must not leak into the archive
    BUNDLE.pack(copied, tmp_path / "two", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2", source_date_epoch=5)
    assert (tmp_path / "one/crew.tar.gz").read_bytes() == (tmp_path / "two/crew.tar.gz").read_bytes()
    assert (tmp_path / "one/profile.json").read_text() == (tmp_path / "two/profile.json").read_text()


@pytest.mark.parametrize("missing", ["host/node", "models/qwen3-embedding-0.6b-q8_0.gguf", "venv/bin/kirocrew", "default-agent.json"])
def test_pack_rejects_an_incomplete_stage(stage, tmp_path, missing):
    (stage / missing).unlink()
    # Removing host/node also strands the .bin/node link, so the message may name both; the removed
    # member must be named either way.
    with pytest.raises(ValueError, match=rf"incomplete: missing .*{re.escape(missing)}"):
        BUNDLE.pack(stage, tmp_path / "out", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2")
    assert not (tmp_path / "out/crew.tar.gz").exists()


def test_pack_rejects_links_the_installer_would_refuse(stage, tmp_path):
    with pytest.raises(ValueError, match="venv/bin/python has an absolute target"):
        BUNDLE.pack(stage, tmp_path / "out", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2")
    BUNDLE.normalize_venv(stage / "venv", stage, INSTALL_PREFIX)
    (stage / "host/escape").symlink_to("../../outside")
    with pytest.raises(ValueError, match="escapes the bundle"):
        BUNDLE.pack(stage, tmp_path / "out", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2")
    (stage / "host/escape").unlink()
    (stage / "host/absolute").symlink_to("/usr/bin/node")
    with pytest.raises(ValueError, match="absolute target"):
        BUNDLE.pack(stage, tmp_path / "out", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2")


@pytest.mark.parametrize("revision,source,message", [
    ("bad revision!", SOURCE, "invalid Crew profile revision"),
    ("crew-r1", "main", "full revision"),
])
def test_pack_rejects_unpinned_identity(stage, tmp_path, revision, source, message):
    with pytest.raises(ValueError, match=message):
        BUNDLE.pack(stage, tmp_path / "out", revision=revision, source_revision=source, crew_version="0.11.2")


def test_validate_rejects_tampering(stage, tmp_path):
    BUNDLE.normalize_venv(stage / "venv", stage, INSTALL_PREFIX)
    BUNDLE.pack(stage, tmp_path / "out", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2")
    bundle, manifest = tmp_path / "out/crew.tar.gz", tmp_path / "out/profile.json"
    assert BUNDLE.validate(bundle, manifest, expected_revision="crew-r1")["revision"] == "crew-r1"
    with pytest.raises(ValueError, match="requested profile revision"):
        BUNDLE.validate(bundle, manifest, expected_revision="crew-r2")
    bundle.write_bytes(bundle.read_bytes() + b"x")
    with pytest.raises(ValueError, match="checksum mismatch"):
        BUNDLE.validate(bundle, manifest)
    data = json.loads(manifest.read_text())
    data["crew_version"] = "0.9.0"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsupported Crew profile"):
        BUNDLE.validate(bundle, manifest)


def test_pins_are_shared_with_the_sandbox_image_and_the_installer():
    pins = BUNDLE.read_pins()
    assert BUNDLE.diff_pins(DOCKERFILE, pins) == [], "Dockerfile.sandbox and crew-bundle.pins must pin the same artifacts"
    assert pins["CREW_VERSION"] == "0.11.2" and f'manifest["crew_version"] != "{pins["CREW_VERSION"]}"' in (MODULE_ROOT / "image/install_crew.py").read_text()
    for key in ("KIROCREW_WHEEL_SHA256", "EMBEDDING_MODEL_SHA256", "NODE_TARBALL_SHA256", "PYTHON_TARBALL_SHA256", "GH_TARBALL_SHA256"):
        assert len(pins[key]) == 64 and int(pins[key], 16) >= 0, key
    assert pins["NODE_TARBALL_URL"] == f"https://nodejs.org/dist/v{pins['NODE_VERSION']}/node-v{pins['NODE_VERSION']}-linux-x64.tar.xz"
    assert pins["NODE_VERSION"].startswith("22."), "host/node tracks the Node 22 LTS line the sandbox image uses"
    assert pins["PYTHON_VERSION"].startswith("3.12."), "kirocrew 0.11.2 requires Python >= 3.12"
    assert "x86_64-unknown-linux-gnu-install_only_stripped" in pins["PYTHON_TARBALL_URL"]
    assert pins["EMBEDDING_MODEL_FILE"] == "qwen3-embedding-0.6b-q8_0.gguf"
    assert (REPO_ROOT / pins["CLIENTS_DIR"] / "package-lock.json").is_file()
    assert (REPO_ROOT / pins["AGENT_DIR"] / "package.json").is_file()
    clients = json.loads((REPO_ROOT / pins["CLIENTS_DIR"] / "package.json").read_text())
    lock = json.loads((REPO_ROOT / pins["CLIENTS_DIR"] / "package-lock.json").read_text())
    version = clients["dependencies"]["@openai/codex"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert lock["packages"][""]["dependencies"]["@openai/codex"] == version
    assert lock["packages"]["node_modules/@openai/codex"]["version"] == version
    assert lock["packages"]["node_modules/@openai/codex"]["bin"] == {"codex": "bin/codex.js"}
    assert '"$STAGE/clients/node_modules/.bin/codex" --version' in BUILD_SCRIPT.read_text()
    assert pins["INSTALL_PREFIX"] == INSTALL_PREFIX
    scripts = json.loads((REPO_ROOT / pins["AGENT_DIR"] / "package.json").read_text())["scripts"]
    assert {"build:host-model", "build:host-dashboard", "build:host-bootstrap"} <= set(scripts)
    script = BUILD_SCRIPT.read_text()
    for pin in ("KIROCREW_WHEEL_URL", "KIROCREW_WHEEL_SHA256", "AWSCLI_VERSION", "EMBEDDING_MODEL_URL", "NODE_TARBALL_URL",
                "PYTHON_TARBALL_URL", "CLIENTS_DIR", "AGENT_DIR"):
        assert f"${pin}" in script or f"${{{pin}}}" in script, f"build-crew-bundle.sh must consume {pin}"
    assert "curl --proto '=https' --tlsv1.2 --fail" in script and "sha256sum -c" in script
    assert "latest" not in [part for part in pins.values()], "no floating download"


def test_the_shell_script_packs_an_offline_stage_identically(stage, tmp_path):
    BUNDLE.normalize_venv(stage / "venv", stage, INSTALL_PREFIX)  # --local does this before packing
    out = tmp_path / "out"
    result = subprocess.run(["bash", str(BUILD_SCRIPT), "--offline-stage", str(stage), "--output-dir", str(out),
                             "--revision", "crew-r1", "--source-revision", SOURCE, "--source-date-epoch", "7", "--env", "dev"],
                            capture_output=True, text=True, check=False, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "test only" in result.stdout and "without network access" in result.stdout
    summary = json.loads((out / "bundle-summary.json").read_text())
    manifest = json.loads((out / "profile.json").read_text())
    assert summary["revision"] == manifest["revision"] == "crew-r1" and summary["bundle_sha256"] == manifest["bundle_sha256"]
    env = dict(line.split("=", 1) for line in (out / "bundle.env").read_text().splitlines())
    assert env == {"CREW_BUNDLE": str(out / "crew.tar.gz"), "CREW_MANIFEST": str(out / "profile.json"),
                   "CREW_PROFILE_REVISION": "crew-r1", "CREW_BUNDLE_SHA256": manifest["bundle_sha256"],
                   "CREW_SOURCE_REVISION": SOURCE, "CREW_VERSION": "0.11.2"}
    BUNDLE.pack(stage, tmp_path / "again", revision="crew-r1", source_revision=SOURCE, crew_version="0.11.2", source_date_epoch=7)
    assert (tmp_path / "again/crew.tar.gz").read_bytes() == (out / "crew.tar.gz").read_bytes()


@pytest.mark.parametrize("arguments,message", [
    (["--offline-stage", "/nonexistent", "--revision", "crew-r1", "--source-revision", SOURCE], "offline stage not found"),
    (["--revision", "not valid", "--source-revision", SOURCE, "--dry-run"], "not a valid profile revision"),
    (["--revision", "crew-r1", "--source-revision", "main", "--dry-run"], "full commit"),
    (["--bundle-mode"], "unknown argument"),
])
def test_the_shell_script_refuses_bad_inputs(tmp_path, arguments, message):
    result = subprocess.run(["bash", str(BUILD_SCRIPT), "--output-dir", str(tmp_path / "out"), *arguments],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2 and message in result.stderr, result.stderr


def test_local_mode_dry_run_downloads_nothing(tmp_path):
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        pytest.skip("--local is Linux x86_64 only (the guest's platform)")
    if any(shutil.which(tool) is None for tool in ("curl", "tar", "xz", "sha256sum")):
        pytest.skip("--local prerequisites are not installed here")
    result = subprocess.run(["bash", str(BUILD_SCRIPT), "--local", "--dry-run", "--output-dir", str(tmp_path / "out"),
                             "--revision", "crew-r1", "--source-revision", SOURCE], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "dry-run: would assemble" in result.stdout
    assert not (tmp_path / "out/.work").exists()


def test_codebuild_mode_needs_credentials_before_anything_else(tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "aws").write_text("#!/bin/sh\nexit 255\n")
    (shim / "aws").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(BUILD_SCRIPT), "--codebuild", "--output-dir", str(tmp_path / "out"),
                             "--revision", "crew-r1", "--source-revision", SOURCE], capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 2 and "AWS credentials" in result.stderr


CODEBUILD_AWS_SHIM = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["AWS_SHIM_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
def option(name):
    return args[args.index(name) + 1]
revision, digest, source = os.environ["SHIM_REVISION"], os.environ["SHIM_DIGEST"], os.environ["SHIM_SOURCE"]
if args[0] == "sts":
    print("000000000000"); sys.exit(0)
if args[:2] == ["ssm", "get-parameter"]:
    values = {"/adp/dev/dev-box/image-builder-project": "adp-dev-dev-box-image",
              "/adp/dev/dev-box/crew-bundle-revision": revision, "/adp/dev/dev-box/crew-bundle-sha256": digest}
    print(values[option("--name")]); sys.exit(0)
if args[:2] == ["codebuild", "start-build"]:
    for value in args[args.index("--environment-variables-override") + 1:]:
        if value.startswith("--"):
            break
        assert value.count("name=") == 1 and ", " not in value, "override list element is not a single name=,value=,type= entry: " + value
    print("adp-dev-dev-box-image:build-1"); sys.exit(0)
if args[:2] == ["codebuild", "batch-get-builds"]:
    print(json.dumps({"s": "SUCCEEDED", "p": "COMPLETED"})); sys.exit(0)
if args[:2] == ["s3", "cp"] and any(a.startswith("s3://") and a.endswith("/profile.json") for a in args):
    manifest = {"schema_version": 1, "revision": revision, "source_revision": source, "crew_version": "0.11.2", "bundle_sha256": digest}
    open(args[-1], "w").write(json.dumps(manifest, sort_keys=True) + "\n"); sys.exit(0)
sys.exit(0)
'''


def test_codebuild_mode_passes_each_override_as_its_own_argument(tmp_path):
    """The first live run (#8057) failed here: the CLI saw one comma-joined string."""
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "aws").write_text(CODEBUILD_AWS_SHIM)
    (shim / "aws").chmod(0o755)
    (shim / "zip").write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in *.zip) : > "$a";; esac; done\n')
    (shim / "zip").chmod(0o755)
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "AWS_SHIM_LOG": str(log), "SHIM_REVISION": "crew-r1",
           "SHIM_DIGEST": "c" * 64, "SHIM_SOURCE": SOURCE, "POLL_INTERVAL": "0"}
    result = subprocess.run(["bash", str(BUILD_SCRIPT), "--codebuild", "--env", "dev", "--output-dir", str(tmp_path / "out"),
                             "--revision", "crew-r1", "--source-revision", SOURCE, "--source-date-epoch", "1792258146"],
                            capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    start = next(call for call in calls if call[:2] == ["codebuild", "start-build"])
    index = start.index("--environment-variables-override")
    assert start[index + 1:index + 4] == [f"name=ADP_SOURCE_SHA,value={SOURCE},type=PLAINTEXT",
                                          "name=BUNDLE_REVISION,value=crew-r1,type=PLAINTEXT",
                                          "name=SOURCE_DATE_EPOCH,value=1792258146,type=PLAINTEXT"]
    assert start[index + 4:index + 6] == ["--buildspec-override", "modules/user-services/dev-box/image/bundle-buildspec.yml"]
    assert start[index + 6:index + 8] == ["--compute-type-override", "BUILD_GENERAL1_MEDIUM"], "the bundle build alone gets 7 GB"
    assert "--project-name" in start and start[start.index("--project-name") + 1] == "adp-dev-dev-box-image"
    env_file = dict(line.split("=", 1) for line in (tmp_path / "out/bundle.env").read_text().splitlines())
    assert env_file["CREW_BUNDLE"] == f"s3://adp-terraform-state-000000000000/dev-box/image-inputs/{'c' * 64}/crew.tar.gz"
    assert env_file["CREW_PROFILE_REVISION"] == "crew-r1" and env_file["CREW_BUNDLE_SHA256"] == "c" * 64
    assert any(call[:2] == ["s3api", "head-object"] for call in calls)


def crafted_bundle(tmp_path, members):
    """A tarball written outside pack(), to exercise inspect_members on inputs pack() never produces."""
    bundle = tmp_path / "crafted.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, kind, extra in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = b"approved"
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                info.type = {"sym": tarfile.SYMTYPE, "link": tarfile.LNKTYPE, "chr": tarfile.CHRTYPE}[kind]
                info.linkname = extra
                archive.addfile(info)
    return bundle


def test_inspect_members_applies_the_installer_rules_without_extracting(tmp_path):
    complete = [(member, "file", None) for member in BUNDLE.required_members()]
    assert BUNDLE.inspect_members(crafted_bundle(tmp_path, complete))["members"] == len(complete)
    with pytest.raises(ValueError, match="incomplete: missing host/node"):
        BUNDLE.inspect_members(crafted_bundle(tmp_path, [m for m in complete if m[0] != "host/node"]))
    with pytest.raises(ValueError, match="unsafe paths"):
        BUNDLE.inspect_members(crafted_bundle(tmp_path, complete + [("venv/bin/python", "sym", "/usr/bin/python3")]))
    with pytest.raises(ValueError, match="unsafe paths"):
        BUNDLE.inspect_members(crafted_bundle(tmp_path, complete + [("../escape", "file", None)]))
    with pytest.raises(ValueError, match="devices or hard links"):
        BUNDLE.inspect_members(crafted_bundle(tmp_path, complete + [("host/node2", "link", "host/node")]))
    with pytest.raises(ValueError, match="devices or hard links"):
        BUNDLE.inspect_members(crafted_bundle(tmp_path, complete + [("dev/null", "chr", "")]))
    assert not list(tmp_path.glob("crew-bundle-validate-*")), "validation never materialises the archive"


def test_pack_and_validate_stream_a_large_member_within_a_small_memory_budget(stage, tmp_path):
    """The second live run was OOM-killed on a 3 GB host while packing a ~1.5 GB stage."""
    BUNDLE.normalize_venv(stage / "venv", stage, INSTALL_PREFIX)
    large = stage / "models/qwen3-embedding-0.6b-q8_0.gguf"
    with large.open("wb") as model:
        model.truncate(300 * 1024 * 1024)  # sparse: 300 MiB to read and hash, no disk cost
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    result = subprocess.run([sys.executable, "-I", str(SCRIPTS / "crew_bundle.py"), "pack", "--stage", str(stage), "--output-dir",
                             str(tmp_path / "out"), "--revision", "crew-r1", "--source-revision", SOURCE, "--crew-version", "0.11.2"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    assert peak_kb >= before
    summary = json.loads(result.stdout)
    assert summary["uncompressed_bytes"] >= 300 * 1024 * 1024 and summary["size_bytes"] < 2 * 1024 * 1024
    assert summary["peak_rss_mb"] < 256, summary
    assert peak_kb / 1024 < 512, f"pack must stream; peak child RSS was {peak_kb / 1024:.0f} MiB"
    assert BUNDLE.validate(tmp_path / "out/crew.tar.gz", tmp_path / "out/profile.json", expected_revision="crew-r1")["members"] == summary["members"]


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_packed_host_cli_and_skills_match_owner_shell_seeding(stage, tmp_path, client):
    fixture_repo = tmp_path / 'repo'
    source = fixture_repo / 'modules/agent-factory/skills'
    (source / 'example').mkdir(parents=True)
    (source / 'MANIFEST.json').write_text(json.dumps({
        'schema_version': 1,
        'skills': [{'name': 'example', 'clients': ['claude-code', 'codex'], 'invocation': 'Use example'}],
    }))
    (source / 'example/SKILL.md').write_text('Fixture skill\n')
    (source / 'example/agents').mkdir()
    (source / 'example/agents/openai.yaml').write_text('interface:\n  display_name: Example\n')
    (source / '_shared').mkdir()
    (source / '_shared/reference.md').write_text('Shared fixture reference\n')
    cli = REPO_ROOT / 'modules/gateway/cli'
    (fixture_repo / 'modules/gateway').symlink_to(cli.parent, target_is_directory=True)
    staging = re.search(r'(mkdir -p "\$STAGE/host/adp"\n.*?)\n# #8555:', BUILD_SCRIPT.read_text(), re.S)
    assert staging
    subprocess.run(['bash', '-euc', staging.group(1)], check=True, env={
        'PATH': os.environ['PATH'], 'REPO_ROOT': str(fixture_repo),
        'WORK_DIR': str(tmp_path / 'work'), 'STAGE': str(stage),
    })
    installed = stage / 'host/adp'
    home = tmp_path / 'home'
    home.mkdir()
    result = subprocess.run([str(installed / 'adp'), client, 'setup', '--skills-only'],
                            capture_output=True, text=True, check=False,
                            env={'PATH': os.environ['PATH'], 'HOME': str(home)})
    assert result.returncode == 0, result.stderr
    seeded = home / f'.{client}/skills/example'
    assert (seeded / 'SKILL.md').read_bytes() == (source / 'example/SKILL.md').read_bytes()
    assert (seeded / 'agents/openai.yaml').read_bytes() == (source / 'example/agents/openai.yaml').read_bytes()
    assert not (home / '.bedrock-gateway/tokens.json').exists()
    assert not (installed / 'skills/skills').exists()

    BUNDLE.normalize_venv(stage / 'venv', stage, INSTALL_PREFIX)
    BUNDLE.pack(stage, tmp_path / 'packed', revision='crew-r1', source_revision=SOURCE,
                crew_version='0.11.2', source_date_epoch=7)
    with tarfile.open(tmp_path / 'packed/crew.tar.gz', 'r:gz') as bundle:
        members = {member.name: member for member in bundle.getmembers()}
        assert members['host/adp/adp'].mode & 0o111
        assert bundle.extractfile('host/adp/adp').read() == (cli / 'adp').read_bytes()
        assert bundle.extractfile('host/adp/adp-skills.tar.gz').read() == (installed / 'adp-skills.tar.gz').read_bytes()
        assert bundle.extractfile('host/adp/skills/example/SKILL.md').read() == b'Fixture skill\n'
        assert json.load(bundle.extractfile('host/adp/skills/MANIFEST.json'))['skills'][0]['name'] == 'example'
        assert bundle.extractfile('host/adp/skills/_shared/reference.md').read() == b'Shared fixture reference\n'

    script = (MODULE_ROOT / 'image/prepare_shell.sh').read_text()
    profile = re.search(r"cat > /etc/profile.d/adp-dev-box-tools.sh <<'PROFILE'\n(.*?)\nPROFILE", script, re.S)
    assert profile
    body = profile.group(1)
    assert 'export PATH="/opt/adp/crew/current/host/adp:' in body
    assert 'if [ -x /opt/adp/crew/current/host/adp/adp ]; then' in body
    assert 'adp claude setup --skills-only' in body
    assert 'adp codex setup --skills-only' in body
    assert 'adp login' not in body


def test_bundle_installs_executable_aws_provider_from_canonical_source():
    script = BUILD_SCRIPT.read_text()
    assert 'install -m 0755 "$REPO_ROOT/modules/agent-factory/agent-worker-image/adp_cred/devbox.py" "$STAGE/host/adp-cred"' in script


def test_bundle_build_stages_the_host_cli_and_reproducible_shared_skills(tmp_path):
    script = BUILD_SCRIPT.read_text()
    for name in ('adp-flow.py', 'adp-product.py', 'adp-github.py', 'adp_skills.py', 'adp_deployments.py'):
        assert name in script
    assert 'install -m 0755 "$REPO_ROOT/modules/gateway/cli/adp" "$STAGE/host/adp/adp"' in script
    assert '"$STAGE/host/adp/adp-skills.tar.gz"' in script
    assert '"$STAGE/host/adp/skills"' in script
    assert {'host/adp/adp', 'host/adp/adp-skills.tar.gz', 'host/adp/skills/MANIFEST.json'} <= set(BUNDLE.required_members())

    source = tmp_path / 'skills'
    (source / 'example').mkdir(parents=True)
    (source / 'MANIFEST.json').write_text(json.dumps({
        'schema_version': 1, 'skills': [{'name': 'example', 'clients': ['codex'], 'invocation': 'Use example'}],
    }))
    (source / 'example/SKILL.md').write_text('Fixture skill\n')
    output = tmp_path / 'output'
    producer = REPO_ROOT / 'modules/gateway/scripts/build-skills-archive.sh'
    cli = REPO_ROOT / 'modules/gateway/cli/adp'
    subprocess.run(['bash', str(producer), '--build', str(source), str(output), str(cli)], check=True)
    archive = output / 'adp-skills.tar.gz'
    first = archive.read_bytes()
    assert first[4:8] == bytes(4)
    with tarfile.open(archive, 'r:gz') as contents:
        assert 'skills/example/SKILL.md' in contents.getnames()
    subprocess.run(['bash', str(producer), '--build', str(source), str(output), str(cli)], check=True)
    assert archive.read_bytes() == first
