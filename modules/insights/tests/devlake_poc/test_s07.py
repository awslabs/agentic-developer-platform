import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "poc" / "devlake" / "verify_campaign.py"
SPEC = importlib.util.spec_from_file_location("verify_campaign", SOURCE)
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
LIMITS = {"controlled_updates": 20, "minimum_observation_hours": 48, "freshness_p95_minutes": 15,
          "worker_vcpu_per_job": 2, "worker_memory_mib_per_job": 4096,
          "incremental_storage_gib_per_cell": 10, "total_incremental_budget_usd": 100}


import json
import subprocess
import sys
import tempfile

class PilotCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / "manifest.json"
        self.plan = Path(__file__).resolve().parents[4] / "docs" / "engineering-insights" / "devlake-poc" / "delivery-flow" / "acceptance-mapping.json"

    def run_cli(self, phase="pilot"):
        return subprocess.run([sys.executable, str(SOURCE), "--phase", phase,
                               "--manifest", str(self.manifest), "--plan", str(self.plan)],
                              capture_output=True, text=True, check=False)

    def test_absent_and_malformed_manifest_fail_without_summary(self):
        for content in (None, "{invalid"):
            if content is not None:
                self.manifest.write_text(content)
            result = self.run_cli()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("manifest is missing or invalid JSON", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_unknown_phase_fails(self):
        self.manifest.write_text("{}")
        result = self.run_cli("unrecognized")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_criteria_fail_without_passing_summary(self):
        self.manifest.write_text(json.dumps({"criteria": {}}))
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("46 required criteria", result.stderr)
        self.assertNotIn("PASS", result.stdout + result.stderr)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.updates = [{"update_id": str(index), "provider": "github" if index % 2 else "jira",
                         "revision": "r1", "source_at": (start + timedelta(hours=index * 3)).isoformat()}
                        for index in range(20)]
        self.reads = [{"update_id": update["update_id"], "revision": "r1", "authorized": True,
                       "read_at": (datetime.fromisoformat(update["source_at"]) + timedelta(minutes=14)).isoformat()}
                      for update in self.updates]

    def test_nearest_rank_uses_nineteenth_of_all_twenty_updates(self):
        self.reads[-1]["read_at"] = (datetime.fromisoformat(self.updates[-1]["source_at"]) +
                                    timedelta(minutes=30)).isoformat()
        result = VERIFY.freshness(self.updates, self.reads, LIMITS)
        self.assertEqual((result["status"], result["matched"], result["p95_minutes"]), ("pass", 20, 14))
        self.reads[-2]["read_at"] = (datetime.fromisoformat(self.updates[-2]["source_at"]) +
                                    timedelta(minutes=16)).isoformat()
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["p95_minutes"], 16)
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["status"], "fail")

    def test_missing_or_stopped_source_never_shrinks_the_denominator(self):
        result = VERIFY.freshness(self.updates, self.reads[:-1], LIMITS)
        self.assertEqual((result["status"], result["matched"], result["p95_minutes"]),
                         ("incomplete", 19, None))
        self.assertEqual(result["missing"], ["19"])
        self.reads[-1]["read_at"] = None
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["status"], "unavailable")
        self.reads[-1]["read_at"] = (datetime.fromisoformat(self.updates[-1]["source_at"]) +
                                    timedelta(minutes=31)).isoformat()
        self.reads[-2]["read_at"] = (datetime.fromisoformat(self.updates[-2]["source_at"]) +
                                    timedelta(minutes=31)).isoformat()
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["status"], "fail")

    def test_authorization_revision_duplicates_and_short_soak(self):
        self.reads[-1]["authorized"] = False
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["status"], "unavailable")
        self.reads[-1]["authorized"] = True
        self.reads[-1]["revision"] = "old"
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "duplicated authorized read"):
            VERIFY.freshness(self.updates, self.reads + [self.reads[-1]], LIMITS)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index, update in enumerate(self.updates):
            update["source_at"] = (start + timedelta(hours=index * 2)).isoformat()
        self.assertEqual(VERIFY.freshness(self.updates, self.reads, LIMITS)["status"], "incomplete")

    def test_unapproved_or_unknown_cost_never_passes_bounds(self):
        observations = {"query_seconds": [2, 3, 1], "jobs": [{"vcpu": 1, "memory_mib": 1024}],
                        "storage_gib_per_cell": [1], "cost_usd": {"estimate": 2, "billed": 3,
                                                                  "basis": "itemized receipt"}}
        self.assertEqual(VERIFY.operating_bounds(observations, {"approved": False, "limits": LIMITS})["status"],
                         "unavailable")
        self.assertEqual(VERIFY.operating_bounds(observations, {"approved": True, "limits": LIMITS})["status"],
                         "pass")
        observations["cost_usd"]["billed"] = None
        self.assertEqual(VERIFY.operating_bounds(observations, {"approved": True, "limits": LIMITS})["status"],
                         "unavailable")



class CampaignTests(unittest.TestCase):
    def setUp(self):
        FreshnessTests.setUp(self)
        import sys
        import tempfile

        sys.path.insert(0, str(SOURCE.parent))
        self.addCleanup(lambda: sys.path.remove(str(SOURCE.parent)))
        import campaign

        self.campaign = campaign
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.checkpoint = Path(self.directory.name) / "private-checkpoint.json"
        self.report = Path(self.directory.name) / "private-report.json"
        self.frozen = {"sample_revision": "pinned-v1", "updates": self.updates}
        self.approved = {"approved": True, "limits": dict(LIMITS)}
        for read in self.reads:
            read.update({"evidence_ref": "private-read-receipt", "observer": "independent reader"})
        self.first = {"batch_id": "window-1", "reads": self.reads[:10],
                      "measurements": {"query_seconds": [2, 4], "jobs": [{"vcpu": 1, "memory_mib": 1024}],
                                       "storage_gib_per_cell": [1],
                                       "cost_usd": {"estimate": 2, "billed": 3, "basis": "private billing receipt"}}}
        self.second = {"batch_id": "window-2", "reads": self.reads[10:]}

    def run_batch(self, batch):
        return self.campaign.record(self.frozen, batch, self.approved, self.checkpoint, self.report)

    def test_checkpointed_campaign_preserves_sample_and_exact_retries(self):
        import json
        import stat

        first = self.run_batch(self.first)
        self.assertEqual((first["status"], first["freshness"]["matched"]), ("incomplete", 10))
        self.assertEqual(stat.S_IMODE(self.checkpoint.stat().st_mode), 0o600)
        second = self.run_batch(self.second)
        self.assertEqual((second["status"], second["freshness"]["p95_minutes"]), ("pass", 14))
        self.assertEqual(second["bounds"]["query_seconds"]["p95"], 4)
        self.assertEqual(self.run_batch(self.second), second)
        self.assertEqual(json.loads(self.report.read_text()), second)
        self.assertEqual(stat.S_IMODE(self.report.stat().st_mode), 0o600)
        self.assertEqual(len(json.loads(self.checkpoint.read_text())["reads"]), 20)

    def test_conflicting_retry_and_changed_sample_or_limits_refuse_before_write(self):
        import copy

        self.run_batch(self.first)
        original = self.checkpoint.read_bytes()
        wrong = copy.deepcopy(self.first)
        wrong["reads"][0]["revision"] = "other"
        with self.assertRaisesRegex(ValueError, "conflicting evidence batch"):
            self.run_batch(wrong)
        self.frozen["sample_revision"] = "changed"
        with self.assertRaisesRegex(ValueError, "sample or limits changed"):
            self.run_batch(self.second)
        self.frozen["sample_revision"] = "pinned-v1"
        self.approved["limits"]["freshness_p95_minutes"] = 14
        with self.assertRaisesRegex(ValueError, "sample or limits changed"):
            self.run_batch(self.second)
        self.assertEqual(self.checkpoint.read_bytes(), original)

    def test_storage_ceiling_breach_survives_later_reduction_and_retry(self):
        import json

        self.first["measurements"]["storage_gib_per_cell"] = [11]
        self.assertEqual(self.run_batch(self.first)["bounds"]["status"], "fail")
        self.second["measurements"] = {"storage_gib_per_cell": [1]}
        result = self.run_batch(self.second)
        self.assertEqual(result["freshness"]["status"], "pass")
        self.assertEqual((result["bounds"]["status"], result["status"]), ("fail", "fail"))
        self.assertEqual(json.loads(self.checkpoint.read_text())["storage_gib_per_cell"], [11, 1])
        checkpoint = self.checkpoint.read_bytes()
        self.assertEqual(self.run_batch(self.second), result)
        self.assertEqual(self.checkpoint.read_bytes(), checkpoint)

    def test_unknown_or_overlapping_observation_does_not_corrupt_checkpoint(self):
        import copy

        unknown = copy.deepcopy(self.first)
        unknown["reads"][0]["update_id"] = "not-frozen"
        with self.assertRaisesRegex(ValueError, "read needs frozen ID"):
            self.run_batch(unknown)
        self.assertFalse(self.checkpoint.exists())
        self.run_batch(self.first)
        overlap = copy.deepcopy(self.first)
        overlap["batch_id"] = "window-2"
        with self.assertRaisesRegex(ValueError, "duplicate update across"):
            self.run_batch(overlap)
        self.assertEqual(self.run_batch(self.second)["status"], "pass")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clients = ["writer", "exporter", "migrator", "gateway"]
        renewal = {"old_token_age_minutes": 16, "new_token_age_minutes": 1,
                   "fresh_sts": True, "verified_tls": True, "password_fallback": False}
        terminal = {"attempted_while_off": True, "owned_absent": True,
                    "foreign_and_holds_retained": True, "interrupted_resumed": True,
                    "authority_loss_escalated": True, "failed_or_not_run_preserved": True}
        b1 = {"item": "sha256:original"}
        self.observations = {
            "b1": {"native": b1, "sealed": b1, "exported": b1, "served": b1,
                   "b2_native": {"item": "sha256:edited"}, "manifest_acknowledged": True,
                   "pinned_mapper": True, "stale_generation_denied": True, "duplicate_count": 0},
            "source_outage": {"duration_minutes": 16, "checkpoint_restored": True,
                              "all_updates_reconciled": True, "duplicate_count": 0},
            "revocation": {"before_epoch": "e1", "after_epoch": "e2", "barrier_observed": True,
                           "late_pages_discarded": True, "read_denied": True, "writes_after": 0,
                           "provider_calls_after": 0},
            "db_clients": {name: dict(renewal) for name in self.clients},
            "modes": {"off": {"provider_calls": 0, "new_optional_resources": 0},
                      "connector_only": {"profile_available": True, "insights_resources": 0, "db_clients": 0},
                      "full": {"grants_checked": True, "collection_available": True},
                      "invalid_rejected": True, "disabled_during_work": True},
            "profile": {"expected_edited": "sha256:edited-profile", "after_reenable": "sha256:edited-profile",
                        "holds_before": "sha256:held", "holds_after": "sha256:held",
                        "shared_before": "sha256:shared", "shared_after": "sha256:shared"},
            "finalization": {name: {**terminal, "original_outcome": name}
                             for name in ("failed", "aborted", "expired")},
        }

    def test_missing_proof_is_unavailable_not_a_recovery_pass(self):
        self.assertEqual(VERIFY.recovery(self.observations, self.clients)["status"], "pass")
        del self.observations["revocation"]["barrier_observed"]
        result = VERIFY.recovery(self.observations, self.clients)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["checks"]["revocation"], "unavailable")
        del self.observations["db_clients"]["exporter"]
        self.assertEqual(VERIFY.recovery(self.observations, self.clients)["checks"]["iam_renewal"], "unavailable")

    def test_restart_loss_and_revoked_late_work_fail(self):
        self.observations["source_outage"]["all_updates_reconciled"] = False
        self.observations["b1"]["exported"] = {"item": "sha256:replayed-B2"}
        self.observations["revocation"]["writes_after"] = 1
        result = VERIFY.recovery(self.observations, self.clients)
        self.assertEqual(result["status"], "fail")
        for name in ("source_outage", "sealed_b1", "revocation"):
            self.assertEqual(result["checks"][name], "fail")

    def test_stale_token_fallback_and_off_mode_finalization_fail(self):
        self.observations["db_clients"]["writer"]["password_fallback"] = True
        self.observations["modes"]["connector_only"]["db_clients"] = 1
        self.observations["profile"]["after_reenable"] = "sha256:lost-edit"
        self.observations["finalization"]["expired"]["attempted_while_off"] = False
        result = VERIFY.recovery(self.observations, self.clients)
        self.assertEqual(result["status"], "fail")
        for name in ("iam_renewal", "optional_modes", "profile_and_holds", "finalization"):
            self.assertEqual(result["checks"][name], "fail")


class RecoveryCampaignTests(unittest.TestCase):
    def setUp(self):
        CampaignTests.setUp(self)
        RecoveryTests.setUp(self)
        self.frozen["required_db_clients"] = self.clients
        self.first["recovery"] = {name: self.receipt(name) for name in ("b1", "source_outage")}
        self.second["recovery"] = {name: self.receipt(name) for name in
                                   ("revocation", "db_clients", "modes", "profile", "finalization")}

    def receipt(self, name):
        import copy

        data = copy.deepcopy(self.observations[name])
        if name == "finalization":
            for outcome, attempt in data.items():
                attempt.update({"attempt_id": outcome + "-attempt-1", "evidence_ref": "private-" + outcome})
        return {"attempt_id": name + "-attempt-1", "observer": "independent verifier",
                "evidence_ref": "private-" + name, "data": data}

    def run_batch(self, batch):
        return CampaignTests.run_batch(self, batch)

    def test_checkpointed_recovery_requires_all_categories_and_retains_attempts(self):
        import json

        initial = self.run_batch(self.first)
        self.assertEqual(initial["recovery"]["status"], "unavailable")
        self.assertEqual(initial["status"], "incomplete")
        final = self.run_batch(self.second)
        self.assertEqual(final["status"], "pass")
        self.assertEqual(final["recovery"]["status"], "pass")
        self.assertEqual(final["recovery_receipts"]["finalization"]["attempt_id"], "finalization-attempt-1")
        self.assertEqual(self.run_batch(self.second), final)
        checkpoint = json.loads(self.checkpoint.read_text())
        self.assertEqual(checkpoint["recovery"]["finalization"]["data"]["expired"]["attempt_id"],
                         "expired-attempt-1")

    def test_missing_attempt_or_conflicting_barrier_does_not_erase_receipts(self):
        import copy

        self.run_batch(self.first)
        original = self.checkpoint.read_bytes()
        changed = copy.deepcopy(self.first)
        changed["batch_id"] = "window-3"
        changed["reads"] = []
        with self.assertRaisesRegex(ValueError, "conflicting recovery case"):
            self.run_batch(changed)
        self.assertEqual(self.checkpoint.read_bytes(), original)
        self.second["recovery"]["finalization"]["data"]["expired"].pop("evidence_ref")
        with self.assertRaisesRegex(ValueError, "each finalization attempt"):
            self.run_batch(self.second)
        self.assertEqual(self.checkpoint.read_bytes(), original)

    def test_revoked_write_cannot_pass_even_when_freshness_is_healthy(self):
        self.run_batch(self.first)
        self.second["recovery"]["revocation"]["data"]["writes_after"] = 1
        final = self.run_batch(self.second)
        self.assertEqual(final["freshness"]["status"], "pass")
        self.assertEqual(final["recovery"]["checks"]["revocation"], "fail")
        self.assertEqual(final["status"], "fail")


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.pins = {"baseline_upstream": "upstream-r1", "candidate_upstream": "upstream-r2",
                     "baseline_image": "sha256:" + "1" * 64, "candidate_image": "sha256:" + "2" * 64,
                     "schema_manifest_sha256": "3" * 64, "requirements_sha256": "4" * 64,
                     "connector_contract_sha256": "5" * 64}
        rejection = {"result": "migration_required", "provider_calls": 0, "writes": 0,
                     "fallback_used": False}
        self.observations = {
            "pins": dict(self.pins),
            "patches": [{"patch_id": "extraction", "status": "reused", "resolved": True, "effort_minutes": 2},
                        {"patch_id": "adapter", "status": "conflicted", "resolved": True, "effort_minutes": 10}],
            "negative_admission": {case: dict(rejection) for case in
                                   ("missing_schema", "incompatible_schema", "missing_guard", "incompatible_connector")},
            "migrated": {"migration_role_applied": True, "receipt_sha256": self.pins["schema_manifest_sha256"],
                         "first_scope_dml": True, "new_scope_dml": True, "worker_ddl_used": False},
            "worker_ddl": {"create": "denied", "alter": "denied", "drop": "denied",
                           "schema_unchanged": True},
            "rollback": {"restored_image": self.pins["baseline_image"], "profile_before": "p",
                         "profile_after": "p", "evidence_before": "e", "evidence_after": "e",
                         "shared_before": "s", "shared_after": "s", "read_compatible": True,
                         "evidence_ref": "private-rollback-receipt"},
        }

    def test_pinned_upgrade_reports_patch_reuse_conflicts_and_effort(self):
        result = VERIFY.compatibility(self.observations, self.pins)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["patch_comparison"],
                         {"reused": 1, "rebased": 0, "conflicted": 1, "effort_minutes": 12})
        self.observations["patches"][1]["resolved"] = False
        self.assertEqual(VERIFY.compatibility(self.observations, self.pins)["checks"]["patch_comparison"], "fail")

    def test_migration_required_precedes_provider_work_and_worker_ddl_is_denied(self):
        self.observations["negative_admission"]["missing_schema"]["provider_calls"] = 1
        self.observations["negative_admission"]["incompatible_connector"]["writes"] = 1
        self.observations["worker_ddl"]["alter"] = "allowed"
        result = VERIFY.compatibility(self.observations, self.pins)
        for case in ("missing_schema", "incompatible_connector", "worker_ddl"):
            self.assertEqual(result["checks"][case], "fail")
        self.assertEqual(result["status"], "fail")

    def test_missing_schema_receipt_or_changed_restore_never_passes(self):
        self.observations["migrated"]["receipt_sha256"] = "wrong"
        self.observations["rollback"]["restored_image"] = self.pins["candidate_image"]
        result = VERIFY.compatibility(self.observations, self.pins)
        self.assertEqual(result["checks"]["migrated_dml"], "fail")
        self.assertEqual(result["checks"]["rollback"], "fail")
        del self.observations["negative_admission"]["missing_guard"]
        self.assertEqual(VERIFY.compatibility(self.observations, self.pins)["checks"]["missing_guard"],
                         "unavailable")


class CompatibilityCampaignTests(unittest.TestCase):
    def setUp(self):
        CampaignTests.setUp(self)
        CompatibilityTests.setUp(self)
        self.frozen["upgrade"] = self.pins
        self.second["upgrade"] = {"attempt_id": "upgrade-attempt-1", "observer": "independent verifier",
                                  "evidence_ref": "private-upgrade", "data": self.observations}

    def run_batch(self, batch):
        return CampaignTests.run_batch(self, batch)

    def test_pinned_upgrade_receipt_is_checkpointed_across_windows(self):
        import json

        initial = self.run_batch(self.first)
        self.assertEqual(initial["compatibility"]["status"], "unavailable")
        final = self.run_batch(self.second)
        self.assertEqual(final["status"], "pass")
        self.assertEqual(final["compatibility"]["patch_comparison"]["conflicted"], 1)
        self.assertEqual(final["upgrade_receipt"]["attempt_id"], "upgrade-attempt-1")
        self.assertEqual(self.run_batch(self.second), final)
        self.assertEqual(json.loads(self.checkpoint.read_text())["upgrade"]["attempt_id"], "upgrade-attempt-1")

    def test_changed_pin_and_conflicting_upgrade_are_refused(self):
        import copy

        self.run_batch(self.first)
        self.pins["candidate_upstream"] = "unreviewed-r3"
        with self.assertRaisesRegex(ValueError, "sample or limits changed"):
            self.run_batch(self.second)
        self.pins["candidate_upstream"] = "upstream-r2"
        self.run_batch(self.second)
        conflicting = copy.deepcopy(self.second)
        conflicting["batch_id"] = "window-3"
        conflicting["reads"] = []
        with self.assertRaisesRegex(ValueError, "conflicting upgrade receipt"):
            self.run_batch(conflicting)

    def test_missing_upgrade_cannot_pass_complete_freshness(self):
        self.run_batch(self.first)
        self.second.pop("upgrade")
        result = self.run_batch(self.second)
        self.assertEqual(result["freshness"]["status"], "pass")
        self.assertEqual((result["compatibility"]["status"], result["status"]), ("unavailable", "unavailable"))


class IsolationTests(unittest.TestCase):
    def setUp(self):
        scopes = [("a1-shared", "tenant-a", "project-1", "shared"),
                  ("a1-private", "tenant-a", "project-1", "private"),
                  ("a2-shared", "tenant-a", "project-2", "shared"),
                  ("b1-shared", "tenant-b", "project-1", "shared")]
        self.pins = {"collision_id": "same-native-id", "descriptor_sha256": "f" * 64,
                     "scopes": [{"scope_key": key, "tenant_id": tenant, "project_id": project,
                                 "realm_id": visibility + "-realm", "cell_id": key + "-cell",
                                 "source_connection_id": key + "-connection", "visibility": visibility}
                                for key, tenant, project, visibility in scopes]}
        rejection = {"result": "denied", "raw_writes": 0, "released_bytes": 0, "shared_mutations": 0}
        self.observations = {
            "routes": [{"scope_key": scope["scope_key"], "cell_id": scope["cell_id"],
                        "realm_id": scope["realm_id"], "visibility": scope["visibility"],
                        "identity": [scope["tenant_id"], scope["project_id"], scope["source_connection_id"],
                                     "issue", "same-native-id", "v1"],
                        "scope_checked_before_raw_write": True, "policy_checked_before_response": True}
                       for scope in self.pins["scopes"]],
            "concurrency": {"overlap_observed": True, "max_active_jobs": 2},
            "negative": {case: dict(rejection) for case in ("forged_route", "foreign_tenant",
                         "same_tenant_project", "private_influence", "stale_policy")},
            "policy_graph": {"descriptor_sha256": self.pins["descriptor_sha256"], "current_epoch": "epoch-2",
                             "closure_verified": True, "private_in_shared": 0, "response_rechecked": True},
            "revocation": {"before_epoch": "epoch-1", "after_epoch": "epoch-2", "barrier_observed": True,
                           "read_denied": True, "released_bytes": 0, "raw_writes_after": 0,
                           "pre_release_rechecked": True},
            "storage_denials": {name: {"workload_role": "owned-" + name, "result": "denied",
                                      "foreign_bytes": 0, "foreign_writes": 0}
                                for name in ("db", "s3", "kms")},
            "shared_publication": {"before_bytes_base64": "c2hhcmVk", "after_bytes_base64": "c2hhcmVk",
                                   "before_status": 200, "after_status": 200,
                                   "before_pointer": "v1", "after_pointer": "v1",
                                   "before_coverage": 1, "after_coverage": 1,
                                   "before_freshness": 14, "after_freshness": 14,
                                   "private_rejected": True},
        }

    def test_colliding_ids_stay_in_distinct_tenant_project_cells(self):
        result = VERIFY.isolation(self.observations, self.pins)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["checks"]["cell_and_identity_routes"], "pass")
        self.observations["routes"][2]["identity"][1] = "project-1"
        self.assertEqual(VERIFY.isolation(self.observations, self.pins)["checks"]["cell_and_identity_routes"],
                         "fail")

    def test_foreign_raw_write_revocation_and_storage_probe_fail(self):
        self.observations["negative"]["forged_route"]["raw_writes"] = 1
        self.observations["revocation"]["released_bytes"] = 4
        self.observations["storage_denials"]["s3"]["result"] = "allowed"
        result = VERIFY.isolation(self.observations, self.pins)
        for name in ("forged_route", "revocation_barrier", "s3_denial"):
            self.assertEqual(result["checks"][name], "fail")
        self.assertEqual(result["status"], "fail")

    def test_private_publication_cannot_modify_shared_bytes_or_status(self):
        self.observations["shared_publication"]["after_bytes_base64"] = "cHJpdmF0ZQ=="
        self.observations["policy_graph"]["private_in_shared"] = 1
        result = VERIFY.isolation(self.observations, self.pins)
        self.assertEqual(result["checks"]["shared_publication"], "fail")
        self.assertEqual(result["checks"]["policy_graph"], "fail")
        self.observations["shared_publication"]["after_bytes_base64"] = "not base64"
        self.assertEqual(VERIFY.isolation(self.observations, self.pins)["checks"]["shared_publication"], "fail")

    def test_missing_kms_denial_is_unavailable(self):
        del self.observations["storage_denials"]["kms"]
        result = VERIFY.isolation(self.observations, self.pins)
        self.assertEqual(result["checks"]["kms_denial"], "unavailable")
        self.assertEqual(result["status"], "unavailable")


class IsolationCampaignTests(unittest.TestCase):
    def setUp(self):
        CampaignTests.setUp(self)
        IsolationTests.setUp(self)
        self.frozen["isolation"] = self.pins
        self.second["isolation"] = {"attempt_id": "isolation-attempt-1", "observer": "independent verifier",
                                    "evidence_ref": "private-isolation", "data": self.observations}

    def run_batch(self, batch):
        return CampaignTests.run_batch(self, batch)

    def test_checkpointed_isolation_requires_proof_and_retains_receipt(self):
        import json

        self.assertEqual(self.run_batch(self.first)["isolation"]["status"], "unavailable")
        result = self.run_batch(self.second)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["isolation_receipt"]["attempt_id"], "isolation-attempt-1")
        self.assertEqual(self.run_batch(self.second), result)
        self.assertEqual(json.loads(self.checkpoint.read_text())["isolation"]["attempt_id"],
                         "isolation-attempt-1")

    def test_missing_or_conflicting_isolation_is_not_a_pass(self):
        import copy

        self.run_batch(self.first)
        missing = copy.deepcopy(self.second)
        missing.pop("isolation")
        self.assertEqual(self.run_batch(missing)["status"], "unavailable")
        conflict = copy.deepcopy(self.second)
        conflict["batch_id"] = "window-3"
        conflict["reads"] = []
        self.run_batch(conflict)
        changed = copy.deepcopy(self.second)
        changed["batch_id"] = "window-4"
        changed["reads"] = []
        with self.assertRaisesRegex(ValueError, "conflicting isolation receipt"):
            self.run_batch(changed)


class FreshnessWindowTests(unittest.TestCase):
    def setUp(self):
        import sys

        CampaignTests.setUp(self)
        from resume_campaign import admitted, qualify, run_window

        self.admitted = admitted
        self.qualify = qualify
        self.run_window = run_window
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.root = Path(self.directory.name)
        self.pilot = self.root / "pilot-fixture"
        self.pilot.mkdir(mode=0o700)
        self.revision = "reviewed-fixture-revision"
        for index, update in enumerate(self.updates):
            source_time = self.start + timedelta(hours=48 * index / 19)
            update["source_at"] = source_time.isoformat()
            self.reads[index]["read_at"] = (source_time + timedelta(minutes=14)).isoformat()
        self.batches = []
        for window in range(12):
            lower = self.start + timedelta(hours=window * 4)
            upper = lower + timedelta(hours=4)
            reads = [read for update, read in zip(self.updates, self.reads)
                     if lower <= datetime.fromisoformat(update["source_at"]) < upper or
                     (window == 11 and datetime.fromisoformat(update["source_at"]) == upper)]
            batch = {"batch_id": f"window-{window:02}", "reads": reads}
            if window == 0:
                batch["measurements"] = self.first["measurements"]
            self.batches.append(batch)
            self.write_private(self.pilot / f"window-{window:02}.json", batch)
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.write_private(self.pilot / "approved-limits.json", self.approved)
        self.launch = {"campaign_id": "pilot-fixture", "revision": self.revision,
                       "planned_start_at": self.start.isoformat(),
                       "frozen_sha256": self.campaign._digest(self.frozen),
                       "limits_sha256": self.campaign._digest(self.approved),
                       "source_consent_ref": "private-consent", "runner_capacity_ref": "private-runner",
                       "authority_ref": "private-authority", "engine_qualification_ref": "private-engine"}
        for name in ("engine_until", "workflow_until", "authority_until", "expires_at"):
            self.launch[name] = (self.start + timedelta(hours=60)).isoformat()
        self.write_private(self.pilot / "launch.json", self.launch)

    def write_private(self, path, document):
        import json

        path.write_text(json.dumps(document))
        path.chmod(0o600)

    def run_at(self, window):
        now = self.start + timedelta(hours=(window + 1) * 4, minutes=15)
        return self.run_window(self.root, "pilot-fixture", self.revision, window, clock=lambda: now)

    def test_freshness_and_bounds(self):
        for window in range(12):
            result = self.run_at(window)
            self.assertEqual(result["batch_count"], window + 1)
        self.assertEqual(result["freshness"]["p95_minutes"], 14)
        self.assertEqual(result["freshness"]["span_hours"], 48)
        self.assertEqual(result["status"], "pass")
        now = self.start + timedelta(hours=49)
        self.assertEqual(self.qualify(self.root, "pilot-fixture", self.revision, clock=lambda: now)["windows"], 12)

    def test_out_of_order_and_short_authority_cannot_advance_window(self):
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            self.run_at(1)
        self.launch["engine_until"] = (self.start + timedelta(hours=55)).isoformat()
        self.write_private(self.pilot / "launch.json", self.launch)
        with self.assertRaisesRegex(ValueError, "56h"):
            self.run_at(0)
        self.assertFalse((self.pilot / "checkpoint.json").exists())

    def test_missing_update_blocks_final_readback(self):
        omitted = next(batch for batch in self.batches if batch["reads"])
        omitted["reads"].pop()
        self.write_private(self.pilot / f'{omitted["batch_id"]}.json', omitted)
        for window in range(12):
            self.run_at(window)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            now = self.start + timedelta(hours=49)
            self.qualify(self.root, "pilot-fixture", self.revision, clock=lambda: now)

    def test_missing_or_cross_window_batch_fails_closed(self):
        import copy

        (self.pilot / "window-00.json").unlink()
        now = self.start + timedelta(hours=4, minutes=21)
        with self.assertRaisesRegex(ValueError, "private campaign input"):
            self.run_window(self.root, "pilot-fixture", self.revision, 0, clock=lambda: now)
        self.assertFalse((self.pilot / "checkpoint.json").exists())
        batch = copy.deepcopy(self.batches[0])
        batch["reads"].append(self.batches[1]["reads"][0])
        self.write_private(self.pilot / "window-00.json", batch)
        with self.assertRaisesRegex(ValueError, "different frozen observation window"):
            self.run_at(0)
        self.assertFalse((self.pilot / "checkpoint.json").exists())

    def test_interrupted_report_write_resumes_exact_original_checkpoint(self):
        import json

        first = self.run_at(0)
        checkpoint = self.pilot / "checkpoint.json"
        report = self.pilot / "report.json"
        original = checkpoint.read_bytes()
        report.unlink()
        retry_at = self.start + timedelta(hours=4, minutes=30)
        self.assertEqual(self.run_window(self.root, "pilot-fixture", self.revision, 0,
                                         clock=lambda: retry_at), first)
        self.assertEqual(checkpoint.read_bytes(), original)
        self.assertEqual(json.loads(report.read_text()), first)
        self.assertEqual(self.run_at(1)["batch_count"], 2)
        self.assertEqual(json.loads(checkpoint.read_text())["reads"][:len(self.batches[0]["reads"])],
                         self.batches[0]["reads"])

    def test_late_first_observation_cannot_be_backfilled_as_on_time(self):
        late = self.start + timedelta(hours=4, minutes=21)
        with self.assertRaisesRegex(ValueError, "missed the approved checkpoint window"):
            self.run_window(self.root, "pilot-fixture", self.revision, 0, clock=lambda: late)
        self.assertFalse((self.pilot / "checkpoint.json").exists())

    def test_resume_refuses_missing_or_modified_original_batch(self):
        import copy
        import json

        self.run_at(0)
        checkpoint = self.pilot / "checkpoint.json"
        original = checkpoint.read_bytes()
        batch_path = self.pilot / "window-00.json"
        batch_path.unlink()
        with self.assertRaisesRegex(ValueError, "private campaign input"):
            self.run_at(1)
        self.write_private(batch_path, self.batches[0])
        changed = copy.deepcopy(self.batches[0])
        changed["reads"][0]["evidence_ref"] = "different-source"
        self.write_private(batch_path, changed)
        with self.assertRaisesRegex(ValueError, "inconsistent with original observation"):
            self.run_at(1)
        self.write_private(batch_path, self.batches[0])
        state = json.loads(original)
        state["reads"][0]["evidence_ref"] = "different-source"
        self.write_private(checkpoint, state)
        with self.assertRaisesRegex(ValueError, "evidence differs"):
            self.run_at(1)
        self.assertEqual(json.loads(checkpoint.read_text()), state)
        self.assertNotIn("window-01", json.loads(checkpoint.read_text())["batches"])

    def test_workflow_has_twelve_sequential_capped_jobs(self):
        import re

        workflow = (SOURCE.parents[4] / ".github/workflows/devlake-poc-qualify.yml").read_text()
        reusable = (SOURCE.parents[4] / ".github/workflows/_devlake-poc-window.yml").read_text()
        self.assertEqual(re.findall(r"^  window-(\d\d):$", workflow, re.MULTILINE),
                         [f"{index:02}" for index in range(12)])
        for index in range(1, 12):
            self.assertIn(f"    needs: [window-{index - 1:02}]", workflow)
        self.assertIn("needs: [window-11]", workflow)
        self.assertIn("timeout-minutes: 300", reusable)
        self.assertIn("environment: adp-devlake-campaign", reusable)
        self.assertIn("persist-credentials: false", reusable)
        self.assertIn("--window \"$WINDOW\"", reusable)
        self.assertIn("pip install -r modules/insights/schemas/requirements.txt", reusable)
        self.assertEqual(workflow.count("pip install -r modules/insights/schemas/requirements.txt"), 2)


class FreshnessQualificationTests(FreshnessWindowTests):
    def test_changed_admitted_pins_refuse_readback(self):
        import copy

        for window in range(12):
            self.run_at(window)
        for changed in ("sample", "limits"):
            with self.subTest(changed=changed):
                frozen = copy.deepcopy(self.frozen)
                limits = copy.deepcopy(self.approved)
                if changed == "sample":
                    frozen["sample_revision"] = "changed-sample"
                else:
                    limits["limits"]["freshness_p95_minutes"] = 1
                self.write_private(self.pilot / "frozen.json", frozen)
                self.write_private(self.pilot / "approved-limits.json", limits)
                self.launch["frozen_sha256"] = self.campaign._digest(frozen)
                self.launch["limits_sha256"] = self.campaign._digest(limits)
                self.write_private(self.pilot / "launch.json", self.launch)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    self.qualify(self.root, "pilot-fixture", self.revision,
                                 clock=lambda: self.start + timedelta(hours=49))

    def test_stale_report_cannot_hide_checkpoint_changes(self):
        import copy
        import json

        for window in range(12):
            self.run_at(window)
        checkpoint = self.pilot / "checkpoint.json"
        original = json.loads(checkpoint.read_text())
        changed_reads = copy.deepcopy(original["reads"])
        changed_reads[0]["read_at"] = (datetime.fromisoformat(changed_reads[0]["read_at"]) -
                                      timedelta(minutes=1)).isoformat()
        for field, value in (("reads", []), ("reads", changed_reads),
                             ("storage_gib_per_cell", [11]), ("query_seconds", []),
                             ("query_seconds", [3])):
            with self.subTest(field=field, value=value):
                state = copy.deepcopy(original)
                state[field] = value
                self.write_private(checkpoint, state)
                with self.assertRaisesRegex(ValueError, "incomplete|inconsistent"):
                    self.qualify(self.root, "pilot-fixture", self.revision,
                                 clock=lambda: self.start + timedelta(hours=49))

    def test_inconsistent_report_refuses_readback(self):
        import copy
        import json

        for window in range(12):
            self.run_at(window)
        report_path = self.pilot / "report.json"
        original = json.loads(report_path.read_text())
        for changed in ("batch_count", "freshness", "bounds"):
            with self.subTest(changed=changed):
                report = copy.deepcopy(original)
                if changed == "batch_count":
                    report["batch_count"] = 11
                elif changed == "freshness":
                    report["freshness"]["p95_minutes"] = 13
                else:
                    report["bounds"]["query_seconds"]["count"] += 1
                self.write_private(report_path, report)
                with self.assertRaisesRegex(ValueError, "incomplete|inconsistent"):
                    self.qualify(self.root, "pilot-fixture", self.revision,
                                 clock=lambda: self.start + timedelta(hours=49))


class UnattendedCampaignTests(FreshnessWindowTests):
    STEPS = ("worker_restart", "workflow_loss", "queued_ci", "token_refresh",
             "checkpoint_recovery", "mediated_repair", "automatic_successor")

    def receipt(self, step):
        checkpoint_digest = self.campaign._digest(self.batches[0])
        data = {"campaign_id": "pilot-fixture", "revision": self.revision,
                "frozen_sha256": self.launch["frozen_sha256"],
                "limits_sha256": self.launch["limits_sha256"],
                "correlation_id": "correlated-operation", "operation_id": "operation-1",
                "workflow_id": "resumed-wf",
                "observed_at": (self.start + timedelta(hours=4 * (self.STEPS.index(step) + 2))).isoformat()}
        data.update({
            "worker_restart": {"lost_worker_id": "worker-1", "resumed_worker_id": "worker-2",
                               "checkpoint_batch_id": "window-00", "checkpoint_batch_sha256": checkpoint_digest},
            "workflow_loss": {"lost_workflow_id": "lost-wf", "resumed_workflow_id": "resumed-wf",
                              "checkpoint_batch_id": "window-00", "checkpoint_batch_sha256": checkpoint_digest},
            "queued_ci": {"ci_run_id": "ci-1", "queued_at": (self.start + timedelta(hours=14)).isoformat(),
                          "started_at": (self.start + timedelta(hours=15)).isoformat()},
            "token_refresh": {"old_session_id": "old-session", "new_session_id": "new-session"},
            "checkpoint_recovery": {"checkpoint_batch_id": "window-00",
                                    "checkpoint_batch_sha256": checkpoint_digest, "readback_ref": "private-readback"},
            "mediated_repair": {"failure_ref": "private-failure", "diagnosis_ref": "private-diagnosis",
                                "allowed_edit_ref": "private-edit", "repair_pr_ref": "private-pr",
                                "ci_run_id": "ci-1", "ci_pass_ref": "private-ci-pass",
                                "verified_merge_ref": "private-merge", "component_release_ref": "private-release",
                                "independent_verification_ref": "private-verification"},
            "automatic_successor": {"successor_operation_id": "successor-1",
                                    "source_operation_id": "operation-1", "trigger": "automatic",
                                    "verification_ref": "private-verification"},
        }[step])
        return {"attempt_id": step + "-attempt", "observer": "independent verifier",
                "evidence_ref": "private-" + step, "data": data}

    def add_step(self, window, step, receipt=None):
        self.batches[window]["continuation"] = {step: receipt or self.receipt(step)}
        self.write_private(self.pilot / f"window-{window:02}.json", self.batches[window])

    def test_lost_worker_and_workflow_resume_with_automatic_successor(self):
        import json

        import operating_report
        from resume_campaign import qualify_unattended

        self.run_at(0)
        for index, step in enumerate(self.STEPS, start=1):
            self.add_step(index, step)
            result = self.run_at(index)
            if index < len(self.STEPS):
                self.assertEqual(result["unattended"]["status"], "incomplete")
            else:
                self.assertEqual(result["unattended"], {"status": "pass", "missing": []})
                self.assertEqual(result["scope"], "offline-evidence-evaluation")
                self.assertEqual(self.run_at(index), result)
        now = self.start + timedelta(hours=33)
        self.assertEqual(qualify_unattended(self.root, "pilot-fixture", self.revision,
                                            clock=lambda: now)["status"], "pass")
        checkpoint = json.loads((self.pilot / "checkpoint.json").read_text())
        self.assertEqual(len(checkpoint["continuation"]), len(self.STEPS))
        self.assertEqual(checkpoint["batches"]["window-00"], self.campaign._digest(self.batches[0]))
        self.assertEqual(json.loads((self.pilot / "report.json").read_text())["continuation_receipts"]
                         ["automatic_successor"]["evidence_ref"], "private-automatic_successor")
        indexed = operating_report.build(self.root, "pilot-fixture", self.revision, clock=lambda: now)
        self.assertIn("automatic_successor-attempt", indexed["acceptance"]["AC-02"]["attempt_ids"])
        self.assertIn("private-workflow_loss", indexed["acceptance"]["AC-02"]["private_evidence_refs"])
        self.assertEqual(indexed["acceptance"]["AC-02"]["live_status"], "incomplete")

    def test_missing_or_changed_recovery_evidence_cannot_advance_checkpoint(self):
        import copy

        from resume_campaign import qualify_unattended

        self.run_at(0)
        self.add_step(1, "worker_restart")
        first = self.run_at(1)
        self.assertEqual(first["unattended"]["status"], "incomplete")
        with self.assertRaisesRegex(ValueError, "missing or inconsistent"):
            qualify_unattended(self.root, "pilot-fixture", self.revision,
                               clock=lambda: self.start + timedelta(hours=9))
        original = (self.pilot / "checkpoint.json").read_bytes()
        for step, changes, error in (
            ("workflow_loss", {"checkpoint_batch_sha256": "f" * 64}, "checkpoint"),
            ("workflow_loss", {"revision": "changed"}, "pins"),
            ("workflow_loss", {"resumed_workflow_id": "lost-wf"}, "replacement"),
            ("queued_ci", {"queued_at": (self.start + timedelta(hours=7)).isoformat(),
                           "started_at": (self.start + timedelta(hours=7)).isoformat()}, "queued CI"),
            ("token_refresh", {"new_session_id": "old-session"}, "replacement"),
            ("mediated_repair", {"ci_pass_ref": ""}, "missing observations"),
            ("workflow_loss", {"observed_at": (self.start + timedelta(hours=99)).isoformat()}, "elapsed time"),
        ):
            receipt = copy.deepcopy(self.receipt(step))
            receipt["data"]["observed_at"] = (self.start + timedelta(hours=12)).isoformat()
            receipt["data"].update(changes)
            self.add_step(2, step, receipt)
            with self.assertRaisesRegex(ValueError, error):
                self.run_at(2)
            self.assertEqual((self.pilot / "checkpoint.json").read_bytes(), original)
        self.add_step(2, "workflow_loss")
        self.run_at(2)
        self.add_step(3, "workflow_loss")
        with self.assertRaisesRegex(ValueError, "conflicting continuation step"):
            self.run_at(3)

    def test_disconnected_successor_and_tampered_report_do_not_qualify(self):
        import copy
        import json

        from resume_campaign import qualify_unattended

        self.run_at(0)
        for index, step in enumerate(self.STEPS[:-1], start=1):
            self.add_step(index, step)
            self.run_at(index)
        original = (self.pilot / "checkpoint.json").read_bytes()
        for changes in ({"verification_ref": "other"}, {"source_operation_id": "other"}, {"trigger": "manual"},
                        {"workflow_id": "lost-wf"}, {"observed_at": (self.start + timedelta(hours=9)).isoformat()}):
            receipt = copy.deepcopy(self.receipt("automatic_successor"))
            receipt["data"].update(changes)
            self.add_step(7, "automatic_successor", receipt)
            with self.assertRaisesRegex(ValueError, "disconnected"):
                self.run_at(7)
            self.assertEqual((self.pilot / "checkpoint.json").read_bytes(), original)
        self.add_step(7, "automatic_successor")
        self.run_at(7)
        report = json.loads((self.pilot / "report.json").read_text())
        report["continuation_receipts"]["workflow_loss"]["evidence_ref"] = "other"
        self.write_private(self.pilot / "report.json", report)
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            qualify_unattended(self.root, "pilot-fixture", self.revision,
                               clock=lambda: self.start + timedelta(hours=33))


class RecoveryWindowTests(FreshnessWindowTests):
    def setUp(self):
        FreshnessWindowTests.setUp(self)
        RecoveryTests.setUp(self)
        from resume_campaign import qualify_recovery

        self.qualify_recovery = qualify_recovery
        self.frozen["required_db_clients"] = self.clients
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.launch["frozen_sha256"] = self.campaign._digest(self.frozen)
        self.write_private(self.pilot / "launch.json", self.launch)
        self.batches[0]["recovery"] = {name: RecoveryCampaignTests.receipt(self, name)
                                       for name in ("b1", "source_outage")}
        self.batches[1]["recovery"] = {name: RecoveryCampaignTests.receipt(self, name)
                                       for name in ("revocation", "db_clients", "modes", "profile", "finalization")}
        for index in (0, 1):
            self.write_private(self.pilot / f"window-{index:02}.json", self.batches[index])

    def check_recovery(self):
        now = self.start + timedelta(hours=8, minutes=15)
        return self.qualify_recovery(self.root, "pilot-fixture", self.revision, clock=lambda: now)

    def test_integrated_recovery(self):
        import contextlib
        import io
        import json
        import sys
        from unittest import mock

        initial = self.run_at(0)
        self.assertEqual(initial["recovery"]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "cases are incomplete"):
            self.check_recovery()
        final = self.run_at(1)
        self.assertEqual(final["status"], "incomplete")
        self.assertEqual(final["recovery"]["checks"]["optional_modes"], "pass")
        self.assertEqual(final["recovery"]["checks"]["finalization"], "pass")
        self.assertEqual(self.check_recovery()["batch_count"], 2)
        self.assertEqual(self.run_at(1), final)
        import resume_campaign

        output = io.StringIO()
        arguments = ["resume_campaign.py", "--root", str(self.root), "--campaign-id", "pilot-fixture",
                     "--revision", self.revision, "--recovery"]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(output):
            resume_campaign.main(clock=lambda: self.start + timedelta(hours=8, minutes=15))
        self.assertEqual(json.loads(output.getvalue())["status"], "pass")
        self.assertEqual(json.loads(output.getvalue())["window_count"], 2)

    def test_missing_source_blocks_qualification_without_masking_failed_finalization(self):
        import json

        self.run_at(0)
        checkpoint = self.pilot / "checkpoint.json"
        preserved = checkpoint.read_bytes()
        (self.pilot / "window-01.json").unlink()
        with self.assertRaisesRegex(ValueError, "private campaign input"):
            self.run_window(self.root, "pilot-fixture", self.revision, 1,
                            clock=lambda: self.start + timedelta(hours=8, minutes=21))
        self.assertEqual(checkpoint.read_bytes(), preserved)
        with self.assertRaisesRegex(ValueError, "cases are incomplete"):
            self.check_recovery()
        with self.assertRaisesRegex(ValueError, "required windows"):
            self.qualify(self.root, "pilot-fixture", self.revision,
                         clock=lambda: self.start + timedelta(hours=49))
        finalizer_workflow = (SOURCE.parents[4] / ".github/workflows/insights-poc-finalize.yml").read_text()
        self.assertIn("options: [probe, abort, failure, hold]", finalizer_workflow)
        self.assertIn("environment: adp-devlake-finalizer", finalizer_workflow)
        self.assertIn('finalize.py --mode manual --action "$ACTION"', finalizer_workflow)
        self.assertEqual(json.loads(checkpoint.read_text())["batches"], {"window-00":
                         self.campaign._digest(self.batches[0])})

    def test_missing_or_failed_recovery_does_not_qualify(self):
        self.run_at(0)
        del self.batches[1]["recovery"]["finalization"]
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        report = self.run_at(1)
        self.assertEqual(report["recovery"]["checks"]["finalization"], "unavailable")
        with self.assertRaisesRegex(ValueError, "cases are incomplete"):
            self.check_recovery()

    def test_revoked_work_and_off_mode_failure_are_rejected(self):
        self.batches[1]["recovery"]["revocation"]["data"]["writes_after"] = 1
        self.batches[1]["recovery"]["modes"]["data"]["off"]["provider_calls"] = 1
        self.batches[1]["recovery"]["finalization"]["data"]["expired"]["attempted_while_off"] = False
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        self.run_at(0)
        result = self.run_at(1)
        for name in ("revocation", "optional_modes", "finalization"):
            self.assertEqual(result["recovery"]["checks"][name], "fail")
        with self.assertRaisesRegex(ValueError, "recovery readback"):
            self.check_recovery()

    def test_changed_report_or_frozen_inventory_rejects_readback(self):
        self.run_at(0)
        self.run_at(1)
        report = self.campaign.json.loads((self.pilot / "report.json").read_text())
        report["recovery_receipts"]["modes"]["attempt_id"] = "unrelated-attempt"
        self.write_private(self.pilot / "report.json", report)
        with self.assertRaisesRegex(ValueError, "recovery readback"):
            self.check_recovery()
        self.frozen["required_db_clients"].append("new-client")
        self.write_private(self.pilot / "frozen.json", self.frozen)
        with self.assertRaisesRegex(ValueError, "launch pins"):
            self.check_recovery()


class CompatibilityWindowTests(FreshnessWindowTests):
    def setUp(self):
        FreshnessWindowTests.setUp(self)
        CompatibilityTests.setUp(self)
        from resume_campaign import qualify_compatibility

        self.qualify_compatibility = qualify_compatibility
        self.frozen["upgrade"] = self.pins
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.launch["frozen_sha256"] = self.campaign._digest(self.frozen)
        self.write_private(self.pilot / "launch.json", self.launch)
        self.batches[1]["upgrade"] = {"attempt_id": "upgrade-attempt-1", "observer": "independent verifier",
                                       "evidence_ref": "private-upgrade", "data": self.observations}
        self.write_private(self.pilot / "window-01.json", self.batches[1])

    def check_compatibility(self):
        now = self.start + timedelta(hours=8, minutes=15)
        return self.qualify_compatibility(self.root, "pilot-fixture", self.revision, clock=lambda: now)

    def test_compatibility_recovery(self):
        import contextlib
        import io
        import json
        import sys
        from unittest import mock
        import resume_campaign

        self.assertEqual(self.run_at(0)["compatibility"]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "upgrade attempt"):
            self.check_compatibility()
        final = self.run_at(1)
        self.assertEqual(final["status"], "incomplete")
        self.assertEqual(final["compatibility"]["status"], "pass")
        self.assertEqual(final["compatibility"]["patch_comparison"]["effort_minutes"], 12)
        self.assertEqual(final["compatibility"]["checks"]["rollback"], "pass")
        self.assertEqual(self.check_compatibility()["batch_count"], 2)
        self.assertEqual(self.run_at(1), final)
        output = io.StringIO()
        arguments = ["resume_campaign.py", "--root", str(self.root), "--campaign-id", "pilot-fixture",
                     "--revision", self.revision, "--compatibility"]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(output):
            resume_campaign.main(clock=lambda: self.start + timedelta(hours=8, minutes=15))
        self.assertEqual(json.loads(output.getvalue()),
                         {"scope": "offline-evidence-evaluation", "status": "pass", "window_count": 2})

    def test_missing_migration_receipt_never_passes(self):
        self.batches[1].pop("upgrade")
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        self.run_at(0)
        self.run_at(1)
        with self.assertRaisesRegex(ValueError, "upgrade attempt"):
            self.check_compatibility()

    def test_incompatible_schema_worker_ddl_and_rollback_are_rejected(self):
        upgrade = self.batches[1]["upgrade"]["data"]
        upgrade["negative_admission"]["incompatible_schema"]["provider_calls"] = 1
        upgrade["worker_ddl"]["create"] = "allowed"
        upgrade["rollback"]["restored_image"] = self.pins["candidate_image"]
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        self.run_at(0)
        result = self.run_at(1)
        for name in ("incompatible_schema", "worker_ddl", "rollback"):
            self.assertEqual(result["compatibility"]["checks"][name], "fail")
        with self.assertRaisesRegex(ValueError, "compatibility readback"):
            self.check_compatibility()

    def test_changed_pins_and_report_refuse_readback(self):
        import json

        self.run_at(0)
        self.run_at(1)
        report = json.loads((self.pilot / "report.json").read_text())
        report["upgrade_receipt"]["attempt_id"] = "other-attempt"
        self.write_private(self.pilot / "report.json", report)
        with self.assertRaisesRegex(ValueError, "compatibility readback"):
            self.check_compatibility()
        self.pins["schema_manifest_sha256"] = "6" * 64
        self.write_private(self.pilot / "frozen.json", self.frozen)
        with self.assertRaisesRegex(ValueError, "launch pins"):
            self.check_compatibility()


class IsolationWindowTests(FreshnessWindowTests):
    def setUp(self):
        FreshnessWindowTests.setUp(self)
        IsolationTests.setUp(self)
        from resume_campaign import qualify_isolation

        self.qualify_isolation = qualify_isolation
        self.frozen["isolation"] = self.pins
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.launch["frozen_sha256"] = self.campaign._digest(self.frozen)
        self.write_private(self.pilot / "launch.json", self.launch)
        self.batches[1]["isolation"] = {"attempt_id": "isolation-attempt-1", "observer": "independent verifier",
                                         "evidence_ref": "private-isolation", "data": self.observations}
        self.write_private(self.pilot / "window-01.json", self.batches[1])

    def check_isolation(self):
        now = self.start + timedelta(hours=8, minutes=15)
        return self.qualify_isolation(self.root, "pilot-fixture", self.revision, clock=lambda: now)

    def test_end_to_end_isolation(self):
        import contextlib
        import io
        import json
        import sys
        from unittest import mock
        import resume_campaign

        self.assertEqual(self.run_at(0)["isolation"]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "isolation attempt"):
            self.check_isolation()
        result = self.run_at(1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["isolation"]["status"], "pass")
        for name in ("cell_and_identity_routes", "revocation_barrier", "shared_publication",
                     "db_denial", "s3_denial", "kms_denial"):
            self.assertEqual(result["isolation"]["checks"][name], "pass")
        self.assertEqual(self.check_isolation()["batch_count"], 2)
        self.assertEqual(self.run_at(1), result)
        output = io.StringIO()
        arguments = ["resume_campaign.py", "--root", str(self.root), "--campaign-id", "pilot-fixture",
                     "--revision", self.revision, "--isolation"]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(output):
            resume_campaign.main(clock=lambda: self.start + timedelta(hours=8, minutes=15))
        self.assertEqual(json.loads(output.getvalue()),
                         {"scope": "offline-evidence-evaluation", "status": "pass", "window_count": 2})

    def test_missing_storage_denial_cannot_pass(self):
        del self.batches[1]["isolation"]["data"]["storage_denials"]["kms"]
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        self.run_at(0)
        result = self.run_at(1)
        self.assertEqual(result["isolation"]["checks"]["kms_denial"], "unavailable")
        with self.assertRaisesRegex(ValueError, "isolation readback"):
            self.check_isolation()

    def test_forged_route_revocation_and_private_publication_are_rejected(self):
        observed = self.batches[1]["isolation"]["data"]
        observed["routes"][2]["identity"][1] = "project-1"
        observed["negative"]["forged_route"]["raw_writes"] = 1
        observed["revocation"]["released_bytes"] = 1
        observed["shared_publication"]["after_bytes_base64"] = "cHJpdmF0ZQ=="
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        self.run_at(0)
        result = self.run_at(1)
        for name in ("cell_and_identity_routes", "forged_route", "revocation_barrier", "shared_publication"):
            self.assertEqual(result["isolation"]["checks"][name], "fail")
        with self.assertRaisesRegex(ValueError, "isolation readback"):
            self.check_isolation()

    def test_changed_policy_pin_or_report_refuses_readback(self):
        import json

        self.run_at(0)
        self.run_at(1)
        report = json.loads((self.pilot / "report.json").read_text())
        report["isolation_receipt"]["attempt_id"] = "other-attempt"
        self.write_private(self.pilot / "report.json", report)
        with self.assertRaisesRegex(ValueError, "isolation readback"):
            self.check_isolation()
        self.pins["descriptor_sha256"] = "changed-policy-pin"
        self.write_private(self.pilot / "frozen.json", self.frozen)
        with self.assertRaisesRegex(ValueError, "launch pins"):
            self.check_isolation()


class CampaignSchemaTests(FreshnessWindowTests):
    def test_schema_is_valid_and_rejects_malformed_launch(self):
        from jsonschema import Draft202012Validator
        import campaign_schema

        Draft202012Validator.check_schema(campaign_schema.SCHEMA)
        campaign_schema.validate(self.launch, "launch")
        campaign_schema.validate(self.frozen, "frozen")
        campaign_schema.validate(self.approved, "limits")
        campaign_schema.validate(self.batches[0], "batch")
        self.launch.pop("engine_until")
        self.write_private(self.pilot / "launch.json", self.launch)
        with self.assertRaisesRegex(ValueError, "launch does not match campaign schema"):
            self.run_at(0)
        self.assertFalse((self.pilot / "checkpoint.json").exists())

    def test_malformed_frozen_sample_and_limits_reject_before_checkpoint(self):
        removed = self.frozen["updates"].pop()
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.launch["frozen_sha256"] = self.campaign._digest(self.frozen)
        self.write_private(self.pilot / "launch.json", self.launch)
        with self.assertRaisesRegex(ValueError, "frozen does not match campaign schema"):
            self.run_at(0)
        self.frozen["updates"].append(removed)
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.launch["frozen_sha256"] = self.campaign._digest(self.frozen)
        self.approved["limits"]["freshness_p95_minutes"] = 16
        self.write_private(self.pilot / "approved-limits.json", self.approved)
        self.launch["limits_sha256"] = self.campaign._digest(self.approved)
        self.write_private(self.pilot / "launch.json", self.launch)
        with self.assertRaisesRegex(ValueError, "limits does not match campaign schema"):
            self.run_at(0)
        self.assertFalse((self.pilot / "checkpoint.json").exists())

    def test_malformed_batch_and_direct_campaign_reject_before_write(self):
        bad_batch = self.batches[0]
        original = bad_batch["reads"][0]["read_at"]
        bad_batch["reads"][0]["read_at"] = "private-malformed-value"
        self.write_private(self.pilot / "window-00.json", bad_batch)
        with self.assertRaisesRegex(ValueError, "batch does not match campaign schema") as failure:
            self.run_at(0)
        self.assertNotIn("private-malformed-value", str(failure.exception))
        self.assertFalse((self.pilot / "checkpoint.json").exists())
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            bad_batch["reads"][0]["read_at"] = original
            bad_batch["measurements"]["query_seconds"] = ["invalid"]
            with self.assertRaisesRegex(ValueError, "batch does not match campaign schema"):
                self.campaign.record(self.frozen, bad_batch, self.approved,
                                     Path(directory) / "checkpoint.json", Path(directory) / "report.json")
            self.assertFalse((Path(directory) / "checkpoint.json").exists())

    def test_invalid_checkpoint_and_report_reject_readback(self):
        import json

        self.run_at(0)
        state = json.loads((self.pilot / "checkpoint.json").read_text())
        state["batches"]["window-00"] = "invalid-digest"
        self.write_private(self.pilot / "checkpoint.json", state)
        with self.assertRaisesRegex(ValueError, "checkpoint does not match campaign schema"):
            self.run_at(1)
        self.assertNotIn("window-01", json.loads((self.pilot / "checkpoint.json").read_text())["batches"])
        state["batches"]["window-00"] = self.campaign._digest(self.batches[0])
        self.write_private(self.pilot / "checkpoint.json", state)
        for window in range(1, 12):
            self.run_at(window)
        report = json.loads((self.pilot / "report.json").read_text())
        del report["status"]
        self.write_private(self.pilot / "report.json", report)
        with self.assertRaisesRegex(ValueError, "report does not match campaign schema"):
            self.qualify(self.root, "pilot-fixture", self.revision,
                         clock=lambda: self.start + timedelta(hours=49))

    def test_older_checkpoint_remains_resumable_without_new_case_fields(self):
        import json

        self.run_at(0)
        checkpoint = self.pilot / "checkpoint.json"
        state = json.loads(checkpoint.read_text())
        for field in ("recovery", "upgrade", "isolation", "continuation"):
            state.pop(field)
        self.write_private(checkpoint, state)
        result = self.run_at(1)
        self.assertEqual(result["batch_count"], 2)
        restored = json.loads(checkpoint.read_text())
        self.assertEqual(restored["recovery"], {})
        self.assertIsNone(restored["upgrade"])
        self.assertIsNone(restored["isolation"])


class OperatingReportTests(FreshnessWindowTests):
    def setUp(self):
        FreshnessWindowTests.setUp(self)
        RecoveryTests.setUp(self)
        self.frozen["required_db_clients"] = self.clients
        self.batches[0]["recovery"] = {name: RecoveryCampaignTests.receipt(self, name)
                                       for name in ("b1", "source_outage")}
        self.batches[1]["recovery"] = {name: RecoveryCampaignTests.receipt(self, name)
                                       for name in ("revocation", "db_clients", "modes", "profile", "finalization")}
        CompatibilityTests.setUp(self)
        self.frozen["upgrade"] = self.pins
        self.batches[1]["upgrade"] = {"attempt_id": "upgrade-attempt-1", "observer": "independent verifier",
                                       "evidence_ref": "private-upgrade", "data": self.observations}
        IsolationTests.setUp(self)
        self.frozen["isolation"] = self.pins
        self.batches[1]["isolation"] = {"attempt_id": "isolation-attempt-1", "observer": "independent verifier",
                                         "evidence_ref": "private-isolation", "data": self.observations}
        self.write_private(self.pilot / "frozen.json", self.frozen)
        self.launch["frozen_sha256"] = self.campaign._digest(self.frozen)
        self.write_private(self.pilot / "launch.json", self.launch)
        for window in (0, 1):
            self.write_private(self.pilot / f"window-{window:02}.json", self.batches[window])

    def report_index(self, hours=9):
        import operating_report

        return operating_report.build(self.root, "pilot-fixture", self.revision,
                                      clock=lambda: self.start + timedelta(hours=hours))

    def test_all_criteria_indexed_with_private_attempts_and_missing_live_proof(self):
        import json
        import stat

        self.run_at(0)
        self.run_at(1)
        result = self.report_index()
        self.assertEqual(result["verdict"], "incomplete")
        self.assertEqual(set(result["acceptance"]), {"AC-01", "AC-02", "AC-03", "AC-04"})
        self.assertEqual([result["acceptance"][key]["offline_status"] for key in sorted(result["acceptance"])],
                         ["incomplete", "pass", "pass", "pass"])
        for criterion, row in result["acceptance"].items():
            self.assertEqual(row["revision"], self.revision)
            self.assertEqual(row["run_id"], "pilot-fixture")
            self.assertEqual(row["scenario"], "SCN-" + criterion[-2:])
            self.assertTrue(row["attempt_ids"])
            self.assertTrue(row["private_evidence_refs"])
            self.assertEqual((row["live_status"], row["cleanup_status"]), ("incomplete", "unverified"))
            self.assertTrue(row["missing_live_proof"])
        self.assertIn("expired-attempt-1", result["acceptance"]["AC-02"]["attempt_ids"])
        self.assertIn("private-upgrade", result["acceptance"]["AC-03"]["private_evidence_refs"])
        output = self.pilot / "operating-report.json"
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(json.loads(output.read_text()), result)
        self.assertEqual(self.report_index(), result)

    def test_complete_offline_soak_or_expiry_does_not_promote_live_acceptance(self):
        import contextlib
        import io
        import json
        import sys
        from unittest import mock
        import operating_report

        for window in range(12):
            self.run_at(window)
        result = self.report_index(hours=70)
        self.assertEqual(result["acceptance"]["AC-01"]["offline_status"], "pass")
        self.assertTrue(all(item["live_status"] == "incomplete" for item in result["acceptance"].values()))
        self.assertEqual(result["measurements"]["freshness"]["p95_minutes"], 14)
        output = io.StringIO()
        args = ["operating_report.py", "--root", str(self.root), "--campaign-id", "pilot-fixture",
                "--revision", self.revision]
        with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(output):
            operating_report.main()
        self.assertEqual(json.loads(output.getvalue())["verdict"], "incomplete")
        self.assertNotIn("private-upgrade", output.getvalue())

    def test_absent_receipts_and_tampering_cannot_fabricate_acceptance(self):
        import json

        for key in ("recovery", "upgrade", "isolation"):
            self.batches[1].pop(key)
        self.write_private(self.pilot / "window-01.json", self.batches[1])
        self.run_at(0)
        self.run_at(1)
        result = self.report_index()
        for criterion in ("AC-03", "AC-04"):
            self.assertEqual(result["acceptance"][criterion]["offline_status"], "unavailable")
            self.assertEqual(result["acceptance"][criterion]["attempt_ids"], [])
        self.assertEqual(result["acceptance"]["AC-02"]["offline_status"], "unavailable")
        output = self.pilot / "operating-report.json"
        original = output.read_bytes()
        report = json.loads((self.pilot / "report.json").read_text())
        report["status"] = "pass"
        self.write_private(self.pilot / "report.json", report)
        with self.assertRaisesRegex(ValueError, "verdict differs"):
            self.report_index()
        self.assertEqual(output.read_bytes(), original)
        report["status"] = "fail"
        self.write_private(self.pilot / "report.json", report)
        state = json.loads((self.pilot / "checkpoint.json").read_text())
        state["recovery"]["b1"]["data"]["duplicate_count"] = 1
        self.write_private(self.pilot / "checkpoint.json", state)
        with self.assertRaisesRegex(ValueError, "evidence differs"):
            self.report_index()
        self.assertEqual(output.read_bytes(), original)


def provider_fixture(source_revision, run_id, run_attempt):
    from modules.insights.poc.devlake.provider_evidence import PROVIDERS

    selection = {"source_revision": source_revision, "providers": {
        provider: {"tenant_id": "tenant-one", "project_id": "project-one",
                   "connection_id": f"connection-{provider}", "connection_revision": 1,
                   "binding_id": f"binding-{provider}", "binding_revision": 1, "source_generation": 1,
                   "record_id": f"native-{provider}", "record_type": "native-scope", "record_revision": "r1"}
        for provider in PROVIDERS}}
    evidence = {provider: {"revision": revision, "source_revision": source_revision,
                           "run_id": run_id, "run_attempt": run_attempt,
                           "evidence_ref": f"protected-{provider}-readback", "outcome": "pass"}
                for provider, revision in selection["providers"].items()}
    return selection, evidence


class ProtectedVerdictHost:
    def __init__(self):
        self.bindings = [{"repository": "example/insights", "issue_number": number,
                          "requirement_revision": revision * 64}
                         for number, revision in ((11, "a"), (12, "b"))]
        self.verdict = {"run_id": "accepted-run", "run_attempt": 1, "phase": "closure", "status": "accepted",
                        "receipt_ref": "protected-final-receipt", "requirements": self.bindings,
                        "acceptance": {f"{kind}-{index:02}": "pass" for kind in ("AC", "SCN")
                                       for index in range(1, 6)}}
        from modules.insights.poc.devlake.provider_evidence import verify
        self.provider_selection, self.verdict["provider_evidence"] = provider_fixture(
            "a" * 40, "accepted-run", 1)
        self.verdict["provider_revisions_digest"] = verify(
            self.verdict["provider_evidence"], self.provider_selection, "a" * 40, "accepted-run", 1)
        self.issues = {item["issue_number"]: {**item, "state": "open", "closure_key": None}
                       for item in self.bindings}
        self.closes = []
        self.lose_ack = False
        self.race = False
        self.race_provider = False
        self.stop_on_number = None

    def read_final_verdict(self, run_id):
        return self.verdict

    def read_accepted_issues(self, run_id):
        return self.bindings

    def read_current_provider_revisions(self, run_id):
        return self.provider_selection

    def read_issue(self, repository, number):
        return dict(self.issues[number])

    def close_issue_if_current(self, repository, number, revision, closure_key, provider_revisions_digest):
        from modules.insights.poc.devlake.provider_evidence import verify
        if self.race_provider:
            self.provider_selection["providers"]["linear"]["binding_revision"] += 1
        current = verify(self.verdict["provider_evidence"], self.provider_selection,
                         self.provider_selection["source_revision"], self.verdict["run_id"],
                         self.verdict["run_attempt"])
        if current != provider_revisions_digest:
            raise ValueError("atomic compare-and-close refused changed provider revisions")
        if number == self.stop_on_number:
            raise ConnectionError("closure unavailable before write")
        issue = self.issues[number]
        if self.race:
            issue["requirement_revision"] = "c" * 64
        if issue["state"] != "open" or issue["requirement_revision"] != revision:
            raise ValueError("atomic compare-and-close refused a changed revision")
        issue.update(state="closed", closure_key=closure_key)
        self.closes.append(number)
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError("closure response lost after write")


class CompletionConsumerTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SOURCE.parent))
        self.addCleanup(lambda: sys.path.remove(str(SOURCE.parent)))
        specification = importlib.util.spec_from_file_location("complete_campaign", SOURCE.with_name("complete_campaign.py"))
        complete_campaign = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(complete_campaign)

        self.consume = complete_campaign.consume
        self.host = ProtectedVerdictHost()

    def test_protected_verdict_closes_once_and_lost_ack_reconciles(self):
        self.host.lose_ack = True
        first = self.consume("accepted-run", self.host)
        self.assertEqual([item["issue_number"] for item in first["closed"]], [11, 12])
        self.assertEqual(self.host.closes, [11, 12])
        retry = self.consume("accepted-run", self.host)
        self.assertEqual(retry["closed"], [])
        self.assertEqual([item["issue_number"] for item in retry["replayed"]], [11, 12])
        self.assertEqual(self.host.closes, [11, 12])

    def test_missing_stale_or_racing_provider_proof_never_closes(self):
        import copy

        for provider in ("slack", "jenkins"):
            with self.subTest(provider=provider):
                self.host.verdict["provider_evidence"].pop(provider)
                with self.assertRaisesRegex(ValueError, "seven current provider"):
                    self.consume("accepted-run", self.host)
                self.assertEqual(self.host.closes, [])
                _, self.host.verdict["provider_evidence"] = provider_fixture("a" * 40, "accepted-run", 1)
        previous = copy.deepcopy(self.host.provider_selection)
        self.host.provider_selection["providers"]["linear"]["binding_revision"] += 1
        with self.assertRaisesRegex(ValueError, "stale"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])
        self.host.provider_selection = previous
        self.host.provider_selection["providers"]["linear"]["project_id"] = "foreign-project"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.consume("accepted-run", self.host)
        self.host.provider_selection = previous
        self.host.verdict["acceptance"]["AC-05"] = "not_run"
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.consume("accepted-run", self.host)
        self.host.verdict["acceptance"]["AC-05"] = "pass"
        self.host.race_provider = True
        with self.assertRaisesRegex(ValueError, "stale"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])

    def test_changed_current_revision_refuses_every_issue_before_closure(self):
        self.host.issues[12]["requirement_revision"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "requirement revision changed"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])
        self.assertEqual(self.host.issues[11]["state"], "open")

    def test_changed_accepted_revision_or_missing_verdict_never_closes(self):
        self.host.verdict["requirements"] = [{**self.host.bindings[0], "requirement_revision": "c" * 64},
                                             self.host.bindings[1]]
        with self.assertRaisesRegex(ValueError, "revisions differ"):
            self.consume("accepted-run", self.host)
        self.host.verdict["requirements"] = self.host.bindings
        self.host.verdict["acceptance"]["AC-02"] = "not_run"
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.consume("accepted-run", self.host)
        self.host.verdict["acceptance"]["AC-02"] = "pass"
        self.host.verdict["phase"] = "pilot"
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])

    def test_atomic_revision_race_refuses_close_and_unrelated_close_cannot_replay(self):
        self.host.race = True
        with self.assertRaisesRegex(ValueError, "requirement revision changed"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])
        self.host.race = False
        self.host.issues[11]["requirement_revision"] = "a" * 64
        self.host.issues[11].update(state="closed", closure_key="unrelated")
        with self.assertRaisesRegex(ValueError, "without this protected verdict"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])

    def test_unknown_state_prevents_closure_and_new_receipt_is_distinct(self):
        self.host.issues[12]["state"] = "unknown"
        with self.assertRaisesRegex(ValueError, "state is unavailable"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [])
        self.host.issues[12]["state"] = "open"
        self.consume("accepted-run", self.host)
        self.host.verdict["run_attempt"] = 2
        _, self.host.verdict["provider_evidence"] = provider_fixture("a" * 40, "accepted-run", 2)
        with self.assertRaisesRegex(ValueError, "without this protected verdict"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [11, 12])

    def test_partial_close_retries_without_reclosing_first_issue(self):
        self.host.stop_on_number = 12
        with self.assertRaisesRegex(ConnectionError, "before write"):
            self.consume("accepted-run", self.host)
        self.assertEqual(self.host.closes, [11])
        self.assertEqual(self.host.issues[12]["state"], "open")
        self.host.stop_on_number = None
        retry = self.consume("accepted-run", self.host)
        self.assertEqual([item["issue_number"] for item in retry["closed"]], [12])
        self.assertEqual([item["issue_number"] for item in retry["replayed"]], [11])
        self.assertEqual(self.host.closes, [11, 12])


if __name__ == "__main__":
    unittest.main()


class PilotEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((SOURCE.parents[4] / "docs/engineering-insights/devlake-poc/delivery-flow/acceptance-mapping.json").read_text())
        self.rows = [row for row in self.plan["criteria"] if row["story"] != "S08"]
        self.pins = {"source_revision": "a" * 40, "definition_revision": "b" * 40,
                     "run_id": "123", "run_attempt": "2",
                     "component_digests": {name: "c" * 64 for name in VERIFY.COMPONENTS}}
        self.readbacks = {}
        self.manifest = {**self.pins, "criteria": {}}
        for row in self.rows:
            criterion_id = row["criterion_id"]
            receipt = {**self.pins, "criterion_id": criterion_id, "scenario_id": row["scenario_id"],
                       "outcome": "pass", "observer": "independent-test-reader",
                       "evidence_ref": "protected-observation"}
            raw = json.dumps(receipt, sort_keys=True).encode()
            self.readbacks[criterion_id] = raw
            self.manifest["criteria"][criterion_id] = {
                "ref": criterion_id, "sha256": VERIFY.hashlib.sha256(raw).hexdigest()}

    def evaluate(self):
        return VERIFY.pilot_document(self.manifest, self.plan, self.readbacks.__getitem__)

    def test_all_original_required_criteria_produce_schema_valid_offline_summary(self):
        self.assertEqual(len(self.rows), 46)
        original = [row for row in self.plan["criteria"] if row["story"] != "S14"]
        self.assertEqual(len(original), 44)
        self.assertEqual([row["scenario_id"] for row in self.plan["criteria"] if row["story"] == "S14"],
                         [f"SCN-{index:02}" for index in range(1, 6)])
        summary = self.evaluate()
        self.assertEqual((summary["status"], summary["required_passed"], summary["required_not_run"]),
                         ("pass", 46, 0))
        self.assertEqual(summary["independent_verification"], "offline-only")
        import jsonschema
        schema = json.loads(SOURCE.with_name("campaign.schema.json").read_text())
        jsonschema.Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/pilot_summary"}).validate(summary)
        with self.assertRaisesRegex(ValueError, "independent pilot observations"):
            VERIFY.pilot_document(self.manifest, self.plan)

    def test_missing_and_failed_criteria_cannot_pass(self):
        removed = self.rows[0]["criterion_id"]
        self.manifest["criteria"].pop(removed)
        with self.assertRaisesRegex(ValueError, "missing 1 required criteria"):
            self.evaluate()
        self.manifest["criteria"][removed] = {"ref": removed,
                                                 "sha256": VERIFY.hashlib.sha256(self.readbacks[removed]).hexdigest()}
        for outcome, count in (("fail", "required_failed"), ("skipped", "required_skipped"),
                               ("not-run", "required_not_run")):
            with self.subTest(outcome=outcome):
                receipt = json.loads(self.readbacks[removed])
                receipt["outcome"] = outcome
                self.readbacks[removed] = json.dumps(receipt, sort_keys=True).encode()
                self.manifest["criteria"][removed]["sha256"] = VERIFY.hashlib.sha256(self.readbacks[removed]).hexdigest()
                summary = self.evaluate()
                self.assertEqual((summary["status"], summary[count], summary["required_passed"]),
                             ("fail", 1, 45))

    def test_missing_independent_source_and_changed_scenario_fail(self):
        criterion_id = self.rows[0]["criterion_id"]
        self.readbacks.pop(criterion_id)
        with self.assertRaisesRegex(ValueError, "independent readback is missing"):
            self.evaluate()
        receipt = {**self.pins, "criterion_id": criterion_id, "scenario_id": "different",
                   "outcome": "pass", "observer": "independent-test-reader",
                   "evidence_ref": "protected-observation"}
        self.readbacks[criterion_id] = json.dumps(receipt, sort_keys=True).encode()
        self.manifest["criteria"][criterion_id]["sha256"] = VERIFY.hashlib.sha256(self.readbacks[criterion_id]).hexdigest()
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.evaluate()

    def test_changed_readback_or_run_revision_and_component_pins_refuse(self):
        criterion_id = self.rows[0]["criterion_id"]
        self.readbacks[criterion_id] += b" "
        with self.assertRaisesRegex(ValueError, "digest differs"):
            self.evaluate()
        self.readbacks[criterion_id] = self.readbacks[criterion_id].strip()
        for field, value in (("run_id", "456"), ("source_revision", "different"),
                             ("definition_revision", "different"), ("run_attempt", "3"),
                             ("component_digests", {**self.pins["component_digests"], "mapper": "d" * 64})):
            with self.subTest(field=field):
                self.manifest[field] = value
                with self.assertRaisesRegex(ValueError, "provenance"):
                    self.evaluate()
                self.manifest[field] = self.pins[field]


class PilotWorkflowContractTests(FreshnessWindowTests):
    def test_private_manifest_is_bound_to_admitted_campaign(self):
        from resume_campaign import campaign_from_manifest

        path = self.pilot / "manifest.json"
        self.write_private(path, {"campaign_id": "pilot-fixture", "source_revision": self.revision})
        clock = lambda: self.start + timedelta(hours=1)
        self.assertEqual(campaign_from_manifest(self.root, path, self.revision, clock), "pilot-fixture")
        for altered in ({"campaign_id": "other", "source_revision": self.revision},
                        {"campaign_id": "pilot-fixture", "source_revision": "other"}):
            self.write_private(path, altered)
            with self.assertRaisesRegex(ValueError, "campaign or source revision"):
                campaign_from_manifest(self.root, path, self.revision, clock)
        with self.assertRaisesRegex(ValueError, "protected campaign root"):
            campaign_from_manifest(self.root, self.root / "outside.json", self.revision, clock)

    def test_dispatch_job_and_artifact_match_native_pilot_contract(self):
        proposal = json.loads((SOURCE.parents[4] / "docs/engineering-insights/devlake-poc/delivery-flow/proposal.authored.json").read_text())

        def find_pilot(value):
            if isinstance(value, dict):
                if value.get("criterion_id") == "pilot.workflow":
                    return value
                for item in value.values():
                    found = find_pilot(item)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = find_pilot(item)
                    if found:
                        return found
            return None

        native = find_pilot(proposal)
        self.assertEqual(native["required_jobs"], ["qualify"])
        self.assertEqual(native["artifacts"][0]["name"], "devlake-pilot-{run_id}-{run_attempt}")
        workflow = (SOURCE.parents[4] / native["path"]).read_text()
        reusable = (SOURCE.parents[4] / ".github/workflows/_devlake-poc-window.yml").read_text()
        self.assertIn("phase:\n        description: Qualification phase", workflow)
        self.assertIn("options: [pilot]", workflow)
        self.assertIn("launch_manifest: ${{ inputs.launch_manifest }}", workflow)
        self.assertIn("  qualify:\n", workflow)
        self.assertIn("needs: [window-11]", workflow)
        self.assertIn("name: devlake-pilot-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("uses: actions/upload-artifact@v4\n        with:\n          name: devlake-pilot-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("path: ${{ runner.temp }}/summary.json", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("--output \"$RUNNER_TEMP/summary.json\"", workflow)
        self.assertIn("--launch-manifest \"$LAUNCH_MANIFEST\"", workflow)
        self.assertIn("--launch-manifest \"$LAUNCH_MANIFEST\"", reusable)
        self.assertNotIn("--campaign-id", workflow + reusable)


class SourceWorkflowReadbackTests(unittest.TestCase):
    def setUp(self):
        PilotEvidenceTests.setUp(self)
        import io
        import zipfile
        source_spec = importlib.util.spec_from_file_location("source_reader", SOURCE.with_name("source_reader.py"))
        source_reader = importlib.util.module_from_spec(source_spec)
        source_spec.loader.exec_module(source_reader)

        self.reader_module = source_reader
        self.source_run = 200
        self.source_attempt = 1
        self.bindings = {}
        self.by_artifact = {}
        for index, row in enumerate(self.rows):
            receipt = json.loads(self.readbacks[row["criterion_id"]])
            receipt["observations"] = {"probe": {"measured": 1}}
            self.readbacks[row["criterion_id"]] = json.dumps(receipt, sort_keys=True).encode()
            self.manifest["criteria"][row["criterion_id"]]["sha256"] = VERIFY.hashlib.sha256(self.readbacks[row["criterion_id"]]).hexdigest()
            criterion_id = row["criterion_id"]
            artifact_id = str(1000 + index)
            self.manifest["criteria"][criterion_id]["ref"] = artifact_id
            self.by_artifact[artifact_id] = self.readbacks[criterion_id]
            self.bindings[criterion_id] = {"workflow": ".github/workflows/approved-source.yml",
                                           "job": "live-observe", "artifact": f"{criterion_id}-{{run_id}}-{{run_attempt}}"}
        self.native = {**self.pins, "observer": "protected-native-source", "evidence_ref": "protected-native-readback",
                       "unresolved_operations": [], "authority_snapshot_valid": True,
                       "requirements_snapshot_matches": True, "human_interventions": 0,
                       "live_profile_contract_verified": True,
                       "unattended": {"resume_qualified": True, "repair_qualified": True},
                       "observations": {"authority": {"measured": 1}}}
        self.selected = {field: self.pins[field] for field in ("source_revision", "definition_revision",
                                                                "run_id", "run_attempt")}
        self.selected["provider_selection"], self.native["provider_evidence"] = provider_fixture(
            self.pins["source_revision"], self.pins["run_id"], self.pins["run_attempt"])
        from modules.insights.poc.devlake.provider_evidence import selection_digest, from_measurement
        self.manifest["provider_selection"] = self.selected["provider_selection"]
        measurement = {"scope": "live-source-measurement", "source_revision": self.pins["source_revision"],
                       "provider_selection_digest": selection_digest(self.selected["provider_selection"],
                                                                       self.pins["source_revision"]),
                       "observations": {provider: {"revision": current, "response_sha256": "a" * 64,
                                                      "binding_sha256": "b" * 64}
                                        for provider, current in self.selected["provider_selection"]["providers"].items()}}
        measurement["observations"].update({"github": {"response_sha256": "a" * 64},
                                             "jira": {"response_sha256": "b" * 64},
                                             "profile": {"revision": 5, "response_sha256": "c" * 64}})
        provider_raw = json.dumps(measurement, sort_keys=True).encode()
        self.manifest["provider_receipt"] = {"ref": "2044", "sha256": VERIFY.hashlib.sha256(provider_raw).hexdigest()}
        self.by_artifact["2044"] = provider_raw
        self.bindings["providers"] = {"workflow": ".github/workflows/devlake-poc-provider-profile.yml",
                                      "job": "observe", "artifact": "devlake-provider-profile-{run_id}-{run_attempt}"}
        self.native["provider_evidence"] = from_measurement(
            provider_raw, "2044", self.selected["provider_selection"],
            self.pins["source_revision"], self.pins["run_id"], self.pins["run_attempt"])
        native_raw = json.dumps(self.native, sort_keys=True).encode()
        self.manifest["native_receipt"] = {"ref": "2042", "sha256": VERIFY.hashlib.sha256(native_raw).hexdigest()}
        self.by_artifact["2042"] = native_raw
        self.bindings["native"] = {"workflow": ".github/workflows/approved-source.yml",
                                   "job": "live-observe", "artifact": "native-{run_id}-{run_attempt}"}
        self.artifact_name = "devlake-pilot-123-2"
        self.zipfile = zipfile
        self.io = io

    def github(self, path):
        prefix = "repos/aws-e/adp/actions/"
        self.assertTrue(path.startswith(prefix))
        item = path.removeprefix(prefix)
        if item.startswith("artifacts/"):
            artifact_id = item.split("/")[1]
            if item.endswith("/zip"):
                archive = self.io.BytesIO()
                with self.zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr("observation.json", self.by_artifact[artifact_id])
                return archive.getvalue()
            criterion_id = next((key for key, spec in self.manifest["criteria"].items()
                                 if spec["ref"] == artifact_id),
                                "providers" if artifact_id == "2044" else "native")
            run_id = 202 if criterion_id == "providers" else 200
            name = ("devlake-provider-profile-202-1" if criterion_id == "providers" else
                    f"{criterion_id}-200-1")
            metadata = {"name": name, "expired": False,
                        "workflow_run": {"id": run_id, "head_sha": self.pins["source_revision"]}}
        elif item in ("runs/200", "runs/202"):
            run_id = 202 if item == "runs/202" else 200
            workflow = "devlake-poc-provider-profile.yml" if run_id == 202 else "approved-source.yml"
            metadata = {"id": run_id, "head_sha": self.pins["source_revision"],
                        "conclusion": "success", "event": "workflow_dispatch", "run_attempt": 1,
                        "path": f".github/workflows/{workflow}@refs/heads/main"}
        elif item in ("runs/200/jobs?per_page=100", "runs/202/jobs?per_page=100"):
            run_id = 202 if item.startswith("runs/202") else 200
            metadata = {"total_count": 1, "jobs": [{"name": "observe" if run_id == 202 else "live-observe",
                                                  "conclusion": "success", "run_id": run_id,
                                                  "runner_name": "protected-runner"}]}
        else:
            self.fail(f"unexpected source path: {path}")
        return json.dumps(metadata).encode()

    def test_bound_source_artifacts_match_native_envelope_in_synthetic_contract(self):
        reader = self.reader_module.approved_reader(self.manifest, self.plan, self.bindings, self.github)
        result = VERIFY.live_pilot_document(self.manifest, self.plan, reader, self.selected, self.artifact_name)
        self.assertEqual((result["independent_verification"], result["required_passed"]), ("confirmed", 46))
        self.assertEqual(result["gateway_revision"], result["source_revision"])
        self.assertNotIn("evidence_ref", result["criteria"][self.rows[0]["criterion_id"]])

    def test_missing_and_stale_seven_provider_readback_reject_original_pass(self):
        import copy

        original = copy.deepcopy(self.native)
        reader = self.by_artifact.__getitem__
        for changed in (None, {"binding_revision": 2}, {"source_generation": 2}):
            with self.subTest(changed=changed):
                self.native = copy.deepcopy(original)
                if changed is None:
                    self.native["provider_evidence"].pop("slack")
                else:
                    self.native["provider_evidence"]["slack"]["revision"].update(changed)
                raw = json.dumps(self.native, sort_keys=True).encode()
                self.by_artifact["2042"] = raw
                self.manifest["native_receipt"]["sha256"] = VERIFY.hashlib.sha256(raw).hexdigest()
                with self.assertRaisesRegex(ValueError, "native provider proof differs|stale"):
                    VERIFY.live_pilot_document(self.manifest, self.plan, reader, self.selected, self.artifact_name)
        self.native = original
        raw = json.dumps(original, sort_keys=True).encode()
        self.by_artifact["2042"] = raw
        self.manifest["native_receipt"]["sha256"] = VERIFY.hashlib.sha256(raw).hexdigest()
        summary = VERIFY.live_pilot_document(self.manifest, self.plan, reader, self.selected, self.artifact_name)
        self.assertEqual(summary["required_passed"], 46)
        self.assertTrue(summary["provider_evidence_verified"])
        self.assertNotIn("connection-", json.dumps(summary))

    def test_independent_provider_receipt_cannot_be_missing_or_stale(self):
        import copy

        original = copy.deepcopy(self.manifest["provider_receipt"])
        measurement = json.loads(self.by_artifact["2044"])
        for change in ("missing", "digest", "revision", "binding", "selection"):
            with self.subTest(change=change):
                altered = copy.deepcopy(measurement)
                if change == "missing":
                    self.manifest.pop("provider_receipt")
                elif change == "digest":
                    self.manifest["provider_receipt"]["sha256"] = "0" * 64
                else:
                    if change == "revision":
                        altered["observations"]["slack"]["revision"]["binding_revision"] += 1
                    elif change == "binding":
                        altered["observations"].pop("slack")
                    else:
                        altered["provider_selection_digest"] = "0" * 64
                    raw = json.dumps(altered, sort_keys=True).encode()
                    self.by_artifact["2044"] = raw
                    self.manifest["provider_receipt"] = {"ref": "2044", "sha256": VERIFY.hashlib.sha256(raw).hexdigest()}
                with self.assertRaisesRegex(ValueError, "provider readback|provider measurement|measurement is missing"):
                    VERIFY.live_pilot_document(self.manifest, self.plan, self.by_artifact.__getitem__,
                                               self.selected, self.artifact_name)
                self.manifest["provider_receipt"] = copy.deepcopy(original)
                self.by_artifact["2044"] = json.dumps(measurement, sort_keys=True).encode()

    def test_missing_binding_or_wrong_artifact_and_provenance_refuse(self):
        first = self.rows[0]["criterion_id"]
        missing = dict(self.bindings)
        missing.pop(first)
        with self.assertRaisesRegex(ValueError, "inventory is incomplete"):
            self.reader_module.approved_reader(self.manifest, self.plan, missing, self.github)
        substituted = dict(self.bindings)
        substituted["providers"] = {**substituted["providers"], "workflow": ".github/workflows/approved-source.yml"}
        with self.assertRaisesRegex(ValueError, "independent provider/Profile source"):
            self.reader_module.approved_reader(self.manifest, self.plan, substituted, self.github)
        reader = self.reader_module.approved_reader(self.manifest, self.plan, self.bindings, self.github)
        with self.assertRaisesRegex(ValueError, "artifact name"):
            VERIFY.live_pilot_document(self.manifest, self.plan, reader, self.selected, "devlake-pilot-123-3")
        for field in ("run_id", "source_revision", "definition_revision", "run_attempt"):
            with self.subTest(field=field):
                changed = dict(self.selected)
                changed[field] = "999" if field in ("run_id", "run_attempt") else "d" * 40
                with self.assertRaisesRegex(ValueError, field):
                    VERIFY.live_pilot_document(self.manifest, self.plan, reader, changed, f"devlake-pilot-{changed['run_id']}-{changed['run_attempt']}")
        self.bindings[first]["artifact"] = "wrong-{run_id}-{run_attempt}"
        reader = self.reader_module.approved_reader(self.manifest, self.plan, self.bindings, self.github)
        with self.assertRaisesRegex(ValueError, "approved bindings"):
            reader(self.manifest["criteria"][first]["ref"])

    def test_private_fixture_without_github_context_never_writes_artifact(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            output = Path(directory) / "summary.json"
            manifest.write_text(json.dumps(self.manifest))
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
                result = subprocess.run([sys.executable, str(SOURCE), "--phase", "pilot",
                                         "--manifest", str(manifest), "--plan", str(SOURCE.parents[4] / "docs/engineering-insights/devlake-poc/delivery-flow/acceptance-mapping.json"),
                                         "--output", str(output)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("protected GitHub Actions context", result.stderr)
            self.assertFalse(output.exists())

    def test_cli_writes_sanitized_artifact_only_after_bound_sources(self):
        import os
        from unittest.mock import patch

        original = self.reader_module.approved_reader
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            plan_path = Path(directory) / "plan.json"
            output_path = Path(directory) / "summary.json"
            manifest_path.write_text(json.dumps(self.manifest))
            plan_path.write_text(json.dumps(self.plan))
            environ = {"GITHUB_ACTIONS": "true", "GITHUB_SHA": self.pins["source_revision"],
                       "ADP_DEVLAKE_WORKFLOW_SHA": self.pins["definition_revision"],
                       "GITHUB_RUN_ID": self.pins["run_id"], "GITHUB_RUN_ATTEMPT": self.pins["run_attempt"],
                       "ADP_DEVLAKE_SOURCE_BINDINGS": json.dumps(self.bindings),
                       "ADP_DEVLAKE_PROVIDER_SELECTION": json.dumps(self.selected["provider_selection"])}
            with patch.dict(os.environ, environ), patch.dict(sys.modules, {"source_reader": self.reader_module}), \
                    patch.object(self.reader_module, "approved_reader",
                                 side_effect=lambda manifest, plan, bindings: original(manifest, plan, bindings, self.github)), \
                    patch("sys.stdout", self.io.StringIO()):
                self.assertEqual(VERIFY.main(["--phase", "pilot", "--manifest", str(manifest_path),
                                              "--plan", str(plan_path), "--output", str(output_path)]), 0)
            summary = json.loads(output_path.read_text())
            self.assertEqual(summary["required_passed"], 46)
            self.assertNotIn("evidence_ref", summary["criteria"][self.rows[0]["criterion_id"]])
            import jsonschema
            schema = json.loads(SOURCE.with_name("campaign.schema.json").read_text())
            jsonschema.Draft202012Validator({"$defs": schema["$defs"],
                                              "$ref": "#/$defs/pilot_artifact"}).validate(summary)

    def test_cli_refuses_wrong_source_provenance_without_writing(self):
        first = self.rows[0]["criterion_id"]
        artifact_id = self.manifest["criteria"][first]["ref"]
        original = self.github

        def changed(path):
            result = original(path)
            if path.endswith(f"/artifacts/{artifact_id}"):
                metadata = json.loads(result)
                metadata["workflow_run"]["head_sha"] = "wrong"
                return json.dumps(metadata).encode()
            return result

        reader = self.reader_module.approved_reader(self.manifest, self.plan, self.bindings, changed)
        with self.assertRaisesRegex(ValueError, "different release"):
            VERIFY.live_pilot_document(self.manifest, self.plan, reader, self.selected, self.artifact_name)


class ProviderProfileSourceTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        import io
        self.patch = patch
        self.io = io
        source = Path(__file__).resolve().parents[2] / "poc" / "devlake" / "provider_profile_source.py"
        spec = importlib.util.spec_from_file_location("provider_profile_source", source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.config = {
            "source_revision": "a" * 40,
            "allowed_hosts": ["api.github.com", "jira.invalid", "gateway.invalid"],
            "providers": {
                "github": {"url": "https://api.github.com/repos/example/repo", "authorization": "Bearer private-provider",
                           "native_id": "35"},
                "jira": {"url": "https://jira.invalid/rest/api/3/project/example", "authorization": "Bearer private-provider",
                         "native_id": "12"},
            },
            "gateway": {"profile_url": "https://gateway.invalid/api/v1/products/p/projects/q",
                        "bindings_url": "https://gateway.invalid/api/v1/products/p/projects/q/bindings",
                        "authorization": "Bearer private-gateway", "product_id": "p", "project_id": "q"},
        }
        self.responses = {
            self.config["providers"]["github"]["url"]: {"id": 35, "name": "repo"},
            self.config["providers"]["jira"]["url"]: {"id": 12, "name": "Project"},
        }
        refs = {}
        proofs = []
        bindings = []
        for provider, kind, field, native_id in (("github", "repository", "repositories", "35"),
                                                 ("jira", "tracker", "trackers", "12")):
            ref = {"record_type": kind, "record_id": native_id, "project_id": "q"}
            refs[field] = [ref]
            proofs.append({"paths": ["/configuration/basic/" + field], "source_refs": [ref],
                           "state": "accepted", "method": "connector", "coverage": "complete",
                           "binding_id": provider + "-binding"})
            bindings.append({"id": provider + "-binding", "project_id": "q", "generation": 1,
                             "revision": 1, "status": "healthy",
                             "selection": {"provider": provider, "kind": kind, "native_id": native_id}})
        self.responses[self.config["gateway"]["profile_url"]] = {
            "product_id": "p", "project_id": "q", "revision": 5,
            "configuration": {"basic": refs}, "provenance": proofs}
        self.responses[self.config["gateway"]["bindings_url"]] = {"items": bindings, "next_cursor": None}
        self.calls = []

    def read(self, url, authorization, hosts, **kwargs):
        self.calls.append(url)
        return self.responses[url], "f" * 64

    def expanded(self):
        selection, _ = provider_fixture("a" * 40, "123", "2")
        examples = {
            "sonarqube": {"component": {"key": "native-sonarqube", "name": "Quality"}},
            "jenkins": {"fullName": "native-jenkins", "name": "native-jenkins", "_class": "hudson.model.FreeStyleProject"},
            "argocd": {"metadata": {"name": "native-argocd", "namespace": "default"},
                       "spec": {"project": "sample"}},
            "gitlab": {"id": "native-gitlab", "name": "Project", "path_with_namespace": "team/project", "namespace": {"id": "12"}},
            "pagerduty": {"service": {"id": "native-pagerduty", "name": "On call"}},
            "linear": {"data": {"team": {"id": "native-linear", "name": "Team"}}},
            "slack": {"ok": True, "channel": {"id": "native-slack", "name": "Updates",
                                                "team_id": "T123"}},
        }
        namespaces = {"argocd": "sample/default", "gitlab": "12", "slack": "T123"}
        from provider_profile_source import DESTINATIONS
        import hashlib
        for provider, current in selection["providers"].items():
            section, field, kind = DESTINATIONS[provider]
            current.update(project_id="q", record_type=kind)
            namespace = namespaces.get(provider, "/")
            url = f"https://gateway.invalid/source/{provider}"
            self.config["providers"][provider] = {"url": url, "authorization": "Bearer private-provider",
                                                   "native_id": current["record_id"], "namespace": namespace}
            self.responses[url] = examples[provider]
            native_id, name = self.module.upstream_identity(provider, examples[provider])
            fields = [kind, namespace, str(native_id), name]
            if provider in ("jenkins", "gitlab"):
                fields.append(examples[provider]["fullName" if provider == "jenkins" else "path_with_namespace"])
            current["record_revision"] = "observation-sha256:" + hashlib.sha256("\x00".join(fields).encode()).hexdigest()
            binding_revision = "observation-sha256:" + hashlib.sha256(
                json.dumps(examples[provider], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            ref = {"tenant_id": current["tenant_id"], "project_id": "q",
                   "source_connection_id": current["connection_id"], "record_type": kind,
                   "record_id": current["record_id"], "record_revision": current["record_revision"]}
            profile = self.responses[self.config["gateway"]["profile_url"]]
            profile["configuration"].setdefault(section, {}).setdefault(field, []).append(ref)
            profile["provenance"].append({"paths": [f"/configuration/{section}/{field}"],
                                          "source_refs": [ref], "state": "accepted", "method": "connector",
                                          "coverage": "complete", "binding_id": current["binding_id"]})
            self.responses[self.config["gateway"]["bindings_url"]]["items"].append(
                {"id": current["binding_id"], "project_id": "q", "generation": 1, "revision": 1,
                 "status": "healthy", "source_revision": binding_revision,
                 "selection": {"provider": provider, "kind": kind, "namespace": namespace,
                               "native_id": current["record_id"]}})
        return selection

    def test_binding_and_profile_revisions_are_checked_against_their_own_current_reads(self):
        selection = self.expanded()
        listing = self.responses[self.config["gateway"]["bindings_url"]]["items"]
        for provider, current in selection["providers"].items():
            binding = next(item for item in listing if item["id"] == current["binding_id"])
            self.assertNotEqual(binding["source_revision"], current["record_revision"])
            with self.subTest(provider=provider, stale="binding"):
                with self.patch.dict(binding, {"source_revision": current["record_revision"]}):
                    with self.assertRaisesRegex(ValueError, "readbacks differ"):
                        self.module.collect(self.config, "a" * 40, self.read, selection)
            with self.subTest(provider=provider, stale="profile"):
                section, field, _ = self.module.DESTINATIONS[provider]
                references = self.responses[self.config["gateway"]["profile_url"]]["configuration"][section][field]
                reference = next(item for item in references if item.get("source_connection_id") == current["connection_id"])
                with self.patch.dict(current, {"record_revision": binding["source_revision"]}), \
                        self.patch.dict(reference, {"record_revision": binding["source_revision"]}):
                    with self.assertRaisesRegex(ValueError, "readbacks differ"):
                        self.module.collect(self.config, "a" * 40, self.read, selection)
            upstream = self.responses[self.config["providers"][provider]["url"]]
            with self.subTest(provider=provider, stale="provider"):
                changed = {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob"} if provider == "jenkins" else {"changed": "new native response"}
                with self.patch.dict(upstream, changed):
                    with self.assertRaisesRegex(ValueError, "readbacks differ"):
                        self.module.collect(self.config, "a" * 40, self.read, selection)
        self.module.collect(self.config, "a" * 40, self.read, selection)

    def test_seven_live_sources_and_profile_bindings_match_protected_selection(self):
        selection = self.expanded()
        result = self.module.collect(self.config, "a" * 40, self.read, selection)
        self.assertEqual(set(result["observations"]), set(selection["providers"]) | {"github", "jira", "profile"})
        self.assertEqual(len(self.calls), 11)
        self.assertNotIn("private", json.dumps(result))
        from modules.insights.poc.devlake.provider_evidence import selection_digest
        self.assertEqual(result["provider_selection_digest"], selection_digest(selection, "a" * 40))
        for provider in ("gitlab", "argocd", "slack"):
            with self.subTest(provider=provider):
                source = self.config["providers"][provider]
                original = source["namespace"]
                source["namespace"] = "foreign"
                with self.assertRaisesRegex(ValueError, "namespace|readbacks differ"):
                    self.module.collect(self.config, "a" * 40, self.read, selection)
                source["namespace"] = original
        selection["providers"]["linear"]["binding_revision"] = 2
        with self.assertRaisesRegex(ValueError, "readbacks differ"):
            self.module.collect(self.config, "a" * 40, self.read, selection)

    def test_protected_selection_is_required_before_source_artifact_is_written(self):
        import os
        import tempfile

        selection = self.expanded()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            config = Path(directory) / "config.json"
            output = Path(directory) / "observation.json"
            manifest.write_text(json.dumps({"source_revision": "a" * 40, "provider_selection": selection}))
            config.write_text(json.dumps(self.config))
            manifest.chmod(0o600)
            config.chmod(0o600)
            with self.patch.object(self.module, "read_json", side_effect=self.read):
                with self.patch.dict(os.environ, {"ADP_DEVLAKE_PROVIDER_SELECTION": json.dumps(selection)}):
                    self.assertEqual(self.module.main(["--config", str(config), "--manifest", str(manifest),
                                                       "--revision", "a" * 40, "--require-providers",
                                                       "--output", str(output)]), 0)
                self.assertIn("provider_selection_digest", json.loads(output.read_text()))
                output.unlink()
                with self.patch.dict(os.environ, {"ADP_DEVLAKE_PROVIDER_SELECTION": "{}"}), \
                        self.patch("sys.stderr", new_callable=self.io.StringIO):
                    self.assertEqual(self.module.main(["--config", str(config), "--manifest", str(manifest),
                                                       "--revision", "a" * 40, "--require-providers",
                                                       "--output", str(output)]), 1)
                self.assertFalse(output.exists())

    def test_two_providers_and_profile_readbacks_produce_only_measurements(self):
        for binding in self.responses[self.config["gateway"]["bindings_url"]]["items"]:
            self.assertNotIn("provider", binding)
        result = self.module.collect(self.config, "a" * 40, self.read)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(set(result["observations"]), {"github", "jira", "profile"})
        self.assertEqual(result["observations"]["profile"]["revision"], 5)
        self.assertNotIn("outcome", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("\"native_id\":", json.dumps(result))

    def test_malformed_and_mismatched_binding_selections_are_rejected(self):
        for binding in self.responses[self.config["gateway"]["bindings_url"]]["items"]:
            selection = binding["selection"]
            missing_provider = {key: value for key, value in selection.items() if key != "provider"}
            for invalid in (None, [], "invalid", {}, missing_provider,
                            {**selection, "provider": "other"},
                            {**selection, "kind": "other"},
                            {**selection, "native_id": "other"}):
                with self.subTest(provider=selection["provider"], selection=invalid):
                    with self.patch.dict(binding, {"provider": selection["provider"], "selection": invalid}):
                        with self.assertRaisesRegex(ValueError, "independent provider, binding and Profile API readbacks differ"):
                            self.module.collect(self.config, "a" * 40, self.read)

    def test_unhealthy_bindings_are_rejected(self):
        for binding in self.responses[self.config["gateway"]["bindings_url"]]["items"]:
            for status in (None, "degraded", "restricted", "revoked", "unlinked"):
                with self.subTest(provider=binding["selection"]["provider"], status=status):
                    with self.patch.dict(binding, {"status": status}):
                        with self.assertRaisesRegex(ValueError, "independent provider, binding and Profile API readbacks differ"):
                            self.module.collect(self.config, "a" * 40, self.read)

    def test_missing_and_mismatched_sources_never_produce_measurements(self):
        import copy
        for mutation in (lambda config, responses: config["providers"].pop("jira"),
                         lambda config, responses: responses.pop(config["providers"]["jira"]["url"]),
                         lambda config, responses: responses[config["providers"]["github"]["url"]].update(id=99),
                         lambda config, responses: responses.pop(config["gateway"]["profile_url"]),
                         lambda config, responses: responses[config["gateway"]["profile_url"]].update(project_id="other"),
                         lambda config, responses: responses[config["gateway"]["profile_url"]]["provenance"].clear(),
                         lambda config, responses: responses[config["gateway"]["bindings_url"]].update(items=[])):
            config, responses = copy.deepcopy(self.config), copy.deepcopy(self.responses)
            mutation(config, responses)
            def optional_read(url, authorization, hosts):
                if url not in responses:
                    raise ValueError("live source is unavailable")
                return responses[url], "f" * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.module.collect(config, "a" * 40, optional_read)
        with self.assertRaisesRegex(ValueError, "source revision"):
            self.module.collect(self.config, "b" * 40, self.read)

    def test_https_host_and_redirect_guard_precede_readback(self):
        with self.assertRaisesRegex(ValueError, "approved HTTPS"):
            self.module.read_json("http://api.github.com/anything", "Bearer private", ["api.github.com"])
        with self.assertRaisesRegex(ValueError, "approved HTTPS"):
            self.module.read_json("https://other.invalid/anything", "Bearer private", ["api.github.com"])
        with self.assertRaisesRegex(ValueError, "redirected"):
            self.module.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_command_rejects_missing_private_source_before_writing_artifact(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            config = Path(directory) / "config.json"
            output = Path(directory) / "observation.json"
            manifest.write_text(json.dumps({"source_revision": "a" * 40}))
            config.write_text(json.dumps(self.config))
            manifest.chmod(0o600)
            config.chmod(0o600)
            with self.patch.object(self.module, "read_json", side_effect=ValueError("live source is unavailable")) as mocked:
                with self.patch("sys.stderr", new_callable=self.io.StringIO) as stderr:
                    result = self.module.main(["--config", str(config), "--manifest", str(manifest),
                                               "--revision", "a" * 40, "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("live source is unavailable", stderr.getvalue())
            self.assertFalse(output.exists())
            mocked.assert_called_once()

    def test_request_uses_no_redirect_and_approved_authorization(self):
        from unittest.mock import Mock
        response = self.io.BytesIO(b'{"id":35,"name":"repo"}')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        with self.patch.object(self.module, "build_opener", return_value=opener) as factory:
            document, digest = self.module.read_json(
                self.config["providers"]["github"]["url"], "Bearer private-provider", ["api.github.com"])
        self.assertEqual(document["id"], 35)
        self.assertEqual(len(digest), 64)
        self.assertIsInstance(factory.call_args.args[0], self.module.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-provider")
        with self.assertRaisesRegex(ValueError, "source access"):
            self.module.read_json("https://api.github.com/anything", "", ["api.github.com"])

    def test_linear_measurement_uses_native_api_key_header(self):
        from unittest.mock import Mock
        response = self.io.BytesIO(b'{"data":{"team":{"id":"team-one","name":"Engineering"}}}')
        response.status = 200
        opener = Mock()
        opener.open.return_value = response
        body = {"query": 'query { team(id: "team-one") { id name } }'}
        with self.patch.object(self.module, "build_opener", return_value=opener):
            self.module.read_json("https://api.linear.app/graphql", "API-KEY fixture-only", ["api.linear.app"], body=body)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "fixture-only")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), body)
        with self.assertRaisesRegex(ValueError, "GraphQL"):
            self.module.read_json("https://api.linear.app/graphql", "API-KEY fixture-only", ["api.linear.app"])

    def test_workflow_uploads_only_successful_private_measurement(self):
        workflow = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "devlake-poc-provider-profile.yml"
        contents = workflow.read_text()
        self.assertIn("environment: adp-devlake-campaign", contents)
        self.assertIn("secrets.ADP_DEVLAKE_PROBE_CONFIG", contents)
        self.assertIn("secrets.ADP_DEVLAKE_PROVIDER_SELECTION", contents)
        self.assertIn("--require-providers", contents)
        self.assertIn("--revision \"$GITHUB_SHA\"", contents)
        self.assertIn("devlake-provider-profile-${{ github.run_id }}-${{ github.run_attempt }}", contents)
        self.assertIn("uses: ./.github/actions/upload-ci-evidence-s3", contents)
        self.assertNotIn("actions/upload-artifact@", contents)
        self.assertIn("if-no-files-found: error", contents)

    def test_command_writes_bounded_private_measurement_not_criterion_receipt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            config = Path(directory) / "config.json"
            output = Path(directory) / "observation.json"
            manifest.write_text(json.dumps({"source_revision": "a" * 40}))
            config.write_text(json.dumps(self.config))
            manifest.chmod(0o600)
            config.chmod(0o600)
            def local_read(url, authorization, hosts):
                self.assertIn(url.split("/")[2], hosts)
                self.assertTrue(authorization.startswith("Bearer "))
                return self.responses[url], "f" * 64
            with self.patch.object(self.module, "read_json", side_effect=local_read) as mocked:
                self.assertEqual(self.module.main(["--config", str(config), "--manifest", str(manifest),
                                                   "--revision", "a" * 40, "--output", str(output)]), 0)
            self.assertEqual(mocked.call_count, 4)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            result = json.loads(output.read_text())
            self.assertEqual(result["scope"], "live-source-measurement")
            self.assertNotIn("criterion_id", result)
            self.assertNotIn("outcome", result)
            self.assertNotIn("private", output.read_text())


class InfrastructureSourceTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.patch = patch
        source = SOURCE.with_name("infrastructure_source.py")
        sys.path.insert(0, str(source.parent))
        self.addCleanup(lambda: sys.path.remove(str(source.parent)))
        spec = importlib.util.spec_from_file_location("infrastructure_source", source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.revision = "a" * 40
        self.account = "123456789012"
        self.scope = {"account": self.account, "role": "observer-role", "label": "private-label",
                      "region": "us-east-1", "resource_id": "db-TESTID"}
        self.iam = {**self.scope, "worker_role": "worker-role", "writer_username": "writer",
                    "forbidden_username": "unselected"}
        self.database = {**self.scope, "host": "test.rds.amazonaws.com", "database": "db",
                         "cell": "cell", "username": "reader", "ca_path": "/private/ca.pem"}
        self.config = {"source_revision": self.revision, "iam": self.iam, "database": self.database,
                       "lineage": {**self.scope, "bucket": "private-test", "tenant_id": "example-tenant",
                                   "project_id": "example-project", "dataset_version": "example-dataset-1",
                                   **{kind + "_version": kind + "-version" for kind in
                                      ("pointer", "snapshot", "policy", "quality")}}}

    def test_iam_simulation_requires_exact_role_and_unselected_user_denial(self):
        writer = f"arn:aws:rds-db:us-east-1:{self.account}:dbuser:db-TESTID/writer"
        denied = writer.replace("/writer", "/unselected")
        def aws(label, service, operation, *args):
            self.assertEqual((service, operation), ("iam", "simulate-principal-policy"))
            self.assertEqual(args[-2:], (writer, denied))
            return {"EvaluationResults": [{"EvalActionName": "rds-db:connect", "EvalResourceName": resource,
                                           "EvalDecision": verdict} for resource, verdict in
                                          ((writer, "allowed"), (denied, decision))]}
        with self.patch.object(self.module, "selected_role") as identity:
            for decision in ("implicitDeny", "explicitDeny"):
                self.assertEqual(self.module.iam_policy(self.iam, aws)["scope"], "policy-simulation-only")
            decision = "allowed"
            with self.assertRaisesRegex(ValueError, "did not deny"):
                self.module.iam_policy(self.iam, aws)
            identity.assert_called()
        with self.assertRaisesRegex(ValueError, "exact role"):
            self.module.selected_role(self.account, "observer-role", "private-label",
                                      lambda *args: json.dumps({"Account": self.account,
                                          "Arn": f"arn:aws:sts::{self.account}:assumed-role/other/fixture"}))

    def test_versioned_lineage_validates_real_dependency_closure_and_missing_objects(self):
        from modules.insights.tests.test_results_read import fixture, shared_policy
        import hashlib
        snapshot = fixture()
        policy = shared_policy(snapshot)
        quality = {"dataset_version": snapshot["dataset_version"], "coverage": "complete",
                   "cost_basis": "actual_charge", "source_scope": "source-policy",
                   "evidence_scope": "evidence-policy"}
        digest = lambda document: hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        pointer = {"schema_version": "results-publication/1.0", "dataset_version": snapshot["dataset_version"],
                   "snapshot_sha256": digest(snapshot), "policy_sha256": digest(policy),
                   "quality_sha256": digest(quality)}
        objects = {"published.json": pointer, "/snapshots/": snapshot,
                   "/policies/": policy, "/quality/": quality}
        def read(config, key, version, limit):
            self.assertTrue(version.endswith("-version"))
            document = next((value for suffix, value in objects.items() if suffix in key), None)
            if document is None:
                raise ValueError("lineage object missing")
            return document, digest(document)
        with self.patch.object(self.module, "selected_role"):
            observed = self.module.lineage(self.config["lineage"], read)
            self.assertGreater(observed["dependency_count"], 0)
            del objects["/policies/"]
            with self.assertRaisesRegex(ValueError, "lineage object missing"):
                self.module.lineage(self.config["lineage"], read)
            objects["/policies/"] = {**policy, "envelopes": []}
            with self.assertRaisesRegex(ValueError, "digest differs"):
                self.module.lineage(self.config["lineage"], read)
        with self.assertRaisesRegex(ValueError, "versioned lineage"):
            self.module.lineage({"bucket": "private-test"}, read)

    def test_cleanup_is_read_only_and_rejects_missing_or_incomplete_terminal_state(self):
        artifact = {"state": "cleaned", "receipt": {"outcome": "absent"}, "digest": "sha256:" + "b" * 64}
        record = {"finalizer_revision": self.revision, "finalization_requested": {"reason": "failure"},
                  "finalization_complete": {"at": "2026-11-01T00:00:00Z"}, "artifacts": {"api": artifact}}
        def read(role, allow_expired=False):
            self.assertEqual((role, allow_expired), ("finalizer", True))
            return (("devlake-" + self.revision, "table", "123456789012", "us-east-1"), None, record)
        self.assertEqual(self.module.finalized_cleanup(self.revision, read, lambda *args: None),
                         {"scope": "registered-artifacts-only", "states": {"api": "absent"},
                          "pilot_id": "devlake-" + self.revision,
                          "account_id": "123456789012", "region": "us-east-1"})
        for change in (lambda item: item.pop("finalization_complete"),
                       lambda item: item.update(optional_resources={"worker": "pending"}),
                       lambda item: item["artifacts"]["api"].update(state="accepted")):
            import copy
            record = copy.deepcopy(record)
            change(record)
            with self.assertRaisesRegex(ValueError, "cleanup|artifact"):
                self.module.finalized_cleanup(self.revision, read, lambda *args: "sha256:" + "b" * 64)

    def test_active_phase_requires_every_source_and_private_output(self):
        with self.patch.object(self.module, "iam_policy", return_value={"scope": "policy-simulation-only"}), \
                self.patch.object(self.module.migration_receipt, "verify_migrated", return_value="f" * 64), \
                self.patch.object(self.module, "lineage", return_value={"dependency_count": 2}), \
                self.patch.object(self.module, "finalized_cleanup", return_value={"states": {"api": "absent"}}):
            active = self.module.collect(self.config, self.revision, "active")
            self.assertEqual(set(active["observations"]), {"iam", "database", "lineage"})
            self.assertNotIn("outcome", active)
            self.assertEqual(set(self.module.collect(self.config, self.revision, "finalized")["observations"]),
                             {"cleanup"})
            import copy
            absent = copy.deepcopy(self.config)
            absent.pop("database")
            with self.assertRaisesRegex(ValueError, "live database"):
                self.module.collect(absent, self.revision, "active")
            absent = copy.deepcopy(self.config)
            absent["iam"]["resource_id"] = "db-OTHER"
            with self.assertRaisesRegex(ValueError, "pins differ"):
                self.module.collect(absent, self.revision, "active")
            with self.assertRaisesRegex(ValueError, "source revision"):
                self.module.collect(self.config, "b" * 40, "active")

    def test_workflow_uses_separate_protected_phases_and_attempt_artifact(self):
        workflow = SOURCE.parents[4] / ".github/workflows/devlake-poc-infrastructure-source.yml"
        text = workflow.read_text()
        self.assertIn("adp-devlake-finalizer", text)
        self.assertIn("adp-devlake-campaign", text)
        self.assertIn("secrets.ADP_DEVLAKE_SOURCE_CONFIG", text)
        self.assertIn("uses: ./.github/actions/upload-ci-evidence-s3\n        if: inputs.phase == 'active'", text)
        self.assertIn("devlake-infrastructure-active-${{ github.run_id }}-${{ github.run_attempt }}", text)
        self.assertIn("uses: actions/upload-artifact@v4\n        if: inputs.phase == 'finalized'", text)
        self.assertIn("devlake-infrastructure-finalized-${{ github.run_id }}-${{ github.run_attempt }}", text)
        self.assertIn("if-no-files-found: error", text)

    def test_versioned_s3_reader_refuses_missing_or_mismatched_objects(self):
        import hashlib
        raw = b'{"dataset_version":"example-dataset-1"}'
        document = json.loads(raw)
        calls = []
        def broker(label, *args):
            self.assertEqual(label, "private-label")
            calls.append(args)
            if args[2] == "head-object":
                return json.dumps({"VersionId": "selected-version", "ContentLength": len(raw)})
            self.assertEqual(args[2], "get-object")
            Path(args[-3]).write_bytes(raw)
            return json.dumps({"VersionId": "selected-version"})
        config = self.config["lineage"]
        self.assertEqual(self.module.s3_document(config, "results/v1/example/a.json", "selected-version",
                                                 4096, broker), (document, hashlib.sha256(raw).hexdigest()))
        self.assertEqual([call[2] for call in calls], ["head-object", "get-object"])
        self.assertTrue(all("--version-id" in call for call in calls))
        with self.assertRaisesRegex(ValueError, "version IDs"):
            self.module.s3_document(config, "results/v1/example/a.json", "latest", 4096, broker)
        with self.assertRaisesRegex(ValueError, "version or size"):
            self.module.s3_document(config, "results/v1/example/a.json", "selected-version", 1, broker)

    def test_cli_refuses_missing_live_sources_before_upload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            config = Path(directory) / "config.json"
            output = Path(directory) / "observation.json"
            manifest.write_text(json.dumps({"source_revision": self.revision}))
            config.write_text(json.dumps(self.config))
            manifest.chmod(0o600)
            config.chmod(0o600)
            with self.patch.object(self.module, "iam_policy", side_effect=ValueError("IAM source denied")):
                with self.patch("sys.stderr", new_callable=__import__("io").StringIO) as stderr:
                    result = self.module.main(["--config", str(config), "--manifest", str(manifest),
                                               "--revision", self.revision, "--phase", "active",
                                               "--output", str(output)])
            self.assertEqual(result, 1)
            self.assertIn("IAM source denied", stderr.getvalue())
            self.assertFalse(output.exists())


class NativeSourceTests(unittest.TestCase):
    def setUp(self):
        import copy
        import sys
        source = SOURCE.with_name("native_source.py")
        sys.path.insert(0, str(source.parent))
        self.addCleanup(lambda: sys.path.remove(str(source.parent)))
        spec = importlib.util.spec_from_file_location("native_source", source)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.copy = copy.deepcopy
        self.flow_id = "00000000-0000-4000-8000-000000000001"
        self.node_id = "00000000-0000-4000-8000-000000000002"
        self.gateway = {"url": "https://gateway.example.invalid", "allowed_hosts": ["gateway.example.invalid"],
                        "authorization": "Bearer example-test-only", "flow_id": self.flow_id,
                        "node_id": self.node_id, "node_cycle": 1, "node_attempts": 2,
                        "plan_version": 3, "plan_hash": "c" * 64, "decision_id": "approved-decision"}
        self.pins = {"run_id": "123", "run_attempt": "2", "source_revision": "a" * 40,
                     "definition_revision": "b" * 40,
                     "component_digests": {component: "c" * 64 for component in VERIFY.COMPONENTS}}

    def test_native_gateway_reads_selected_plan_execution_and_recovery_snapshots(self):
        documents = {
            "/plans": [{"version": 3, "plan_hash": "c" * 64,
                        "accepted_by_decision_id": "approved-decision", "superseded_at": None}],
            "/execution?limit=200&offset=0": {"flow_id": self.flow_id, "legacy": False, "offset": 0,
                                                 "total": 1, "executions": [
                                                     {"node_id": self.node_id, "cycle": 1,
                                                      "action_overflow": False, "actions": [
                                                          {"resolved": True, "receipt_ref": "receipt"}]}]},
            "/recovery?flow_id=" + self.flow_id: {"contract": "node-recovery-v1",
                                                       "flow_id": self.flow_id, "node_id": self.node_id,
                                                       "attempts": 2, "revision": "d" * 64},
        }
        seen = []
        def read(url, authorization, allowed_hosts):
            self.assertEqual((authorization, allowed_hosts),
                             ("Bearer example-test-only", ["gateway.example.invalid"]))
            seen.append(url)
            for suffix, document in documents.items():
                if url.endswith(suffix):
                    return document, "e" * 64
            self.fail("unexpected native gateway endpoint")
        result = self.module.flow_observation(self.gateway, read)
        self.assertEqual((result["observed_action_count"], result["unresolved_action_count"]), (1, 0))
        self.assertEqual(len(seen), 3)
        self.assertNotIn("authority_snapshot_valid", result)
        for key, value, message in (("plan_hash", "f" * 64, "plan"),
                                    ("decision_id", "different", "plan"),
                                    ("node_attempts", 3, "recovery"),
                                    ("node_id", "00000000-0000-4000-8000-000000000003", "recovery")):
            changed = {**self.gateway, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, message):
                self.module.flow_observation(changed, read)
        for suffix in documents:
            removed = {key: value for key, value in documents.items() if key != suffix}
            def missing_read(url, *_args):
                for key, value in removed.items():
                    if url.endswith(key):
                        return value, "e" * 64
                raise ValueError("native gateway read unavailable")
            with self.subTest(missing=suffix), self.assertRaisesRegex(ValueError, "unavailable"):
                self.module.flow_observation(self.gateway, missing_read)

    def test_actions_readback_rejects_changed_run_attempt_or_missing_window(self):
        run = {"id": 123, "run_attempt": 2, "head_sha": "a" * 40, "event": "workflow_dispatch",
               "path": ".github/workflows/devlake-poc-qualify.yml@refs/heads/main", "conclusion": None}
        jobs = {"jobs": [{"name": f"window-{index:02} / observe", "run_id": 123,
                          "conclusion": "success"} for index in range(12)], "total_count": 12}
        def read(path):
            self.assertIn("/runs/123", path)
            return (jobs if "/jobs?" in path else run), "f" * 64
        self.assertEqual(self.module.workflow_observation(self.pins, read)["window_count"], 12)
        for document, key, value in ((run, "run_attempt", 1), (run, "head_sha", "f" * 40),
                                     (jobs, "total_count", 11)):
            document[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "run, attempt or job"):
                self.module.workflow_observation(self.pins, read)
            document[key] = 2 if key == "run_attempt" else "a" * 40 if key == "head_sha" else 12
        jobs["jobs"].pop()
        jobs["total_count"] = 11
        with self.assertRaisesRegex(ValueError, "observation windows"):
            self.module.workflow_observation(self.pins, read)

    def test_missing_checkpoint_or_changed_pins_refuse_native_measurement(self):
        manifest = self.copy(self.pins)
        config = {"source_revision": manifest["source_revision"],
                  "definition_revision": manifest["definition_revision"], "gateway": self.gateway}
        def workflow(_pins):
            return {"window_count": 12}
        def checkpoint(*_args):
            return {"window_count": 12, "scope": "offline-checkpoint-correlated-with-native-readbacks"}
        def flow(_config):
            return {"observed_action_count": 1, "unresolved_action_count": 0}
        result = self.module.collect(manifest, config, "/private", "/private/manifest.json",
                                     workflow, flow, checkpoint)
        self.assertEqual(result["scope"], "live-source-measurement")
        self.assertNotIn("authority_snapshot_valid", json.dumps(result))
        self.assertNotIn("resume_qualified", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "revision pins"):
            self.module.collect(manifest, {**config, "definition_revision": "changed"}, "", "",
                                workflow, flow, checkpoint)
        with self.assertRaisesRegex(ValueError, "checkpoint windows"):
            self.module.collect(manifest, config, "", "", workflow, flow,
                                lambda *_args: {"window_count": 11})
        with self.assertRaisesRegex(ValueError, "checkpoint unavailable"):
            self.module.collect(manifest, config, "", "", workflow, flow,
                                lambda *_args: (_ for _ in ()).throw(ValueError("checkpoint unavailable")))

    def test_checkpoint_readback_is_explicitly_offline(self):
        from unittest.mock import patch
        root = "/private"
        manifest = "/private/campaign/manifest.json"
        with patch.object(self.module.resume_campaign, "campaign_from_manifest", return_value="campaign"), \
                patch.object(self.module.resume_campaign, "qualify_unattended", return_value={"status": "pass"}), \
                patch.object(self.module.resume_campaign, "_read_private", side_effect=[
                    {"batches": {f"window-{index:02}": {} for index in range(12)}}, {"sample_sha256": "f" * 64}]):
            result = self.module.checkpoint_observation(root, manifest, "a" * 40)
        self.assertEqual(result["window_count"], 12)
        self.assertTrue(result["scope"].startswith("offline-"))
        with patch.object(self.module.resume_campaign, "campaign_from_manifest", side_effect=ValueError("no launch")):
            with self.assertRaisesRegex(ValueError, "no launch"):
                self.module.checkpoint_observation(root, manifest, "a" * 40)

    def test_protected_native_job_outputs_measurements_not_pilot_predicates(self):
        workflow = SOURCE.parents[4] / ".github/workflows/devlake-poc-native-source.yml"
        text = workflow.read_text()
        self.assertIn("secrets.ADP_DEVLAKE_NATIVE_SOURCE_CONFIG", text)
        self.assertIn("environment: adp-devlake-campaign", text)
        self.assertIn("devlake-native-readback-${{ github.run_id }}-${{ github.run_attempt }}", text)
        self.assertIn("uses: ./.github/actions/upload-ci-evidence-s3", text)
        self.assertNotIn("actions/upload-artifact@", text)
        self.assertIn("if-no-files-found: error", text)


class GenericDeliveryTests(unittest.TestCase):
    def setUp(self):
        SourceWorkflowReadbackTests.setUp(self)
        sys.path.insert(0, str(SOURCE.parent))
        self.addCleanup(lambda: sys.path.remove(str(SOURCE.parent)))
        import delivery_receipt

        self.delivery = delivery_receipt
        self.target = {"account_id": "123456789012", "region": "us-east-1",
                       "resource_kind": "engineering-poc", "resource_id": "devlake-" + "a" * 40,
                       "component": "devlake-poc"}
        self.environment = {"adp_source_revision": "a" * 40, "adp_definition_revision": "b" * 40,
                            "GITHUB_SHA": "a" * 40, "ADP_DEVLAKE_WORKFLOW_SHA": "b" * 40,
                            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                            "GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_REPOSITORY_ID": "12345",
                            "adp_contract_digest": "d" * 64, "adp_correlation": "sample-operation"}
        self.environment["ADP_DEVLAKE_PROVIDER_SELECTION"] = json.dumps(self.selected["provider_selection"])
        self.cleanup = {"scope": "live-source-measurement", "source_revision": "a" * 40,
                        "phase": "finalized", "observed_at": "2026-11-01T00:00:00Z",
                        "observations": {"cleanup": {
                            "scope": "registered-artifacts-only", "states": {"api": "absent"},
                            "pilot_id": self.target["resource_id"],
                            "account_id": self.target["account_id"], "region": self.target["region"]}}}
        self.cleanup_raw = json.dumps(self.cleanup, sort_keys=True).encode()
        self.manifest["delivery_cleanup_receipt"] = {
            "ref": "2043", "sha256": VERIFY.hashlib.sha256(self.cleanup_raw).hexdigest()}
        self.bindings["delivery_cleanup"] = {
            "workflow": ".github/workflows/devlake-poc-infrastructure-source.yml",
            "job": "observe", "artifact": "devlake-infrastructure-finalized-{run_id}-{run_attempt}"}
        self.by_artifact["2043"] = self.cleanup_raw
        self.summary = VERIFY.live_pilot_document(
            self.manifest, self.plan, self.by_artifact.__getitem__, self.selected, self.artifact_name)

    def receipt(self):
        return self.delivery.receipt(self.summary, self.manifest, self.plan, self.cleanup_raw,
                                     self.target, self.environment)

    def github(self, path):
        if path == "repos/aws-e/adp/actions/artifacts/2043":
            return json.dumps({"name": "devlake-infrastructure-finalized-201-1", "expired": False,
                               "workflow_run": {"id": 201, "head_sha": "a" * 40}}).encode()
        if path == "repos/aws-e/adp/actions/runs/201":
            return json.dumps({"id": 201, "head_sha": "a" * 40, "conclusion": "success",
                               "event": "workflow_dispatch", "run_attempt": 1,
                               "path": ".github/workflows/devlake-poc-infrastructure-source.yml@refs/heads/main"}).encode()
        if path == "repos/aws-e/adp/actions/runs/201/jobs?per_page=100":
            return json.dumps({"total_count": 1, "jobs": [
                {"name": "observe", "conclusion": "success", "run_id": 201,
                 "runner_name": "protected-test-runner"}]}).encode()
        return SourceWorkflowReadbackTests.github(self, path)

    def test_receipt_matches_generic_contract_with_independent_finalizer_source(self):
        proposal = json.loads((SOURCE.parents[4] / "docs/engineering-insights/devlake-poc/delivery-flow/proposal.authored.json").read_text())
        def find_contract(value):
            if isinstance(value, dict):
                if value.get("evidence_artifact") == "devlake-pilot-delivery":
                    return value
                for item in value.values():
                    result = find_contract(item)
                    if result is not None:
                        return result
            elif isinstance(value, list):
                for item in value:
                    result = find_contract(item)
                    if result is not None:
                        return result
            return None
        contract = find_contract(proposal)
        self.assertIsNotNone(contract)
        self.assertTrue(contract["cleanup_required"])
        self.assertEqual(list(self.delivery.CHECKS), contract["required_checks"])
        workflow = (SOURCE.parents[4] / ".github/workflows/devlake-poc-qualify.yml").read_text()
        for name in ("adp_correlation", "adp_source_revision", "adp_definition_revision", "adp_contract_digest"):
            self.assertIn(name + ": ${{ inputs." + name + " }}", workflow)
        self.assertIn("name: devlake-pilot-delivery-${{ github.run_attempt }}", workflow)
        self.assertIn("uses: actions/upload-artifact@v4\n        with:\n          name: devlake-pilot-delivery-${{ github.run_attempt }}", workflow)
        self.assertIn("path: ${{ runner.temp }}/delivery.json", workflow)
        self.assertIn("--summary \"$RUNNER_TEMP/summary.json\"", workflow)
        reader = self.reader_module.approved_reader(self.manifest, self.plan, self.bindings, self.github)
        result = self.delivery.receipt(self.summary, self.manifest, self.plan, reader("2043"),
                                       self.target, self.environment)
        self.assertEqual(result["cleanup"], "passed")
        self.assertEqual([item["id"] for item in result["checks"]], contract["required_checks"])
        self.assertEqual({item["outcome"] for item in result["checks"]}, {"passed"})
        self.assertEqual((result["run_id"], result["run_attempt"], result["actual_revision"]),
                         (123, 2, "a" * 40))
        self.assertEqual(result["resource_id"], self.target["resource_id"])

    def test_delivery_refuses_forged_or_stale_provider_summary(self):
        self.summary["provider_revisions_digest"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "provider evidence"):
            self.receipt()
        self.summary["provider_revisions_digest"] = VERIFY.hashlib.sha256(
            json.dumps(self.selected["provider_selection"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.environment.pop("ADP_DEVLAKE_PROVIDER_SELECTION")
        with self.assertRaises(ValueError):
            self.receipt()

    def test_missing_or_failed_check_and_cleanup_never_publish(self):
        self.manifest.pop("delivery_cleanup_receipt")
        with self.assertRaisesRegex(ValueError, "cleanup receipt"):
            self.receipt()
        self.manifest["delivery_cleanup_receipt"] = {
            "ref": "2043", "sha256": VERIFY.hashlib.sha256(self.cleanup_raw).hexdigest()}
        self.summary["criteria"].pop(next(iter(self.summary["criteria"])))
        with self.assertRaisesRegex(ValueError, "pilot artifact"):
            self.receipt()
        self.summary = VERIFY.live_pilot_document(
            self.manifest, self.plan, self.by_artifact.__getitem__, self.selected, self.artifact_name)
        for changed in ("source_revision", "phase"):
            with self.subTest(changed=changed):
                original = self.cleanup[changed]
                self.cleanup[changed] = "wrong"
                raw = json.dumps(self.cleanup).encode()
                self.manifest["delivery_cleanup_receipt"]["sha256"] = VERIFY.hashlib.sha256(raw).hexdigest()
                with self.assertRaisesRegex(ValueError, "cleanup|identity"):
                    self.delivery.receipt(self.summary, self.manifest, self.plan, raw,
                                          self.target, self.environment)
                self.cleanup[changed] = original
        self.cleanup["observations"]["cleanup"]["states"] = {}
        raw = json.dumps(self.cleanup).encode()
        self.manifest["delivery_cleanup_receipt"]["sha256"] = VERIFY.hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError, "cleanup"):
            self.delivery.receipt(self.summary, self.manifest, self.plan, raw, self.target, self.environment)

    def test_wrong_source_binding_transport_and_target_refuse(self):
        binding = self.bindings["delivery_cleanup"].copy()
        self.bindings["delivery_cleanup"]["artifact"] = "unrelated-{run_id}-{run_attempt}"
        with self.assertRaisesRegex(ValueError, "finalized infrastructure source"):
            self.reader_module.approved_reader(self.manifest, self.plan, self.bindings, self.github)
        self.bindings["delivery_cleanup"] = binding
        for field, value in (("adp_contract_digest", "wrong"), ("GITHUB_RUN_ATTEMPT", "3"),
                             ("adp_definition_revision", "c" * 40)):
            with self.subTest(field=field):
                original = self.environment[field]
                self.environment[field] = value
                with self.assertRaises(ValueError):
                    self.receipt()
                self.environment[field] = original
        self.target["resource_id"] = "wrong-target"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.receipt()

    def test_cli_missing_finalized_source_does_not_write_receipt(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            plan_path = Path(directory) / "plan.json"
            summary_path = Path(directory) / "summary.json"
            output = Path(directory) / "delivery.json"
            for path, content in ((manifest_path, self.manifest), (plan_path, self.plan),
                                  (summary_path, self.summary)):
                path.write_text(json.dumps(content))
            manifest_path.chmod(0o600)
            with patch.dict("os.environ", {**self.environment, "ADP_DEVLAKE_DELIVERY_TARGET": json.dumps(self.target),
                                            "ADP_DEVLAKE_SOURCE_BINDINGS": "{}"}):
                self.assertEqual(self.delivery.main(["--manifest", str(manifest_path), "--plan", str(plan_path),
                                                     "--summary", str(summary_path), "--output", str(output)]), 1)
            self.assertFalse(output.exists())

    def test_cli_upload_input_is_private_and_schema_shaped(self):
        import stat
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            plan_path = Path(directory) / "plan.json"
            summary_path = Path(directory) / "summary.json"
            output = Path(directory) / "delivery.json"
            for path, content in ((manifest_path, self.manifest), (plan_path, self.plan),
                                  (summary_path, self.summary)):
                path.write_text(json.dumps(content))
            manifest_path.chmod(0o600)
            with patch.dict("os.environ", {**self.environment, "ADP_DEVLAKE_DELIVERY_TARGET": json.dumps(self.target),
                                            "ADP_DEVLAKE_SOURCE_BINDINGS": json.dumps(self.bindings)}), \
                    patch.object(self.delivery, "approved_reader", return_value=lambda ref: self.cleanup_raw):
                self.assertEqual(self.delivery.main(["--manifest", str(manifest_path), "--plan", str(plan_path),
                                                     "--summary", str(summary_path), "--output", str(output)]), 0)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            result = json.loads(output.read_text())
            self.assertEqual(set(result), {"schema_version", "contract_digest", "correlation", "repository_id",
                                           "run_id", "run_attempt", "workflow_path", "workflow_revision",
                                           "source_revision", "actual_revision", "account_id", "region",
                                           "resource_kind", "resource_id", "component", "checks", "cleanup",
                                           "produced_at"})
            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["workflow_path"], ".github/workflows/devlake-poc-qualify.yml")
