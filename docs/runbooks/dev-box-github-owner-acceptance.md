# Dev Box GitHub owner-access acceptance (delivery owner)

This is a **post-merge, deployed-dev** procedure, not a record of a completed live test.
Run only with the Dev Box delivery owner's authorized target access. Keep the target
account, deployment URL, box ID, repository names, logs, screenshots and token
material in private operator records, never in this public repository or a PR.
Do not run these steps from an agent pod or use a substitute AWS credential.

## Prepare the target

1. Record the deployed commit, UTC time, environment, operator and box identity
   privately. Confirm the release has the GitHub user-access feature enabled,
   the AgentCore GitHub user provider uses this deployment's **existing** App,
   and its callback is listed in the GitHub App's OAuth callback URLs. The
   existing App needs its client secret supplied privately. For an App created
   through the manifest UI, `register-github-app.sh --user-access --from-manifest`
   can read its stored credentials and register/replace the provider, printing
   the callback. **Check that callback in GitHub**: the manifest flow currently
   supplies the login-broker callback, not necessarily the later-created
   AgentCore callback. If an extra GitHub settings edit is needed, record this
   as an AC-06 acceptance gap rather than claiming zero extra UI steps.
2. Choose two real *private* repositories the **same owner** can access:
   `REPO_INSTALLED` in an installation of this App, and `REPO_UNINSTALLED`
   outside every installation of this App. Verify their installation status
   independently with the GitHub App settings, not by inference from a 404.
   Use an owner Dev Box with a running Crew adapter; check the shell resolves
   `adp-github-token`. Clear any alternate GitHub authentication and stored
   Git credential helpers for the probes. Never turn on shell xtrace, curl
   verbose/trace mode or HTTP header logging.
3. In the owner's browser, capture the Dev Box card before consent; click
   **Use GitHub as you on this box** and complete GitHub authorization. Record
   the return-page card (Connected as the owner), and inspect the connection
   binding privately: `observed_resources[0].kind == github_app_user`, a
   nonempty `installation_ids` list and a matching owner account ID. Redact
   IDs/login before sharing evidence. Do not treat an OAuth `repo` scope as
   App-installation evidence.

## Repository boundary — owner Dev Box shell (Terminal A)

Supply the two repository names privately as `owner/name` in this shell.
The helper normally prints a token; **never run it without redirection or
capture**, including with `--json`. The two API checks below use exactly the
same captured token; the Git commands separately exercise the box's helper.
Run in a private interactive shell, not a recorded SSM command, CI job or
terminal with command tracing. Never print, persist, paste or transmit the
captured token except to GitHub over HTTPS.

```bash
set +x
umask 077
unset GH_TOKEN GITHUB_TOKEN
GH_CONFIG_DIR=$(mktemp -d)
export GH_CONFIG_DIR
trap 'rm -rf -- "$GH_CONFIG_DIR"' EXIT
: "${REPO_INSTALLED:?set privately to owner/name}"
: "${REPO_UNINSTALLED:?set privately to owner/name}"
command -v adp-github-token >/dev/null || exit 1
type -a gh
printf 'start UTC: '; date -u '+%Y-%m-%dT%H:%M:%SZ'
token=$(adp-github-token) || exit 1
probe_repo() {
  printf 'header = "Authorization: Bearer %s"\n' "$token" |
    curl --config - --silent --show-error --output /dev/null \
      --write-out '%{http_code}\n' --header 'Accept: application/vnd.github+json' \
      "https://api.github.com/repos/$1"
}
probe_repo "$REPO_INSTALLED"
probe_repo "$REPO_UNINSTALLED"
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  git -c credential.helper= \
      -c 'credential.https://github.com.helper=!adp-github-token --git-credential get' \
      ls-remote "https://github.com/${REPO_INSTALLED}.git" HEAD >/dev/null
printf 'installed git exit: %s\n' "$?"
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  git -c credential.helper= \
      -c 'credential.https://github.com.helper=!adp-github-token --git-credential get' \
      ls-remote "https://github.com/${REPO_UNINSTALLED}.git" HEAD >/dev/null
printf 'uninstalled git exit: %s\n' "$?"
```

Record UTC time and HTTP statuses privately: installed must be 200 and its
`git ls-remote` must exit 0; uninstalled must be 403/404 and its Git command
must fail. If the uninstalled API succeeds, **fail AC-01** even if Git fails.
A `000`/network error proves neither permission nor denial; a 403 caused
by rate limiting or a global policy is not evidence of an installation bound.
Keep stderr
private and redact repository names before sharing. Do not use the owner's
personal `gh` login as a fallback.

## Aged grant, pause, disconnect — browser (Terminal B) and box (Terminal A)

While this **same connection remains active**, wait more than eight hours
from the recorded GitHub authorization. In the same Terminal A shell with
alternate `GH_TOKEN`/`GITHUB_TOKEN` unset and the clean `GH_CONFIG_DIR`, run:

```bash
printf 'aged-grant UTC: '; date -u '+%Y-%m-%dT%H:%M:%SZ'
adp-github-token >/dev/null
gh api user >/dev/null
```

A successful
request demonstrates a usable refreshed token; otherwise inspect the gateway
mint response privately: 409 `github_reconnect_required` must include a
readable reconnect message. Record timestamp, redacted gateway/box logs and
which branch occurred. A failure without a readable reason fails AC-05.
Do **not** pause or disconnect before this aged-grant check.

To test pause, first retain a fresh token in Terminal A (no output) and
verify the direct GitHub probe responds 200. Keep this shell open while using
the browser. The probe sends the token only in a private stdin-fed curl
configuration; never run curl with `-v`, `--trace`, or a token-bearing CLI flag.

```bash
token=$(adp-github-token) || exit 1
probe_user() {
  printf 'header = "Authorization: Bearer %s"\n' "$token" |
    curl --config - --silent --show-error --output /dev/null \
      --write-out '%{http_code}\n' --header 'Accept: application/vnd.github+json' \
      https://api.github.com/user
}
probe_user
```

In Terminal B switch **Allow agents to use this account** off
for this owner's GitHub credential (the owner's
`PUT /auth/credentials/{credential_id}/agent-consent` path). Record the UTC
pause time, `github_consent_paused` audit and `github_token_revoked` outcome
and HTTP status. If revocation was confirmed, within 60 seconds run the
following in Terminal A; `probe_user` must report 401 for the *saved* token.
Wait **11 minutes after pause** and check the box helper/`gh` call with no
alternate credentials:

```bash
printf 'paused UTC: '; date -u '+%Y-%m-%dT%H:%M:%SZ'
probe_user
sleep 660
printf 'post-hold UTC: '; date -u '+%Y-%m-%dT%H:%M:%SZ'
if adp-github-token >/dev/null; then echo 'FAIL: box minted after pause'; else echo 'box mint refused'; fi
if gh api user >/dev/null; then echo 'FAIL: gh authenticated after pause'; else echo 'gh refused'; fi
unset token
```

Verify the gateway/host refusal is 403
`github_consent_required` and the box does not authenticate. A failed revoke
still requires local denial; do not report the copied token as revoked.
Capture the paused Dev Box card, then reconnect with fresh App consent: a
paused App grant cannot be resumed by simply flipping the old consent flag.

For disconnect, first complete fresh consent. In Terminal A retain another
fresh token and confirm `probe_user` returns 200. Then in Terminal B use the
owner's Disconnect action (`DELETE /auth/connections/oauth/github`); in
Terminal A run `probe_user` within 60 seconds and `adp-github-token >/dev/null`
for the box mint. Expect
`status=disconnected`, with `provider_token_revocation=confirmed` only when
the audit records `github_token_revoked outcome=confirmed` (HTTP 204).
Within 60 seconds a direct header-only GitHub `GET /user` with the *saved*
token must return 401; the next box mint must be refused as 404
`github_not_connected`. Check audit ordering: local denial committed before
the GitHub revoke request. Record UTC times and redacted statuses, not the
response headers or token. In Terminal A, `unset token` when finished.

An approved isolated-dev **revocation-failure stub** may separately return
404, 5xx or time out only the GitHub grant DELETE; never send credentials to
an untrusted endpoint or intercept `/user` or installation checks. The
operator must restore the normal route afterwards. Expect disconnect to
succeed with `provider_token_revocation=failed`, audit `outcome=failed` and
HTTP status (or null for timeout), an operator warning without credentials,
and a refused box mint. If no authorized deployed stub exists, keep AC-08-t3
blocked rather than claim it was exercised.

## Private evidence handoff

For each AC-01 through AC-08 and SCN-06, record **pass/fail/blocked/not run**,
deployed SHA, timestamp, operator, redacted response code, relevant audit
result, screenshots and the private evidence location. Separate code/unit
results from deployed checks; do not paste tokens, raw audit records, real
repo names, callback URL, or host IDs into GitHub. A merged PR is not a
successful deployment, and a deployed feature is not live acceptance.
