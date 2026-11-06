"""Retirement may remove only observed grants after ingress and jobs quiesce."""

import importlib.util
import json
import copy
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "chat", Path(__file__).resolve().parents[1] / "upgrade-chat.py"
)
chat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chat)
ACCOUNT = "123456789012"
ROLE = "adp-dev-chat-worker-role"


def change(address, before, after, actions, kind="aws_iam_role_policy"):
    return {
        "address": address,
        "type": kind,
        "change": {"before": before, "after": after, "actions": actions},
    }


class RetirementTests(unittest.TestCase):
    def fixture(self):
        document = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["sqs:ReceiveMessage"],
                    "Resource": "queue",
                }
            ],
        }
        old = {
            "role": ROLE,
            "name": "chat-sqs",
            "id": ROLE + ":chat-sqs",
            "policy": json.dumps(document),
        }
        removed = change(
            'aws_iam_role_policy.chat_worker_runtime["sqs"]', old, None, ["delete"]
        )
        role = {
            "name": ROLE,
            "arn": f"arn:aws:iam::{ACCOUNT}:role/{ROLE}",
            "permissions_boundary": None,
        }
        denial = {
            "role": ROLE,
            "name": "deny-direct-chat-authority",
            "policy": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Deny",
                            "Action": sorted(chat.DENIED),
                            "Resource": "*",
                        }
                    ],
                }
            ),
        }
        plan = {
            "variables": {
                k: {"value": v}
                for k, v in {
                    "environment": "dev",
                    "aws_region": "us-east-1",
                    "chat_legacy_upgrade_policies": {},
                }.items()
            },
            "resource_changes": [
                removed,
                change(
                    "aws_iam_role.chat_worker", role, role, ["no-op"], "aws_iam_role"
                ),
                change(
                    "aws_iam_role_policy.chat_worker_deny_direct_authority[0]",
                    denial,
                    denial,
                    ["no-op"],
                ),
            ],
        }
        receipt = {
            "account": ACCOUNT,
            "region": "us-east-1",
            "environment": "dev",
            "phase": "drained",
            "policies": {"chat-sqs": chat.digest(document)},
        }
        return removed, plan, receipt

    def terraform_denial(self):
        # Use the shipped IAM actions, not the verifier constant: duplicating
        # that constant in fixtures hid the AgentCore policy/checker mismatch.
        source = (Path(__file__).resolve().parents[3] /
                  "modules/agent-factory/infra/chat-worker-iam.tf").read_text()
        block = source.split('resource "aws_iam_role_policy" "chat_worker_deny_direct_authority" {', 1)[1].split('\nresource ', 1)[0]
        actions = re.search(r'Action\s*=\s*\[(.*?)\]', block, re.S).group(1)
        return {"Version": "2012-10-17", "Statement": [{"Effect": "Deny",
                "Action": re.findall(r'"([^"\n]+)"', actions), "Resource": "*"}]}

    def test_shipped_denial_passes_live_verification_and_removal_gate(self):
        document = self.terraform_denial()
        self.assertIn("bedrock-agentcore:*", document["Statement"][0]["Action"])
        self.assertTrue(chat.retirement_denial(document))
        resource, plan, receipt = self.fixture()
        for side in ("before", "after"):
            plan["resource_changes"][-1]["change"][side]["policy"] = json.dumps(document)
        self.assertTrue(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))
        session = Mock()
        iam = session.client.return_value
        iam.list_role_policies.return_value = {"PolicyNames": sorted(chat.CURRENT)}
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        iam.get_role_policy.return_value = {"PolicyDocument": document}
        with patch.object(chat, "require_ingress"), patch.object(chat, "queue_empty"), \
             patch.object(chat, "kube", return_value=None), patch.object(chat, "work_remaining", return_value=[]):
            self.assertEqual(chat.verify(session, ACCOUNT, "us-east-1", "dev")["status"], "retired")

    def test_previous_denial_can_prepare_but_cannot_accept_retirement(self):
        for previous in (False, True):
            document = self.terraform_denial()
            if previous:
                document["Statement"][0]["Action"].remove("bedrock-agentcore:*")
            session = Mock()
            iam = session.client.return_value
            iam.list_role_policies.return_value = {"PolicyNames": sorted(chat.CURRENT)}
            iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
            iam.get_role_policy.return_value = {"PolicyDocument": document}
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as tmp:
                chat.prepare(session, Path(tmp), ACCOUNT, "us-east-1", "dev")
                settings = json.loads((Path(tmp) / "chat-retirement.tfvars.json").read_text())
                self.assertTrue(settings["chat_legacy_upgrade_deny_ready"])
                self.assertEqual(settings["chat_legacy_upgrade_policies"], {})
            self.assertEqual(chat.retirement_denial(document), not previous)
            if previous:
                resource, plan, receipt = self.fixture()
                for side in ("before", "after"):
                    plan["resource_changes"][-1]["change"][side]["policy"] = json.dumps(document)
                self.assertFalse(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))
                with patch.object(chat, "require_ingress"), patch.object(chat, "queue_empty"), \
                     patch.object(chat, "kube", return_value=None), patch.object(chat, "work_remaining", return_value=[]):
                    with self.assertRaisesRegex(ValueError, "denial is incomplete"):
                        chat.verify(session, ACCOUNT, "us-east-1", "dev")

    def test_changed_denial_scope_is_rejected_including_during_preparation(self):
        current = self.terraform_denial()
        variants = []
        for action in current["Statement"][0]["Action"]:
            if action == "bedrock-agentcore:*":
                continue  # The one recognized previous-release policy.
            document = copy.deepcopy(current)
            document["Statement"][0]["Action"].remove(action)
            variants.append(document)
        for key, value in (("Effect", "Allow"), ("Resource", "queue"),
                           ("Condition", {"Bool": {"aws:SecureTransport": "false"}})):
            document = copy.deepcopy(current)
            document["Statement"][0][key] = value
            variants.append(document)
        for document in variants:
            with self.subTest(document=document):
                self.assertFalse(chat.retirement_denial(document))
                self.assertFalse(chat.retirement_denial(document, allow_previous=True))

    def test_exact_observed_policy_removal_after_drain(self):
        resource, plan, receipt = self.fixture()
        self.assertTrue(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))

    def test_other_account_phase_policy_or_role_is_not_authorized(self):
        for field, value in [
            ("account", "999999999999"),
            ("region", "eu-west-1"),
            ("phase", "prepared"),
            ("policies", {}),
        ]:
            resource, plan, receipt = self.fixture()
            receipt[field] = value
            self.assertFalse(
                chat.permitted_deletion(resource, plan, ACCOUNT, receipt), field
            )
        for field, value in [
            ("role", "other"),
            ("name", "other"),
            ("id", "other"),
            ("policy", "{}"),
        ]:
            resource, plan, receipt = self.fixture()
            resource["change"]["before"][field] = value
            self.assertFalse(
                chat.permitted_deletion(resource, plan, ACCOUNT, receipt), field
            )

    def test_missing_or_weaker_denial_and_role_replacement_stay_blocked(self):
        resource, plan, receipt = self.fixture()
        plan["resource_changes"][-1]["change"]["after"]["policy"] = json.dumps(
            {
                "Statement": [
                    {"Effect": "Allow", "Action": sorted(chat.DENIED), "Resource": "*"}
                ]
            }
        )
        self.assertFalse(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))
        resource, plan, receipt = self.fixture()
        plan["resource_changes"][1]["change"]["actions"] = ["delete", "create"]
        self.assertFalse(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))

    def test_replacement_denial_must_already_be_installed(self):
        resource, plan, receipt = self.fixture()
        plan["resource_changes"][-1]["change"]["before"] = None
        plan["resource_changes"][-1]["change"]["actions"] = ["create"]
        self.assertFalse(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))

    def test_retained_policies_cannot_authorize_deletions(self):
        resource, plan, receipt = self.fixture()
        plan["variables"]["chat_legacy_upgrade_policies"]["value"] = {"sqs": "{}"}
        self.assertFalse(chat.permitted_deletion(resource, plan, ACCOUNT, receipt))

    def test_active_pods_include_pending_and_terminating_not_other_workers(self):
        pods = {
            "items": [
                {
                    "metadata": {"name": phase},
                    "spec": {"serviceAccountName": "adp-agent"},
                    "status": {"phase": phase},
                }
                for phase in ["Pending", "Running", "Succeeded", "Failed"]
            ]
        }
        pods["items"].append(
            {
                "metadata": {"name": "python"},
                "spec": {"serviceAccountName": "adp-gateway-worker"},
                "status": {"phase": "Running"},
            }
        )
        self.assertEqual(chat.active_pods(pods), ["Pending", "Running"])

    def test_jobs_without_pods_are_still_work_that_must_drain(self):
        pending = {
            "metadata": {"name": "waiting"},
            "spec": {"template": {"spec": {"serviceAccountName": "adp-agent"}}},
        }
        finished = {
            **pending,
            "status": {"conditions": [{"type": "Complete", "status": "True"}]},
        }
        self.assertEqual(chat.active_jobs({"items": [pending, finished]}), ["waiting"])

    def test_prepare_preserves_only_the_observed_policy_bytes(self):
        session = Mock()
        iam = session.client.return_value
        document = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["sqs:ReceiveMessage"],
                    "Resource": "queue",
                }
            ],
        }
        iam.list_role_policies.return_value = {"PolicyNames": ["chat-sqs"]}
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        iam.get_role_policy.return_value = {"PolicyDocument": document}
        with tempfile.TemporaryDirectory() as tmp:
            chat.prepare(session, Path(tmp), ACCOUNT, "us-east-1", "dev")
            variables = json.loads(
                (Path(tmp) / "chat-retirement.tfvars.json").read_text()
            )
            self.assertEqual(
                variables,
                {
                    "chat_legacy_upgrade_policies": {"sqs": json.dumps(document)},
                    "chat_legacy_upgrade_deny_ready": False,
                },
            )
        iam.put_role_policy.assert_not_called()
        iam.delete_role_policy.assert_not_called()

    def test_nonempty_or_incomplete_queue_does_not_get_consumed(self):
        for attrs in [
            {},
            {
                "ApproximateNumberOfMessages": "1",
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
            },
        ]:
            session = Mock()
            sqs = session.client.return_value
            sqs.get_queue_url.return_value = {"QueueUrl": "url"}
            sqs.get_queue_attributes.return_value = {"Attributes": attrs}
            with self.assertRaises(ValueError):
                chat.queue_empty(session, ACCOUNT, "us-east-1", "dev")
            sqs.receive_message.assert_not_called()
            sqs.purge_queue.assert_not_called()

    def test_ingress_must_match_package_and_be_unavailable_before_any_mutation(self):
        with (
            patch.object(
                chat, "require_ingress", side_effect=ValueError("wrong image")
            ),
            patch.object(chat, "kube") as kube,
        ):
            with self.assertRaises(ValueError):
                chat.drain(Mock(), Path("/unused"), ACCOUNT, "us-east-1", "dev")
            kube.assert_not_called()

    def test_queued_work_prevents_pausing_and_deletion(self):
        with (
            patch.object(chat, "require_ingress", return_value="hash"),
            patch.object(chat, "queue_empty", side_effect=ValueError("work")),
            patch.object(chat, "kube") as kube,
        ):
            with self.assertRaises(ValueError):
                chat.drain(Mock(), Path("/unused"), ACCOUNT, "us-east-1", "dev")
            kube.assert_not_called()

    def test_empty_idle_retirement_pauses_before_deleting_without_deleting_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            chat.write(
                p / "chat-retirement.json",
                {
                    "account": ACCOUNT,
                    "region": "us-east-1",
                    "environment": "dev",
                    "phase": "prepared",
                    "policies": {},
                },
            )
            with (
                patch.object(chat, "require_ingress", return_value="hash"),
                patch.object(chat, "queue_empty") as empty,
                patch.object(chat.time, "sleep"),
                patch.object(chat, "kube") as kube,
                patch.object(chat.subprocess, "run") as run,
            ):
                kube.side_effect = [{"metadata": {"resourceVersion": "5"}}, {}] + [
                    {"items": []}
                ] * 6
                chat.drain(Mock(), p, ACCOUNT, "us-east-1", "dev")
                self.assertEqual(
                    kube.call_args_list[1].args[:2], ("patch", "scaledjob")
                )
                self.assertIn("--cascade=orphan", run.call_args.args[0])
                self.assertEqual(run.call_args.args[0][2], "scaledjob")
                self.assertEqual(empty.call_count, 2)
                self.assertEqual(
                    json.loads((p / "chat-retirement.json").read_text())["phase"],
                    "drained",
                )

    def test_prepare_refuses_unknown_or_mixed_policies(self):
        for names in [["unknown"], ["chat-sqs", "deny-direct-chat-authority"]]:
            session = Mock()
            iam = session.client.return_value
            iam.list_role_policies.return_value = {"PolicyNames": names}
            iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
            iam.get_role_policy.return_value = {"PolicyDocument": {}}
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                chat.prepare(session, Path(tmp), ACCOUNT, "us-east-1", "dev")


def scaledjob(service_account):
    return {
        "metadata": {"name": chat.NAME, "resourceVersion": "9"},
        "spec": {"jobTargetRef": {"template": {"spec": {"serviceAccountName": service_account}}}},
    }


class DelegatedModeTests(unittest.TestCase):
    """--delegated (deploy-all.sh --chat delegated, #7899): the supervisor ScaledJob
    reuses the legacy name and must survive retirement; legacy work must still drain."""

    def lambda_session(self, policy_enabled):
        session = Mock()
        session.client.return_value.get_function_configuration.return_value = {
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "CodeSha256": "ZmFrZQ==",
            "Environment": {"Variables": {
                "ADP_CHAT_LEGACY_RETIRED": "true",
                "ADP_CHAT_MODEL_POLICY_ENABLED": policy_enabled,
            }},
        }
        return session

    def test_delegated_ingress_requires_model_policy_enabled_and_retire_requires_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "ingest.zip"
            package.write_bytes(b"fake")
            with patch.object(chat.base64, "b64encode", return_value=b"ZmFrZQ=="):
                for policy, delegated, accepted in (
                    ("true", True, True), ("false", True, False),
                    ("false", False, True), ("true", False, False),
                ):
                    with self.subTest(policy=policy, delegated=delegated):
                        session = self.lambda_session(policy)
                        if accepted:
                            chat.require_ingress(session, "dev", package, delegated=delegated)
                        else:
                            with self.assertRaises(ValueError):
                                chat.require_ingress(session, "dev", package, delegated=delegated)

    def verifying_session(self):
        session = Mock()
        iam = session.client.return_value
        iam.list_role_policies.return_value = {"PolicyNames": sorted(chat.CURRENT)}
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        iam.get_role_policy.return_value = {"PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Deny", "Action": sorted(chat.DENIED), "Resource": "*"}],
        }}
        return session

    def test_verify_accepts_only_the_supervisor_scaledjob_in_delegated_mode(self):
        session = self.verifying_session()
        with patch.object(chat, "require_ingress") as ingress, patch.object(chat, "queue_empty") as queue, \
             patch.object(chat, "kube", return_value=scaledjob("adp-chat-supervisor")), \
             patch.object(chat, "work_remaining", return_value=[]):
            result = chat.verify(session, ACCOUNT, "us-east-1", "dev", delegated=True)
            self.assertEqual(result["status"], "delegated")
            self.assertTrue(ingress.call_args.kwargs["delegated"])
            queue.assert_not_called()  # the supervisor owns the shared FIFO queue now
        for service_account in ("adp-agent", None):
            with self.subTest(service_account=service_account), \
                 patch.object(chat, "require_ingress"), patch.object(chat, "queue_empty"), \
                 patch.object(chat, "kube", return_value=scaledjob(service_account)), \
                 patch.object(chat, "work_remaining", return_value=[]):
                with self.assertRaisesRegex(ValueError, "still exists"):
                    chat.verify(session, ACCOUNT, "us-east-1", "dev", delegated=True)
        # Without --delegated the supervisor ScaledJob is still a retirement failure.
        with patch.object(chat, "require_ingress"), patch.object(chat, "queue_empty"), \
             patch.object(chat, "kube", return_value=scaledjob("adp-chat-supervisor")), \
             patch.object(chat, "work_remaining", return_value=[]):
            with self.assertRaisesRegex(ValueError, "still exists"):
                chat.verify(session, ACCOUNT, "us-east-1", "dev")
        with patch.object(chat, "require_ingress"), patch.object(chat, "queue_empty"), \
             patch.object(chat, "kube", return_value=None), \
             patch.object(chat, "work_remaining", return_value=[]):
            with self.assertRaisesRegex(ValueError, "missing"):
                chat.verify(session, ACCOUNT, "us-east-1", "dev", delegated=True)

    def test_verify_still_fails_while_legacy_pods_run_in_delegated_mode(self):
        with patch.object(chat, "require_ingress"), patch.object(chat, "queue_empty"), \
             patch.object(chat, "kube", return_value=scaledjob("adp-chat-supervisor")), \
             patch.object(chat, "work_remaining", return_value=["legacy-pod"]):
            with self.assertRaisesRegex(ValueError, "running pods"):
                chat.verify(self.verifying_session(), ACCOUNT, "us-east-1", "dev", delegated=True)

    def test_delegated_drain_leaves_the_supervisor_scaledjob_and_waits_for_legacy_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            chat.write(p / "chat-retirement.json", {
                "account": ACCOUNT, "region": "us-east-1", "environment": "dev",
                "phase": "prepared", "policies": {},
            })
            with patch.object(chat, "require_ingress", return_value="hash") as ingress, \
                 patch.object(chat, "queue_empty") as queue, \
                 patch.object(chat.time, "sleep") as sleep, \
                 patch.object(chat, "kube") as kube, \
                 patch.object(chat.subprocess, "run") as run:
                # One legacy (adp-agent) job is still running on the first observation.
                kube.side_effect = [scaledjob("adp-chat-supervisor")] + [
                    {"items": [{"metadata": {"name": "legacy"},
                                "spec": {"template": {"spec": {"serviceAccountName": "adp-agent"}}}}]},
                    {"items": []},
                ] + [{"items": []}] * 6
                chat.drain(Mock(), p, ACCOUNT, "us-east-1", "dev", delegated=True)
                self.assertTrue(ingress.call_args.kwargs["delegated"])
                queue.assert_not_called()
                self.assertFalse(any(c.args[0] == "patch" for c in kube.call_args_list))
                run.assert_not_called()  # neither paused nor deleted
                sleep.assert_called()  # waited for the legacy pod to finish
                self.assertEqual(json.loads((p / "chat-retirement.json").read_text())["phase"], "drained")

    def test_delegated_drain_refuses_when_the_legacy_scaledjob_is_still_in_place(self):
        with patch.object(chat, "require_ingress", return_value="hash"), \
             patch.object(chat, "queue_empty"), \
             patch.object(chat, "kube", return_value=scaledjob("adp-agent")), \
             patch.object(chat.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "applied and Ready"):
                chat.drain(Mock(), Path("/unused"), ACCOUNT, "us-east-1", "dev", delegated=True)
            run.assert_not_called()

    def test_cli_exposes_the_delegated_flag_only_for_drain_and_verify(self):
        source = (Path(__file__).resolve().parents[1] / "upgrade-chat.py").read_text()
        self.assertIn('"--delegated"', source)
        self.assertIn("delegated=args.delegated", source)
        deploy_all = (Path(__file__).resolve().parents[3] / "platform/scripts/deploy-all.sh").read_text()
        self.assertIn("CHAT_RETIREMENT_ARGS+=(--delegated)", deploy_all)


if __name__ == "__main__":
    unittest.main()
