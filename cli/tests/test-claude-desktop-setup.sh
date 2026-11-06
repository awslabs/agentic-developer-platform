#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
temporary_home="$(mktemp -d)"
trap 'rm -rf "${temporary_home}"' EXIT
export HOME="${temporary_home}"
export ADP_HOME="${HOME}/.adp"
unset ADP_DEPLOYMENT ADP_DEPLOYMENT_ID BG_CONFIG_DIR ADP_TENANT ADP_TENANT_ID

mkdir -p "${HOME}/cli"
adp="${HOME}/cli/adp"
cp "${root}/modules/gateway/cli/adp" "${adp}"
chmod 700 "${adp}"
ln -s "${root}/modules/gateway/cli/bg-cognito-auth.sh" "${HOME}/cli/bg-cognito-auth.sh"
ln -s "${root}/modules/gateway/cli/adp_deployments.py" "${HOME}/cli/adp_deployments.py"

mkdir -p "${HOME}/.config/Claude" "${HOME}/.claude"
printf '{"mcpServers":{"saved":{"command":"existing"}},"other":true}\n' >"${HOME}/.config/Claude/claude_desktop_config.json"
printf '{"permissions":{"allow":["Read"]}}\n' >"${HOME}/.claude/settings.json"
cp "${HOME}/.config/Claude/claude_desktop_config.json" "${HOME}/saved-desktop-settings"
cp "${HOME}/.claude/settings.json" "${HOME}/saved-claude-settings"
mkdir -p "${HOME}/.bedrock-gateway"
printf '{"gateway_url":"https://gateway.example.test/api"}\n' >"${HOME}/.bedrock-gateway/config.json"
printf '{"access_token":"header.payload.signature","expires_at":%s}\n' "$(( $(date +%s) + 3600 ))" >"${HOME}/.bedrock-gateway/tokens.json"

bash "${adp}" claude-desktop setup >"${HOME}/first-output"
helper="${HOME}/.adp/bin/adp-claude-desktop-token"
test -x "${helper}"
python3 - "${helper}" <<'PY'
import os
import stat
import sys

assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o700
PY
first_modified="$(python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "${helper}")"
first_hash="$(python3 - "${helper}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
PY
)"
bash "${adp}" claude-desktop setup >"${HOME}/second-output"
second_hash="$(python3 - "${helper}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())
PY
)"
test "${first_hash}" = "${second_hash}"
second_modified="$(python3 -c 'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns)' "${helper}")"
test "${first_modified}" = "${second_modified}"
cmp -s "${HOME}/first-output" "${HOME}/second-output"
cmp -s "${HOME}/saved-desktop-settings" "${HOME}/.config/Claude/claude_desktop_config.json"
cmp -s "${HOME}/saved-claude-settings" "${HOME}/.claude/settings.json"
grep -Fq 'Provider: ADP gateway (Anthropic Messages)' "${HOME}/first-output"
grep -Fq 'Base URL: https://gateway.example.test/api' "${HOME}/first-output"
grep -Fq 'Authentication: Bearer token' "${HOME}/first-output"
grep -Fq "Credential helper: ${helper}" "${HOME}/first-output"
grep -Fq 'Models: https://gateway.example.test/api/v1/models?client=claude-desktop' "${HOME}/first-output"
test -f "${helper}"
(
    cd /
    env -i HOME="${HOME}" ADP_HOME="${ADP_HOME}" PATH=/usr/bin:/bin "${helper}" >"${HOME}/valid-output" 2>"${HOME}/valid-error"
)
cmp -s "${HOME}/valid-output" <(printf 'header.payload.signature\n')
test "$(wc -l <"${HOME}/valid-output")" -eq 1
test ! -s "${HOME}/valid-error"

printf '{"gateway_url":"https://gateway.example.test/api","refresh_via":"direct"}\n' >"${HOME}/.bedrock-gateway/config.json"
printf '{"access_token":"header.payload.signature","expires_at":0}\n' >"${HOME}/.bedrock-gateway/tokens.json"
status=0
env -i HOME="${HOME}" ADP_HOME="${ADP_HOME}" AWS_EC2_METADATA_DISABLED=true PATH=/usr/bin:/bin "${helper}" >"${HOME}/expired-output" 2>"${HOME}/expired-error" || status=$?
test "${status}" -eq 1
test ! -s "${HOME}/expired-output"
test -s "${HOME}/expired-error"
! grep -Fq 'header.payload.signature' "${HOME}/expired-error"
bash "${adp}" deployment add alpha --url https://alpha.example.test >/dev/null
bash "${adp}" deployment add beta --url https://beta.example.test >/dev/null
alpha_id="$(jq -r '.deployments.alpha.id' "${ADP_HOME}/deployments.json")"
beta_id="$(jq -r '.deployments.beta.id' "${ADP_HOME}/deployments.json")"
alpha_store="${ADP_HOME}/deployments/${alpha_id}"
beta_store="${ADP_HOME}/deployments/${beta_id}"
printf '{"gateway_url":"https://alpha.example.test/api"}\n' >"${alpha_store}/config.json"
printf '{"access_token":"header.alpha.signature","expires_at":%s}\n' "$(( $(date +%s) + 3600 ))" >"${alpha_store}/tokens.json"
printf '{"gateway_url":"https://beta.example.test/api"}\n' >"${beta_store}/config.json"
printf '{"access_token":"header.beta.signature","expires_at":%s}\n' "$(( $(date +%s) + 3600 ))" >"${beta_store}/tokens.json"
bash "${adp}" deployment use alpha >/dev/null
bash "${adp}" --deployment alpha claude-desktop setup >/dev/null
bash "${adp}" deployment use beta >/dev/null
env -i HOME="${HOME}" ADP_HOME="${ADP_HOME}" ADP_DEPLOYMENT=beta PATH=/usr/bin:/bin "${helper}" >"${HOME}/pinned-output" 2>"${HOME}/pinned-error"
cmp -s "${HOME}/pinned-output" <(printf 'header.alpha.signature\n')
test ! -s "${HOME}/pinned-error"
gateway_port_file="${HOME}/gateway-port"
gateway_requests="${HOME}/gateway-requests"
scoped_credentials="${HOME}/scoped-credentials"
python3 -u - "${gateway_port_file}" "${gateway_requests}" "${scoped_credentials}" <<'SERVER' &
import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port_file, requests_file, credentials_file = sys.argv[1:]
payload = base64.urlsafe_b64encode(json.dumps({"sub": "desktop-user", "iss": "https://issuer.example.test"}).encode()).decode().rstrip("=")
access_token = "header." + payload + ".signature"
context_token = "context.payload.signature"
scoped_token = "adpctx1~" + context_token + "~" + access_token
with open(credentials_file, "w", encoding="utf-8") as credentials:
    json.dump({"access_token": access_token, "scoped_token": scoped_token}, credentials)

class Gateway(BaseHTTPRequestHandler):
    def do_GET(self):
        with open(requests_file, "a", encoding="utf-8") as requests:
            requests.write(self.path + "\n")
        if self.path == "/api/workspaces":
            authorized = self.headers.get("Authorization") == "Bearer " + access_token
            self.send_response(200 if authorized else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"items": [{"org_id": "desktop-tenant", "name": "Desktop tenant"}]} if authorized else {}).encode())
            return
        authorized = self.headers.get("Authorization") in ("Bearer header.gateway.signature", "Bearer " + scoped_token)
        correct_path = self.path == "/api/v1/models?client=claude-desktop"
        self.send_response(200 if authorized and correct_path else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = {"object": "list", "data": [{"id": "global.anthropic.claude-sonnet-4-6"}, {"id": "global.anthropic.claude-haiku-4-5"}]}
        self.wfile.write(json.dumps(body if authorized and correct_path else {"error": "unauthorized"}).encode())

    def do_POST(self):
        with open(requests_file, "a", encoding="utf-8") as requests:
            requests.write(self.path + "\n")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        authorized = (
            self.path == "/api/workspaces/context"
            and self.headers.get("Authorization") == "Bearer " + access_token
            and body.get("org_id") == "desktop-tenant"
            and body.get("expected_membership", "desktop-membership") == "desktop-membership"
        )
        self.send_response(200 if authorized else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        lease = {
            "tenant_id": "desktop-tenant",
            "identity": "desktop-user",
            "membership_id": "desktop-membership",
            "context_token": context_token,
        }
        self.wfile.write(json.dumps(lease if authorized else {}).encode())

    def log_message(self, format, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Gateway)
with open(port_file, "w", encoding="utf-8") as port:
    port.write(str(server.server_port))
server.serve_forever()
SERVER
gateway_pid=$!
trap 'kill "${gateway_pid}" 2>/dev/null || true; rm -rf "${temporary_home}"' EXIT
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    test -s "${gateway_port_file}" && break
    sleep 0.1
done
test -s "${gateway_port_file}"
gateway_url="http://127.0.0.1:$(cat "${gateway_port_file}")/api"
bash "${adp}" deployment add gateway --url "${gateway_url}" >/dev/null
gateway_id="$(jq -r '.deployments.gateway.id' "${ADP_HOME}/deployments.json")"
gateway_store="${ADP_HOME}/deployments/${gateway_id}"
printf '{"gateway_url":"%s"}\n' "${gateway_url}" >"${gateway_store}/config.json"
printf '{"access_token":"header.gateway.signature","expires_at":%s}\n' "$(( $(date +%s) + 3600 ))" >"${gateway_store}/tokens.json"
bash "${adp}" --deployment gateway claude-desktop setup --json >"${HOME}/setup-json"
jq -e --arg path "${helper}" --arg url "${gateway_url}" \
    '.helper_path == $path and .base_url == $url and .deployment == "gateway" and .settings_written == false' \
    "${HOME}/setup-json" >/dev/null
bash "${adp}" --deployment gateway claude-desktop setup --check >"${HOME}/check-output" 2>"${HOME}/check-error"
cmp -s "${HOME}/check-output" <(printf 'HTTP status: 200\nModel count: 2\n')
test ! -s "${HOME}/check-error"
test "$(wc -l <"${gateway_requests}")" -eq 1
grep -Fxq '/api/v1/models?client=claude-desktop' "${gateway_requests}"
! grep -Fq 'header.gateway.signature' "${HOME}/check-output"
bash "${adp}" --deployment gateway claude-desktop setup --json --check >"${HOME}/check-json"
jq -e '.http_status == 200 and .model_count == 2 and .settings_written == false' "${HOME}/check-json" >/dev/null
! grep -Fq 'header.gateway.signature' "${HOME}/check-json"
test "$(wc -l <"${gateway_requests}")" -eq 2
printf '{"access_token":"header.invalid.signature","expires_at":%s}\n' "$(( $(date +%s) + 3600 ))" >"${gateway_store}/tokens.json"
status=0
bash "${adp}" --deployment gateway claude-desktop setup --check >"${HOME}/denied-output" 2>"${HOME}/denied-error" || status=$?
test "${status}" -eq 1
test ! -s "${HOME}/denied-output"
test -s "${HOME}/denied-error"
! grep -Fq 'header.invalid.signature' "${HOME}/denied-error"
test "$(wc -l <"${gateway_requests}")" -eq 3
printf '{"access_token":"invalid-token","expires_at":%s}\n' "$(( $(date +%s) + 3600 ))" >"${gateway_store}/tokens.json"
status=0
bash "${adp}" --deployment gateway claude-desktop setup --check >"${HOME}/bad-helper-output" 2>"${HOME}/bad-helper-error" || status=$?
test "${status}" -eq 1
test ! -s "${HOME}/bad-helper-output"
! grep -Fq 'invalid-token' "${HOME}/bad-helper-error"
test "$(wc -l <"${gateway_requests}")" -eq 3
status=0
bash "${adp}" --deployment absent claude-desktop setup >"${HOME}/missing-output" 2>"${HOME}/missing-error" || status=$?
test "${status}" -eq 2
test ! -s "${HOME}/missing-output"
rm "${gateway_store}/tokens.json"
status=0
bash "${adp}" --deployment gateway claude-desktop setup >"${HOME}/no-login-output" 2>"${HOME}/no-login-error" || status=$?
test "${status}" -eq 3
test ! -s "${HOME}/no-login-output"
grep -Fq 'login' "${HOME}/no-login-error"

cp "${root}/modules/gateway/cli/adp-tenant.py" "${root}/modules/gateway/cli/adp_common.py" "${HOME}/cli/"
jq --argjson expires "$(( $(date +%s) + 3600 ))" \
    '{access_token: .access_token, expires_at: $expires}' "${scoped_credentials}" >"${gateway_store}/tokens.json"
bash "${adp}" --deployment gateway token >"${HOME}/scoped-cli-output"
cmp -s "${HOME}/scoped-cli-output" <(jq -jr '.scoped_token' "${scoped_credentials}")

dependency_bin="${HOME}/Homebrew dependencies' bin"
mkdir -p "${dependency_bin}"
for dependency in python3 jq; do
    dependency_path="$(command -v "${dependency}")"
    python3 - "${dependency}" "${dependency_path}" >"${dependency_bin}/${dependency}" <<'PY'
import shlex
import sys

print('#!/bin/sh')
print('printf "%s\\n" ' + shlex.quote(sys.argv[1]) + ' >>"${HOME}/dependency-calls"')
print('exec ' + shlex.quote(sys.argv[2]) + ' "$@"')
PY
    chmod 700 "${dependency_bin}/${dependency}"
done
PATH="${dependency_bin}:${PATH}" bash "${adp}" --deployment gateway claude-desktop setup >"${HOME}/scoped-setup-output"
: >"${HOME}/dependency-calls"
: >"${gateway_requests}"
(
    cd /
    env -i HOME="${HOME}" ADP_HOME="${ADP_HOME}" ADP_DEPLOYMENT=beta PATH=/usr/bin:/bin \
        "${helper}" >"${HOME}/scoped-helper-output" 2>"${HOME}/scoped-helper-error"
)
cmp -s "${HOME}/scoped-helper-output" <(jq -r '.scoped_token' "${scoped_credentials}")
test "$(wc -l <"${HOME}/scoped-helper-output")" -eq 1
test ! -s "${HOME}/scoped-helper-error"
grep -Fxq python3 "${HOME}/dependency-calls"
grep -Fxq jq "${HOME}/dependency-calls"
grep -Fxq '/api/workspaces' "${gateway_requests}"
grep -Fxq '/api/workspaces/context' "${gateway_requests}"
PATH="${dependency_bin}:${PATH}" bash "${adp}" --deployment gateway claude-desktop setup --check \
    >"${HOME}/scoped-check-output" 2>"${HOME}/scoped-check-error"
cmp -s "${HOME}/scoped-check-output" <(printf 'HTTP status: 200\nModel count: 2\n')
test ! -s "${HOME}/scoped-check-error"
test "$(grep -Fxc '/api/v1/models?client=claude-desktop' "${gateway_requests}")" -eq 1
for output in scoped-setup-output scoped-check-output scoped-check-error; do
    ! grep -Fq 'adpctx1~' "${HOME}/${output}"
    ! grep -Fq "$(jq -r '.access_token' "${scoped_credentials}")" "${HOME}/${output}"
done

rm "${HOME}/cli/adp-tenant.py" "${HOME}/cli/adp_common.py"
models_before="$(grep -Fxc '/api/v1/models?client=claude-desktop' "${gateway_requests}")"
for invalid_token in \
    'adpctx1~context.payload.signature' \
    'adpctx1~context.payload.signature~header.payload.signature~extra' \
    'adpctx1~invalid~header.payload.signature' \
    $'header.payload.signature\nheader.payload.signature' \
    $'header.payload.signature\r' \
    $'header.payload.signature\t' \
    $'header.payload.signature\033'; do
    jq -n --arg token "${invalid_token}" --argjson expires "$(( $(date +%s) + 3600 ))" \
        '{access_token: $token, expires_at: $expires}' >"${gateway_store}/tokens.json"
    status=0
    "${helper}" >"${HOME}/malformed-output" 2>"${HOME}/malformed-error" || status=$?
    test "${status}" -eq 1
    test ! -s "${HOME}/malformed-output"
    test -s "${HOME}/malformed-error"
    ! grep -Eq 'header[.]payload|context[.]payload|adpctx1~' "${HOME}/malformed-error"
    status=0
    bash "${adp}" --deployment gateway claude-desktop setup --check \
        >"${HOME}/malformed-check-output" 2>"${HOME}/malformed-check-error" || status=$?
    test "${status}" -eq 1
    test ! -s "${HOME}/malformed-check-output"
    test -s "${HOME}/malformed-check-error"
    ! grep -Eq 'header[.]payload|context[.]payload|adpctx1~' "${HOME}/malformed-check-error"
done
test "$(grep -Fxc '/api/v1/models?client=claude-desktop' "${gateway_requests}")" -eq "${models_before}"

bash "${adp}" --help | grep -Fq 'claude-desktop setup [--check] [--json]'
grep -Fq 'COPY cli/ cli/' "${root}/modules/gateway/Dockerfile"
grep -Fq '"adp": (_CLI_DIR / "adp").resolve()' "${root}/modules/gateway/src/cli_download/routes.py"
grep -Fq 'CLI_FILES="adp ' "${root}/modules/gateway/cli/install.sh"
grep -Fq "modules/gateway/cli/**" "${root}/.github/workflows/gateway-ci.yml"
printf 'PASS: repeat setup, pinned helper, and secret-free models check\n'
