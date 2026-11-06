"""Synthetic input shape tests; these do not generate live B11 evidence."""

import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts/v13_packet.py"
spec = importlib.util.spec_from_file_location("v13_packet", SCRIPT)
packet_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packet_module)


def synthetic_run():
    source = "a" * 40
    scenario = packet_module.SCENARIOS
    return {
        "plan_revision": "b" * 64, "requirements_revision": source,
        "source_revision": source, "schema_head": "example-schema-head",
        "deployments": {
            kind: {"source_revision": source, "workflow_revision": "c" * 40,
                   "image_digest": "sha256:" + "d" * 64,
                   "receipt": "example-private-" + kind + "-receipt"}
            for kind in ("emitter", "read_service")} | {
            kind: {"source_revision": source, "workflow_revision": "c" * 40,
                   "artifact_digest": "d" * 64,
                   "receipt": "example-private-" + kind + "-receipt"}
            for kind in ("profile", "catalog")},
        "artifacts": {"cli_checksum": "e" * 64,
                      "cli_report": "example-private-cli-report",
                      "api_report": "example-private-api-report",
                      "run_report": "example-private-run-report"},
        "cleanup": {"status": "complete", "receipt": "example-private-cleanup-receipt"},
        "attempts": [
            {"ac": ac, "scenario": selector, "test": "example-v13-selector",
             "run_id": "example-run", "attempt": 1, "action": "example action",
             "expected": "example expected", "actual": "example actual",
             "outcome": "passed", "evidence": {
                 "readback": "example-private-readback", "run_report": "example-private-attempt",
                 **({"cli": [
                     {"case": "authorized", "command": "adp insights context show --json",
                      "stdout": "example authorized output", "stderr": "", "exit_code": 0},
                     {"case": "revoked", "command": "adp insights context show --json",
                      "stdout": "example denied output", "stderr": "", "exit_code": 5},
                 ]} if ac == "AC-04" else {}),
                 **({"quarantine": "example-private-quarantine"} if ac == "AC-03" else {}),
             }} for ac, selector in scenario.items()],
    }


class V13PacketTests(unittest.TestCase):
    def test_content_revision_and_complete_packet_from_supplied_runs(self):
        run = synthetic_run()
        packet = packet_module.prepare_packet(run)
        self.assertEqual(packet["status"], "complete")
        self.assertEqual(packet["human_decision"], "pending")
        self.assertEqual(packet["attempts"], run["attempts"])
        self.assertEqual(packet, packet_module.prepare_packet(run))
        self.assertEqual(len(packet["packet_revision"]), 64)
        with tempfile.TemporaryDirectory(prefix="example-v13-packet-") as directory:
            source = pathlib.Path(directory) / "private-input.json"
            output = pathlib.Path(directory) / "private-packet.json"
            source.write_text(json.dumps(run))
            self.assertEqual(packet_module.main(source, output), 0)
            self.assertEqual(json.loads(output.read_text()), packet)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_retain_failed_attempts_without_reporting_complete(self):
        run = synthetic_run()
        failed = run["attempts"][0]
        failed["outcome"] = "failed"
        packet = packet_module.prepare_packet(run)
        self.assertEqual(packet["status"], "incomplete")
        self.assertEqual(packet["attempts"][0]["outcome"], "failed")
        run["attempts"].append({**copy.deepcopy(failed), "attempt": 2, "outcome": "passed"})
        self.assertEqual(packet_module.prepare_packet(run)["status"], "complete")
        run["cleanup"]["status"] = "failed"
        self.assertEqual(packet_module.prepare_packet(run)["status"], "incomplete")
        with tempfile.TemporaryDirectory(prefix="example-v13-packet-") as directory:
            source = pathlib.Path(directory) / "private-input.json"
            output = pathlib.Path(directory) / "private-packet.json"
            source.write_text(json.dumps(run))
            self.assertEqual(packet_module.main(source, output), 1)
            self.assertEqual(json.loads(output.read_text())["status"], "incomplete")

    def test_missing_or_mismatched_evidence_never_passes(self):
        for change in (
            lambda run: run["attempts"].pop(),
            lambda run: run["deployments"]["read_service"].pop("receipt"),
            lambda run: run["deployments"]["profile"].pop("receipt"),
            lambda run: run["artifacts"].pop("cli_checksum"),
            lambda run: run["cleanup"].pop("receipt"),
            lambda run: run["attempts"][0]["evidence"].pop("readback"),
            lambda run: run["attempts"][0].update(scenario="SCN-99"),
            lambda run: run["attempts"][3]["evidence"].pop("cli"),
            lambda run: run["attempts"][3]["evidence"]["cli"][0].pop("exit_code"),
            lambda run: run.update(source_revision="not-immutable"),
        ):
            with self.subTest(change=change):
                run = synthetic_run()
                change(run)
                if len(run["attempts"]) == 3:
                    self.assertEqual(packet_module.prepare_packet(run)["status"], "incomplete")
                else:
                    with self.assertRaises(ValueError):
                        packet_module.prepare_packet(run)

    def test_input_cannot_claim_human_acceptance_or_override_status(self):
        run = synthetic_run()
        run.update(status="complete", human_decision="accepted", story=9999,
                   packet_revision="f" * 64)
        packet = packet_module.prepare_packet(run)
        self.assertEqual(packet["story"], 8276)
        self.assertEqual(packet["human_decision"], "pending")
        self.assertEqual(packet, packet_module.prepare_packet(synthetic_run()))
        run["attempts"][-1]["outcome"] = "skipped"
        self.assertEqual(packet_module.prepare_packet(run)["status"], "incomplete")

    def test_reordered_retries_are_rejected(self):
        run = synthetic_run()
        retry = copy.deepcopy(run["attempts"][0])
        retry["attempt"] = 2
        run["attempts"].insert(0, retry)
        with self.assertRaisesRegex(ValueError, "out of order"):
            packet_module.prepare_packet(run)


    def test_review_handoff_pins_exact_packet_without_accepting_it(self):
        packet = packet_module.prepare_packet(synthetic_run())
        handoff = packet_module.review_handoff(packet, "example-private-packet-ref")
        self.assertEqual(handoff, {
            "bolt": "B11", "story": 8276,
            "plan_revision": packet["plan_revision"],
            "packet_revision": packet["packet_revision"],
            "private_artifact_ref": "example-private-packet-ref",
            "human_decision": "pending",
        })
        for change in (
            lambda value: value.update(packet_revision="f" * 64),
            lambda value: value.update(plan_revision="f" * 64),
            lambda value: value.update(human_decision="accepted"),
            lambda value: value.update(status="incomplete"),
            lambda value: value.pop("story"),
        ):
            with self.subTest(change=change):
                altered = copy.deepcopy(packet)
                change(altered)
                with self.assertRaises((ValueError, KeyError)):
                    packet_module.review_handoff(altered, "example-private-packet-ref")
        with self.assertRaises(ValueError):
            packet_module.review_handoff(packet, "")

    def test_private_packet_never_overwrites_existing_revision(self):
        with tempfile.TemporaryDirectory(prefix="example-v13-packet-") as directory:
            source = pathlib.Path(directory) / "run.json"
            output = pathlib.Path(directory) / "packet.json"
            source.write_text(json.dumps(synthetic_run()))
            output.write_text("existing-revision")
            with self.assertRaises(FileExistsError):
                packet_module.main(source, output)
            self.assertEqual(output.read_text(), "existing-revision")


if __name__ == "__main__":
    unittest.main()
