# Offline runner tests

Run these checks from `modules/source-control/gitlab/runners`. Prerequisites are
Terraform with the module's locked providers initialized, Python 3.11 or newer
with pytest, boto3 and cryptography, and OpenSSL. The real Runner binary is
release-locked in `versions.lock.json`; prepare it over HTTPS without Docker,
verify its checksum and version, and keep the temporary executable private:

```sh
export GITLAB_RUNNER_BINARY="$(mktemp -d)/gitlab-runner"
python3 scripts/prepare_test_runner.py --output "$GITLAB_RUNNER_BINARY"
```

The CLI regression fails rather than skipping if this binary is missing.

```sh
terraform init -backend=false -input=false
terraform test -no-color
python3 -m pytest tests -q
```

The registration regression executes the actual chart-rendered registration
script for two groups. It substitutes only the executable/configuration paths,
the local HTTPS server address and the retry count. A temporary certificate is
trusted through `SSL_CERT_FILE`; the deployment's CA-file setting remains intact.
The test checks the Runner-written configuration with the shipped manager guard.
All tokens, receipts and certificates are synthetic and temporary.

The successful chart-render test retains its Terraform test statuses, planned Helm
values, actual rendered YAML, TOML template and registration script for each group
in a private temporary directory outside the source checkout. It prints
`runner_rendered_fixture_path=...`; pytest also shows that path in its final
summary with normal output capture enabled. Copy these offline artifacts to the
private evidence store when needed, then remove the directory. The assertions
check the chart-native `runners.runUntagged=false` value and registration script;
they do not introduce a `run_untagged` TOML setting.

Cleanup tests mock AWS transport, covering manager recovery, retained attached
jobs, detached orphan termination, timeout failure and old terminated records.
These tests do not deploy infrastructure, qualify live jobs or replace protected
environment checks.
