"""Offline protocol/failure tests; no cloud identities or success evidence."""

import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "api_demo", Path(__file__).parents[1] / "api_demo.py"
)
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class Server:
    """Stateful API double using the published native lifecycle wire contracts."""

    url = "https://adp.example/api/superplane/v1"

    def __init__(self):
        self.calls = []
        self.tickets = {}
        self.operations = {}
        self.ready = True
        self.status = "Provisioning"
        self.fail_after_admission = False
        self.revision = "a" * 64
        self.identity_value = {"org_id": "org", "subject": "requester"}
        self.workspace = "workspace"
        self.operation_state = "succeeded"
        self.broker_denied = False
        self.streamlined = False

    def identity(self):
        return self.identity_value.copy()

    def allow(self):
        for ticket in self.tickets.values():
            ticket["result"] = "allowed-once"

    def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/capabilities":
            return {"ready": self.ready, "features": ["create-operation-id-v1"]}
        if path.endswith("/preview"):
            # Cleanup preparation has a distinct, server-derived idempotency key.
            key = (
                "cleanup-request"
                if "/retirement/access" in path
                else body["operation_id"]
            )
            return {
                "revision": self.revision,
                "admission_available": True,
                "workspace_id": self.workspace,
                "approval_request": {
                    "workspace_id": self.workspace,
                    "action": "teardown"
                    if path.endswith("/retirement/preview")
                    else "provision",
                    "idempotency_key": key,
                    "parameters": {"phase": path},
                },
            }
        if path == "/operation-approvals":
            approval_id = body["idempotency_key"]
            self.tickets.setdefault(
                approval_id,
                {
                    "approval_id": approval_id,
                    "workspace_id": self.workspace,
                    "request": {
                        "contract_version": "v1",
                        **{
                            k: copy.deepcopy(body[k])
                            for k in ("action", "idempotency_key", "parameters")
                        },
                    },
                    "plan_digest": "b" * 64,
                    "revoked": False,
                    "expires_at": "2999-01-01T00:00:00Z",
                    "result": "pending",
                    "requires_distinct_approver": not self.streamlined,
                    "can_decide": self.streamlined,
                },
            )
            return copy.deepcopy(self.tickets[approval_id])
        if path.endswith("/decision"):
            ticket = self.tickets[path.split("/")[-2]]
            assert ticket["can_decide"] and not ticket["requires_distinct_approver"]
            ticket["result"] = body["result"]
            return copy.deepcopy(ticket)
        if path.startswith("/operation-approvals/"):
            return copy.deepcopy(self.tickets[path.rsplit("/", 1)[1]])
        if path.startswith("/operations/by-idempotency/"):
            key = path.rsplit("/", 1)[1]
            return {
                "request_id": key,
                "workspace_id": self.workspace,
                "provisioning_operation_id": self.operations[key],
                "state": self.operation_state,
            }
        if method == "POST":
            if path not in {
                "/workspaces",
                "/workspaces/workspace/lifecycle-proposals/artifact/continue",
                "/workspaces/workspace/retirement/access",
                "/workspaces/workspace/retirement",
            }:
                raise AssertionError(f"Unexpected mutation {path}")
            key = self.tickets[body["approval_id"]]["request"]["idempotency_key"]
            operation = self.operations.setdefault(key, "op-" + key)
            if "/continue" in path:
                if "plan_revision" in body:
                    raise AssertionError("Continuation schema forbids plan_revision")
                self.status = "active"
            if path.endswith("/retirement"):
                self.status = "Deleted"
            if self.fail_after_admission:
                self.fail_after_admission = False
                raise demo.APIError(503, method, path)
            if path == "/workspaces":
                return {"id": self.workspace, "provisioning_operation_id": operation}
            field = (
                "control_operation_id" if path.endswith("/access") else "operation_id"
            )
            return {"workspace_id": self.workspace, field: operation}
        if path.endswith("/lifecycle-proposals"):
            return {
                "proposals": [
                    {
                        "artifact_id": "artifact",
                        "source_operation_id": next(iter(self.operations.values())),
                    }
                ]
            }
        if path == "/workspaces/workspace":
            return {"id": self.workspace, "org_id": "org", "status": self.status}
        if path.endswith("/nodes"):
            if self.broker_denied:
                raise demo.APIError(403, method, path)
            return {"nodes": [{"name": "real-node", "ready": "True"}], "total": 1}
        if path.endswith("/lifecycle"):
            return {"status": self.status, "billing_state": "unconfirmed"}
        raise AssertionError((method, path, body))


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "state"
        self.store = demo.Store(self.directory)
        self.server = Server()
        self.store.data.update(
            version=1,
            base_url=self.server.url,
            identity=self.server.identity(),
            request={"name": "demo", "operation_id": "create-request"},
            history=[],
        )
        self.store.save()
        self.runner = demo.Demo(self.server, self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def restart(self):
        self.store.close()
        self.store = demo.Store(self.directory)
        self.runner = demo.Demo(self.server, self.store)
        self.runner.bind()

    def pending(self, retire=False):
        with self.assertRaisesRegex(demo.Stop, "Awaiting explicit approval"):
            self.runner.step(retire=retire)

    def reach_ready(self):
        self.pending()
        self.server.allow()
        self.pending()  # create admitted; continuation requires another approval
        self.server.allow()
        self.assertTrue(self.runner.step())

    def test_streamlined_flow_uses_explicit_requester_decisions(self):
        self.server.streamlined = True
        self.runner.approve_own_plans = True
        self.runner.step()
        self.assertTrue(self.runner.step())
        decisions = [
            call for call in self.server.calls if call[1].endswith("/decision")
        ]
        self.assertEqual(len(decisions), 2)
        self.restart()
        self.assertTrue(self.runner.step())
        self.assertEqual(
            len([call for call in self.server.calls if call[1].endswith("/decision")]),
            2,
        )

    def test_ready_check_rejects_empty_or_unready_nodes(self):
        self.reach_ready()
        original = self.server.call
        for nodes in ([], [{"name": "pending-node", "ready": "False"}]):

            def reply(method, path, body=None):
                return (
                    {"nodes": nodes}
                    if path.endswith("/nodes")
                    else original(method, path, body)
                )

            with patch.object(self.server, "call", side_effect=reply):
                with self.assertRaisesRegex(demo.Stop, "not Ready"):
                    self.runner.verify_ready()

    def test_automation_option_does_not_override_strict_server_policy(self):
        self.runner.approve_own_plans = True
        self.pending()
        self.assertFalse(
            any(call[1].endswith("/decision") for call in self.server.calls)
        )

    def test_streamlined_policy_alone_does_not_approve(self):
        self.server.streamlined = True
        self.pending()
        self.assertFalse(
            any(call[1].endswith("/decision") for call in self.server.calls)
        )

    def test_full_lifecycle_with_restarts_and_separate_cleanup_approval(self):
        self.pending()
        self.restart()
        self.server.allow()
        self.pending()
        self.restart()
        self.server.allow()
        self.assertTrue(self.runner.step())
        self.restart()
        self.assertTrue(self.runner.step())  # actual re-entry with fresh client state
        self.pending(retire=True)
        self.server.allow()
        self.pending(retire=True)  # cleanup-access is distinct from removal
        self.restart()
        self.server.allow()
        self.assertTrue(self.runner.step(retire=True))
        self.assertIn("ready_verified_at", self.store.data)
        self.assertIn("retirement_api_verified_at", self.store.data)
        self.assertEqual(len(self.server.operations), 4)
        self.assertFalse(any(call[0] == "DELETE" for call in self.server.calls))

    def test_lost_admission_response_replays_exact_request_after_restart(self):
        self.pending()
        self.server.allow()
        self.server.fail_after_admission = True
        with self.assertRaises(demo.APIError):
            self.runner.step()
        first = copy.deepcopy(self.store.data["pending"]["submission"])
        self.restart()
        self.pending()
        creates = [c[2] for c in self.server.calls if c[:2] == ("POST", "/workspaces")]
        self.assertEqual(creates, [first, first])
        self.assertEqual(len(self.server.operations), 1)

    def test_pending_approval_is_idempotent(self):
        self.pending()
        self.restart()
        self.pending()
        self.assertEqual(len(self.server.tickets), 1)
        self.assertEqual(len(self.server.operations), 0)

    def test_changed_preview_never_submits(self):
        self.pending()
        self.server.allow()
        self.server.revision = "c" * 64
        with self.assertRaisesRegex(demo.Stop, "Plan changed"):
            self.runner.step()
        self.assertFalse(self.server.operations)

    def test_rejected_or_expired_ticket_never_submits(self):
        self.pending()
        ticket = self.server.tickets["create-request"]
        ticket["result"] = "rejected"
        with self.assertRaisesRegex(demo.Stop, "rejected"):
            self.runner.step()
        ticket.update(result="allowed-once", expires_at="2000-01-01T00:00:00Z")
        with self.assertRaisesRegex(demo.Stop, "expired"):
            self.runner.step()
        self.assertFalse(self.server.operations)

    def test_ticket_payload_mismatch_refused(self):
        self.pending()
        self.server.tickets["create-request"]["request"]["parameters"]["budget"] = "999"
        self.server.allow()
        with self.assertRaisesRegex(demo.Stop, "does not match"):
            self.runner.step()
        self.assertFalse(self.server.operations)

    def test_unknown_outcome_does_not_recreate_workspace(self):
        self.reach_ready()
        count = len(self.server.operations)
        self.server.operation_state = "unknown"
        with self.assertRaisesRegex(demo.Stop, "outcome is unknown"):
            self.runner.step()
        self.assertEqual(len(self.server.operations), count)

    def test_runtime_unavailable_causes_no_mutation(self):
        self.server.ready = False
        with self.assertRaisesRegex(demo.Stop, "runtime is not ready"):
            self.runner.step()
        self.assertFalse(any(c[0] == "POST" for c in self.server.calls))

    def test_scope_change_refused(self):
        self.server.identity_value["subject"] = "someone-else"
        with self.assertRaisesRegex(demo.Stop, "requester changed"):
            self.runner.bind()

    def test_broker_failure_does_not_claim_ready(self):
        self.pending()
        self.server.allow()
        self.pending()
        self.server.allow()
        self.server.broker_denied = True
        with self.assertRaises(demo.APIError):
            self.runner.step()
        self.assertNotIn("ready_verified_at", self.store.data)

    def test_retire_cannot_create_workspace(self):
        self.pending()
        self.server.allow()
        with self.assertRaisesRegex(demo.Stop, "Finish workspace creation"):
            self.runner.step(retire=True)
        self.assertFalse(self.server.operations)

    def test_running_retirement_is_not_success(self):
        self.reach_ready()
        self.pending(retire=True)
        self.server.allow()
        self.pending(retire=True)
        self.server.allow()
        self.server.operation_state = "running"
        self.assertFalse(self.runner.step(retire=True))
        self.assertNotIn("retirement_api_verified_at", self.store.data)

    def test_same_state_directory_cannot_run_concurrently(self):
        with self.assertRaisesRegex(demo.Stop, "Another demo process"):
            demo.Store(self.directory)

    def test_private_state_permissions(self):
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_decision_requires_exact_digest_and_eligible_caller(self):
        self.pending()
        ticket = self.server.tickets["create-request"]
        ticket["can_decide"] = True
        with patch.object(demo, "API", return_value=self.server):
            code = demo.main(
                [
                    "--base-url",
                    self.server.url,
                    "--token-file",
                    "unused",
                    "decide",
                    "--approval-id",
                    "create-request",
                    "--plan-digest",
                    "wrong",
                    "--decision",
                    "allowed-once",
                ]
            )
        self.assertEqual(code, 2)
        self.assertFalse(any(c[1].endswith("/decision") for c in self.server.calls))


class TransportTests(unittest.TestCase):
    def test_https_only_and_no_credentials_in_url(self):
        for url in (
            "http://adp.example",
            "https://user:pass@adp.example",
            "https://adp.example?token=x",
        ):
            with self.assertRaises(demo.Stop):
                demo.base_url(url)

    def test_bearer_token_never_follows_redirect(self):
        with self.assertRaisesRegex(demo.Stop, "redirect refused"):
            demo.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example"
            )

    def test_token_file_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("secret-token")
            path.chmod(0o644)
            with self.assertRaisesRegex(demo.Stop, "private"):
                demo.API("https://adp.example", path).token()


if __name__ == "__main__":
    unittest.main()
