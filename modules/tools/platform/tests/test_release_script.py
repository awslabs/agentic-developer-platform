import base64
import hashlib
import json
import os
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from modules.tools.platform.tests.release_plan_fixtures import BINDING, SELECTORS, existing_target_changes, gateway_change, knowledge_change


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "modules/tools/deploy.sh"
WORKFLOW = ROOT / ".github/workflows/tools-platform-deploy.yml"
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-tools-trusted-deployment"
FACADE_ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-role-tools-facade"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/example"
KEY_ARN = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/00000000-0000-0000-0000-000000000001"
ASSERTION_KEY = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/00000000-0000-0000-0000-000000000002"
INTERCEPTOR_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-tools-dev-interceptor:live"
INTERCEPTOR_ROLE = f"arn:aws:iam::{ACCOUNT}:role/adp-tools-dev-interceptor"
URL = "https://tools.example.invalid/mcp"


class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        executable_dir = self.directory / "bin"
        executable_dir.mkdir()
        executables = {
            "git": '''#!/usr/bin/env bash
if [[ "$1" == -C ]]; then shift 2; fi
if [[ "$1" == rev-parse ]]; then
  if [[ "$2" == HEAD ]]; then echo "$ADP_SOURCE_REVISION"; else echo "${FAKE_MAIN_SHA:-$ADP_SOURCE_REVISION}"; fi
  exit 0
fi
if [[ "$1" == merge-base ]]; then test "${FAKE_GIT_REJECT:-}" != true; exit; fi
exit 2
''',
            "terraform": '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TF_LOG"
case " $* " in
  *" output -json "*) cat "$FAKE_OUTPUTS" ;;
  *" show -json "*) cat "$FAKE_PLAN_JSON" ;;
  *" plan "*)
    for argument in "$@"; do
      if [[ "$argument" == -out=* ]]; then touch "${argument#-out=}"; fi
      if [[ "$argument" == -var-file=* && -n "${FAKE_CAPTURE_INPUTS:-}" ]]; then cp "${argument#-var-file=}" "$FAKE_CAPTURE_INPUTS"; fi
    done
    exit "${FAKE_PLAN_RESULT:-0}" ;;
  *" apply "*) test -f "${!#}" ;;
esac
''',
            "aws": '''#!/usr/bin/env bash
case "$1 $2" in
  'sts get-caller-identity')
    if [[ " $* " == *' --query Account '* ]]; then echo 123456789012
    elif [[ " $* " == *' --query Arn '* ]]; then echo arn:aws:sts::123456789012:assumed-role/adp-tools-trusted-deployment/ci
    else printf '{"Account":"123456789012","Arn":"arn:aws:sts::123456789012:assumed-role/adp-tools-trusted-deployment/ci"}\\n'; fi ;;
  'bedrock-agentcore-control get-gateway') printf '{"status":"%s","gatewayId":"example","gatewayArn":"%s","gatewayUrl":"%s","kmsKeyArn":"%s"}\\n' "${FAKE_GATEWAY_STATUS:-READY}" "$FAKE_GATEWAY_ARN" "$FAKE_GATEWAY_URL" "$FAKE_KEY_ARN" ;;
  'bedrock-agentcore-control synchronize-gateway-targets')
    printf '%s\\n' "$*" >> "$FAKE_AWS_LOG"
    target_ids=()
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == --target-id-list ]]; then
        shift
        while [[ $# -gt 0 && "$1" != --* ]]; do target_ids+=("$1"); shift; done
        break
      fi
      shift
    done
    [[ ${#target_ids[@]} -eq 1 ]] || { echo 'targetIdList must contain exactly one item' >&2; exit 2; }
    [[ "${FAKE_SYNC_REJECT:-}" != true && "${FAKE_SYNC_REJECT_TARGET:-}" != "${target_ids[0]}" ]] || exit 1
    printf '{"targets":[]}\\n' ;;
  'bedrock-agentcore-control list-gateway-targets')
    if [[ "${FAKE_KNOWLEDGE_TARGET:-}" == true ]]; then
      printf '{"items":[{"targetId":"search-target"},{"targetId":"code-target"},{"targetId":"browser-target"},{"targetId":"knowledge-target"}]}\\n'
    else
      printf '{"items":[{"targetId":"search-target"},{"targetId":"code-target"},{"targetId":"browser-target"}]}\\n'
    fi ;;
  'bedrock-agentcore-control get-gateway-target')
    while [[ $# -gt 0 ]]; do if [[ "$1" == --target-id ]]; then target_id=$2; break; fi; shift; done
    if [[ "$target_id" == "${FAKE_DYNAMIC_TARGET:-}" ]]; then
      printf '{"targetId":"%s","gatewayArn":"%s","status":"%s","updatedAt":"2026-01-01T00:00:00+00:00","targetConfiguration":{"mcp":{"mcpServer":{"listingMode":"DYNAMIC"}}}}\\n' "$target_id" "$FAKE_GATEWAY_ARN" "${FAKE_TARGET_STATUS:-READY}"
    elif [[ "${FAKE_TARGET_TYPE:-}" == lambda ]]; then
      printf '{"targetId":"%s","gatewayArn":"%s","status":"%s","updatedAt":"2026-01-01T00:00:00+00:00","targetConfiguration":{"mcp":{"lambda":{"lambdaArn":"arn:aws:lambda:us-east-1:123456789012:function:shared"}}}}\\n' "$target_id" "$FAKE_GATEWAY_ARN" "${FAKE_TARGET_STATUS:-READY}"
    else
      printf '{"targetId":"%s","gatewayArn":"%s","status":"%s","lastSynchronizedAt":"2026-01-01T00:00:00Z"}\\n' "$target_id" "$FAKE_GATEWAY_ARN" "${FAKE_TARGET_STATUS:-READY}"
    fi ;;
  'ssm put-parameter') printf '%s\\n' "$*" >> "$FAKE_AWS_LOG" ;;
  'ssm get-parameter') [[ "${FAKE_MISSING_SSM:-}" != true ]] ;;
  'lambda get-alias')
    count=$(cat "$FAKE_ALIAS_COUNT_FILE" 2>/dev/null || echo 0)
    count=$((count + 1))
    printf '%s\n' "$count" > "$FAKE_ALIAS_COUNT_FILE"
    version="${FAKE_INTERCEPTOR_VERSION:-42}"
    if [[ "${FAKE_ALIAS_CHANGE_ON:-}" == "$count" ]]; then version=43; fi
    printf '{"AliasArn":"%s","FunctionVersion":"%s","RoutingConfig":{"AdditionalVersionWeights":%s}}\n' \
      "$FAKE_INTERCEPTOR_ARN" "$version" "${FAKE_ALIAS_WEIGHTS:-{}}" ;;
  'lambda get-function')
    if [[ -n "${FAKE_INTERCEPTOR_ENVIRONMENT_ERROR:-}" ]]; then
      printf '%s\n' "$FAKE_INTERCEPTOR_ENVIRONMENT_ERROR"
      exit 0
    fi
    version="${FAKE_INTERCEPTOR_VERSION:-42}"
    printf '{"FunctionArn":"%s:%s","Version":"%s","CodeSha256":"%s","Role":"%s","Runtime":"python3.12","Handler":"%s","State":"%s","LastUpdateStatus":"%s","Environment":{"Variables":{"ADP_TOOLS_FACADE_ROLE_ARN":"%s","ADP_TOOLS_ASSERTION_KEY_ARN":"%s"}}}\n' \
      "${FAKE_INTERCEPTOR_ARN%:live}" "$version" "$version" "${FAKE_INTERCEPTOR_SHA:-}" \
      "${FAKE_INTERCEPTOR_ROLE:-}" "${FAKE_INTERCEPTOR_HANDLER:-tools_interceptor.handler.handler}" \
      "${FAKE_INTERCEPTOR_STATE:-Active}" "${FAKE_INTERCEPTOR_STATUS:-Successful}" \
      "$FAKE_FACADE_ROLE" "${FAKE_INTERCEPTOR_KEY:-}" ;;
  'ecr describe-repositories') [[ "${FAKE_MISSING_ECR:-}" != true ]] ;;
  *) exit 2 ;;
esac
''',
            "sleep": "#!/usr/bin/env bash\nexit 0\n",
            "python3": f'''#!{sys.executable}
import os
import sys
sys.path.insert(0, {str(ROOT)!r})
if sys.argv[1:3] == ["-m", "modules.tools.platform.scripts.native_release"]:
    from modules.tools.platform.tests.release_native_wire import main
    main(sys.argv[3:])
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''',
            "kubectl": "#!/usr/bin/env bash\n[[ ${FAKE_MISSING_CONTEXT:-} != true ]]\n",
        }
        for name, text in executables.items():
            destination = executable_dir / name
            destination.write_text(text)
            destination.chmod(0o755)
        self.outputs = self.directory / "outputs.json"
        self.outputs.write_text(json.dumps({
            "gateway_id": {"value": "example"},
            "assertion_signing_key_arn": {"value": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000002"},
            "execution_table_name": {"value": "adp-tools-dev-executions"},
            "shared_tools_lambda_arn": {"value": "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared"},
            "gateway_arn": {"value": GATEWAY_ARN},
            "gateway_url": {"value": URL},
            "kms_key_arn": {"value": KEY_ARN},
            "caller_role_arn": {"value": FACADE_ROLE},
            "target_ids": {"value": {"websearch": "search-target", "code-interpreter": "code-target", "browser": "browser-target"}},
        }))
        self.plan = self.directory / "plan.json"
        self.plan.write_text(json.dumps({
            "resource_changes": [gateway_change(["update"]), *existing_target_changes()],
        }))
        archive = self.directory / "interceptor.zip"
        runpy.run_path(str(ROOT / "modules/tools/interceptor/package.py"))["build"](archive)
        digest = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()
        self.tf_log = self.directory / "terraform.log"
        self.aws_log = self.directory / "aws.log"
        self.release_dir = self.directory / "release"
        self.smoke_log = self.directory / "smoke.log"
        self.env = {
            **os.environ,
            "PATH": f"{executable_dir}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.directory),
            "ADP_TOOLS_MODE": "apply",
            "ADP_TOOLS_ENVIRONMENT": "dev",
            "ADP_TOOLS_AWS_REGION": "us-east-1",
            "ADP_TOOLS_ACCOUNT_ID": ACCOUNT,
            "ADP_TOOLS_STATE_BUCKET": "state.example.invalid",
            "ADP_TOOLS_DEPLOY_ROLE_ARN": ROLE,
            "ADP_TOOLS_TFVARS_JSON": json.dumps({"aws_account_id": ACCOUNT, "tools_facade_role_arn": FACADE_ROLE, "interceptor_lambda_arn": INTERCEPTOR_ARN, "interceptor_execution_role_arn": INTERCEPTOR_ROLE, "assertion_signing_key_arn": ASSERTION_KEY}),
            "ADP_TOOLS_RELEASE_SELECTORS_JSON": json.dumps(SELECTORS),
            "ADP_TOOLS_RELEASE_DIR": str(self.release_dir),
            "ADP_TOOLS_SMOKE_GATEWAY_URL": "https://facade.example.invalid",
            "ADP_TOOLS_SMOKE_EVIDENCE_DIR": str(self.directory / "native-evidence"),
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipeline.actions.githubusercontent.com/oidc?api-version=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "offline-oidc-request",
            "AWS_ACCESS_KEY_ID": "offline-access",
            "AWS_SECRET_ACCESS_KEY": "offline-secret",
            "AWS_SESSION_TOKEN": "offline-session",
            "ADP_SOURCE_REVISION": "a" * 40,
            "ADP_DEFINITION_REVISION": "b" * 40,
            "ADP_CORRELATION": "c" * 64,
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ID": "18",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW": "Deploy tools platform",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REPOSITORY": "aws-e/adp",
            "GITHUB_JOB": "release",
            "FAKE_TF_LOG": str(self.tf_log),
            "FAKE_AWS_LOG": str(self.aws_log),
            "FAKE_OUTPUTS": str(self.outputs),
            "FAKE_PLAN_JSON": str(self.plan),
            "FAKE_GATEWAY_ARN": GATEWAY_ARN,
            "FAKE_GATEWAY_URL": URL,
            "FAKE_KEY_ARN": KEY_ARN,
            "FAKE_SMOKE_LOG": str(self.smoke_log),
            "FAKE_INTERCEPTOR_ARN": INTERCEPTOR_ARN,
            "FAKE_INTERCEPTOR_ROLE": INTERCEPTOR_ROLE,
            "FAKE_INTERCEPTOR_KEY": ASSERTION_KEY,
            "FAKE_FACADE_ROLE": FACADE_ROLE,
            "FAKE_INTERCEPTOR_SHA": digest,
            "FAKE_ALIAS_COUNT_FILE": str(self.directory / "alias-count"),
        }

        from time import time
        from uuid import uuid4
        approval = {
            "version": 1, "run_id": str(uuid4()), "approval_revision": str(uuid4()),
            "tenant_id": "offline-tenant", "deployment_id": "offline-deployment",
            "gateway_origin": self.env["ADP_TOOLS_SMOKE_GATEWAY_URL"],
            "source_revision": "a" * 40, "definition_revision": "b" * 40,
            "github_run_id": "18", "github_run_attempt": "1",
        }
        fixture = {"version": 1, "run_id": approval["run_id"], "approval_revision": approval["approval_revision"],
                   "contract_sha256": "c" * 64, "arguments": {"query": "explicit approved offline query"}}
        delivery = {**approval, "run_started_at": int(time()), "expires_at": int(time()) + 300}
        for variable, value in (
            ("ADP_TOOLS_SMOKE_APPROVAL_FILE", json.dumps(approval)),
            ("ADP_TOOLS_SMOKE_FIXTURE_FILE", json.dumps(fixture)),
            ("ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE", "fixture.access.signature"),
            ("ADP_TOOLS_SMOKE_DELIVERY_FILE", json.dumps(delivery)),
        ):
            path = self.directory / variable.lower()
            path.write_text(value)
            path.chmod(0o600)
            self.env[variable] = str(path)

    def invoke(self):
        return subprocess.run(["bash", str(SCRIPT), "--infra", "--targets"], env=self.env,
                              cwd=ROOT, capture_output=True, text=True, check=False)

    def operational_inputs(self):
        from time import time
        from uuid import uuid4
        self.env.update(ADP_CORRELATION="", ADP_TOOLS_RELEASE_QUALIFICATION="operational")
        config = {"version": 1, "scope": "ordinary_user_behavior_only", "source_revision": "a" * 40,
                  "definition_revision": "b" * 40, "gateway_origin": self.env["ADP_TOOLS_SMOKE_GATEWAY_URL"],
                  "subject_id": str(uuid4()), "user_id": str(uuid4()), "tenant_id": "fixture-tenant",
                  "team_id": "fixture-team", "query": "explicit operational fixture query",
                  "authorized_at": int(time()) - 1, "expires_at": int(time()) + 1800}
        claims = base64.urlsafe_b64encode(json.dumps({"sub": config["subject_id"], "token_use": "access",
                                                   "exp": int(time()) + 3600}).encode()).decode().rstrip("=")
        for name, body in (("ADP_TOOLS_OPERATIONAL_CONFIG_FILE", json.dumps(config)),
                           ("ADP_TOOLS_OPERATIONAL_LOGIN_FILE", "header." + claims + ".signature")):
            path = self.directory / name
            path.write_text(body)
            path.chmod(0o600)
            self.env[name] = str(path)

    def test_operational_release_keeps_plan_and_live_gates_without_cost_claim(self):
        self.operational_inputs()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        release = json.loads((self.release_dir / "release.json").read_text())
        self.assertNotIn("native_smoke", release)
        self.assertEqual(release["operational_smoke"]["behavioral_status"], "passed")
        self.assertEqual(release["operational_smoke"]["cost_qualification"], "not_requested")
        self.assertFalse(release["operational_smoke"]["governance_complete"])
        self.assertIn(" apply ", self.tf_log.read_text())
        self.assertIn("ssm put-parameter", self.aws_log.read_text())

    def test_operational_failed_cleanup_stops_ssm_publication(self):
        self.operational_inputs()
        self.env["FAKE_OPERATIONAL_FAULT"] = "cleanup"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("publication refused", result.stderr)
        self.assertNotIn("ssm put-parameter", self.aws_log.read_text())
        self.assertFalse(self.release_dir.exists())

    def test_unverified_interceptor_never_reaches_terraform(self):
        cases = [
            ("FAKE_INTERCEPTOR_SHA", "invalid", "version, hash or verification configuration"),
            ("FAKE_INTERCEPTOR_ROLE", "arn:aws:iam::123456789012:role/other", "version, hash or verification configuration"),
            ("FAKE_INTERCEPTOR_KEY", KEY_ARN, "version, hash or verification configuration"),
            ("FAKE_INTERCEPTOR_HANDLER", "bootstrap_placeholder.handler", "version, hash or verification configuration"),
            ("FAKE_INTERCEPTOR_STATE", "Pending", "version, hash or verification configuration"),
            ("FAKE_INTERCEPTOR_STATUS", "InProgress", "version, hash or verification configuration"),
            ("FAKE_INTERCEPTOR_VERSION", "$LATEST", "must point to one numbered version"),
            ("FAKE_ALIAS_WEIGHTS", '{"41":0.5}', "must point to one numbered version"),
            ("FAKE_INTERCEPTOR_ARN", INTERCEPTOR_ARN.replace("adp-tools-dev", "untrusted"), "must point to one numbered version"),
        ]
        for key, value, message in cases:
            with self.subTest(key=key):
                self.env[key] = value
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.tf_log.exists())
                self.assertFalse(self.release_dir.exists())
                self.env.pop(key)
                (self.directory / "alias-count").unlink(missing_ok=True)

    def test_unreadable_environment_is_distinct_from_a_source_mismatch(self):
        self.env["FAKE_INTERCEPTOR_ENVIRONMENT_ERROR"] = json.dumps({
            "Environment": {"Error": {"ErrorCode": "KMSAccessDeniedException", "Message": "private-deployment-sentinel"}},
        })
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment cannot be read", result.stderr)
        self.assertIn("KMS decrypt authorization", result.stderr)
        self.assertNotIn("private-deployment-sentinel", result.stderr + result.stdout)
        self.assertNotIn("version, hash or verification configuration", result.stderr)
        self.assertFalse(self.tf_log.exists())
        self.assertFalse(self.release_dir.exists())
        self.assertFalse(self.smoke_log.exists())

    def test_interceptor_alias_changes_during_plan_never_apply(self):
        self.env["FAKE_ALIAS_CHANGE_ON"] = "2"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias changed during platform release", result.stderr)
        self.assertIn(" plan ", self.tf_log.read_text())
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.release_dir.exists())

    def test_interceptor_alias_changes_after_apply_never_publish_receipt(self):
        self.env["FAKE_ALIAS_CHANGE_ON"] = "3"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias changed during platform release", result.stderr)
        self.assertIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.release_dir.exists())

    def test_interceptor_alias_changes_before_receipt_is_rejected(self):
        self.env["FAKE_ALIAS_CHANGE_ON"] = "4"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias changed during platform release", result.stderr)
        self.assertIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.release_dir.exists())
        self.assertEqual(len(json.loads(self.smoke_log.read_text())), 7)
        self.assertNotIn("ssm put-parameter", self.aws_log.read_text())

    def test_interceptor_configuration_requires_managed_live_alias(self):
        for name in ("adp-tools-dev-interceptor", "other-interceptor:live"):
            with self.subTest(name=name):
                config = json.loads(self.env["ADP_TOOLS_TFVARS_JSON"])
                config["interceptor_lambda_arn"] = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{name}"
                self.env["ADP_TOOLS_TFVARS_JSON"] = json.dumps(config)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reviewed interceptor alias", result.stderr)
                self.assertFalse(self.tf_log.exists())
                self.assertFalse(self.release_dir.exists())

    def test_release_plans_applies_verifies_and_publishes(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        terraform_calls = self.tf_log.read_text()
        self.assertLess(terraform_calls.index(" plan "), terraform_calls.index(" apply "))
        self.assertIn("key=tools/platform/dev.tfstate", terraform_calls)
        release = json.loads((self.release_dir / "release.json").read_text())
        self.assertEqual(release["gateway_id"], "example")
        self.assertEqual(release["qualification_status"], "smoke_passed_evidence_pending")
        self.assertEqual(release["kms_key_arn"], KEY_ARN)
        self.assertEqual(release["caller_role_arn"], FACADE_ROLE)
        self.assertEqual(set(release["targets"]), {"websearch", "code-interpreter", "browser"})
        self.assertEqual(release["source_revision"], "a" * 40)
        self.assertEqual(release["tools_list_sha256"], hashlib.sha256(b"live-discovery-fixture").hexdigest())
        revision_values = [[target, "2026-01-01T00:00:00+00:00"]
                           for target in ["browser-target", "code-target", "search-target"]]
        self.assertEqual(release["target_sync_revision"], hashlib.sha256(
            json.dumps(revision_values, separators=(",", ":")).encode()).hexdigest())
        calls = json.loads(self.smoke_log.read_text())
        self.assertEqual(calls[0], "https://pipeline.actions.githubusercontent.com/oidc?api-version=1&audience=adp-native-test-enrollment-v1")
        self.assertEqual(calls[1], "https://facade.example.invalid/api/v1/native-tests/capabilities/exchange")
        self.assertEqual(calls[2], "https://facade.example.invalid/api/agent/tools?live=true")
        self.assertEqual(calls[3], "https://facade.example.invalid/api/agent/tools/call")
        self.assertIn("/close?tool=websearch___search", calls[4])
        self.assertEqual(calls[5], "https://facade.example.invalid/api/agent/tools?live=true")
        self.assertTrue(calls[6].endswith("/status"))
        self.assertEqual(release["native_smoke"]["cleanup_status"], "confirmed")
        self.assertEqual(release["native_smoke"]["funding_status"], "held")
        self.assertEqual(release["native_smoke"]["accounting_status"], "pending")
        self.assertFalse(release["native_smoke"]["governance_complete"])
        self.assertEqual(release["interceptor_lambda_arn"], INTERCEPTOR_ARN)
        self.assertEqual(release["interceptor_version"], "42")

        self.assertEqual(release["interceptor_code_sha256"], self.env["FAKE_INTERCEPTOR_SHA"])
        self.assertEqual((self.directory / "alias-count").read_text().strip(), "4")
        writes = self.aws_log.read_text()
        self.assertEqual(writes.count("ssm put-parameter"), 10)
        self.assertIn("--name /adp/dev/tools/assertion-signing-key-arn --value arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000002", writes)
        self.assertIn("--name /adp/dev/tools/execution-table-name --value adp-tools-dev-executions", writes)
        self.assertIn("--name /adp/dev/tools/shared-tools-lambda-arn --value arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared", writes)
        self.assertIn("--name /adp/dev/tools/gateway-id --value example", writes)
        self.assertIn(f"--name /adp/dev/tools/gateway-url --value {URL}", writes)
        self.assertIn(f"--name /adp/dev/tools/gateway-arn --value {GATEWAY_ARN}", writes)
        self.assertIn(f"--name /adp/dev/tools/caller-role-arn --value {FACADE_ROLE}", writes)
        self.assertIn("--name /adp/dev/tools/release", writes)
        self.assertIn("bedrock-agentcore-control synchronize-gateway-targets", writes)
        sync_calls = [shlex.split(line) for line in writes.splitlines()
                      if line.startswith("bedrock-agentcore-control synchronize-gateway-targets ")]
        requested_targets = []
        for call in sync_calls:
            start = call.index("--target-id-list") + 1
            end = next((index for index in range(start, len(call)) if call[index].startswith("--")), len(call))
            requested_targets.append(call[start:end])
            self.assertEqual(call[call.index("--gateway-identifier") + 1], "example")
            self.assertEqual(call[call.index("--region") + 1], "us-east-1")
        self.assertEqual(requested_targets, [["browser-target"], ["code-target"], ["search-target"]])
        self.assertLess(writes.rindex("synchronize-gateway-targets"), writes.index("ssm put-parameter"))
        published = json.loads(writes.split("--name /adp/dev/tools/release --value ", 1)[1].split(" --type", 1)[0])
        self.assertEqual(published, release)

    def test_output_only_plan_is_applied_to_persist_state(self):
        self.plan.write_text(json.dumps({
            "resource_changes": [gateway_change(), *existing_target_changes()],
            "output_changes": {
                "shared_tools_lambda_arn": {
                    "actions": ["create"], "before": None,
                    "after": f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-dev-shared-tools-shared",
                },
                "gateway_url": {"actions": ["no-op"], "before": URL, "after": URL},
            },
        }))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.tf_log.read_text().splitlines()
        applies = [call for call in calls if " apply " in call]
        self.assertEqual(len(applies), 1)
        self.assertTrue(applies[0].endswith("/platform.tfplan"))
        self.assertLess(next(index for index, call in enumerate(calls) if " apply " in call),
                        next(index for index, call in enumerate(calls) if " output -json" in call))
        self.assertTrue((self.release_dir / "release.json").exists())

    def test_unchanged_outputs_and_resources_do_not_apply(self):
        self.plan.write_text(json.dumps({
            "resource_changes": [gateway_change(), *existing_target_changes()],
            "output_changes": {"gateway_url": {"actions": ["no-op"], "before": URL, "after": URL}},
        }))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertIn("0 to add, 0 to change, 0 to destroy", result.stdout)

    def test_output_only_plan_still_requires_smoke_preflight_before_apply(self):
        self.plan.write_text(json.dumps({
            "resource_changes": [gateway_change(), *existing_target_changes()],
            "output_changes": {"shared_tools_lambda_arn": {"actions": ["create"]}},
        }))
        self.env.pop("ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.tf_log.exists())
        self.assertFalse(self.smoke_log.exists())

    def test_lambda_targets_are_not_synchronized_but_must_be_ready(self):
        # AgentCore rejects SynchronizeGatewayTargets for Lambda targets; the
        # release must still require READY and publish without a sync timestamp.
        self.env["FAKE_TARGET_TYPE"] = "lambda"
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = self.aws_log.read_text()
        self.assertNotIn("synchronize-gateway-targets", writes)
        self.assertIn("--name /adp/dev/tools/release", writes)
        self.env["FAKE_TARGET_STATUS"] = "CREATING"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)

    def test_dynamic_target_skips_sync_while_cached_targets_still_sync(self):
        self.env["FAKE_DYNAMIC_TARGET"] = "code-target"
        self.env["FAKE_SYNC_REJECT_TARGET"] = "code-target"
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        writes = self.aws_log.read_text()
        sync = [line for line in writes.splitlines() if "synchronize-gateway-targets" in line]
        self.assertEqual(len(sync), 2)
        self.assertTrue(all("code-target" not in line for line in sync))
        self.assertIn("--name /adp/dev/tools/release", writes)
        self.env["FAKE_TARGET_STATUS"] = "CREATING"
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_smoke_failures_cannot_publish(self):
        for scenario in ("authentication-failure", "discovery-failure", "wrong-gateway", "old-facade",
                         "search-error", "search-pending", "unbound-search", "digest-mismatch",
                         "close-failure", "cleanup-late", "accounting-unknown"):
            with self.subTest(scenario=scenario):
                self.env["FAKE_SMOKE_SCENARIO"] = scenario
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Native authenticated MCP smoke failed", result.stderr)
                self.assertFalse(self.release_dir.exists())
                self.assertNotIn("ssm put-parameter", self.aws_log.read_text())
                self.assertNotIn("private-auth-sentinel", result.stderr + result.stdout)
                self.assertNotIn("fixture.access.signature", result.stderr + result.stdout)
                evidence = self.directory / "native-evidence/native-smoke.json"
                report = json.loads(evidence.read_text())
                self.assertEqual(report["behavioral_status"], "failed")
                self.assertFalse(report["governance_complete"])
                evidence.unlink()

    def test_missing_smoke_session_refuses_before_apply(self):
        self.env.pop("ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.tf_log.exists())
        self.assertFalse(self.release_dir.exists())

    def test_later_target_sync_failure_stops_before_publication(self):
        self.env["FAKE_SYNC_REJECT_TARGET"] = "code-target"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.release_dir / "release.json").exists())
        calls = self.aws_log.read_text()
        self.assertIn("--target-id-list browser-target --region", calls)
        self.assertIn("--target-id-list code-target --region", calls)
        self.assertNotIn("--target-id-list search-target", calls)
        self.assertNotIn("ssm put-parameter", calls)

    def test_failed_target_sync_does_not_publish(self):
        for key, value in [("FAKE_SYNC_REJECT", "true"), ("FAKE_TARGET_STATUS", "FAILED")]:
            with self.subTest(key=key):
                self.env[key] = value
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.release_dir / "release.json").exists())
                self.assertNotIn("ssm put-parameter", self.aws_log.read_text())
                self.env.pop(key)


    def test_missing_interceptor_refuses_unprotected_target_release(self):
        self.env["ADP_TOOLS_TFVARS_JSON"] = json.dumps({"aws_account_id": ACCOUNT, "tools_facade_role_arn": FACADE_ROLE})
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tools targets require a configured interceptor", result.stderr)
        self.assertFalse(self.tf_log.exists())

    def test_release_refuses_bootstrap_or_destructive_plan_before_apply(self):
        for resource_type, address, actions in [
            ("aws_iam_role_policy", "aws_iam_role_policy.gateway_kms", ["update"]),
            ("aws_kms_key", "aws_kms_key.gateway", ["update"]),
            ("aws_ecr_repository", "aws_ecr_repository.facade", ["create"]),
            ("aws_bedrockagentcore_gateway", "aws_bedrockagentcore_gateway.tools", ["delete", "create"]),
        ]:
            with self.subTest(address=address, actions=actions):
                self.plan.write_text(json.dumps({"resource_changes": [{
                    "type": resource_type, "address": address, "change": {"actions": actions},
                }]}))
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("bootstrap-owned or destructive", result.stderr)
                self.assertNotIn(" apply ", self.tf_log.read_text())
                self.tf_log.write_text("")

    def test_first_knowledge_create_requires_protected_binding_before_apply(self):
        self.plan.write_text(json.dumps({"resource_changes": [gateway_change(), *existing_target_changes(), knowledge_change()]}))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected Knowledge creation binding", result.stderr)
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.env["ADP_TOOLS_KNOWLEDGE_CREATE_JSON"] = json.dumps(BINDING)
        self.env["FAKE_KNOWLEDGE_TARGET"] = "true"
        outputs = json.loads(self.outputs.read_text())
        outputs["target_ids"]["value"]["knowledge"] = "knowledge-target"
        self.outputs.write_text(json.dumps(outputs))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" apply ", self.tf_log.read_text())
        self.assertIn("--target-id-list knowledge-target", self.aws_log.read_text())
        self.assertEqual(json.loads((self.release_dir / "release.json").read_text())["native_smoke"]["cleanup_status"], "confirmed")

    def test_protected_knowledge_config_merges_into_authentic_job_inputs(self):
        captured = self.directory / "captured-inputs.json"
        original = self.env["ADP_TOOLS_TFVARS_JSON"]
        self.env.update(ADP_TOOLS_KNOWLEDGE_CONFIG_JSON=json.dumps(BINDING),
                        ADP_TOOLS_KNOWLEDGE_CREATE_JSON=json.dumps(BINDING),
                        FAKE_CAPTURE_INPUTS=str(captured), FAKE_KNOWLEDGE_TARGET="true")
        self.plan.write_text(json.dumps({"resource_changes": [gateway_change(), *existing_target_changes(), knowledge_change()]}))
        outputs = json.loads(self.outputs.read_text())
        outputs["target_ids"]["value"]["knowledge"] = "knowledge-target"
        self.outputs.write_text(json.dumps(outputs))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        merged = json.loads(captured.read_text())
        self.assertEqual({key: merged[key] for key in json.loads(original)}, json.loads(original))
        self.assertEqual(merged["knowledge_door_endpoint"], BINDING["endpoint"])
        self.assertEqual(merged["knowledge_door_subnet_ids"], BINDING["subnet_ids"])
        self.assertEqual(merged["knowledge_door_security_group_ids"], BINDING["security_group_ids"])
        self.assertEqual(merged["knowledge_door_vpc_id"], BINDING["vpc_id"])
        self.assertEqual(merged["knowledge_door_routing_domain"], BINDING["routing_domain"])
        self.assertEqual(self.env["ADP_TOOLS_TFVARS_JSON"], original)
        self.assertTrue(self.smoke_log.exists())

    def test_conflicting_protected_knowledge_config_stops_before_terraform(self):
        original = json.loads(self.env["ADP_TOOLS_TFVARS_JSON"])
        original["knowledge_door_endpoint"] = "private-conflicting-value-do-not-log"
        self.env.update(ADP_TOOLS_TFVARS_JSON=json.dumps(original), ADP_TOOLS_KNOWLEDGE_CONFIG_JSON=json.dumps(BINDING))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts with protected Knowledge configuration", result.stderr)
        self.assertNotIn("private-conflicting-value-do-not-log", result.stdout + result.stderr)
        self.assertFalse(self.tf_log.exists())
        self.assertFalse(self.aws_log.exists())

    def test_knowledge_config_cannot_bypass_original_native_preflight(self):
        self.env["ADP_TOOLS_KNOWLEDGE_CONFIG_JSON"] = json.dumps(BINDING)
        self.env.pop("ADP_TOOLS_SMOKE_APPROVAL_FILE")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing ADP_TOOLS_SMOKE_APPROVAL_FILE", result.stderr)
        self.assertFalse(self.tf_log.exists())

    def test_durable_config_remains_available_to_readonly_drift_without_create_authority(self):
        captured = self.directory / "drift-inputs.json"
        self.env.update(ADP_TOOLS_MODE="drift", GITHUB_EVENT_NAME="schedule",
                        ADP_TOOLS_KNOWLEDGE_CONFIG_JSON=json.dumps(BINDING), FAKE_CAPTURE_INPUTS=str(captured))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(captured.read_text())["knowledge_door_endpoint"], BINDING["endpoint"])
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.aws_log.exists())

    def test_stage_selection_and_resume_use_target_bound_state(self):
        state = self.directory / "deploy-state.json"
        self.env["ADP_TOOLS_DEPLOY_STATE_FILE"] = str(state)
        command = ["bash", str(SCRIPT), "--infra"]
        first = subprocess.run(command, env=self.env, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        recorded = json.loads(state.read_text())
        self.assertEqual(recorded["stages"]["infra"],
                         {"status": "command-succeeded", "run_id": 18, "run_attempt": 1})
        self.assertEqual(self.tf_log.read_text().count(" apply "), 1)
        resume = subprocess.run([*command, "--resume"], env=self.env, cwd=ROOT,
                                capture_output=True, text=True)
        self.assertNotEqual(resume.returncode, 0)
        self.assertIn("Coordinator: independently verify infra", resume.stderr)
        self.assertEqual(json.loads(state.read_text()), recorded)
        receipt_dir = self.directory / "stage-receipts"
        receipt_dir.mkdir()
        (receipt_dir / "infra.json").write_text(json.dumps({
            "stage": "infra", "source_revision": self.env["ADP_SOURCE_REVISION"],
            "account": ACCOUNT, "region": "us-east-1", "environment": "dev",
            "run_id": 18, "run_attempt": 1, "mode": "apply",
            "bindings_sha256": recorded["bindings_sha256"],
            "evidence_type": "independent-runtime",
            "expected_runtime": {"gateway": "sha256:" + "a" * 64},
            "observed_runtime": {"gateway": "sha256:" + "a" * 64}}))
        (receipt_dir / "infra.json").chmod(0o600)
        (receipt_dir / "manifest.json").write_text(json.dumps({
            "source_revision": self.env["ADP_SOURCE_REVISION"], "account": ACCOUNT,
            "region": "us-east-1", "environment": "dev",
            "bindings_sha256": recorded["bindings_sha256"],
            "stages": {"infra": {"gateway": "sha256:" + "a" * 64}}}))
        (receipt_dir / "manifest.json").chmod(0o600)
        self.env["ADP_TOOLS_STAGE_RECEIPTS_DIR"] = str(receipt_dir)
        resume = subprocess.run([*command, "--resume"], env=self.env, cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(resume.returncode, 0, resume.stderr)
        self.assertIn("Resuming: infra independently verified", resume.stdout)
        self.assertEqual(self.tf_log.read_text().count(" apply "), 1)
        rerun = subprocess.run(command, env=self.env, cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(rerun.returncode, 0)
        self.assertEqual(self.tf_log.read_text().count(" apply "), 1)
        self.env["ADP_TOOLS_ENVIRONMENT"] = "prod"
        mismatch = subprocess.run([*command, "--resume"], env=self.env, cwd=ROOT,
                                  capture_output=True, text=True)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertIn("private tools state or configuration changed", mismatch.stderr)

    def test_no_change_plan_does_not_reapply_infrastructure(self):
        self.env["ADP_TOOLS_DEPLOY_STATE_FILE"] = str(self.directory / "deploy-state.json")
        self.plan.write_text(json.dumps({"resource_changes": [gateway_change(), *existing_target_changes()]}))
        result = subprocess.run(["bash", str(SCRIPT), "--infra"], env=self.env,
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 to add, 0 to change, 0 to destroy", result.stdout)
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertEqual(json.loads((self.directory / "deploy-state.json").read_text())["stages"]["infra"]["status"],
                         "command-succeeded")

    def test_stage_plan_never_applies_or_writes_state(self):
        self.env["ADP_TOOLS_DEPLOY_STATE_FILE"] = str(self.directory / "deploy-state.json")
        self.env["ADP_TOOLS_MODE"] = "plan"
        result = subprocess.run(["bash", str(SCRIPT), "--plan", "--infra", "--targets"],
                                env=self.env, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["Plan: infra", "Plan: targets"])
        self.assertFalse(self.tf_log.exists())
        self.assertFalse((self.directory / "deploy-state.json").exists())

    def test_plan_fails_before_apply_when_platform_prerequisites_are_missing(self):
        # The façade stage writes its own kubeconfig, so a missing local context is
        # not a prerequisite failure; the gateway URL parameter and the repository are.
        self.env.update(ADP_TOOLS_EKS_CLUSTER_NAME="example-eks", FAKE_MISSING_CONTEXT="true")
        command = ["bash", str(SCRIPT), "--plan", "--facade"]
        for missing, expected in [("FAKE_MISSING_SSM", "gateway invoke URL SSM parameter"),
                                  ("FAKE_MISSING_ECR", "ECR repository")]:
            self.env[missing] = "true"
            result = subprocess.run(command, env=self.env, cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)
            self.assertFalse(self.tf_log.exists())
            self.assertFalse(self.aws_log.exists())
            self.env.pop(missing)

    def test_tools_only_launch_uses_shared_script_and_rejects_conflicting_scopes(self):
        launcher = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        self.assertIn('--tools-only) TOOLS_ONLY=true', launcher)
        self.assertIn('"$AGENT_CONTEXT_ONLY" "$DEV_BOX_ONLY" "$INSIGHTS_ONLY" "$TOOLS_ONLY"', launcher)
        self.assertIn('exec bash "$ROOT_DIR/modules/tools/deploy.sh" "${args[@]}"', launcher)
        help_result = subprocess.run(["bash", str(ROOT / "platform/scripts/deploy-all.sh"), "--help"],
                                     env=self.env, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--tools-only", help_result.stdout)

    def test_mismatched_caller_role_cannot_publish(self):
        outputs = json.loads(self.outputs.read_text())
        outputs["caller_role_arn"]["value"] = f"arn:aws:iam::{ACCOUNT}:role/other-role"
        self.outputs.write_text(json.dumps(outputs))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.aws_log.exists())
        self.assertFalse(self.release_dir.exists())

    def test_plan_does_not_apply_or_publish(self):
        self.env["ADP_TOOLS_MODE"] = "plan"
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" plan ", self.tf_log.read_text())
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.aws_log.exists())
        self.assertFalse(self.release_dir.exists())

    def test_deploy_rejects_leftover_teardown_override(self):
        override = ROOT / "modules/tools/platform/infra/adp_teardown_target_override.tf.json"
        self.addCleanup(override.unlink, missing_ok=True)
        override.write_text('{}')
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("teardown provider/KMS override", result.stderr)
        self.assertFalse(self.tf_log.exists())

    def test_apply_refuses_non_ci_or_unreviewed_source(self):
        self.env["GITHUB_ACTIONS"] = "false"
        self.assertNotEqual(self.invoke().returncode, 0)
        self.env["GITHUB_ACTIONS"] = "true"
        self.env["FAKE_GIT_REJECT"] = "true"
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse(self.tf_log.exists())

    def test_failed_health_check_cannot_publish(self):
        self.env["FAKE_GATEWAY_STATUS"] = "FAILED"
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become READY", result.stderr)
        self.assertFalse(self.aws_log.exists())
        self.assertFalse(self.release_dir.exists())

    def test_clean_drift_exits_without_apply_or_publication(self):
        self.env.update(ADP_TOOLS_MODE="drift", GITHUB_EVENT_NAME="schedule")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-detailed-exitcode -lock=false", self.tf_log.read_text())
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.aws_log.exists())

    def test_changed_gateway_fails_naming_the_drift(self):
        self.env.update(ADP_TOOLS_MODE="drift", GITHUB_EVENT_NAME="schedule", FAKE_PLAN_RESULT="2")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tools platform drift: aws_bedrockagentcore_gateway.tools", result.stderr)
        self.assertNotIn(" apply ", self.tf_log.read_text())
        self.assertFalse(self.release_dir.exists())

    def test_failed_plan_is_not_reported_as_drift(self):
        self.env.update(ADP_TOOLS_MODE="drift", GITHUB_EVENT_NAME="schedule", FAKE_PLAN_RESULT="1")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Tools platform drift:", result.stderr)

    def test_drift_refuses_a_source_older_than_main(self):
        self.env.update(ADP_TOOLS_MODE="drift", GITHUB_EVENT_NAME="schedule", FAKE_MAIN_SHA="b" * 40)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse(self.tf_log.exists())

    def test_interceptor_alias_and_gateway_apply_share_serial_release_group(self):
        platform = yaml.safe_load(WORKFLOW.read_text())
        interceptor = yaml.safe_load((ROOT / ".github/workflows/tools-interceptor-deploy.yml").read_text())
        expected = "tools-release-${{ inputs.environment || 'dev' }}"
        for workflow in (platform, interceptor):
            self.assertEqual(workflow["concurrency"]["group"], expected)
            self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        apply = next(step for step in platform["jobs"]["release"]["steps"]
                     if "modules/tools/deploy.sh --infra --targets" in step.get("run", ""))
        self.assertEqual(apply["env"]["ADP_TOOLS_TFVARS_JSON"], "${{ secrets.ADP_TOOLS_DEPLOY_TFVARS_JSON }}")

    def test_workflow_release_requires_guard_and_environment(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        release = workflow["jobs"]["release"]
        self.assertEqual(release["needs"], "guard")
        self.assertIn("adp-tools-deploy-prod", release["environment"])
        self.assertIn("adp-tools-deploy-dev", release["environment"])
        self.assertIn("refs/heads/main", release["if"])
        self.assertTrue(any("modules/tools/deploy.sh --infra --targets" in step.get("run", "") for step in release["steps"]))
        self.assertEqual(workflow["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"], "s3-oidc-v1")
        self.assertFalse(any("upload-artifact" in step.get("uses", "") for step in release["steps"]))
        self.assertTrue(any('publish-deployment-evidence.py" release tools-platform' in step.get("run", "") for step in release["steps"]))
        self.assertTrue(any('publish-deployment-evidence.py" context tools-platform-deploy.yml' in step.get("run", "") for step in release["steps"]))
        self.assertTrue(any("deployment_evidence.py bind-release" in step.get("run", "") for step in release["steps"]))

    def test_scheduled_or_dispatched_drift_job_cannot_apply(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        self.assertIn("schedule", workflow[True])
        drift = workflow["jobs"]["drift"]
        self.assertEqual(drift["needs"], "guard")
        self.assertEqual(drift["environment"], "adp-tools-deploy-dev")
        self.assertIn("inputs.mode == 'drift'", drift["if"])
        self.assertIn("inputs.mode != 'drift'", workflow["jobs"]["release"]["if"])
        run_step = next(step for step in drift["steps"] if "bash modules/tools/deploy.sh --infra --targets" in step.get("run", ""))
        self.assertEqual(run_step["env"]["ADP_TOOLS_MODE"], "drift")


if __name__ == "__main__":
    unittest.main()
