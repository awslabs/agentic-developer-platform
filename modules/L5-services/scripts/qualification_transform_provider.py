"""Bind actual provider traces and installed executor controls to original cases.

Runs only after the independent source/patch/test verifier. This collector makes
bounded installed metadata/checkpoint reads; it cannot launch, resume, qualify
or cancel a provider session. A stored qualification flag is never evidence.
"""

import asyncio
import os

from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE
from l5_services.adapters.transform.provider_trace import verify_provider_trace
from l5_services.adapters.transform.qualification import ProviderQualification
from qualification_transform_isolation import OBSERVATIONS, boundary, installed_target
from qualification_transform_journal import digest


ACTIONS = {
    "provider-selected-original-case": "Reconcile the selected case with its independently verified original result; do not submit replacement work.",
    "provider-installed-target": "Restore the exact protected worker, node and provider/check image pins and current read access.",
    "provider-original-trace": "Retain the source-owned trace from the original bounded provider run; legacy checkpoints cannot prove continuation or tool controls.",
    "provider-installed-controls": "Run or reverify the existing bounded executor-isolation producer on this exact node and both images in the current release attempt.",
    "provider-capability-controls": "Exercise the missing controls through an authorized bounded original provider session and retain its actual trace; do not replace unknown work or fabricate verdicts.",
    "provider-current-authority": "Restore current Product/source authority and the original installed pins before re-reading evidence.",
}


def qualification_pins(run, target):
    """Stored policy is an expected selection, never proof it was exercised."""
    qualification = ProviderQualification.model_validate(run.admission["transform_qualification"])
    settings = run.admission["transform_settings"]
    policy = run.admission["transform_test_policy"]
    if (not qualification.ready_for_submission or qualification.interface != NDJSON_INTERFACE
            or qualification.cli_version != "3.19.0" or qualification.binary_sha256 != NDJSON_BINARY_SHA256
            or qualification.image_digest != target["images"]["provider"].split("@", 1)[1]
            or policy["image_digest"] != target["images"]["check"].split("@", 1)[1]
            or qualification.definition_revision != settings["definition_ref"]["record_revision"]
            or run.admission.get("qualified_executor_ref") != settings["executor_binding_ref"]):
        raise ValueError("Transform selected provider/executor differs from installed pins")
    return {"interface": qualification.interface, "cli_version": qualification.cli_version,
            "binary_sha256": qualification.binary_sha256, "provider_image_digest": qualification.image_digest,
            "check_image_digest": policy["image_digest"], "definition_ref": settings["definition_ref"],
            "executor_binding_ref": settings["executor_binding_ref"], "test_policy_ref": policy["test_policy_ref"]}


def current_isolation_receipt(evidence, target, node):
    """Only a verifier executed in this EvidenceSet may contribute controls."""
    selected = [item for item in evidence.progress if item.get("kind") == "executor-isolation-components"
                and item.get("outcome") == "partial" and item.get("observed") == sorted(OBSERVATIONS)]
    indexed = {digest(row): row for row in evidence.journal.records}
    receipts = []
    for item in selected:
        reference = item.get("collector_receipt")
        row = indexed.get(reference)
        if (row is not None and row["kind"] == "executor-isolation-components"
                and row["value"].get("target_sha256") == digest(target)
                and row["value"].get("boundary_sha256") == digest(node)):
            probes = row["value"].get("probes")
            if (not isinstance(probes, list) or len(probes) != 2
                    or any(not isinstance(probe, dict) for probe in probes)
                    or {probe.get("kind") for probe in probes} != {"provider", "check"}):
                continue
            verified = True
            for probe in probes:
                output = indexed.get(probe.get("output_receipt"))
                if (probe.get("image") != target["images"][probe["kind"]]
                        or probe.get("observations") != dict.fromkeys(OBSERVATIONS, True)
                        or probe.get("owned_absence") is not True or not probe.get("pod_uid")
                        or output is None or output["kind"] != "executor-isolation-output"
                        or output["value"].get("kind") != probe["kind"]
                        or output["value"].get("pod", {}).get("metadata", {}).get("uid") != probe["pod_uid"]):
                    verified = False
            if not verified:
                continue
            receipts.append(reference)
    if len(receipts) != 1:
        raise ValueError("Transform exact installed isolation controls were not reverified in this collection")
    return receipts[0]


class InstalledProvider:
    def __init__(self, evidence, native_results, installed, *, environment=None):
        self.evidence, self.native_results, self.installed = evidence, native_results, installed
        self.environment = os.environ if environment is None else environment
        self.completed = []

    async def collect(self, **values):
        self.stage = "provider-selected-original-case"
        try:
            await self._collect(**values)
        except Exception as error:
            receipt = self.evidence.journal.append("provider-collection-incomplete", {
                "stage": self.stage, "exception_type": type(error).__name__,
                "run_id": values["run"].run_id, "next_action": ACTIONS[self.stage]})
            self.evidence.progress.append({"kind": self.stage, "criterion": "provider-executor-qualified", "outcome": "incomplete",
                "stage": self.stage, "receipt_sha256": receipt, "next_action": ACTIONS[self.stage]})

    async def _collect(self, *, run, attempt, result, authority, observer, intent, files, client, sessions):
        cases = [case for case in self.native_results.cases if case["run_id"] == run.run_id
                 and case["scope"] == authority.scope and case["result_sha256"] == digest(result)
                 and case["attempt_id"] == attempt.attempt_id and case["manifest_sha256"] == attempt.input_hash]
        if len(cases) != 1 or any(case["case"]["run_id"] == run.run_id for case in self.completed):
            raise ValueError("Transform capability collection differs from its independently verified original case")
        case = cases[0]
        series = self.installed.series
        self.stage = "provider-installed-target"
        if series is None:
            raise ValueError("Transform installed provider selection unavailable")
        selected = {"selection": series.config, "release": vars(series.fixture)}
        await authority.revalidate()
        api, target = await asyncio.to_thread(installed_target, selected, series.context, self.environment)
        try:
            node = await asyncio.to_thread(boundary, api, target)
            if node["node_labels"].get("adp.dev/transform-isolation") != "v1":
                raise ValueError("Transform installed node has no current isolation qualification")
            pins = qualification_pins(run, target)
            self.stage = "provider-original-trace"
            checkpoints = await observer.verify_checks(intent=intent, run=run, result=result,
                                                       base_files=files, authority=authority)
            providers = [item["checkpoint"] for item in checkpoints if item["kind"] == "provider"]
            if len(providers) != 1:
                raise ValueError("Transform original provider checkpoint unavailable")
            trace = providers[0]["observation"].get("capability_trace")
            observed = verify_provider_trace(trace, native_id=case["native_id"],
                                            agent_minutes=run.admission["limits"]["agent_minutes"])
            self.stage = "provider-installed-controls"
            isolation = current_isolation_receipt(self.evidence, target, node)
            self.stage = "provider-capability-controls"
            missing = list(observed["missing_tool_controls"])
            if not observed["resume_observed"]:
                missing.append("original-session-continuation")
            observation = {"case": case, "pins": pins, "target_sha256": digest(target),
                "boundary_sha256": digest(node), "isolation_receipt_sha256": isolation,
                "provider_checkpoint_sha256": digest(providers[0]), "provider_trace_sha256": digest(trace),
                "observations": observed, "missing": sorted(missing)}
            self.stage = "provider-current-authority"
            await authority.revalidate()
            current_api, current_target = await asyncio.to_thread(installed_target, selected, series.context, self.environment)
            try:
                current_node = await asyncio.to_thread(boundary, current_api, current_target)
                if current_target != target or current_node != node:
                    raise ValueError("Transform installed provider target changed during collection")
            finally:
                current_api.client.close()
            authority.assert_current()
            receipt = self.evidence.journal.append("provider-capability-readback", observation)
            self.evidence.progress.append({"kind": "provider-capability-controls", "criterion": "provider-executor-qualified", "outcome": "incomplete" if missing else "observed",
                "case_sha256": digest(case), "receipt_sha256": receipt, "missing": sorted(missing),
                "next_action": ACTIONS["provider-capability-controls"] if missing else "Complete both selected original native cases on the same installed executor."})
            if not missing:
                self.completed.append({"case": case, "receipt_sha256": receipt, "pins": pins,
                    "target_sha256": digest(target), "boundary_sha256": digest(node),
                    "isolation_receipt_sha256": isolation})
        finally:
            api.client.close()

    def finish(self):
        if (len(self.completed) != 2 or len(self.native_results.cases) != 2
                or {item["case"]["capability"] for item in self.completed} != {"refactor", "dependency_upgrade"}
                or {item["case"]["run_id"] for item in self.completed}
                != {case["run_id"] for case in self.native_results.cases}):
            return
        for field in ("target_sha256", "boundary_sha256", "isolation_receipt_sha256"):
            if len({item[field] for item in self.completed}) != 1:
                raise ValueError("Transform provider cases use different installed executor controls")
        self.evidence._collected("provider-executor-qualified", {"cases": self.completed})

    def close(self):
        # Each bounded installed-read client is closed within its case.
        pass
