"""Offline AMI and login-shell tests; a live cross-user socket test still needs a box."""

import json
import os
from pathlib import Path
import pty
import re
import subprocess
import tomllib

import pytest

IMAGE = Path(__file__).resolve().parents[1] / 'image'


def embedded_script(source, name):
    match = re.search(rf"cat > /etc/profile.d/{re.escape(name)} <<'EOF'\n(.*?)\nEOF", source, re.S)
    if not match:
        match = re.search(rf"cat > /etc/profile.d/{re.escape(name)} <<'PROFILE'\n(.*?)\nPROFILE", source, re.S)
    assert match
    return match.group(1)


def test_base_ami_installs_tmux_with_owner_config_and_login_notice():
    packer = (IMAGE / 'base.pkr.hcl').read_text()
    shell = (IMAGE / 'prepare_shell.sh').read_text()
    ssh = (IMAGE / 'prepare_ssh.sh').read_text()
    assert 'source      = "${path.root}/prepare_shell.sh"' in packer
    assert 'sudo bash /tmp/adp-prepare-shell.sh' in packer
    assert 'dnf install -y tmux' in shell and 'rpm -q tmux' in shell
    assert 'bubblewrap' in shell, 'Codex 0.164 warns on every start without bubblewrap'
    assert 'bubblewrap jq' in shell and 'command -v curl' in shell, 'adp needs jq and the curl binary; AL2023 provides curl via curl-minimal, so the full package must not be forced'
    assert 'install -m 0600 -o ec2-user -g ec2-user' in shell
    assert 'default-command "env -u TMOUT /bin/bash -l"' in shell
    assert 'not instance stop/start or Replace' in shell
    assert 'TMOUT=900' in ssh and 'unset TMOUT' in ssh
    assert 'tmux new-session -A -s main' in shell and 'umask 077' in shell
    assert 'd /run/adp-dev-box-tmux 0700 ec2-user ec2-user -' in shell
    assert 'systemd-tmpfiles --create /etc/tmpfiles.d/adp-dev-box-tmux.conf' in shell
    assert 'export TMUX_TMPDIR=/run/adp-dev-box-tmux' in shell
    assert '[ ! -O /run/adp-dev-box-tmux ]' in shell and '[ -L /run/adp-dev-box-tmux ]' in shell
    assert 'tmux -S' not in shell
    for script in (IMAGE / 'prepare_shell.sh', IMAGE / 'prepare_ssh.sh'):
        subprocess.run(['bash', '-n', str(script)], check=True)


def test_owner_claude_preapproves_only_adp_tools_without_persisting_a_key():
    source = (IMAGE / 'prepare_shell.sh').read_text()
    settings = source.split("cat > /home/ec2-user/.claude/settings.json <<'SETTINGS'\n", 1)[1].split('\nSETTINGS', 1)[0]
    assert json.loads(settings) == {'permissions': {'allow': ['mcp__adp-tools']}}
    assert 'install -d -m 0700 -o ec2-user -g ec2-user /home/ec2-user/.claude' in source
    assert 'install -m 0600 -o ec2-user -g ec2-user /dev/null /home/ec2-user/.claude/settings.json' in source
    assert 'ANTHROPIC_API_KEY' not in settings and 'ADP_CREW_TOOLS_TOKEN' not in settings


def test_owner_clients_share_run_bound_tools_without_secrets_in_home():
    source = (IMAGE / 'prepare_shell.sh').read_text()
    mcp = source.split("cat > /home/ec2-user/.claude.json <<'MCP'\n", 1)[1].split('\nMCP', 1)[0]
    shim = {'command': '/opt/adp/crew/current/venv/bin/python3',
            'args': ['/opt/adp/crew/current/host/adp/adp-mcp.py']}
    assert json.loads(mcp) == {'mcpServers': {'adp-tools': shim}}
    assert 'install -m 0600 -o ec2-user -g ec2-user /dev/null /home/ec2-user/.claude.json' in source
    config = source.split("cat > /home/ec2-user/.codex/config.toml <<'CONFIG'\n", 1)[1].split('\nCONFIG', 1)[0]
    assert tomllib.loads(config)['mcp_servers']['adp-tools'] == shim
    assert 'ADP_CREW_TOOLS_TOKEN' not in mcp + config
    assert 'export ADP_CREW_TOOLS_URL="$ANTHROPIC_BASE_URL" ADP_CREW_TOOLS_TOKEN="$ANTHROPIC_API_KEY" ADP_CREW_RUN_ID' in source


def test_owner_profile_seeds_both_clients_without_a_human_session(tmp_path):
    import shutil

    profile = embedded_script((IMAGE / 'prepare_shell.sh').read_text(), 'adp-dev-box-tools.sh')
    assert 'export PATH="/opt/adp/crew/current/host/adp:' in profile
    assert 'if [ -x /opt/adp/crew/current/host/adp/adp ]; then' in profile
    assert 'adp claude setup --skills-only' in profile
    assert 'adp codex setup --skills-only' in profile

    cli = Path(__file__).resolve().parents[3] / 'gateway/cli'
    installed = tmp_path / 'cli'
    installed.mkdir()
    for name in ('adp', 'adp_skills.py'):
        shutil.copy2(cli / name, installed / name)
    skills = installed / 'skills'
    (skills / 'example').mkdir(parents=True)
    (skills / 'MANIFEST.json').write_text(json.dumps({
        'schema_version': 1, 'version': 'fixture',
        'skills': [{'name': 'example', 'clients': ['claude-code', 'codex'], 'invocation': 'Use example'}],
    }))
    (skills / 'example/SKILL.md').write_text('Fixture skill\n')
    home = tmp_path / 'owner'
    home.mkdir()
    env = {**os.environ, 'HOME': str(home), 'ADP_CREW_TOOLS_URL': 'http://127.0.0.1:12345',
           'ADP_CREW_TOOLS_TOKEN': 'a' * 64, 'ADP_CREW_RUN_ID': '11111111-2222-4333-8444-555555555555'}
    for key in ('ADP_DEPLOYMENT', 'ADP_DEPLOYMENT_ID', 'ADP_TENANT', 'ADP_TENANT_ID'):
        env.pop(key, None)
    for client in ('claude', 'codex'):
        result = subprocess.run(['bash', str(installed / 'adp'), client, 'setup', '--skills-only'],
                                env=env, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        folder = '.claude' if client == 'claude' else '.codex'
        assert (home / folder / 'skills/example/SKILL.md').read_text() == 'Fixture skill\n'
    assert not (home / '.bedrock-gateway/tokens.json').exists()
    assert not (home / '.claude/settings.json').exists()
    assert not (home / '.codex/config.toml').exists()
    denied = subprocess.run(['bash', str(installed / 'adp'), 'claude', 'setup'], env=env, text=True, capture_output=True)
    assert denied.returncode == 2
    assert not (home / '.bedrock-gateway/tokens.json').exists()


def test_owner_claude_uses_private_runtime_configuration_without_login(tmp_path):
    source = (IMAGE / 'prepare_shell.sh').read_text()
    assert 'chmod 0644 /etc/profile.d/adp-dev-box-tools.sh' in source
    assert 'export ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL' in source, 'Claude Code /model aliases resolve to gateway ids'
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    config = runtime / 'model.env'
    config.write_text('ANTHROPIC_BASE_URL=http://127.0.0.1:12345\n'
                      'ANTHROPIC_API_KEY=synthetic-boot-key\nANTHROPIC_MODEL=approved-model\nADP_CREW_RUN_ID=11111111-2222-4333-8444-555555555555\n')
    config.chmod(0o600)
    profile = tmp_path / 'tools.sh'
    profile.write_text(embedded_script(source, 'adp-dev-box-tools.sh').replace('/run/adp-dev-box-shell', str(runtime)))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, script in (('id', '#!/bin/sh\necho ec2-user\n'),
                         ('claude', '#!/bin/sh\nprintf "%s|%s|%s|%s|%s|%s|%s" "$ANTHROPIC_BASE_URL" "$ANTHROPIC_MODEL" "$ANTHROPIC_API_KEY" "$*" "$ADP_CREW_TOOLS_URL" "$ADP_CREW_TOOLS_TOKEN" "$ADP_CREW_RUN_ID"\n')):
        executable = bin_dir / name
        executable.write_text(script)
        executable.chmod(0o755)

    def invoke():
        return subprocess.run(['bash', '--noprofile', '--norc', '-c', '. "$1"; claude -p pong', '_', str(profile)],
                              env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}, text=True, capture_output=True)

    result = invoke()
    assert result.returncode == 0
    assert result.stdout == ('http://127.0.0.1:12345|approved-model|synthetic-boot-key|-p pong|'
                             'http://127.0.0.1:12345|synthetic-boot-key|11111111-2222-4333-8444-555555555555')
    config.chmod(0o644)
    result = invoke()
    assert result.returncode == 1 and not result.stdout
    assert 'private runtime configuration missing' in result.stderr
    config.chmod(0o600)
    config.unlink()
    config.symlink_to(bin_dir / 'claude')
    assert 'private runtime configuration missing' in invoke().stderr
    config.unlink()
    assert 'private runtime configuration missing' in invoke().stderr
    assert not invoke().stdout


def test_owner_codex_uses_nonsecret_profile_and_runtime_adapter(tmp_path):
    source = (IMAGE / 'prepare_shell.sh').read_text()
    config = source.split("cat > /home/ec2-user/.codex/config.toml <<'CONFIG'\n", 1)[1].split('\nCONFIG', 1)[0]
    settings = tomllib.loads(config)
    assert settings['model_provider'] == 'adp-shell'
    assert settings['model_providers']['adp-shell']['wire_api'] == 'responses'
    assert settings['model_providers']['adp-shell']['env_key'] == 'OPENAI_API_KEY'
    assert settings['projects']['/home/ec2-user']['trust_level'] == 'trusted', 'owner home pre-trusted: no Codex setup screen (#8719)'
    assert settings['model_providers']['adp-shell']['base_url'] == 'http://127.0.0.1:1/v1'
    # GPT via Bedrock rejects Codex's default hosted web_search tool; the config turns it off (#8859).
    assert settings['web_search'] == 'disabled'
    assert 'install -m 0600 -o ec2-user -g ec2-user /dev/null /home/ec2-user/.codex/config.toml' in source
    assert 'synthetic-boot-key' not in config
    runtime = tmp_path / 'runtime'
    runtime.mkdir(mode=0o700)
    secret = runtime / 'model.env'
    secret.write_text('ANTHROPIC_BASE_URL=http://127.0.0.1:12345\n'
                      'ANTHROPIC_API_KEY=synthetic-boot-key\nANTHROPIC_MODEL=approved-model\nADP_CREW_RUN_ID=11111111-2222-4333-8444-555555555555\n')
    secret.chmod(0o600)
    profile = tmp_path / 'tools.sh'
    profile.write_text(embedded_script(source, 'adp-dev-box-tools.sh').replace('/run/adp-dev-box-shell', str(runtime)))
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, script in (('id', '#!/bin/sh\necho ec2-user\n'),
                         ('codex', '#!/bin/sh\nprintf "%s\\n" "$OPENAI_API_KEY" "$@"\n')):
        executable = bin_dir / name
        executable.write_text(script)
        executable.chmod(0o755)

    def invoke():
        return subprocess.run(['bash', '--noprofile', '--norc', '-c',
                               '. "$1"; codex exec --skip-git-repo-check pong', '_', str(profile)],
                              env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
                              text=True, capture_output=True)

    result = invoke()
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['synthetic-boot-key', '-c', 'model_provider="adp-shell"', '-c',
                                          'model="approved-model"', '-c',
                                          'model_providers.adp-shell.base_url="http://127.0.0.1:12345/v1"',
                                          'exec', '--skip-git-repo-check', 'pong']
    secret.unlink()
    result = invoke()
    assert result.returncode == 1 and 'private runtime configuration missing' in result.stderr
    assert not result.stdout


def test_tmux_pane_unsets_inherited_idle_timeout_while_external_shell_keeps_it():
    idle = embedded_script((IMAGE / 'prepare_ssh.sh').read_text(), 'adp-dev-box-idle.sh')
    for inside, expected in (('in-session', 'unset'), ('', '900')):
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                                 f'{idle}\nprintf "%s" "${{TMOUT-unset}}"'],
                                env={**os.environ, 'TMUX': inside}, capture_output=True, text=True, check=True)
        assert result.stdout == expected


@pytest.mark.parametrize('interactive,owner,nested,should_attach', [
    (True, 'ec2-user', False, True),
    (False, 'ec2-user', False, False),
    (True, 'agent', False, False),
    (True, 'ec2-user', True, False),
])
def test_only_owner_interactive_outer_shell_attaches(tmp_path, interactive, owner, nested, should_attach):
    profile = tmp_path / 'profile.sh'
    runtime = tmp_path / 'volatile' / 'tmux-owner'
    runtime.parent.mkdir()
    runtime.mkdir(mode=0o700)
    profile.write_text(embedded_script((IMAGE / 'prepare_shell.sh').read_text(), 'adp-dev-box-tmux.sh')
                       .replace('/run/adp-dev-box-tmux', str(runtime)))
    tools = tmp_path / 'bin'
    tools.mkdir()
    identity = tools / 'id'
    identity.write_text(f'#!/bin/sh\necho {owner}\n')
    identity.chmod(0o755)
    tmux = tools / 'tmux'
    tmux.write_text('#!/bin/sh\nprintf "%s|%s" "$*" "$TMUX_TMPDIR" > "$TRACE_FILE"\n')
    tmux.chmod(0o755)
    trace = tmp_path / 'tmux-args'
    env = {**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}', 'TRACE_FILE': str(trace), 'TMUX_TMPDIR': '/tmp/inherited-shared'}
    if nested:
        env['TMUX'] = '/tmp/tmux-private'
    else:
        env.pop('TMUX', None)
    command = ['bash', '--noprofile', '--norc', '-ic' if interactive else '-c', f'source "{profile}"; echo ready']
    if interactive:
        master, slave = pty.openpty()
        try:
            result = subprocess.run(command, stdin=slave, stdout=slave, stderr=slave, env=env, timeout=5, check=False)
        finally:
            os.close(slave)
            os.close(master)
    else:
        result = subprocess.run(command, input='', capture_output=True, text=True, env=env, timeout=5, check=False)
    assert result.returncode == 0
    assert trace.exists() is should_attach
    if should_attach:
        assert trace.read_text() == f'new-session -A -s main|{runtime}'


@pytest.mark.parametrize('runtime_state', ['missing', 'public', 'symlink'])
def test_owner_refuses_missing_or_unsafe_tmux_runtime(tmp_path, runtime_state):
    runtime = tmp_path / 'runtime'
    if runtime_state == 'public':
        runtime.mkdir(mode=0o755)
    elif runtime_state == 'symlink':
        target = tmp_path / 'target'
        target.mkdir(mode=0o700)
        runtime.symlink_to(target, target_is_directory=True)
    profile = tmp_path / 'profile.sh'
    profile.write_text(embedded_script((IMAGE / 'prepare_shell.sh').read_text(), 'adp-dev-box-tmux.sh')
                       .replace('/run/adp-dev-box-tmux', str(runtime)))
    tools = tmp_path / 'bin'
    tools.mkdir()
    identity = tools / 'id'
    identity.write_text('#!/bin/sh\necho ec2-user\n')
    identity.chmod(0o755)
    tmux = tools / 'tmux'
    tmux.write_text('#!/bin/sh\necho attach > "$TRACE_FILE"\n')
    tmux.chmod(0o755)
    trace = tmp_path / 'tmux-trace'
    master, slave = pty.openpty()
    try:
        result = subprocess.run(['bash', '--noprofile', '--norc', '-ic', f'source "{profile}"'],
                                stdin=slave, stdout=slave, stderr=slave,
                                env={**os.environ, 'PATH': f'{tools}:{os.environ["PATH"]}', 'TRACE_FILE': str(trace)},
                                timeout=5, check=False)
    finally:
        os.close(slave)
        os.close(master)
    assert result.returncode == 1
    assert not trace.exists()



def test_owner_shell_acceptance_procedure_matches_cli_and_usage_contracts():
    repository = IMAGE.parents[3]
    guide = (repository / 'docs/adp-platform-deployment/deployment-reference.md').read_text()
    procedure = guide.split('### Dev Box owner-shell acceptance (#8559)', 1)[1].split('\n## Operator-set configuration', 1)[0]
    cli = (repository / 'modules/gateway/cli/adp-devbox.py').read_text()
    shell = (IMAGE / 'prepare_shell.sh').read_text()
    usage = (repository / 'modules/gateway/src/shared/models/usage.py').read_text()
    attribution = (repository / 'modules/gateway/src/usage/service.py').read_text()
    owner_script = procedure.split("**Owner's SSH/SSM terminal", 1)[1].split('```sh\n', 1)[1].split('\n```', 1)[0]

    assert 'choices=("proxy", "ssh", "ssh-config")' in cli
    assert 'adp devbox ssh-config <box-name>' in procedure
    assert 'adp devbox ssh <box-name>' in procedure
    assert 'claude() {' in shell and 'codex() {' in shell
    assert "claude -p 'Reply with the single word pong.'" in owner_script
    assert "codex exec --skip-git-repo-check 'Reply with the single word pong.'" in owner_script
    assert 'code_interpreter' in owner_script and '6*7' in owner_script
    assert owner_script.index('date -u') < owner_script.index('claude -p')
    assert 'export ADP_CREW_TOOLS_URL=' in shell and 'ADP_CREW_RUN_ID' in shell
    assert "printf 'Run ID: %s\\n' \"$ADP_CREW_RUN_ID\"" in owner_script
    assert 'adp login' not in owner_script and 'ANTHROPIC_API_KEY' not in owner_script
    subprocess.run(['bash', '-n'], input=owner_script, text=True, check=True)
    for field in ('org_id', 'agent_run_id', 'root_human_id', 'timestamp', 'input_tokens', 'output_tokens', 'cost_usd', 'status_code'):
        assert field in usage and field in procedure
    assert '_crew_root_human_id' in attribution and '_crew_root_run_id' in attribution
    assert 'FROM usage_logs' in procedure and 'agent_run_id = :run_id' in procedure
    assert 'root_human_id` exactly equal to `:owner_user_id`' in ' '.join(procedure.split())
    assert 'POST /cloud-desktops/{desktop_id}/stop' in procedure
    assert 'under 512 MB' in procedure
