"""Read back versioned private CI evidence before accepting a protected client run."""

import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from tests.e2e.tools.runner import CLIENTS, OPERATIONS, select_scenarios
from tests.e2e.tools.release_authorization import authorization_cases
from tests.e2e.tools.release_observer import from_environment as observer_from_environment
from tests.e2e.tools import atlassian_flow_summary as flow_summary


DIGEST = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[A-Za-z0-9._+=/-]{1,255}\Z")
MAX_EVIDENCE = 32 * 1024 * 1024


def decode_report(data):
    """Evidence must have one unambiguous, finite JSON interpretation."""
    return flow_summary._decode(data)


def validate_atlassian_rows(files, report, *, selected, run_id, attempt, revision, target, setup_binding=None, recovery_bindings=None, recovery_publications=None, regression_binding=None, authority_manifest=None, release_manifest=None, release_binding=None):
    """Qualify supported setup/client records; never promote unimplemented cases."""
    from tests.e2e.tools.atlassian_client_matrix import CLIENTS as ATLASSIAN_CLIENTS, READS, WRITES
    from tests.e2e.tools.atlassian_integration import SetupActionLedger

    require(bool(selected) and set(selected) <= set(flow_summary.FLOW_SCENARIOS))
    expected = Counter()
    if "SCN-8454-01" in selected:
        expected[("SCN-8454-01", "operator", "setup")] = 1
    if "SCN-8454-02" in selected:
        expected.update(("SCN-8454-02", client, tool) for client in ATLASSIAN_CLIENTS for tool in READS | WRITES)
    if "SCN-8454-04" in selected:
        expected.update(("SCN-8454-04", client, "recovery") for client in ATLASSIAN_CLIENTS)
    if "SCN-8454-05" in selected:
        expected[("SCN-8454-05", "operator", "regression")] = 1
    if "SCN-8454-03" in selected:
        expected[("SCN-8454-03", "operator", "authority")] = 1
    if "SCN-8454-06" in selected:
        expected[("SCN-8454-06", "operator", "handoff")] = 1
    rows = report["scenarios"]
    require(Counter((row.get("scenario_id"), row.get("client"), row.get("tool")) for row in rows) == expected)
    operations = set()
    for row in rows:
        require(row.get("status") == "pass" and row.get("evidence_scope") == "deployed-target"
                and type(row.get("cleanup")) is dict and row["cleanup"].get("status") == "confirmed")
        observed = row.get("observations")
        require(type(observed) is dict)
        if row["scenario_id"] == "SCN-8454-06":
            from tests.e2e.tools.atlassian_release_scenario import qualify_handoff

            require(type(release_manifest) is dict and type(release_binding) is dict)
            require(release_binding.get("run_id") == run_id and release_binding.get("run_attempt") == attempt
                    and release_binding.get("definition_revision") == revision)
            dependencies = {f"SCN-8454-{number:02}" for number in range(2, 6)}
            require(dependencies <= set(selected))
            validate_atlassian_rows(files, {**report, "scenarios": [item for item in rows if item["scenario_id"] in dependencies]},
                selected=dependencies, run_id=run_id, attempt=attempt, revision=revision, target=target,
                setup_binding=setup_binding, recovery_bindings=recovery_bindings, recovery_publications=recovery_publications,
                regression_binding=regression_binding, authority_manifest=authority_manifest)
            result = qualify_handoff(observed, manifest=release_manifest, binding=release_binding, qualified_scenarios=dependencies)
            require(result["full_ac06"] == "pass" and row.get("handoff") == result)
            continue
        if row["scenario_id"] == "SCN-8454-03":
            from tests.e2e.tools.atlassian_authority_scenario import qualify_authority

            require(type(authority_manifest) is dict)
            qualified = qualify_authority(observed, manifest=authority_manifest)
            require(qualified["status"] == "pass" and row.get("authority") == qualified)
            continue
        if row["scenario_id"] == "SCN-8454-05":
            from tests.e2e.tools.atlassian_regression_scenario import predecessor_gaps, qualify_regression

            require(type(regression_binding) is dict)
            require(row.get("regression") == qualify_regression(observed, binding=regression_binding, target=target))
            require(row.get("predecessors") == predecessor_gaps(observed, binding=regression_binding, target=target))
            continue
        if row["scenario_id"] == "SCN-8454-04":
            from tests.e2e.tools.atlassian_recovery_scenario import qualify_recovery

            require(row.get("recovery") == qualify_recovery(observed, client=row["client"], revision=target["gateway"]))
            require(row["recovery"].get("runner_loss") == "persisted-runner-loss")
            require(type(recovery_bindings) is dict and set(recovery_bindings) == ATLASSIAN_CLIENTS)
            require(type(recovery_bindings[row["client"]]) is dict and set(recovery_bindings[row["client"]]) == {
                "client", "run_id", "owner_id", "gateway_revision", "gateway_sha256", "transport_sha256", "original_calls"})
            require(all(observed["consumer"]["binding"].get(key) == value
                        for key, value in recovery_bindings[row["client"]].items()))
            require(type(recovery_publications) is dict and row["client"] in recovery_publications)
            published = recovery_publications[row["client"]]
            require(type(published) is dict and published.get("binding") == observed["consumer"]["binding"]
                    and published.get("archive_sha256") == observed["handoff"]["archive_sha256"])
            continue
        if row["scenario_id"] == "SCN-8454-01":
            require("setup-recovery.json" in files)
            snapshot = decode_report(files["setup-recovery.json"])
            require(type(snapshot) is dict and type(snapshot.get("version")) is int and snapshot["version"] == 1
                    and set(snapshot) == {"version", "run_id", "owner_id", "fixture_kind", "gateway_sha256",
                                          "phase", "connection_id", "revision", "actions"}
                    and snapshot.get("phase") == "cleaned"
                    and all(type(snapshot.get(key)) is str and 0 < len(snapshot[key]) <= 512
                            for key in ("connection_id", "revision"))
                    and type(snapshot.get("run_id")) is str and bool(snapshot["run_id"])
                    and type(snapshot.get("owner_id")) is str and bool(snapshot["owner_id"])
                    and snapshot.get("fixture_kind") in {"fresh", "legacy"}
                    and type(snapshot.get("gateway_sha256")) is str and DIGEST.fullmatch(snapshot["gateway_sha256"])
                    and observed.get("scenario") == "SCN-01" and observed.get("status") == "pass"
                    and observed.get("cleanup") == "confirmed" and observed.get("unverified_actions") == []
                    and observed.get("fixture_kind") == snapshot["fixture_kind"]
                    and type(observed.get("policy_required")) is bool
                    and "live_status" not in observed)
            require(type(setup_binding) is dict
                    and set(setup_binding) == {"run_id", "owner_id", "fixture_kind", "gateway_sha256"}
                    and all(snapshot.get(key) == value for key, value in setup_binding.items()))
            actions = snapshot.get("actions")
            require(type(actions) is list and 0 < len(actions) <= 256)
            latest = {}
            for action in actions:
                require(type(action) is dict and set(action) == {"action", "actor", "status", "evidence_sha256"}
                        and action["action"] in SetupActionLedger.MACHINE_ACTIONS | SetupActionLedger.HUMAN_ACTIONS | SetupActionLedger.POLICY_ACTIONS
                        and action["actor"] == ("machine" if action["action"] in SetupActionLedger.MACHINE_ACTIONS | SetupActionLedger.POLICY_ACTIONS else "human")
                        and action["status"] in {"started", "observed", "blocked", "unknown"}
                        and (action["status"] != "observed" or type(action["evidence_sha256"]) is str
                             and DIGEST.fullmatch(action["evidence_sha256"])))
                latest[action["action"]] = action["status"]
            required = SetupActionLedger.MACHINE_ACTIONS | {"consent", "site_selection"}
            if observed["policy_required"]:
                required |= {"policy_observation"} if "policy_observation" in latest else {"policy_approval"}
            require(all(latest.get(action) == "observed" for action in required))
            continue
        checksum = row.get("evidence_sha256")
        require(type(checksum) is str and DIGEST.fullmatch(checksum)
                and hashlib.sha256(flow_summary.canonical({key: value for key, value in row.items()
                                                         if key != "evidence_sha256"})).hexdigest() == checksum)
        record = row.get("client_evidence")
        require(type(record) is dict and record.get("schema") == "installed-client-evidence/v1"
                and record.get("scenario_id") == row["scenario_id"]
                and record.get("client") == row["client"] and record.get("cell_id") == row.get("cell_id")
                and row.get("cell_id") == row["client"] + ":" + row["tool"]
                and record.get("status") == "pass" and record.get("acceptance_ids") == ["AC-02"]
                and record.get("run") == {"id": run_id, "attempt": attempt}
                and record.get("target") == target
                and record.get("source") == {"harness_revision": revision, "gateway_revision": target["gateway"],
                                              "invocation": observed.get("source")}
                and record.get("provider_source") == "direct-atlassian-provider"
                and type(row.get("client_version")) is str and bool(row["client_version"])
                and observed.get("source") == "confirmed-client-operation-and-direct-provider"
                and all(type(observed.get(key)) is str and DIGEST.fullmatch(observed[key])
                        for key in ("installed_cli_sha256", "installed_mcp_sha256", "discovery_sha256", "content_sha256"))
                and type(observed.get("operation_id")) is str
                and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", observed["operation_id"])
                and observed["operation_id"] not in operations
                and type(observed.get("effect_count")) is int
                and observed["effect_count"] == (1 if row["tool"] in WRITES else 0))
        operations.add(observed["operation_id"])
    return len(rows)


def require(condition):
    if not condition:
        raise ValueError("protected client evidence or independent readback is incomplete")


def fetch_s3(bucket, key):
    role = os.environ.get("CI_EVIDENCE_RUNNER_ROLE_ARN")
    token_file = os.environ.get("CI_EVIDENCE_RUNNER_TOKEN_FILE")
    require(bool(role and token_file and Path(token_file).is_file()))
    environ = dict(os.environ)
    for key_name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                     "AWS_DEFAULT_PROFILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                     "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
                     "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "AWS_SECURITY_TOKEN"):
        environ.pop(key_name, None)
    environ.update(AWS_ROLE_ARN=role, AWS_WEB_IDENTITY_TOKEN_FILE=token_file,
                   AWS_EC2_METADATA_DISABLED="true", AWS_CONFIG_FILE="/dev/null",
                   AWS_SHARED_CREDENTIALS_FILE="/dev/null", BOTO_CONFIG="/dev/null")
    try:
        with tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as directory:
            destination = Path(directory) / "readback"
            result = subprocess.run(["aws", "s3api", "get-object", "--bucket", bucket, "--key", key,
                                     "--checksum-mode", "ENABLED", str(destination), "--output", "json"],
                                    env=environ, capture_output=True, text=True, check=True, timeout=120)
            require(destination.stat().st_size <= MAX_EVIDENCE)
            return destination.read_bytes(), json.loads(result.stdout)
    except (KeyError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError("protected evidence S3 readback unavailable") from None


def checked_object(bucket, key, version, sha, size, fetch):
    require(isinstance(version, str) and version != "null" and VERSION.fullmatch(version) and
            isinstance(sha, str) and DIGEST.fullmatch(sha) and type(size) is int and 0 < size <= MAX_EVIDENCE)
    data, metadata = fetch(bucket, key)
    digest = hashlib.sha256(data).digest()
    require(isinstance(metadata, dict) and metadata.get("VersionId") == version and
            metadata.get("ChecksumSHA256") == base64.b64encode(digest).decode() and
            metadata.get("ContentLength") == size == len(data) and digest.hex() == sha)
    return data


def archived_files(data, records, *, required=("evidence.json", "junit.xml")):
    require(isinstance(records, list) and len(required) <= len(records) <= 50)
    expected = {}
    for record in records:
        require(isinstance(record, dict) and isinstance(record.get("path"), str) and
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,150}", record["path"]) and
                ".." not in record["path"].split("/") and record["path"] not in expected and
                type(record.get("size")) is int and 0 < record["size"] <= MAX_EVIDENCE and
                isinstance(record.get("sha256"), str) and DIGEST.fullmatch(record["sha256"]))
        expected[record["path"]] = record
    require(set(required) <= expected.keys() and sum(item["size"] for item in records) <= MAX_EVIDENCE)
    actual = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            require(member.isfile() and member.name in expected and member.name not in actual and
                    member.size == expected[member.name]["size"])
            contents = archive.extractfile(member).read(member.size + 1)
            require(len(contents) == member.size and
                    hashlib.sha256(contents).hexdigest() == expected[member.name]["sha256"])
            actual[member.name] = contents
    require(set(actual) == set(expected))
    return actual


def validate_authorization(files, *, authorization_binding, revision, run_id, attempt):
    require("authorization.json" in files)
    authorization = decode_report(files["authorization.json"])
    require(isinstance(authorization_binding, dict) and
            set(authorization_binding) == {"account", "region", "cluster", "role", "group", "components"} and
            {"gateway", "facade and reconciler", "shared Lambda and Browser"} <=
            authorization_binding.get("components", {}).keys())
    expected_checks = [
        {"name": case.name, "expected": case.allowed, "observed": case.allowed, "pass": True}
        for case in authorization_cases(authorization_binding["components"])
    ]
    binding_sha = hashlib.sha256(json.dumps(authorization_binding, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
    require(isinstance(authorization, dict) and authorization.get("version") == 1 and
            authorization.get("status") == "pass" and authorization.get("reason") == "verified" and
            authorization.get("definition_revision") == revision and
            authorization.get("workflow_run_id") == run_id and
            authorization.get("workflow_run_attempt") == attempt and
            authorization.get("binding_sha256") == binding_sha and
            authorization.get("identity") == {"aws_matches": True, "kubernetes_matches": True} and
            type(authorization.get("expected_checks")) is int and
            authorization["expected_checks"] == len(expected_checks) and
            authorization.get("checks") == expected_checks and
            all(type(check.get(field)) is bool for check in authorization["checks"]
                for field in ("expected", "observed", "pass")))


def validate_rows(files, *, scenario, run_id, attempt, revision, gateway, gateway_image, facade_image,
                  authorization_binding, release_source_revision, setup_binding=None, recovery_bindings=None, recovery_publications=None, regression_binding=None, authority_manifest=None, release_manifest=None, release_binding=None):
    require(isinstance(release_source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", release_source_revision))
    report = decode_report(files["evidence.json"])
    expected_ids = [case.identifier for case in select_scenarios(scenario)]
    required_atlassian = {f"SCN-8454-{number:02}" for number in range(1, 7)
                          if scenario == "repair" or fnmatch.fnmatchcase(f"SCN-8454-{number:02}", scenario)}
    require(required_atlassian <= set(expected_ids))
    require(isinstance(report, dict) and report.get("evidence_schema") == "workflow-evaluation/v1" and
            report.get("schema_version") == 1 and report.get("live_attestation") is True and
            report.get("matrix_complete") is True and report.get("selected_scenario_ids") == expected_ids and
            report.get("harness_revision") == revision and report.get("github_run_id") == run_id and
            report.get("github_run_attempt") == attempt)
    target = {"gateway": gateway, "gateway_image": gateway_image, "facade_image": facade_image}
    require(report.get("deployed_revisions") == target)
    rows = report.get("scenarios")
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) and row.get("status") == "pass" for row in rows))
    if any(identifier.startswith("SCN-8454-") for identifier in expected_ids):
        require(all(identifier.startswith("SCN-8454-") for identifier in expected_ids))
        if any(identifier != "SCN-8454-01" for identifier in expected_ids):
            validate_authorization(files, authorization_binding=authorization_binding,
                                   revision=revision, run_id=run_id, attempt=attempt)
        return validate_atlassian_rows(files, report, selected=expected_ids, run_id=run_id,
                                       attempt=attempt, revision=revision, target=target, setup_binding=setup_binding,
                                       recovery_bindings=recovery_bindings, recovery_publications=recovery_publications,
                                       regression_binding=regression_binding, authority_manifest=authority_manifest,
                                       release_manifest=release_manifest, release_binding=release_binding)
    selected = [identifier for identifier in expected_ids if identifier.startswith("SCN-8067-")]
    require(bool(selected))
    expected = Counter((identifier, client, tool) for identifier in selected for client in CLIENTS
                       for tool in ({"SCN-8067-01": ("install",), "SCN-8067-02": OPERATIONS,
                                     "SCN-8067-03": ("faults",), "SCN-8067-04": ("evidence",)}[identifier]))
    client_rows = [row for row in rows if row.get("scenario_id") in selected]
    require(Counter((row.get("scenario_id"), row.get("client"), row.get("tool")) for row in client_rows) == expected)
    for row in client_rows:
        checksum = row.get("evidence_sha256")
        require(isinstance(checksum, str) and DIGEST.fullmatch(checksum) and
                hashlib.sha256(json.dumps({key: value for key, value in row.items() if key != "evidence_sha256"},
                                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest() == checksum)
        record = row.get("client_evidence", {})
        observations = row.get("observations", {})
        require(isinstance(record, dict) and isinstance(observations, dict) and
                record.get("schema") == "installed-client-evidence/v1" and
                record.get("scenario_id") == row["scenario_id"] and record.get("client") == row["client"] and
                record.get("cell_id") == row.get("cell_id") and record.get("status") == "pass" and
                record.get("acceptance_ids") == ["AC-" + row["scenario_id"][-2:]] and
                record.get("run") == {"id": run_id, "attempt": attempt} and
                record.get("target") == target and
                record.get("source") == {"harness_revision": revision, "gateway_revision": gateway,
                                         "invocation": observations.get("source")})
        require(isinstance(row.get("client_version"), str) and row["client_version"] and
                all(isinstance(observations.get(key), str) and DIGEST.fullmatch(observations[key])
                    for key in ("installed_cli_sha256", "installed_mcp_sha256", "discovery_sha256")))
        if row["scenario_id"] == "SCN-8067-01":
            require(observations.get("source") == "verified-installed-cli")
        elif row["scenario_id"] == "SCN-8067-02":
            require(observations.get("source") == ("installed-cli-mcp" if row["client"] == "cli"
                                                       else row["client"] + "-native-mcp"))
        if row["scenario_id"] != "SCN-8067-01":
            provider = observations.get("provider_observation", {})
            require(record.get("provider_source") == "provider-control-plane" and
                    isinstance(provider, dict) and provider.get("source") == "provider-control-plane" and
                    provider.get("assertion") == {"source": "provider-control-plane",
                                                   "operation": row["tool"], "passed": True} and
                    type(provider.get("provider_request_count")) is int and
                    provider["provider_request_count"] >= 0 and
                    row.get("cleanup", {}).get("status") == "confirmed" and
                    provider.get("cleanup") == row["cleanup"] and row["cleanup"].get("observed_at"))
        if row["scenario_id"] == "SCN-8067-01":
            window = observations.get("runtime_window", {})
            receipts = window.get("receipts", {})
            # Workflow definition/harness, gateway and released tools can have
            # different immutable revisions. Bind each to its trusted input;
            # the retained artifact cannot choose its own expected source.
            require(window.get("source_revision") == release_source_revision and isinstance(receipts, dict) and
                    {"gateway", "facade and reconciler"} <= set(window.get("components", [])) == set(receipts) and
                    all(isinstance(receipt, dict) and re.fullmatch(r"[0-9a-f]{40}", receipt.get("source_revision", "")) and
                        type(receipt.get("run_id")) is int and receipt["run_id"] > 0 and
                        type(receipt.get("run_attempt")) is int and receipt["run_attempt"] > 0
                        for receipt in receipts.values()) and
                    receipts["gateway"]["source_revision"] == gateway and
                    receipts["facade and reconciler"]["source_revision"] == release_source_revision)
    validate_authorization(files, authorization_binding=authorization_binding,
                           revision=revision, run_id=run_id, attempt=attempt)
    if "SCN-8067-01" in selected:
        require({"runtime-before.json", "runtime-after.json"} <= files.keys())
        snapshots = [json.loads(files[f"runtime-{phase}.json"]) for phase in ("before", "after")]
        before, after = (snapshot.get("runtime") for snapshot in snapshots)
        require([snapshot.get("phase") for snapshot in snapshots] == ["before", "after"] and
                isinstance(before, dict) and before == after and
                before.get("gateway", {}).get("image") == gateway_image.rsplit("@", 1)[-1] and
                before.get("facade and reconciler", {}).get("image") == facade_image.rsplit("@", 1)[-1])
    return len(client_rows)


def verify_retained(uri, version, *, repository, account, run_id, attempt, revision, gateway,
                    gateway_image, facade_image, scenario, authorization_binding, release_source_revision, fetch=fetch_s3,
                    setup_binding=None, flow_binding=None, flow_output=None, recovery_bindings=None, regression_binding=None, authority_manifest=None, release_manifest=None, release_binding=None, regression_manifest=None):
    require(re.fullmatch(r"[0-9]{12}", account) and re.fullmatch(r"[1-9][0-9]*", run_id) and
            re.fullmatch(r"[1-9][0-9]*", attempt) and re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repository) and
            re.fullmatch(r"[0-9a-f]{40}", revision) and re.fullmatch(r"[0-9a-f]{40}", gateway) and
            isinstance(release_source_revision, str) and re.fullmatch(r"[0-9a-f]{40}", release_source_revision) and
            isinstance(authorization_binding, dict) and authorization_binding.get("account") == account)
    bucket = f"adp-dev-ci-evidence-{account}"
    prefix = f"artifacts/14/{repository}/{run_id}/{attempt}/evaluate/tools-live-evaluation-{run_id}-{attempt}/"
    require(isinstance(uri, str) and re.fullmatch(re.escape(f"s3://{bucket}/{prefix}") +
                                                   r"[0-9a-f]{64}\.manifest\.json", uri))
    manifest_key = uri.removeprefix(f"s3://{bucket}/")
    manifest_sha = manifest_key.rsplit("/", 1)[-1].split(".", 1)[0]
    manifest_data, metadata = fetch(bucket, manifest_key)
    require(isinstance(version, str) and VERSION.fullmatch(version) and len(manifest_data) <= MAX_EVIDENCE and
            isinstance(metadata, dict) and metadata.get("VersionId") == version and
            metadata.get("ChecksumSHA256") == base64.b64encode(hashlib.sha256(manifest_data).digest()).decode() and
            metadata.get("ContentLength") == len(manifest_data) and hashlib.sha256(manifest_data).hexdigest() == manifest_sha)
    manifest = decode_report(manifest_data)
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1 and
            manifest.get("repository") == repository and manifest.get("run_id") == run_id and
            manifest.get("run_attempt") == attempt and manifest.get("job") == "evaluate" and
            manifest.get("name") == f"tools-live-evaluation-{run_id}-{attempt}" and
            manifest.get("retention_days") == 14 and manifest.get("checked_out_revision") == revision and
            manifest.get("workflow_revision") == revision and not manifest.get("missing_path_count"))
    artifact = manifest.get("artifact", {})
    require(isinstance(artifact, dict) and artifact.get("bucket") == bucket and
            isinstance(artifact.get("sha256"), str) and DIGEST.fullmatch(artifact["sha256"]) and
            artifact.get("key") == f'{prefix}{artifact["sha256"]}.tar.gz')
    payload = checked_object(bucket, artifact["key"], artifact.get("version_id"), artifact["sha256"],
                             artifact.get("size"), fetch)
    files = archived_files(payload, manifest.get("files"))
    recovery_publications = {}
    for row in decode_report(files["evidence.json"]).get("scenarios", []):
        if row.get("scenario_id") == "SCN-8454-04" and row.get("status") == "pass":
            from tests.e2e.tools.atlassian_recovery_handoff import read_publication

            handoff = row.get("observations", {}).get("handoff")
            require(type(handoff) is dict and row.get("client") not in recovery_publications)
            require(handoff["publication"]["revision"] == revision)
            _, recovery_publications[row["client"]] = read_publication(
                handoff["publication"], account=account, client=row["client"], fetch=fetch)
    arguments = dict(scenario=scenario, run_id=run_id, attempt=attempt, revision=revision, gateway=gateway,
                     gateway_image=gateway_image, facade_image=facade_image, authorization_binding=authorization_binding,
                     release_source_revision=release_source_revision, setup_binding=setup_binding, recovery_bindings=recovery_bindings,
                     recovery_publications=recovery_publications, regression_binding=regression_binding, authority_manifest=authority_manifest,
                     release_manifest=release_manifest, release_binding=release_binding)
    if flow_binding is not None:
        require(flow_binding.get("source_revision") == release_source_revision
                and flow_binding.get("definition_revision") == revision
                and flow_binding.get("run_id") == run_id and flow_binding.get("run_attempt") == attempt)
        result = read_flow_bundle(files, binding=flow_binding, validation={**arguments, "regression_manifest": regression_manifest})
        if flow_output is not None:
            flow_summary._write_private(flow_output, result)
        require(result["completion_ready"] is True)
        require("atlassian-flow-summary.json" in files
                and flow_summary._same(decode_report(files["atlassian-flow-summary.json"]), result))
    return validate_rows(files, **arguments)


def read_flow_bundle(files, *, binding, validation):
    """Rebuild from retained reports and rerun scenario qualification locally.

    Missing stage reports or unsupported live cases remain visible in the
    returned blocked result; neither a producer's summary nor its flags grant
    qualification. Each new scenario needs a concrete reader before it passes.
    """
    evaluation = decode_report(files["evidence.json"])
    if "completion-reports.json" in files:
        from tests.e2e.tools.atlassian_flow_producer import build_reports, CONTEXT_FIELDS

        context = {key: validation.get(key) for key in CONTEXT_FIELDS}
        context["regression_manifest"] = validation.get("regression_manifest")
        context["target"] = {"gateway": validation["gateway"], "gateway_image": validation["gateway_image"],
                             "facade_image": validation["facade_image"]}
        rebuilt = build_reports(evaluation, files=files, binding=binding, context=context)
        require(flow_summary._same(decode_report(files["completion-reports.json"]), rebuilt))
        require(flow_summary._same(decode_report(files["live.json"]), rebuilt["live"]))
        require(all(flow_summary._same(decode_report(files[f"stage-{stage}.json"]), value)
                    for stage, value in rebuilt["stages"].items()))
        return rebuilt["summary"]
    live = decode_report(files["live.json"]) if "live.json" in files else {}
    stages = {stage: decode_report(files[f"stage-{stage}.json"])
              for stage in flow_summary.STAGES if f"stage-{stage}.json" in files}
    qualified = set()
    try:
        validate_rows(files, **validation)
    except (KeyError, TypeError, ValueError):
        pass
    else:
        qualified.update(evaluation["selected_scenario_ids"])
    return flow_summary.assemble_evaluation_summary(live, stages, evaluation, binding=binding,
                                                   qualified_scenarios=qualified)


def main():
    env = os.environ
    try:
        from tests.e2e.tools.atlassian_scenario_selection import selection_flags

        selected_flags = selection_flags(env["SCENARIO_FILTER"])
        setup_binding = None
        recovery_bindings = None
        if selected_flags["atlassian_recovery"]:
            from tests.e2e.tools.atlassian_recovery_consumer import protected_bindings

            recovery_bindings = protected_bindings(env["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"],
                gateway_url=env["GATEWAY_URL"], gateway_revision=env["GATEWAY_REVISION"])
        if selected_flags["atlassian_setup"]:
            from tests.e2e.tools.atlassian_setup_live import load_setup
            _, config = load_setup(env["ADP_TOOLS_ATLASSIAN_SETUP_FILE"], gateway_url=env["GATEWAY_URL"])
            require(config["account"] == env["EXPECTED_ACCOUNT"])
            setup_binding = {key: config[key] for key in ("run_id", "owner_id", "fixture_kind")}
            setup_binding["gateway_sha256"] = hashlib.sha256(config["gateway_url"].encode()).hexdigest()
            binding = {"account": config["account"]}
        if not selected_flags["atlassian_setup"] or selected_flags["atlassian_clients"]:
            observer = observer_from_environment(env)
            role = env["EXPECTED_ROLE"]
            role_match = re.fullmatch(rf"arn:aws:iam::{observer.account}:role/adp-([a-z0-9-]+)-tools-trusted-deployment", role)
            require(role_match is not None)
            binding = {"account": observer.account, "region": observer.region, "cluster": observer.cluster,
                       "role": role, "group": "adp:tools-release-" + role_match[1], "components": observer.components}
        flow_binding = None
        regression_binding = None
        authority_manifest = None
        release_manifest = None
        handoff_binding = None
        regression_manifest = None
        if env.get("ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"):
            from tests.e2e.tools.atlassian_regression_binding import load_manifest

            regression_manifest = load_manifest(env)
        if selected_flags["atlassian_handoff"]:
            from tests.e2e.tools.atlassian_release_binding import load_manifest
            from tests.e2e.tools.atlassian_authority_binding import release_binding

            release_manifest = load_manifest(env)
            handoff_binding = release_binding(env)
        if selected_flags["atlassian_authority"]:
            from tests.e2e.tools.atlassian_authority_scenario import validate_manifest
            from tests.e2e.tools.atlassian_authority_binding import release_binding

            authority_manifest = validate_manifest(
                flow_summary._read_private(env["ADP_TOOLS_ATLASSIAN_AUTHORITY_FILE"]), binding=release_binding(env),
                gateway_url=env["GATEWAY_URL"], account=env["EXPECTED_ACCOUNT"], region=env["AWS_REGION"],
                reader_role=env["ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN"],
            )
        if env.get("ADP_TOOLS_ATLASSIAN_FLOW_OUTPUT") or selected_flags["atlassian_regression"]:
            release = flow_summary._read_private(env["ADP_TOOLS_RELEASE_MANIFEST_FILE"])
            flow_binding = {
                "source_revision": env["SOURCE_REVISION"], "definition_revision": env["GITHUB_SHA"],
                "run_id": env["GITHUB_RUN_ID"], "run_attempt": env["GITHUB_RUN_ATTEMPT"],
                "release_sha256": hashlib.sha256(flow_summary.canonical(release)).hexdigest(),
            }
            if selected_flags["atlassian_regression"]:
                regression_binding = flow_binding
            if not env.get("ADP_TOOLS_ATLASSIAN_FLOW_OUTPUT"):
                flow_binding = None
        count = verify_retained(env.get("CI_EVIDENCE_MANIFEST_URI"), env.get("CI_EVIDENCE_MANIFEST_VERSION"),
                                repository=env["GITHUB_REPOSITORY"], account=env["EXPECTED_ACCOUNT"],
                                run_id=env["GITHUB_RUN_ID"], attempt=env["GITHUB_RUN_ATTEMPT"],
                                revision=env["GITHUB_SHA"], gateway=env["GATEWAY_REVISION"],
                                gateway_image=env["GATEWAY_IMAGE"], facade_image=env["FACADE_IMAGE"],
                                scenario=env["SCENARIO_FILTER"], authorization_binding=binding,
                                release_source_revision=env["SOURCE_REVISION"], setup_binding=setup_binding,
                                recovery_bindings=recovery_bindings,
                                regression_binding=regression_binding,
                                authority_manifest=authority_manifest,
                                release_manifest=release_manifest, release_binding=handoff_binding,
                                regression_manifest=regression_manifest,
                                flow_binding=flow_binding, flow_output=env.get("ADP_TOOLS_ATLASSIAN_FLOW_OUTPUT"))
    except (KeyError, OSError, ValueError, TypeError, tarfile.TarError):
        raise SystemExit("Protected client evidence could not be independently validated") from None
    print(f"Validated {count} protected installed-client evidence rows")


if __name__ == "__main__":
    main()
