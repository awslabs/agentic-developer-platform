"""Source-owned Transform evidence collection; fixture-shaped verdicts are not inputs."""

from copy import deepcopy
from qualification_transform_journal import digest


class NativeResults:
    """Observe only after the existing source/patch/test verifier has succeeded."""
    def __init__(self, evidence):
        self.evidence = evidence
        self.cases = []
        self.selected = {}

    def select(self, selected):
        scope = selected.get("scope", selected)
        value = {"scope": {key: scope[key] for key in ("tenant_id", "product_id", "project_id")},
                 **{key: selected[key] for key in ("run_id", "result_id", "result_revision")}}
        identity = (value["scope"]["tenant_id"], value["run_id"])
        if identity in self.selected and self.selected[identity] != value:
            raise ValueError("selected original Transform result changed")
        self.selected[identity] = value
        self.evidence.journal.append("selected-original-result", value)

    async def verified(self, run, attempt, result, report):
        if (report.get("status") != "evaluated" or report.get("blockers") != []
                or result.get("run_id") != run.run_id or result.get("revision") != report["result_revision"]):
            raise ValueError("native case has not passed independent result verification")
        selected = self.selected.get((run.tenant_id, run.run_id))
        if selected != {"scope": {key: run.admission["scope"][key]
                                 for key in ("tenant_id", "product_id", "project_id")},
                        "run_id": run.run_id, "result_id": result.get("result_id"),
                        "result_revision": result.get("revision")}:
            raise ValueError("native observation differs from the selected original case")
        goal = run.input.get("transform_input", {}).get("goal", {})
        kind = goal.get("kind")
        sources = run.manifest.get("sources", [])
        if (kind not in {"refactor", "dependency_upgrade"} or len(sources) != 1
                or not sources[0].get("record_revision") or not run.native_job_ref.get("native_id")):
            raise ValueError("native acceptance requires one pinned source and an original supported operation")
        for stage in ("before", "after"):
            entries = result.get("tests_" + stage)
            if (not isinstance(entries, list) or not entries
                    or any(item.get("status") != "passed" or item.get("receipt", {}).get("test_count", 0) < 1
                           for item in entries)):
                raise ValueError("native acceptance requires real passing detached before/after tests")
        if kind == "dependency_upgrade" and result.get("dependency_comparison", {}).get("status") != "passed":
            raise ValueError("native dependency comparison was not independently verified")
        value = {"capability": kind, "scope": {key: run.admission["scope"][key]
                                              for key in ("tenant_id", "product_id", "project_id")},
                 "run_id": run.run_id, "result_id": result["result_id"], "result_revision": result["revision"],
                 "source_revision": sources[0]["record_revision"], "native_id": run.native_job_ref["native_id"],
                 "attempt_id": attempt.attempt_id, "manifest_sha256": attempt.input_hash,
                 "original_patch_sha256": result["original_patch"]["sha256"],
                 "result_sha256": digest(result), "verification": deepcopy(report)}
        if any(item["run_id"] == value["run_id"] for item in self.cases):
            raise ValueError("native release repeats an original run")
        self.cases.append(value)
        self.evidence.journal.append("native-result-readback", value)

    def finish(self):
        self.evidence.progress.append({"kind": "selected-native-results", "count": len(self.cases),
                                       "capabilities": sorted(case["capability"] for case in self.cases)})
        if len(self.cases) != 2 or {case["capability"] for case in self.cases} != {"refactor", "dependency_upgrade"}:
            return
        for key in ("run_id", "result_id", "source_revision", "native_id", "original_patch_sha256"):
            if len({case[key] for case in self.cases}) != 2:
                raise ValueError("native fixtures must retain distinct original source/work identities")
        if len({(case["scope"]["tenant_id"], case["scope"]["product_id"], case["scope"]["project_id"])
                for case in self.cases}) != 2:
            raise ValueError("native operations must use separate selected Product projects")
        self.evidence._collected("EC-03", {"cases": self.cases})
