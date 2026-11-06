#!/usr/bin/env bash
set -euo pipefail

# git from the AL2023 repository (the build instance reaches those, not cli.github.com: the
# GitHub CLI ships in the Crew bundle as host/gh-cli/gh behind the host/gh wrapper, #8555).
# bubblewrap: Codex 0.164 warns on every start without it and falls back to a bundled copy.
# Amazon Linux 2023 ships curl-minimal, which provides /usr/bin/curl and
# conflicts with the full curl package; installing "curl" fails the image build.
# The adp CLI only needs the binary, so install the rest and verify curl exists.
dnf install -y tmux git bubblewrap jq
rpm -q tmux git bubblewrap jq >/dev/null
command -v curl >/dev/null || dnf install -y --allowerasing curl
command -v curl >/dev/null

# git on the box authenticates to github.com as the owner through the bundle's credential helper
# (#8555): system-wide so Kiro Crew, Claude Code, Codex and the owner shell all use it, no token
# ever lands in ~/.git-credentials. useHttpPath=false: one owner token covers every repository.
git config --system credential.https://github.com.helper /opt/adp/crew/current/host/git-credential-adp
git config --system credential.https://github.com.useHttpPath false

tmux_config=/home/ec2-user/.tmux.conf
install -m 0600 -o ec2-user -g ec2-user /dev/null "$tmux_config"
cat > "$tmux_config" <<'CONFIG'
set -g mouse on
set -g history-limit 10000
set -g default-terminal "screen-256color"
set -g status-right "Dev Box · #{session_name} · %H:%M"
set -g default-command "env -u TMOUT /bin/bash -l"
CONFIG
chown ec2-user:ec2-user "$tmux_config"
chmod 0600 "$tmux_config"

printf '%s\n' 'd /run/adp-dev-box-tmux 0700 ec2-user ec2-user -' > /etc/tmpfiles.d/adp-dev-box-tmux.conf
chmod 0644 /etc/tmpfiles.d/adp-dev-box-tmux.conf
systemd-tmpfiles --create /etc/tmpfiles.d/adp-dev-box-tmux.conf

cat > /etc/profile.d/adp-dev-box-tmux.sh <<'PROFILE'
if [[ $- == *i* ]] && [ -t 0 ] && [ -t 1 ] && [ "$(id -un)" = ec2-user ] && [ -z "${TMUX:-}" ]; then
  if [ ! -d /run/adp-dev-box-tmux ] || [ -L /run/adp-dev-box-tmux ] || [ ! -O /run/adp-dev-box-tmux ] ||
    [ "$(stat -c '%a' /run/adp-dev-box-tmux)" != 700 ]; then
    printf '%s\n' 'Dev Box terminal unavailable: private runtime directory missing' >&2
    exit 1
  fi
  export TMUX_TMPDIR=/run/adp-dev-box-tmux
  umask 077
  exec tmux new-session -A -s main
fi
PROFILE
chmod 0644 /etc/profile.d/adp-dev-box-tmux.sh

install -d -m 0700 -o ec2-user -g ec2-user /home/ec2-user/.codex
if [ ! -e /home/ec2-user/.codex/config.toml ] && [ ! -L /home/ec2-user/.codex/config.toml ]; then
  install -m 0600 -o ec2-user -g ec2-user /dev/null /home/ec2-user/.codex/config.toml
  cat > /home/ec2-user/.codex/config.toml <<'CONFIG'
model = "adp-unavailable"
model_provider = "adp-shell"
# Bedrock serves no hosted web-search tool for GPT models; Codex sends it by default and the whole turn is
# rejected ("web search is not supported for this request"). ADP's websearch lives in adp-tools (#8859).
web_search = "disabled"
[model_providers.adp-shell]
name = "ADP Dev Box"
base_url = "http://127.0.0.1:1/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
requires_openai_auth = false
[mcp_servers.adp-tools]
command = "/opt/adp/crew/current/venv/bin/python3"
args = ["/opt/adp/crew/current/host/adp/adp-mcp.py"]
# The owner's home is theirs: skip Codex's first-run "trust this folder?" setup screen there (#8719).
[projects."/home/ec2-user"]
trust_level = "trusted"
CONFIG
fi

install -d -m 0700 -o ec2-user -g ec2-user /home/ec2-user/.claude
if [ ! -e /home/ec2-user/.claude/settings.json ] && [ ! -L /home/ec2-user/.claude/settings.json ]; then
  install -m 0600 -o ec2-user -g ec2-user /dev/null /home/ec2-user/.claude/settings.json
  cat > /home/ec2-user/.claude/settings.json <<'SETTINGS'
{
  "permissions": { "allow": ["mcp__adp-tools"] }
}
SETTINGS
fi

if [ ! -e /home/ec2-user/.claude.json ] && [ ! -L /home/ec2-user/.claude.json ]; then
  install -m 0600 -o ec2-user -g ec2-user /dev/null /home/ec2-user/.claude.json
  cat > /home/ec2-user/.claude.json <<'MCP'
{
  "mcpServers": {
    "adp-tools": {
      "command": "/opt/adp/crew/current/venv/bin/python3",
      "args": ["/opt/adp/crew/current/host/adp/adp-mcp.py"]
    }
  }
}
MCP
fi

cat > /etc/profile.d/adp-dev-box-tools.sh <<'PROFILE'
if [ "$(id -un)" = ec2-user ]; then
  export AWS_CONFIG_FILE=/run/adp-crew-aws/config AWS_EC2_METADATA_DISABLED=true
  export AWS_SHARED_CREDENTIALS_FILE=/dev/null
  export PATH="/opt/adp/crew/current/host/adp:/opt/adp/crew/current/host:/opt/adp/crew/current/clients/node_modules/.bin:/opt/adp/crew/current/venv/bin:$PATH"
  if [ -x /opt/adp/crew/current/host/adp/adp ]; then
    adp claude setup --skills-only
    adp codex setup --skills-only
  fi
  adp_dev_box_model() {
    local directory=/run/adp-dev-box-shell
    local settings="$directory/model.env"
    if [ ! -d "$directory" ] || [ -L "$directory" ] || [ ! -O "$directory" ] ||
      [ "$(stat -c '%a' "$directory" 2>/dev/null)" != 700 ] ||
      [ ! -f "$settings" ] || [ -L "$settings" ] || [ ! -O "$settings" ] ||
      [ "$(stat -c '%a' "$settings" 2>/dev/null)" != 600 ]; then
      printf '%s\n' 'Dev Box model access unavailable: private runtime configuration missing' >&2
      return 1
    fi
    unset ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL OPENAI_API_KEY ADP_CREW_RUN_ID ADP_CREW_TOOLS_URL ADP_CREW_TOOLS_TOKEN \
      ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
    . "$settings"
    if [ -z "${ANTHROPIC_API_KEY:-}" ] || [ -z "${ANTHROPIC_MODEL:-}" ] ||
      ! [[ "${ADP_CREW_RUN_ID:-}" =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]] ||
      [[ "${ANTHROPIC_BASE_URL:-}" != http://127.0.0.1:* ]]; then
      printf '%s\n' 'Dev Box model access unavailable: invalid runtime configuration' >&2
      return 1
    fi
    export ANTHROPIC_BASE_URL ANTHROPIC_API_KEY ANTHROPIC_MODEL
    # Claude Code's /model opus|sonnet|haiku resolve to the gateway's model ids (published per boot); the
    # gateway applies your entitlement, so you choose the model as you would from a laptop.
    export ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL
    export ADP_CREW_TOOLS_URL="$ANTHROPIC_BASE_URL" ADP_CREW_TOOLS_TOKEN="$ANTHROPIC_API_KEY" ADP_CREW_RUN_ID
  }
  claude() {
    adp_dev_box_model || return
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_TELEMETRY=1
    command claude "$@"
  }
  codex() {
    adp_dev_box_model || return
    export OPENAI_API_KEY="$ANTHROPIC_API_KEY"
    command codex -c 'model_provider="adp-shell"' -c "model=\"$ANTHROPIC_MODEL\"" \
      -c "model_providers.adp-shell.base_url=\"${ANTHROPIC_BASE_URL}/v1\"" "$@"
  }
fi
PROFILE
chmod 0644 /etc/profile.d/adp-dev-box-tools.sh

cat > /etc/sudoers.d/adp-crew-shell <<'SUDOERS'
agent ALL=(root) NOPASSWD: /usr/bin/python3 -I /opt/adp/crew/publish_shell_model.py, /usr/bin/python3 -I /opt/adp/crew/publish_shell_model.py --remove
SUDOERS
chmod 0440 /etc/sudoers.d/adp-crew-shell
visudo -cf /etc/sudoers.d/adp-crew-shell

if ! grep -Fq 'Dev Box terminal sessions' /etc/motd; then
  printf '%s\n' 'Dev Box terminal sessions survive disconnects, not instance stop/start or Replace.' >> /etc/motd
fi
