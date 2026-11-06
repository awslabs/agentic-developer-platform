import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from campaign_adp import ADPFixtureBackend, run_selected
from campaign_controller import ConfigError, checkpoint, load_config, setup_fixtures
from campaign_recovery import RecoveryError, RecoveryJournal, load_recovery_config, resume_cleanup


class ADPFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.token = self.root / "issued-token"
        self.admin = self.root / "admin-token"
        self.health = self.root / "health.json"
        self.write(self.admin, "test-admin-secret")
        self.config = {
            "schema_version": 1, "backend": "adp", "campaign": "owned-campaign", "owner": "fixture-owner",
            "journal_dir": str(self.root), "target": "https://gateway.example.invalid",
            "revision": "exact-test-revision", "approved_account": "000000000000",
            "approved_role": "arn:aws:sts::000000000000:assumed-role/fixture-recovery",
            "aws_connection": "selected-fixture-connection", "aws_region": "us-east-1",
            "user_pool_id": "us-east-1_example", "gateway_admin_token_file": str(self.admin),
            "health_file": str(self.health), "health_owner": "signal-owner", "cleanup_seconds": 20,
            "fixtures": [
                {"kind": "identity", "binding": "owned-sub", "username": "owned-user", "tenant": "owned-tenant",
                 "root_human": "owned-sub", "ownership_receipt": "owned-receipt", "desired": {"enabled": True}},
                {"kind": "quota", "binding": "owned-sub", "ownership_receipt": "owned-receipt",
                 "desired": {"rpm": 20, "tpm": 200, "concurrent_requests": 2}},
                {"kind": "secret", "binding": str(self.token), "identity": "owned-sub",
                 "password_secret_id": "owned-password-reference", "client_id": "owned-client",
                 "ownership_receipt": "owned-receipt", "desired": {"reference": str(self.token)}},
            ],
        }
        health = {key: self.config[key] for key in ("campaign", "target", "revision", "approved_account", "approved_role")}
        health.update(owner="signal-owner", observed_at=time.time(), healthy=True,
                      no_owned_active_clients=True, normal_user_denials_ok=True)
        self.write(self.health, json.dumps(health))
        self.config_file = self.root / "recovery.json"
        self.write(self.config_file, json.dumps(self.config))
        self.user = {"Username": "owned-user", "Enabled": True, "UserLastModifiedDate": "initial",
                     "UserAttributes": [{"Name": "sub", "Value": "owned-sub"}]}
        self.membership = {"subject": "owned-sub", "tenant": "owned-tenant", "value": {"enabled": False}, "version": "initial"}
        self.calls = []
        self.quota = {"rpm": 3, "tpm": None, "concurrent_requests": 1,
                      "updated_at": "2026-01-01T00:00:00+00:00"}
        self.receipts = {}
        self.lost_quota_reply = False
        self.process = patch("campaign_adp.run_selected", side_effect=self.aws)
        self.process.start()
        self.addCleanup(self.process.stop)
        self.real_http = ADPFixtureBackend._http
        self.transport = patch.object(ADPFixtureBackend, "_http", side_effect=self.http)
        self.transport.start()
        self.addCleanup(self.transport.stop)
        self.backend = ADPFixtureBackend(self.config)
        self.journal = RecoveryJournal(self.root, self.config["campaign"], self.config["owner"])
        self.journal.bind_recovery(self.config, initialize=True)

    def write(self, path, text):
        path.write_text(text)
        path.chmod(0o600)

    def observe_health(self):
        health = json.loads(self.health.read_text())
        health["observed_at"] = time.time()
        self.write(self.health, json.dumps(health))

    def aws(self, command, **kwargs):
        self.assertEqual(command[:7], ["adp-cred", "assume", "--service", "aws", "--label",
                                      "selected-fixture-connection", "--exec"])
        self.assertEqual(command[-2:], ["--cli-input-json", "file:///dev/stdin"])
        self.assertNotIn("password-never-log", " ".join(command))
        operation = command[9]
        payload = json.loads(kwargs["input"])
        self.calls.append((operation, payload))
        if operation == "get-caller-identity":
            value = {"Account": self.config["approved_account"], "Arn": self.config["approved_role"] + "/session"}
        elif operation == "admin-get-user":
            self.assertEqual(payload, {"UserPoolId": self.config["user_pool_id"], "Username": "owned-user"})
            value = self.user
        elif operation == "get-secret-value":
            self.assertEqual(payload, {"SecretId": "owned-password-reference"})
            value = {"SecretString": json.dumps({"username": "owned-user", "password": "password-never-log"})}
        elif operation == "admin-initiate-auth":
            self.assertEqual(payload["AuthFlow"], "ADMIN_USER_PASSWORD_AUTH")
            self.assertEqual(payload["AuthParameters"]["PASSWORD"], "password-never-log")
            value = {"AuthenticationResult": {"AccessToken": "access-token-never-log"}}
        else:
            self.fail("unexpected AWS operation " + operation)
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    def http(self, method, route, payload=None, *, token=None):
        if route == "/health/detailed":
            return {"all_healthy": True}
        if route == "/admin/campaign-identity":
            self.assertEqual(token, "access-token-never-log")
            return ({"subject": "owned-sub", "tenant": "owned-tenant"} if self.membership["value"]["enabled"]
                    else {"unusable": True})
        if "/campaign-identities/" in route:
            if "/operations/" not in route:
                return copy.deepcopy(self.membership)
            operation_id = route.rsplit("/", 1)[-1]
            if method == "GET":
                return copy.deepcopy(self.receipts.get(operation_id))
            if self.membership["version"] != payload["expected_version"]:
                raise RecoveryError("membership changed")
            before = copy.deepcopy(self.membership)
            self.membership.update(value={"enabled": payload["enabled"]}, version=operation_id)
            result = {"before": before, "after": copy.deepcopy(self.membership), "request": payload}
            self.receipts[operation_id] = result
            self.observe_health()
            return result
        self.assertTrue(route.startswith("/admin/organizations/owned-tenant/ratelimit-cli/user/owned-sub"))
        if "/campaign/" in route:
            operation_id = route.rsplit("/", 1)[-1]
            if method == "GET":
                return copy.deepcopy(self.receipts.get(operation_id))
            self.assertEqual(method, "PUT")
            self.assertEqual(payload["expected_revision"], self.quota["updated_at"] if self.quota else None)
            before = copy.deepcopy(self.quota)
            self.quota = (dict(payload["desired"], updated_at=f"2026-01-01T00:00:0{len(self.receipts) + 1}+00:00")
                          if payload["desired"] is not None else None)
            receipt = {"operation_id": operation_id, "request": payload, "before": before, "after": self.quota}
            self.receipts[operation_id] = copy.deepcopy(receipt)
            self.observe_health()
            if self.lost_quota_reply:
                self.lost_quota_reply = False
                raise TimeoutError("response lost after committed quota and receipt")
            return receipt
        return {"org_id": "owned-tenant", "entity_id": "owned-sub", "entity_type": "user", "saved": self.quota}

    def mutate(self, kind):
        fixture = next(item for item in self.config["fixtures"] if item["kind"] == kind)
        return self.journal.mutate(self.backend, kind, fixture["binding"], fixture["desired"], fixture["ownership_receipt"])

    def expire(self):
        with self.journal._locked() as record:
            record["lease_until"] = time.time() - 1
            self.journal._save(record)

    def test_restart_revokes_membership_and_removes_only_owned_cache(self):
        unrelated = self.root / "unrelated"
        self.write(unrelated, "preserve")
        for kind in ("identity", "quota", "secret"):
            self.mutate(kind)
        self.assertTrue(self.user["Enabled"])
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.journal.record_attempt("inflight", 10, 20, 0.01)
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 5)
        record = json.loads(self.journal.path.read_text())
        self.assertEqual(record["receipt"]["status"], "clean")
        self.assertEqual(record["receipt"]["accounting"], "unknown")
        self.assertFalse(self.membership["value"]["enabled"])
        self.assertTrue(self.user["Enabled"])
        self.assertEqual(self.quota["rpm"], 3)
        self.assertFalse(self.token.exists())
        self.assertEqual(unrelated.read_text(), "preserve")
        for path in (self.journal.path, self.backend.path):
            self.assertNotIn("password-never-log", path.read_text())
            self.assertNotIn("access-token-never-log", path.read_text())

    def test_lost_quota_reply_is_reconciled_through_transactional_receipt(self):
        self.lost_quota_reply = True
        with self.assertRaises(TimeoutError):
            self.mutate("quota")
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 0)
        self.assertEqual(self.quota["rpm"], 3)
        self.assertEqual(len(self.receipts), 2)

    def test_unrelated_edit_and_replaced_cache_are_held_not_overwritten(self):
        for kind in ("identity", "quota", "secret"):
            self.mutate(kind)
        self.quota.update(rpm=70, updated_at="operator-version")
        self.token.unlink()
        self.write(self.token, "replacement-credential")
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertEqual(self.quota["rpm"], 70)
        self.assertEqual(self.token.read_text(), "replacement-credential")
        self.assertFalse(self.membership["value"]["enabled"])
        self.assertEqual(len(json.loads(self.journal.path.read_text())["holds"]), 2)

    def test_cache_replaced_during_credential_probe_is_preserved_with_hold(self):
        for kind in ("identity", "quota", "secret"):
            self.mutate(kind)
        self.expire()

        def replace_cache(method, route, payload=None, **kwargs):
            result = self.http(method, route, payload, **kwargs)
            if route == "/admin/campaign-identity" and "token" in kwargs:
                self.token.unlink()
                self.write(self.token, "operator-replacement")
            return result

        with patch.object(ADPFixtureBackend, "_http", side_effect=replace_cache):
            result = resume_cleanup(self.config_file)
        self.assertTrue(self.token.exists(), "cleanup deleted the operator replacement")
        self.assertEqual(self.token.read_text(), "operator-replacement")
        self.assertEqual(result, 5)
        record = json.loads(self.journal.path.read_text())
        self.assertEqual(record["receipt"]["status"], "hold")
        self.assertTrue(any(hold.get("kind") == "secret" for hold in record["holds"]))
        self.assertEqual(self.quota["rpm"], 3)
        self.assertFalse(self.membership["value"]["enabled"])
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertEqual(self.token.read_text(), "operator-replacement")

    def test_cache_replacement_at_atomic_claim_is_preserved(self):
        for kind in ("identity", "secret"):
            self.mutate(kind)
        self.expire()
        rename = os.rename

        def replace_before_claim(source, destination, **kwargs):
            self.assertEqual(Path(source), self.token)
            self.token.unlink()
            self.write(self.token, "replacement-at-rename")
            return rename(source, destination, **kwargs)

        with patch("campaign_adp.os.rename", side_effect=replace_before_claim):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertEqual(self.token.read_text(), "replacement-at-rename")
        self.assertEqual(json.loads(self.journal.path.read_text())["receipt"]["status"], "hold")

    def test_cache_replacement_at_claimed_unlink_is_preserved(self):
        self.mutate("identity")
        intent = self.mutate("secret")
        self.expire()
        unlink = os.unlink

        def replace_before_unlink(path, **kwargs):
            self.assertEqual(path, "token")
            self.assertIn("dir_fd", kwargs)
            self.assertFalse(self.token.exists())
            self.write(self.token, "replacement-at-unlink")
            return unlink(path, **kwargs)

        with patch("campaign_adp.os.unlink", side_effect=replace_before_unlink):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertEqual(self.token.read_text(), "replacement-at-unlink")
        operation = json.loads(self.backend.path.read_text())[intent["id"]]
        self.assertEqual(operation["cleanup"]["phase"], "deleted")
        self.assertNotIn("restored", operation)

    def interrupted_secret_cleanup(self, action, replacement=False, usable=False):
        with patch("campaign_adp.time.time", return_value=time.time()):
            self.mutate("identity")
            intent = self.mutate("secret")
            self.expire()
            original = getattr(os, action)

            def interrupt(*args, **kwargs):
                if replacement:
                    self.token.unlink()
                    self.write(self.token, "interrupted-replacement")
                original(*args, **kwargs)
                raise KeyboardInterrupt("runner stopped after filesystem mutation")

            with patch("campaign_adp.os." + action, side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                resume_cleanup(self.config_file)
            self.assertFalse(self.token.exists())
            self.assertEqual(json.loads(self.journal.path.read_text())["version"], 2)
            operation = json.loads(self.backend.path.read_text())[intent["id"]]
            claim = Path(operation["cleanup"]["directory"]) / "token"
            self.assertEqual(claim.exists(), action == "rename")
            self.assertEqual(claim.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("restored", operation)
            if usable:
                self.membership.update(value={"enabled": True}, version="operator-enabled")
            self.expire()
            result = resume_cleanup(self.config_file)
            if usable:
                self.assertEqual(result, 5)
                self.assertEqual(claim.read_text(), "access-token-never-log")
                self.assertFalse(self.token.exists())
                self.assertTrue(self.membership["value"]["enabled"])
            elif replacement:
                self.assertEqual(result, 5)
                self.assertEqual(self.token.read_text(), "interrupted-replacement")
                self.assertEqual(claim.read_text(), "interrupted-replacement")
            else:
                self.assertEqual(result, 0)
                self.assertFalse(self.token.exists())
                self.assertFalse(claim.exists())
                operation = json.loads(self.backend.path.read_text())[intent["id"]]
                self.assertEqual(operation["cleanup"]["phase"], "deleted")
                self.assertEqual(operation["restored"], {"value": None, "version": None, "owner": self.config["owner"]})
            self.assertNotIn("access-token-never-log", self.backend.path.read_text())

    def test_restart_finishes_claim_after_interrupted_rename(self):
        self.interrupted_secret_cleanup("rename")

    def test_restart_finishes_receipt_after_interrupted_unlink(self):
        self.interrupted_secret_cleanup("unlink")

    def test_restart_preserves_unexpected_claim_instead_of_certifying_missing_cache(self):
        self.interrupted_secret_cleanup("rename", replacement=True)

    def test_restart_rechecks_claimed_credential_before_deletion(self):
        self.interrupted_secret_cleanup("rename", usable=True)

    def test_unexpected_claim_does_not_overwrite_a_second_operator_replacement(self):
        self.mutate("identity")
        intent = self.mutate("secret")
        self.expire()
        rename, link = os.rename, os.link

        def replace_before_claim(source, destination, **kwargs):
            self.token.unlink()
            self.write(self.token, "first-replacement")
            return rename(source, destination, **kwargs)

        def replace_before_return(source, destination, **kwargs):
            self.write(self.token, "second-replacement")
            return link(source, destination, **kwargs)

        with patch("campaign_adp.os.rename", side_effect=replace_before_claim), patch(
                "campaign_adp.os.link", side_effect=replace_before_return):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        operation = json.loads(self.backend.path.read_text())[intent["id"]]
        self.assertEqual(operation["cleanup"]["phase"], "hold")
        claim = Path(operation["cleanup"]["directory"]) / "token"
        self.assertEqual(claim.read_text(), "first-replacement")
        self.assertEqual(self.token.read_text(), "second-replacement")

    def test_lost_claimed_unlink_reply_reconciles_to_durable_receipt(self):
        self.mutate("identity")
        intent = self.mutate("secret")
        self.expire()
        unlink = os.unlink

        def lost_reply(path, **kwargs):
            unlink(path, **kwargs)
            raise TimeoutError("unlink reply lost")

        with patch("campaign_adp.os.unlink", side_effect=lost_reply):
            self.assertEqual(resume_cleanup(self.config_file), 0)
        operation = json.loads(self.backend.path.read_text())[intent["id"]]
        self.assertEqual(operation["cleanup"]["phase"], "deleted")
        self.assertEqual(operation["restored"], {"value": None, "version": None, "owner": self.config["owner"]})
        self.assertEqual(json.loads(self.journal.path.read_text())["receipt"]["status"], "clean")

    def test_symlink_replacement_is_preserved_without_reading_or_deleting_its_target(self):
        self.mutate("identity")
        intent = self.mutate("secret")
        unrelated = self.root / "operator-secret"
        self.write(unrelated, "unrelated-credential")
        self.expire()

        def replace_during_probe(method, route, payload=None, **kwargs):
            result = self.http(method, route, payload, **kwargs)
            if route == "/admin/campaign-identity":
                self.token.unlink()
                self.token.symlink_to(unrelated)
            return result

        with patch.object(ADPFixtureBackend, "_http", side_effect=replace_during_probe):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertTrue(self.token.is_symlink())
        self.assertEqual(self.token.readlink(), unrelated)
        self.assertEqual(unrelated.read_text(), "unrelated-credential")
        operation = json.loads(self.backend.path.read_text())[intent["id"]]
        self.assertEqual(operation["cleanup"]["phase"], "hold")
        self.assertTrue((Path(operation["cleanup"]["directory"]) / "token").is_symlink())

    def test_unexpected_directory_is_retained_in_claim_with_hold(self):
        self.mutate("identity")
        intent = self.mutate("secret")
        self.expire()

        def replace_during_probe(method, route, payload=None, **kwargs):
            result = self.http(method, route, payload, **kwargs)
            if route == "/admin/campaign-identity":
                self.token.unlink()
                self.token.mkdir(mode=0o700)
                self.write(self.token / "operator-data", "preserve")
            return result

        with patch.object(ADPFixtureBackend, "_http", side_effect=replace_during_probe):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        operation = json.loads(self.backend.path.read_text())[intent["id"]]
        claim = Path(operation["cleanup"]["directory"]) / "token"
        self.assertEqual(operation["cleanup"]["phase"], "hold")
        self.assertEqual((claim / "operator-data").read_text(), "preserve")
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertEqual((claim / "operator-data").read_text(), "preserve")

    def test_wrong_selected_role_blocks_before_mutation(self):
        with patch("campaign_adp.run_selected", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({
                "Account": self.config["approved_account"], "Arn": "arn:aws:sts::000000000000:assumed-role/wrong/session"}))):
            with self.assertRaisesRegex(RecoveryError, "exact assumed role"):
                self.mutate("identity")
        self.assertEqual(json.loads(self.journal.path.read_text())["intents"], [])

    def test_recovery_refuses_retargeted_inventory_before_any_operation(self):
        self.mutate("identity")
        self.expire()
        changed = dict(self.config, target="https://other.example.invalid")
        self.write(self.config_file, json.dumps(changed))
        count = len(self.calls)
        with self.assertRaisesRegex(RecoveryError, "journaled target"):
            resume_cleanup(self.config_file)
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.user["Enabled"])

    def test_lost_membership_write_is_reconciled_through_authoritative_receipt(self):
        def lost(method, route, payload=None):
            result = self.http(method, route, payload)
            if method == "PUT":
                raise TimeoutError("lost enable result")
            return result

        with patch.object(self.backend, "_http", side_effect=lost):
            with self.assertRaises(TimeoutError):
                self.mutate("identity")
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 0)
        self.assertFalse(self.membership["value"]["enabled"])

    def test_lost_restore_reply_is_reconciled_without_cognito_mutations(self):
        self.mutate("identity")
        failures = [1]

        def lost_restore(method, route, payload=None):
            result = self.http(method, route, payload)
            if method == "PUT" and failures[0]:
                failures[0] -= 1
                raise TimeoutError("lost restore reply")
            return result

        self.expire()
        with patch.object(ADPFixtureBackend, "_http", side_effect=lost_restore):
            self.assertEqual(resume_cleanup(self.config_file), 0)
        self.assertFalse(self.membership["value"]["enabled"])
        operations = [operation for operation, _ in self.calls]
        self.assertNotIn("admin-disable-user", operations)
        self.assertNotIn("admin-user-global-sign-out", operations)

    def test_cleanup_holds_if_issued_credential_still_authenticates(self):
        for kind in ("identity", "quota", "secret"):
            self.mutate(kind)
        self.expire()

        def still_usable(method, route, payload=None, **kwargs):
            if route == "/admin/campaign-identity":
                return {"subject": "owned-sub", "tenant": "owned-tenant"}
            return self.http(method, route, payload, **kwargs)

        with patch.object(ADPFixtureBackend, "_http", side_effect=still_usable):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertTrue(self.token.exists())
        self.assertEqual(json.loads(self.journal.path.read_text())["status"], "hold")

    def test_only_authoritative_revocation_or_expiry_proves_credential_unusable(self):
        for status, error, unusable in ((403, "tenant_membership_revoked", True),
                                        (401, "token_expired", True), (401, "invalid_token", False),
                                        (403, "campaign_credential_unsupported", False),
                                        (503, "authentication_error", False)):
            with self.subTest(status=status, error=error):
                response = urllib.error.HTTPError(self.config["target"], status, "test response", {},
                    io.BytesIO(json.dumps({"detail": {"error": error}}).encode()))
                with patch("urllib.request.build_opener") as opener:
                    opener.return_value.open.side_effect = response
                    if unusable:
                        self.assertEqual(self.real_http(self.backend, "GET", "/admin/campaign-identity", token="fixture-token"),
                                         {"unusable": True})
                    else:
                        with self.assertRaises(RecoveryError):
                            self.real_http(self.backend, "GET", "/admin/campaign-identity", token="fixture-token")

    def test_unavailable_credential_probe_retains_cache_and_nonclean_hold(self):
        for kind in ("identity", "secret"):
            self.mutate(kind)
        self.expire()

        def unavailable(method, route, payload=None, **kwargs):
            if route == "/admin/campaign-identity":
                raise TimeoutError("probe unavailable")
            return self.http(method, route, payload, **kwargs)

        with patch.object(ADPFixtureBackend, "_http", side_effect=unavailable):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertTrue(self.token.exists())
        self.assertEqual(json.loads(self.journal.path.read_text())["status"], "hold")

    def test_legacy_cognito_identity_intent_does_not_mutate_the_user(self):
        self.mutate("identity")
        self.receipts.clear()
        self.expire()
        self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertTrue(self.user["Enabled"])
        self.assertTrue(self.membership["value"]["enabled"])
        self.assertIn("legacy identity requires operator recovery", json.loads(self.journal.path.read_text())["holds"][0]["reason"])

    def test_ownership_change_between_final_read_and_restore_preserves_membership(self):
        for kind in ("identity", "secret"):
            self.mutate(kind)
        self.expire()

        def change_owner(method, route, payload=None, **kwargs):
            if method == "PUT" and payload.get("enabled") is False:
                self.user["UserAttributes"].append({"Name": "owner", "Value": "new-owner"})
                self.membership["version"] = "new-owner-version"
            return self.http(method, route, payload, **kwargs)

        with patch.object(ADPFixtureBackend, "_http", side_effect=change_owner):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertTrue(self.membership["value"]["enabled"])
        self.assertTrue(self.user["Enabled"])
        self.assertTrue(self.token.exists())

    def test_health_from_before_recovery_cannot_certify_cleanup(self):
        self.mutate("quota")
        old = self.health.read_text()
        self.expire()
        with patch.object(self, "observe_health", side_effect=lambda: self.write(self.health, old)):
            self.assertEqual(resume_cleanup(self.config_file), 5)
        self.assertEqual(self.quota["rpm"], 3)
        self.assertIn({"reason": "baseline_health_unverified"}, json.loads(self.journal.path.read_text())["holds"])

    def test_quota_requires_actual_human_root_and_restorable_original_limits(self):
        self.config["fixtures"][0]["root_human"] = "someone-else"
        with self.assertRaisesRegex(RecoveryError, "different human root"):
            self.mutate("quota")
        self.config["fixtures"][0]["root_human"] = "owned-sub"
        self.quota["rpm"] = 0
        with self.assertRaisesRegex(RecoveryError, "cannot be restored"):
            self.mutate("quota")
        self.assertEqual(self.quota["rpm"], 0)

    def test_controller_setup_failure_cleans_prior_mutations_without_inference(self):
        config = {**self.config, "campaign_owner": self.config["owner"], "recovery_config": str(self.config_file),
                  "limits": {"setup_seconds": 20}, "local_test": True}
        original = self.backend.compare_write

        def fail_secret(kind, *args):
            if kind == "secret":
                raise RecoveryError("credential issuance refused")
            return original(kind, *args)

        with patch("campaign_recovery.fixture_backend", return_value=self.backend), patch.object(
                self.backend, "compare_write", side_effect=fail_secret):
            with self.assertRaisesRegex(RecoveryError, "fixture setup failed"):
                setup_fixtures(config)
        self.assertFalse(self.membership["value"]["enabled"])
        self.assertEqual(self.quota["rpm"], 3)
        self.assertEqual(json.loads(self.journal.path.read_text())["status"], "clean")

    def controller_config(self):
        return {**self.config, "campaign_owner": self.config["owner"], "recovery_config": str(self.config_file),
            "seed": 1, "recovery_owner": "fixture-owner", "slos": {"ttft_ms": 1000, "http_ms": 5000, "terminal_ms": 1000},
            "scenario": "checkpoint", "evidence_dir": str(self.root), "local_test": True,
            "identities": [{"id": "owned-sub", "kind": "developer", "token_file": str(self.token),
                            "tenant": "owned-tenant", "root_human": "owned-sub", "ownership_receipt": "owned-receipt"}],
            "models": [{"api": api, "max_input_tokens": 10, "max_output_tokens": 10,
                        "model": "fixture-model", "sdk_version": "fixture-sdk",
                        "price_per_input_token": 0.001, "price_per_output_token": 0.001} for api in ("chat", "responses")],
            "limits": {"attempts": 2, "active_seconds": 10, "drain_seconds": 2, "input_tokens": 100,
                       "output_tokens": 100, "max_spend": 1, "setup_seconds": 20, "cleanup_seconds": 20},
            "stages": [{"name": api, "required": True, "desired_sessions": 1, "min_dwell_seconds": 0,
                        "spawn_rate": 1} for api in ("chat", "responses")]}

    def test_controller_journals_all_setup_before_both_apis_and_cleans_up(self):
        config = self.controller_config()

        def stream(*args):
            self.assertEqual(len(json.loads(self.journal.path.read_text())["intents"]), 3)
            self.assertTrue(self.user["Enabled"])
            self.assertTrue(self.token.exists())
            return {"success": True, "complete": True, "ttft_ms": 1}

        with patch("campaign_controller.stream_request", side_effect=stream) as requests:
            self.assertEqual(checkpoint(config), 0)
        self.assertEqual(requests.call_count, 2)
        self.assertFalse(self.token.exists())
        self.assertEqual(json.loads(self.journal.path.read_text())["status"], "clean")

    def test_validation_has_no_operations_and_refuses_agent_impersonation_but_reports_holds(self):
        config = self.controller_config()
        config["local_test"] = False
        path = self.root / "controller.json"
        self.write(path, json.dumps(config))
        self.assertEqual(load_config(path), config)
        config["identities"][0]["kind"] = "agent"
        self.write(path, json.dumps(config))
        with self.assertRaisesRegex(ConfigError, "cannot impersonate workload agents"):
            load_config(path)
        config["identities"][0]["kind"] = "developer"
        self.write(path, json.dumps(config))
        self.write(self.token, "held-token")
        with self.assertRaisesRegex(ConfigError, "cache must be absent"):
            load_config(path)
        self.assertEqual(load_config(path, for_report=True), config)
        self.assertEqual(self.calls, [])

    def test_live_legacy_adapter_is_refused_and_duplicate_controller_does_not_clean_active_run(self):
        legacy = dict(self.config, backend="http", adapter_url="https://fixtures.example.invalid", adapter_token_file=str(self.admin))
        self.write(self.config_file, json.dumps(legacy))
        with self.assertRaisesRegex(RecoveryError, "supported ADP"):
            load_recovery_config(self.config_file)
        self.write(self.config_file, json.dumps(self.config))
        config = {**self.config, "campaign_owner": self.config["owner"], "recovery_config": str(self.config_file),
                  "limits": {"setup_seconds": 20}, "local_test": True}
        setup_fixtures(config)
        with self.assertRaisesRegex(RecoveryError, "already started"):
            setup_fixtures(config)
        self.assertTrue(self.user["Enabled"])
        self.assertEqual(json.loads(self.journal.path.read_text())["status"], "active")


class BrokerDeadlineTests(unittest.TestCase):
    def test_timeout_stops_child_mutation_not_just_the_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            late_write = Path(directory) / "late-write"
            child = ("import time; from pathlib import Path; time.sleep(0.6); "
                     f"Path({str(late_write)!r}).write_text('unexpected mutation')")
            parent = ("import subprocess,sys,time; "
                      f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(5)")
            with self.assertRaises(subprocess.TimeoutExpired):
                run_selected([sys.executable, "-c", parent], input="", timeout=0.3)
            time.sleep(0.7)
            self.assertFalse(late_write.exists())


if __name__ == "__main__":
    unittest.main()
