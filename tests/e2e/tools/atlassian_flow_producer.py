"""Produce the acceptance inventory from the same semantic readers used at readback."""

import os
from pathlib import Path

from tests.e2e.tools import atlassian_completion_report as completion
from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_authority_binding import release_binding


CONTEXT_FIELDS = (
    "setup_binding", "recovery_bindings", "recovery_publications",
    "regression_binding", "authority_manifest", "release_manifest", "release_binding",
)


def protected_context(environment):
    binding = release_binding(environment)
    context = {
        "target": {"gateway": environment["GATEWAY_REVISION"],
                   "gateway_image": environment["GATEWAY_IMAGE"],
                   "facade_image": environment["FACADE_IMAGE"]},
        "account": environment["EXPECTED_ACCOUNT"],
        "regression_binding": binding, "release_binding": binding,
    }
    if environment.get("ADP_TOOLS_ATLASSIAN_SETUP_FILE"):
        import hashlib
        from tests.e2e.tools.atlassian_setup_live import load_setup

        _, config = load_setup(environment["ADP_TOOLS_ATLASSIAN_SETUP_FILE"], gateway_url=environment["GATEWAY_URL"])
        if config["account"] != environment["EXPECTED_ACCOUNT"]:
            raise ValueError("Setup account differs from the protected workflow")
        context["setup_binding"] = {key: config[key] for key in ("run_id", "owner_id", "fixture_kind")}
        context["setup_binding"]["gateway_sha256"] = hashlib.sha256(config["gateway_url"].encode()).hexdigest()
    if environment.get("ADP_TOOLS_ATLASSIAN_AUTHORITY_FILE"):
        from tests.e2e.tools.atlassian_authority_scenario import validate_manifest

        context["authority_manifest"] = validate_manifest(
            flow._read_private(environment["ADP_TOOLS_ATLASSIAN_AUTHORITY_FILE"]), binding=binding,
            gateway_url=environment["GATEWAY_URL"], account=environment["EXPECTED_ACCOUNT"],
            region=environment["AWS_REGION"], reader_role=environment["ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN"],
        )
    if environment.get("ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"):
        from tests.e2e.tools.atlassian_recovery_consumer import protected_bindings

        try:
            context["recovery_bindings"] = protected_bindings(
                environment["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"], gateway_url=environment["GATEWAY_URL"],
                gateway_revision=environment["GATEWAY_REVISION"],
            )
        except (KeyError, OSError, TypeError, ValueError):
            context["recovery_bindings"] = None
    if environment.get("ADP_TOOLS_ATLASSIAN_RELEASE_FILE"):
        from tests.e2e.tools.atlassian_release_binding import load_manifest

        context["release_manifest"] = load_manifest(environment)
    if environment.get("ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"):
        from tests.e2e.tools.atlassian_regression_binding import load_manifest

        context["regression_manifest"] = load_manifest(environment)
    return context


def qualify_scenarios(evaluation, *, files, binding, context):
    from tests.e2e.tools.evidence_reader import validate_atlassian_rows

    flow.validate_binding(binding)
    if (evaluation.get("harness_revision") != binding["definition_revision"]
            or evaluation.get("github_run_id") != binding["run_id"]
            or evaluation.get("github_run_attempt") != binding["run_attempt"]
            or evaluation.get("deployed_revisions") != context.get("target")):
        raise ValueError("Acceptance sources differ from the protected run or target")
    qualified = set()
    arguments = {key: context.get(key) for key in CONTEXT_FIELDS}
    if arguments["recovery_publications"] is None and context.get("account"):
        from tests.e2e.tools.atlassian_recovery_handoff import read_publication

        publications = {}
        for row in evaluation["scenarios"]:
            if row.get("scenario_id") != "SCN-8454-04" or row.get("status") != "pass":
                continue
            try:
                handoff = row["observations"]["handoff"]
                _, publications[row["client"]] = read_publication(
                    handoff["publication"], account=context["account"], client=row["client"],
                )
            except (KeyError, OSError, TypeError, ValueError):
                continue
        arguments["recovery_publications"] = publications
    for identifier in flow.FLOW_SCENARIOS:
        if identifier not in evaluation.get("selected_scenario_ids", []):
            continue
        selected = ({f"SCN-8454-{number:02}" for number in range(2, 7)}
                    if identifier == "SCN-8454-06" else {identifier})
        subset = {**evaluation, "scenarios": [row for row in evaluation["scenarios"] if row["scenario_id"] in selected]}
        try:
            validate_atlassian_rows(
                files, subset, selected=selected, run_id=binding["run_id"], attempt=binding["run_attempt"],
                revision=binding["definition_revision"], target=context["target"], **arguments,
            )
        except (KeyError, TypeError, ValueError):
            continue
        qualified.add(identifier)
    return qualified


def finalize_handoff(evaluation, *, binding, context):
    from tests.e2e.tools.atlassian_release_scenario import qualify_handoff

    qualified = qualify_scenarios(evaluation, files={}, binding=binding, context=context)
    for row in evaluation["scenarios"]:
        if row.get("scenario_id") != "SCN-8454-06" or "observations" not in row:
            continue
        result = qualify_handoff(row["observations"], manifest=context["release_manifest"],
                                 binding=binding, qualified_scenarios=qualified)
        row["handoff"] = result
        if result["full_ac06"] == "pass" and row.get("evidence_scope") == "deployed-target":
            row.update(status="pass", cleanup={"status": "confirmed"}, reason="Release and original client/provider scenarios verified")


def build_reports(evaluation, *, files, binding, context):
    from tests.e2e.tools.atlassian_regression_scenario import inspect_native_regression

    for row in evaluation["scenarios"]:
        if "native_observations" in row.get("observations", {}):
            inspect_native_regression(row["observations"], manifest=context["regression_manifest"])
    qualified = qualify_scenarios(evaluation, files=files, binding=binding, context=context)
    cases = set()
    if context.get("release_manifest") is not None:
        from tests.e2e.tools.atlassian_release_scenario import qualify_release

        for row in evaluation["scenarios"]:
            if (row.get("scenario_id") != "SCN-8454-06" or "observations" not in row
                    or row.get("evidence_scope") != "deployed-target"):
                continue
            result = qualify_release(row["observations"], manifest=context["release_manifest"], binding=binding)
            cases.update(case["case"] for case in result["cases"] if case["status"] == "pass")
    return completion.build_reports(
        evaluation, binding=binding, qualified_scenarios=qualified,
        qualified_cases=cases & set(completion.inventory()["cases"]),
    )


def main():
    try:
        environment = os.environ
        directory = Path(environment["RUNNER_TEMP"]) / "tools-eval-evidence"
        files = {path.name: path.read_bytes() for path in directory.glob("*.json")}
        reports = build_reports(flow._decode(files["evidence.json"]), files=files,
                                binding=release_binding(environment), context=protected_context(environment))
        flow._write_private(directory / "completion-reports.json", reports)
        flow._write_private(directory / "live.json", reports["live"])
        for stage, report in reports["stages"].items():
            flow._write_private(directory / f"stage-{stage}.json", report)
        flow._write_private(directory / "atlassian-flow-summary.json", reports["summary"])
    except (KeyError, OSError, TypeError, ValueError):
        raise SystemExit("Acceptance inventory could not be independently produced") from None


if __name__ == "__main__":
    main()
