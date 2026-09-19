"""Exercise deployment boundaries through installed entrypoints (#5413)."""

import configparser
import fcntl
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_codex_recovers_from_proxy_pid_reused_by_unrelated_process(installed):
    run, env, stores, _, prefix, _ = installed
    tool = prefix / "codex"
    tool.write_text("#!/bin/sh\necho tool-started\n")
    tool.chmod(0o755)
    runtime = stores["dev"] / "runtime"
    unrelated = subprocess.Popen(["sleep", "60"])
    proxy_pid = None
    try:
        (runtime / "proxy.pid").write_text(str(unrelated.pid))
        (runtime / "proxy.json").write_text(
            json.dumps(
                {
                    "pid": unrelated.pid,
                    "port": 9999,
                    "process_start": "old-process-start",
                    "proxy": "adp-gateway-proxy",
                    "deployment_id": stores["dev"].name,
                    "gateway_url": "https://dev.example.test/api",
                }
            )
        )
        result = run("--deployment", "dev", "codex", extra={"PATH": env["PATH"].split(":", 1)[1]})
        assert result.returncode == 0, result.stderr
        assert "tool-started" in result.stdout
        identity = json.loads((runtime / "proxy.json").read_text())
        assert identity["pid"] != unrelated.pid and identity["process_start"]
        proxy_pid = identity["pid"]
        assert unrelated.poll() is None
        assert f"kill {unrelated.pid}" not in (stores["dev"] / "logs/proxy.log").read_text()
    finally:
        if proxy_pid:
            os.kill(proxy_pid, signal.SIGINT)
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ANTHROPIC_API_KEY", "other-credential"),
        ("ANTHROPIC_AUTH_TOKEN", "other-credential"),
        ("ANTHROPIC_BASE_URL", "https://other.example.test"),
        ("ANTHROPIC_BEDROCK_BASE_URL", "https://other.example.test"),
        ("CLAUDE_CODE_USE_BEDROCK", "0"),
        ("CLAUDE_CODE_SKIP_BEDROCK_AUTH", "0"),
        ("CLAUDE_CODE_USE_VERTEX", "1"),
        ("CLAUDE_CODE_USE_FOUNDRY", "1"),
    ],
)
def test_claude_inherited_transport_override_fails_before_refresh(installed, key, value):
    run, _, stores, _, prefix, network = installed
    tool = prefix / "claude"
    tool.write_text("#!/bin/sh\necho incorrectly-started\n")
    tool.chmod(0o755)
    tokens = stores["dev"] / "tokens.json"
    session = json.loads(tokens.read_text())
    session["expires_at"] = 1
    tokens.write_text(json.dumps(session))
    result = run("--deployment", "dev", "claude", extra={key: value})
    assert result.returncode != 0
    assert key in result.stderr and "override" in result.stderr
    assert "incorrectly-started" not in result.stdout
    assert "other-credential" not in result.stderr
    assert not network.exists()


def test_alias_logout_clears_stable_and_legacy_alias_profiles(installed):
    run, _, stores, home, _, _ = installed
    assert run("deployment", "add", "development", "--url", "https://dev.example.test").returncode == 0
    aws_dir = home / ".aws"
    aws_dir.mkdir(exist_ok=True)
    stable = "adp-deployment-" + stores["dev"].name
    names = [stable, "bedrock-gateway-dev", "bedrock-gateway-development", "unrelated"]
    for filename, prefix in (("credentials", ""), ("config", "profile ")):
        (aws_dir / filename).write_text("".join(f"[{prefix}{name}]\nfixture=value\n" for name in names))
    result = run("--deployment", "development", "logout")
    assert result.returncode == 0, result.stderr
    assert not (stores["dev"] / "tokens.json").exists()
    for filename, prefix in (("credentials", ""), ("config", "profile ")):
        parsed = configparser.RawConfigParser()
        parsed.read(aws_dir / filename)
        assert parsed.sections() == [prefix + "unrelated"]


@pytest.mark.parametrize("other_action", ["refresh", "logout"])
def test_concurrent_named_aws_profile_updates_preserve_other_profiles(installed, tmp_path, other_action):
    _, env, stores, home, binary, network = installed
    for name, store in stores.items():
        config = json.loads((store / "config.json").read_text())
        config.update(user_pool_id="pool", client_id="client", identity_pool_id="identity", region="us-east-1")
        (store / "config.json").write_text(json.dumps(config))
    aws_dir = home / ".aws"
    aws_dir.mkdir(exist_ok=True)
    credentials, config = aws_dir / "credentials", aws_dir / "config"
    credentials.write_text(
        "[unrelated]\naws_access_key_id=keep\n[bedrock-gateway-dev]\naws_access_key_id=old-dev\n[bedrock-gateway-integration]\naws_access_key_id=old-int\n"
    )
    config.write_text(
        "[profile unrelated]\nregion=us-west-2\n[profile bedrock-gateway-dev]\nregion=us-east-1\n"
        "[profile bedrock-gateway-integration]\nregion=us-east-1\n"
    )
    before = (credentials.read_bytes(), config.read_bytes())
    ready = tmp_path / "aws-ready"
    stub = tmp_path / "stubs" / "aws"
    stub.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
        "name=os.environ['ADP_DEPLOYMENT_NAME']\n"
        "if sys.argv[1]=='cognito-idp':\n"
        " print(json.dumps({'AuthenticationResult':{'IdToken':'id-'+name,'AccessToken':'access-'+name,'ExpiresIn':3600}}))\n"
        "elif sys.argv[2]=='get-id': print(json.dumps({'IdentityId':'identity-'+name}))\n"
        "else:\n"
        f" Path({str(ready)!r}).touch()\n"
        " print(json.dumps({'Credentials':{'AccessKeyId':'new-'+name,'SecretKey':'secret-fixture',"
        "'SessionToken':'session-fixture','Expiration':'later'}}))\n"
    )
    stub.chmod(0o755)
    processes = []
    with (aws_dir / ".adp-profiles.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for name, action in (("dev", "refresh"), ("integration", other_action)):
                processes.append(
                    subprocess.Popen(
                        ["bash", str(binary / "adp"), "--deployment", name, action],
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            deadline = time.monotonic() + 8
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "refresh did not reach the mocked Identity Pool exchange"
            time.sleep(0.2)
            assert all(process.poll() is None for process in processes), "a profile writer bypassed the shared lock"
            assert (credentials.read_bytes(), config.read_bytes()) == before
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            for process in processes:
                out, err = process.communicate(timeout=15)
                assert process.returncode == 0, out + err
    profiles, regions = configparser.RawConfigParser(), configparser.RawConfigParser()
    profiles.read(credentials)
    regions.read(config)
    assert profiles["unrelated"]["aws_access_key_id"] == "keep"
    assert regions["profile unrelated"]["region"] == "us-west-2"
    dev_profile = "adp-deployment-" + stores["dev"].name
    integration_profile = "adp-deployment-" + stores["integration"].name
    assert profiles[dev_profile]["aws_access_key_id"] == "new-dev"
    assert "bedrock-gateway-dev" not in profiles and "bedrock-gateway-integration" not in profiles
    assert (integration_profile in profiles) == (other_action == "refresh")
    if other_action == "refresh":
        assert profiles[integration_profile]["aws_access_key_id"] == "new-integration"
    assert not network.exists()


@pytest.fixture
def installed(adp_bin, adp_home, tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ADP_", "BG_", "ANTHROPIC_", "CLAUDE_"))}
    env.update(HOME=str(adp_home), AWS_EC2_METADATA_DISABLED="true")
    # Unexpected network calls fail locally and leave evidence.
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    network = tmp_path / "network"
    (stubs / "curl").write_text(f'#!/bin/sh\necho called >> "{network}"\nexit 97\n')
    (stubs / "curl").chmod(0o755)
    env["PATH"] = f"{stubs}:{adp_bin}:{env['PATH']}"

    def run(*args, extra=None, script="adp"):
        return subprocess.run(
            ["bash", str(adp_bin / script), *args],
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            timeout=15,
        )

    stores = {}
    for name in ("dev", "integration"):
        result = run("deployment", "add", name, "--url", f"https://{name}.example.test", "--json")
        assert result.returncode == 0, result.stderr
        registry = json.loads((adp_home / ".adp/deployments.json").read_text())
        root = adp_home / ".adp/deployments" / registry["deployments"][name]["id"]
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / "config.json").write_text(json.dumps({"gateway_url": f"https://{name}.example.test/api"}))
        (root / "tokens.json").write_text(
            json.dumps(
                {
                    "access_token": f"{name}-token",
                    "refresh_token": f"{name}-refresh",
                    "id_token": f"{name}-id",
                    "expires_at": int(time.time()) + 3600,
                }
            )
        )
        stores[name] = root
    return run, env, stores, adp_home, adp_bin, network


def test_direct_auth_helper_honors_terminal_selection(installed):
    run, _, _, _, _, network = installed
    result = run("token", extra={"ADP_DEPLOYMENT": "integration"}, script="bg-cognito-auth.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "integration-token"
    assert not network.exists()


def test_direct_auth_helper_honors_saved_default(installed):
    run, _, _, _, _, _ = installed
    assert run("deployment", "use", "integration").returncode == 0
    result = run("token", script="bg-cognito-auth.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "integration-token"


def test_duplicate_import_url_cannot_overwrite_selected_store(installed):
    run, _, stores, _, _, network = installed
    before = {p: p.read_bytes() for p in stores["dev"].iterdir() if p.is_file()}
    result = run(
        "--deployment",
        "dev",
        "import",
        "--gateway-url",
        "https://dev.example.test/api",
        "--gateway-url",
        "https://wrong.example.test/api",
        "--refresh-token",
        "fixture",
        "--client-id",
        "fixture",
    )
    assert result.returncode != 0
    assert not network.exists(), "a mismatched import contacted the network"
    assert all(p.read_bytes() == data for p, data in before.items())


def test_token_refuses_config_bound_to_another_gateway(installed):
    run, _, stores, _, _, network = installed
    (stores["dev"] / "config.json").write_text('{"gateway_url":"https://wrong.example.test/api"}')
    result = run("--deployment", "dev", "token")
    assert result.returncode != 0
    assert "dev-token" not in result.stdout
    assert not network.exists()


def test_bare_claude_helper_stays_with_setup_after_default_and_env_change(installed):
    run, env, _, home, _, _ = installed
    assert run("--deployment", "dev", "claude", "setup").returncode == 0
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert run("deployment", "use", "integration").returncode == 0
    result = subprocess.run(
        ["bash", "-c", settings["apiKeyHelper"]], env={**env, "ADP_DEPLOYMENT": "integration"}, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dev-token"
    assert settings["env"]["ANTHROPIC_BEDROCK_BASE_URL"] == "https://dev.example.test/api"


def test_remove_actually_deletes_last_alias_private_store(installed):
    run, _, stores, _, _, _ = installed
    result = run("deployment", "remove", "integration", "--json")
    assert result.returncode == 0, result.stderr
    assert not stores["integration"].exists(), "successful removal left credentials behind"
    assert (stores["dev"] / "tokens.json").exists()


def test_management_inside_named_session_does_not_adopt_its_store_as_legacy(installed):
    run, _, stores, home, _, _ = installed
    result = run(
        "deployment",
        "add",
        "preprod",
        "--url",
        "https://preprod.example.test",
        extra={"ADP_DEPLOYMENT_ID": stores["dev"].name, "BG_CONFIG_DIR": str(stores["dev"])},
    )
    assert result.returncode == 0, result.stderr
    registry = json.loads((home / ".adp/deployments.json").read_text())
    assert "default" not in registry["deployments"], "named credentials were adopted as a phantom legacy store"


def test_duplicate_selection_is_rejected(installed):
    run, _, _, _, _, network = installed
    result = run("--deployment", "dev", "--deployment=integration", "token")
    assert result.returncode != 0
    assert "token" not in result.stdout
    assert not network.exists()


def test_claude_launch_overrides_setup_helper_without_rewriting_settings(installed):
    run, _, stores, home, prefix, _ = installed
    assert run("--deployment", "dev", "claude", "setup").returncode == 0
    saved = (home / ".claude/settings.json").read_bytes()
    tool = prefix / "claude"
    tool.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys
settings = json.loads(sys.argv[sys.argv.index('--settings') + 1])
token = subprocess.check_output(['bash', '-c', settings['apiKeyHelper']], text=True).strip()
print(json.dumps({{'settings': settings, 'token': token, 'args': sys.argv[1:]}}))
""")
    tool.chmod(0o755)
    result = run("--deployment", "integration", "claude", "--settings", '{"permissions":{"allow":["Read"]}}', "--print", "hello world")
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["token"] == "integration-token"
    assert observed["settings"]["env"]["ANTHROPIC_BEDROCK_BASE_URL"] == "https://integration.example.test/api"
    assert observed["settings"]["permissions"] == {"allow": ["Read"]}
    assert observed["args"][-2:] == ["--print", "hello world"]
    assert (home / ".claude/settings.json").read_bytes() == saved


def test_removal_refuses_a_running_claude_session_and_recovers_after_exit(installed):
    run, env, stores, home, prefix, _ = installed
    tool = prefix / "claude"
    ready = home / "ready"
    tool.write_text(f'#!/bin/sh\ntouch "{ready}"\nread line\n')
    tool.chmod(0o755)
    process = subprocess.Popen(
        ["bash", str(prefix / "adp"), "--deployment", "integration", "claude"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), process.communicate(timeout=2)
        result = run("deployment", "remove", "integration")
        assert result.returncode != 0
        assert "in use" in result.stderr
        assert (stores["integration"] / "tokens.json").exists()
    finally:
        process.communicate("done\n", timeout=5)
    assert run("deployment", "remove", "integration").returncode == 0
    assert not stores["integration"].exists()


def test_named_codex_setup_uses_distinct_stable_ports(installed):
    run, _, _, home, _, _ = installed
    import tomllib

    ports = {}
    for name in ("dev", "integration", "dev"):
        assert run("--deployment", name, "codex", "setup").returncode == 0
        config = tomllib.loads((home / ".codex/config.toml").read_text())
        endpoint = config["model_providers"]["adp-gateway"]["base_url"]
        assert ":9191/" not in endpoint
        if name in ports:
            assert ports[name] == endpoint
        ports[name] = endpoint
    assert ports["dev"] != ports["integration"]


def test_direct_python_helper_fails_closed_on_corrupt_registry(installed):
    _, env, _, home, prefix, network = installed
    (home / ".adp/deployments.json").write_text("{")
    result = subprocess.run([sys.executable, str(prefix / "adp-aws.py"), "list", "--json"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["code"] == "deployment_state_unreadable"
    assert not network.exists()


def test_admin_session_can_be_saved_for_new_registration(installed):
    run, env, stores, _, prefix, _ = installed
    assert run("deployment", "add", "fresh", "--url", "https://fresh.example.test").returncode == 0
    source = """import adp_common as c
c.save_session(dict(access_token='a', id_token='i', refresh_token='r', expires_in=3600,
                    client_id='c', user_pool_id='p', region='us-east-1'))
print(c.gateway_url())
"""
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=prefix, env={**env, "ADP_DEPLOYMENT": "fresh"}, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://fresh.example.test/api"
    assert (stores["dev"] / "tokens.json").exists()


@pytest.mark.parametrize(
    "override",
    [
        "--config=model_provider='other'",
        '-cmodel_providers."adp-gateway".base_url="https://wrong.test"',
        r'-cmodel_providers."adp\u002dgateway".base_url="https://wrong.test"',
        ["-c", r'model_providers."adp\u002dgateway".base_url="https://wrong.test"'],
        r'-c=model_providers."adp\U0000002dgateway".base_url="https://wrong.test"',
        r'--config="model\u005fproviders"."adp-gateway"."base\u005furl"="https://wrong.test"',
        "-c=\tmodel_provider \t= 'other'",
        "--oss",
    ],
)
def test_codex_transport_override_is_rejected_before_startup(installed, override):
    run, _, _, _, prefix, network = installed
    tool = prefix / "codex"
    tool.write_text("#!/bin/sh\necho incorrectly-started\n")
    tool.chmod(0o755)
    result = run("--deployment", "dev", "codex", *(override if isinstance(override, list) else [override]))
    assert result.returncode != 0
    assert "incorrectly-started" not in result.stdout
    assert not network.exists()


def test_codex_launch_pins_complete_provider_without_global_setup(installed):
    run, env, stores, home, prefix, _ = installed
    tool = prefix / "codex"
    tool.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    tool.chmod(0o755)
    # Permit localhost health probes by the real CLI; the stub model sends no
    # request upstream. Credentials and gateway URLs are fixture values only.
    try:
        result = run("--deployment", "dev", "codex", "exec", "hello world", extra={"PATH": env["PATH"].split(":", 1)[1:][0]})
        assert result.returncode == 0, result.stderr
        args = json.loads(result.stdout)
        identity = json.loads((stores["dev"] / "runtime/proxy.json").read_text())
        assert 'model_provider="adp-gateway"' in args
        assert f'model_providers.adp-gateway.base_url="http://127.0.0.1:{identity["port"]}/openai/v1"' in args
        assert 'model_providers.adp-gateway.wire_api="responses"' in args
        assert 'model_providers.adp-gateway.env_key="ADP_GATEWAY_DUMMY"' in args
        assert args[-2:] == ["exec", "hello world"]
        assert not (home / ".codex/config.toml").exists()
    finally:
        identity_file = stores["dev"] / "runtime/proxy.json"
        if identity_file.exists():
            identity = json.loads(identity_file.read_text())
            os.kill(identity["pid"], signal.SIGINT)


def test_daemons_have_separate_labels_pinned_context_and_saved_ports(installed):
    run, _, stores, home, prefix, _ = installed
    for name, body in (("uname", "echo Darwin"), ("launchctl", "exit 0")):
        script = prefix / name
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
    paths = []
    for name, store in stores.items():
        assert run("--deployment", name, "codex", "setup").returncode == 0
        result = run("--deployment", name, "daemon", "install")
        assert result.returncode == 0, result.stderr
        path = home / "Library/LaunchAgents" / f"com.adp.gateway-proxy.{store.name}.plist"
        paths.append(path)
        plist = plistlib.loads(path.read_bytes())
        assert plist["EnvironmentVariables"]["ADP_DEPLOYMENT_ID"] == store.name
        assert plist["EnvironmentVariables"]["ADP_DEPLOYMENT_URL"] == f"https://{name}.example.test/api"
        assert plist["ProgramArguments"][-1] == str(json.loads((store / "runtime/setup-port.json").read_text())["port"])
    result = run("deployment", "remove", "integration")
    assert result.returncode != 0 and "always-on proxy" in result.stderr
    assert run("--deployment", "integration", "daemon", "uninstall").returncode == 0
    assert paths[0].exists() and not paths[1].exists()


def test_claude_helper_quotes_install_path_with_spaces(installed):
    _, env, _, home, prefix, _ = installed
    spaced = home / "installed cli"
    shutil.copytree(prefix, spaced)
    result = subprocess.run(
        ["bash", str(spaced / "adp"), "--deployment", "dev", "claude", "setup"], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    settings = json.loads((home / ".claude/settings.json").read_text())
    token = subprocess.run(
        ["bash", "-c", settings["apiKeyHelper"]], env={**env, "ADP_DEPLOYMENT": "integration"}, capture_output=True, text=True, timeout=10
    )
    assert token.returncode == 0, token.stderr
    assert token.stdout.strip() == "dev-token"


def test_named_deployments_work_without_the_ps_command(installed, tmp_path):
    """A slim image has no `ps`, and every named command takes a lease (#5413 review).

    `ps` ships in base macOS but is a separate package on slim Linux images, so a
    lease that could only read `ps` made `login`, `status`, `token` and `logout`
    all exit 5 there — while the legacy single-deployment machine kept working.
    """
    run, env, _, _, _, _ = installed
    bare = tmp_path / "nops"
    bare.mkdir()
    tools = (
        "bash jq python3 date mktemp cat sed awk grep find rm mkdir chmod mv env tr "
        "dirname basename sleep kill curl uname head tail cut sort wc id stat touch ln cp printf"
    ).split()
    for tool in tools:
        located = shutil.which(tool, path=env["PATH"])
        if located:
            (bare / tool).symlink_to(located)
    assert shutil.which("ps", path=str(bare)) is None

    result = run("--deployment", "dev", "status", extra={"PATH": str(bare)})
    if not Path("/proc/self/stat").is_file():
        # macOS has no /proc: deliberately removing its bundled ps removes
        # both identity sources, so a clear refusal is the correct outcome.
        assert result.returncode == 5
        assert "neither /proc nor 'ps' is available" in result.stderr
        assert not result.stdout
        return
    assert result.returncode != 5, f"named deployment unusable without ps: {result.stderr}"
    assert "dev" in result.stdout
    assert "identity" not in result.stderr
