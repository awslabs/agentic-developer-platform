"""Real route/outage rehearsal through the existing protected release helper.

The evaluation collector is read-only. The mutation entrypoint requires the
existing deployment-job guard and restores original adapter replicas on every
exit. Provider/client parity remains a separate, explicitly pending join; these
infrastructure observations cannot establish provider effects or grant history.
"""

import argparse
from copy import deepcopy
import fcntl
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

import httpx

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_authority_scenario import _write, digest
from modules.tools.platform.scripts.native_test_session import private_file

CONNECTORS = ("atlassian", "github", "jira", "slack")
CASES = (
    "adapter-four-routes",
    "forged-direct-denials",
    "partial-release-resume",
    "core-health-through-outage",
    "group-rollback-redeploy",
    "controller-restart-stale-lease",
    "claude-code-parity",
    "codex-parity",
    "provider-grants-no-duplicate",
)
MAX_BYTES = 2 * 1024 * 1024


def helpers():
    """Import only repository-owned production helpers, never configured modules."""
    directory = str(Path(__file__).resolve().parents[3] / "modules/tools/adapters")
    sys.path.insert(0, directory)
    try:
        return importlib.import_module("handoff_release"), importlib.import_module(
            "guard_release"
        )
    finally:
        sys.path.remove(directory)


def require(condition, message="Original release observation is unavailable"):
    if not condition:
        raise ValueError(message)


def manifest(value, *, account, region):
    require(
        type(value) is dict
        and set(value) == {"account", "region", "gateway_url", "handoff"}
    )
    require(value["account"] == account and re.fullmatch(r"[0-9]{12}", account))
    require(
        value["region"] == region and re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", region)
    )
    require(
        type(value["gateway_url"]) is str
        and re.fullmatch(r"https://[A-Za-z0-9.-]+(?::443)?", value["gateway_url"])
    )
    config = value["handoff"]
    require(type(config) is dict and set(config) == {"plan", "selectors"})
    plan, selectors = config["plan"], config["selectors"]
    require(type(plan) is dict and set(plan) == {"api_id", "stage", "before", "after"})
    require(selectors["account_id"] == account and selectors["region"] == region)
    require(re.fullmatch(r"[a-z0-9]{1,32}", plan["api_id"]))
    for key in ("legacy_host", "adapter_host"):
        require(
            re.fullmatch(
                r"internal-[A-Za-z0-9.-]+\.elb\.amazonaws\.com", selectors[key]
            )
        )
    require(selectors["legacy_host"] != selectors["adapter_host"])
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", selectors["adapter_namespace"]))
    require(plan["before"]["deployment_id"] != plan["after"]["deployment_id"])
    for label in ("before", "after"):
        snapshot = plan[label]
        require(set(snapshot) == {"status", "release", "routes", "deployment_id"})
        require(snapshot["status"] in {"legacy", "active"})
        require(re.fullmatch(r"[a-z0-9]{1,32}", snapshot["deployment_id"]))
        require(set(snapshot["routes"]) == set(CONNECTORS))
        require(len({item["target_id"] for item in snapshot["routes"].values()}) == 4)
        for connector, route in snapshot["routes"].items():
            expected = (
                "adapter"
                if snapshot["status"] == "active"
                else "disabled"
                if connector == "atlassian"
                else "legacy"
            )
            require(
                set(route) == {"backend", "target_id", "digest"}
                and route["backend"] == expected
            )
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", route["target_id"]))
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", route["digest"]))
        release = snapshot["release"]
        if release is None:
            require(label == "before" and snapshot["status"] == "legacy")
            continue
        require(
            set(release)
            == {
                "contract_version",
                "source_revision",
                "image_digest",
                "group",
                "group_generation",
                "backend_generation",
            }
        )
        require(
            release["contract_version"] == "1.0"
            and release["group"] == "tool-connectors"
        )
        require(re.fullmatch(r"[0-9a-f]{40}", release["source_revision"]))
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", release["image_digest"]))
        require(
            all(
                type(release[key]) is int and release[key] >= 1
                for key in ("group_generation", "backend_generation")
            )
        )
    require(
        plan["after"]["status"] == "active" and plan["after"]["release"] is not None
    )
    for field in ("group_generation", "backend_generation"):
        require(
            plan["after"]["release"][field]
            == ((plan["before"]["release"] or {}).get(field, 0) + 1)
        )
    require(len(flow.canonical(value)) <= 65536)
    return deepcopy(value)


def redeployment(config):
    handoff, _ = helpers()
    result = deepcopy(config)
    result["plan"]["before"] = handoff.restored(config["plan"])
    for field in ("group_generation", "backend_generation"):
        result["plan"]["after"]["release"][field] = (
            result["plan"]["before"]["release"][field] + 1
        )
    return result


def http_observation(url, *, body=None, headers=None):
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        with client.stream(
            "POST" if body is not None else "GET", url, json=body, headers=headers
        ) as response:
            data = bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                require(len(data) <= 65536)
            return {
                "status_code": response.status_code,
                "body": flow._decode(bytes(data)),
            }


def health(config, *, read=http_observation):
    return read(config["gateway_url"] + "/api/health")


def healthy(value):
    return (
        type(value) is dict
        and value.get("status_code") == 200
        and value.get("body", {}).get("status") == "healthy"
    )


def denials(config, *, read=http_observation):
    origin = "http://" + config["handoff"]["selectors"]["adapter_host"]
    rows = []
    for connector in CONNECTORS:
        for variant in ("unsigned", "forged"):
            result = read(
                origin + "/internal/v1/tools/" + connector + "/mcp",
                body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers=(
                    {
                        "x-caller-identity": "forged-test-identity",
                        "x-adp-edge-provenance": "invalid-test-signature",
                    }
                    if variant == "forged"
                    else {}
                ),
            )
            rows.append({"connector": connector, "variant": variant, **result})
    return rows


def denied(rows):
    if type(rows) is not list or len(rows) != 8:
        return False
    seen = set()
    for row in rows:
        key = (row.get("connector"), row.get("variant"))
        if (
            key in seen
            or key[0] not in CONNECTORS
            or key[1] not in {"unsigned", "forged"}
        ):
            return False
        seen.add(key)
        body = row.get("body")
        if row.get("status_code") != 403 or body not in (
            {"error": "adapter_authority_required"},
            {"detail": {"error": "adapter_authority_required"}},
        ):
            return False
    return True


class CoreTransition:
    def __init__(self, environment):
        _, self.guard = helpers()
        self.environment = environment

    def __call__(self, **value):
        return self.guard.authorize(self.environment, transition=value)

    def stale(self, plan):
        observations = []
        inner = urllib.request.build_opener(self.guard.NoRedirects())

        class Observe:
            def open(self, request, **kwargs):
                try:
                    response = inner.open(request, **kwargs)
                except urllib.error.HTTPError as error:
                    observations.append(
                        {
                            "path": request.selector.split("?", 1)[0],
                            "status": error.code,
                        }
                    )
                    raise
                observations.append(
                    {
                        "path": request.selector.split("?", 1)[0],
                        "status": response.status,
                    }
                )
                return response

        try:
            self.guard.authorize(
                self.environment,
                opener=Observe(),
                transition={"action": "fence", "plan": plan},
            )
        except self.guard.GuardRefused:
            pass
        require(
            observations
            and observations[-1]
            == {"path": "/api/tools/adapter-release/transition", "status": 409},
            "Stale controller denial was not observed",
        )
        return observations[-1]


def deployment(backend):
    return flow._decode(
        backend.kubectl(
            "-n",
            backend.selectors["adapter_namespace"],
            "get",
            "deployment/tool-adapters",
            "-o",
            "json",
        )
    )


def replica_snapshot(value):
    metadata, spec = value["metadata"], value["spec"]
    require(
        metadata["name"] == "tool-adapters" and not metadata.get("deletionTimestamp")
    )
    require(type(spec.get("replicas")) is int and 0 <= spec["replicas"] <= 20)
    return {
        "uid": metadata["uid"],
        "replicas": spec["replicas"],
        "template": deepcopy(spec["template"]),
    }


def set_replicas(backend, original, count, *, clock=time.monotonic, sleep=time.sleep):
    current = deployment(backend)
    observed = replica_snapshot(current)
    require(
        observed["uid"] == original["uid"]
        and observed["template"] == original["template"],
        "Adapter identity or template changed during outage",
    )
    require(
        observed["replicas"] in {0, original["replicas"]},
        "Adapter replicas changed outside this rehearsal",
    )
    if observed["replicas"] != count:
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "replace", "path": "/spec/replicas", "value": count},
        ]
        backend.kubectl(
            "-n",
            backend.selectors["adapter_namespace"],
            "patch",
            "deployment/tool-adapters",
            "--type=json",
            "--patch=" + json.dumps(patch),
            mutation=True,
        )
    deadline = clock() + 60
    while True:
        current = deployment(backend)
        actual = replica_snapshot(current)
        require(actual == {**original, "replicas": count})
        status = current.get("status", {})
        if status.get("observedGeneration", 0) >= current["metadata"][
            "generation"
        ] and all(
            status.get(key, 0) == count
            for key in (
                "replicas",
                "updatedReplicas",
                "readyReplicas",
                "availableReplicas",
            )
        ):
            return {"deployment": current, "replicas": count}
        require(clock() < deadline, "Adapter replica restoration is unconfirmed")
        sleep(2)


class InterruptedRelease(BaseException):
    """Intentional process boundary; production execute must retain its fence."""


def rehearse_release(
    config_path,
    journal_path,
    *,
    environment=os.environ,
    backend_factory=None,
    transition=None,
    read=http_observation,
):
    handoff, guard = helpers()
    guard.authorize(environment)
    config = manifest(
        flow._decode(private_file(Path(config_path), 65536)),
        account=environment["ADP_TOOLS_ACCOUNT_ID"],
        region=environment["ADP_TOOLS_AWS_REGION"],
    )
    require(
        config["handoff"]["plan"]["after"]["release"]["source_revision"]
        == environment["ADP_SOURCE_REVISION"]
    )
    factory = backend_factory or (lambda value: handoff.Backend(value, environment))
    core = transition or CoreTransition(environment)
    path = Path(journal_path)
    require(
        path.parent.is_dir()
        and path.parent.stat().st_uid == os.getuid()
        and not path.parent.stat().st_mode & 0o077
    )
    lock = os.open(str(path) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _rehearse(config, path, environment, factory, core, read)
    finally:
        os.close(lock)


def _rehearse(config, path, environment, factory, core, read):
    handoff, _ = helpers()
    identity = {
        key: environment[key]
        for key in (
            "ADP_SOURCE_REVISION",
            "ADP_DEFINITION_REVISION",
            "ADP_CORRELATION",
            "ADP_TOOLS_DEPLOY_ROLE_ARN",
        )
    }
    if path.exists():
        journal = flow._decode(private_file(path, MAX_BYTES))
        require(
            journal.get("manifest") == config and journal.get("owner") == identity,
            "Original rehearsal ownership changed",
        )
    else:
        journal = {
            "schema": "adapter-release-rehearsal/v1",
            "manifest": config,
            "owner": identity,
            "original_run_id": environment["GITHUB_RUN_ID"],
            "observations": {},
            "cleanup": "pending",
        }
        _write(path, journal)
    rows = journal["observations"]
    original_config = config["handoff"]
    plan = original_config["plan"]
    backend = factory(original_config)
    # A lost process during outage restores the exact original deployment first.
    if "outage_original" in rows and "outage_restored" not in rows:
        rows["outage_restored"] = set_replicas(
            backend, rows["outage_original"], rows["outage_original"]["replicas"]
        )
        _write(path, journal)
    if "interruption" not in rows:
        backend.verify_prepared(plan)
        actual = core(action="read", plan=plan)
        require(actual is None or actual.get("transition", {}).get("plan") == plan)

        class Interrupt:
            def __getattr__(self, name):
                return getattr(backend, name)

            def configure_release(self, release, *, configure_adapter=True):
                backend.configure_release(release, configure_adapter=configure_adapter)
                raise InterruptedRelease()

        try:
            handoff.execute(original_config, Interrupt(), core)
        except InterruptedRelease:
            rows["interruption"] = {
                "state": core(action="read", plan=plan),
                "route": backend.observe(plan["stage"], plan["before"]),
                "health": health(config, read=read),
            }
            _write(path, journal)
        require(
            "interruption" in rows, "Original interrupted transition was not observed"
        )
    if "handoff" not in rows:
        state = handoff.execute(original_config, factory(original_config), core)
        rows["handoff"] = {
            "state": state,
            "route": backend.observe(plan["stage"], plan["after"]),
            "health": health(config, read=read),
        }
        _write(path, journal)
    if "denials" not in rows:
        rows["denials"] = denials(config, read=read)
        _write(path, journal)
    if "outage_original" not in rows:
        original = replica_snapshot(deployment(backend))
        require(original["replicas"] > 0)
        rows["outage_original"] = original
        _write(path, journal)
        try:
            rows["outage_down"] = set_replicas(backend, original, 0)
            rows["outage_health"] = health(config, read=read)
            _write(path, journal)
        finally:
            rows["outage_restored"] = set_replicas(
                backend, original, original["replicas"]
            )
            _write(path, journal)
    if "rollback" not in rows:
        state = handoff.execute(
            original_config, factory(original_config), core, rollback=True
        )
        rows["rollback"] = {
            "state": state,
            "route": backend.observe(plan["stage"], handoff.restored(plan)),
            "health": health(config, read=read),
        }
        _write(path, journal)
    next_config = redeployment(original_config)
    next_backend = factory(next_config)
    if "redeploy" not in rows:
        state = handoff.execute(next_config, next_backend, core)
        rows["redeploy"] = {
            "state": state,
            "route": next_backend.observe(plan["stage"], next_config["plan"]["after"]),
            "health": health(config, read=read),
        }
        _write(path, journal)
    if "stale" not in rows:
        rows["stale"] = core.stale(plan)
        _write(path, journal)
    rows["final"] = {
        "state": core(action="read", plan=next_config["plan"]),
        "route": next_backend.observe(plan["stage"], next_config["plan"]["after"]),
        "health": health(config, read=read),
    }
    journal["cleanup"] = "confirmed"
    _write(path, journal)
    return journal


def collect_release(
    config_path,
    *,
    binding,
    account,
    region,
    environment=os.environ,
    journal_path=None,
    backend_factory=None,
    read=http_observation,
):
    flow.validate_binding(binding)
    config = manifest(
        flow._decode(private_file(Path(config_path), 65536)),
        account=account,
        region=region,
    )
    require(
        config["handoff"]["plan"]["after"]["release"]["source_revision"]
        == binding["source_revision"]
    )
    handoff, _ = helpers()
    report = {
        "schema": "adapter-release-observations/v1",
        "binding": binding,
        "manifest_sha256": digest(config),
        "observations": {},
        "rehearsal": None,
    }
    rows = report["observations"]
    for key, operation in (
        ("health", lambda: health(config, read=read)),
        ("denials", lambda: denials(config, read=read)),
    ):
        try:
            rows[key] = operation()
        except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
            rows[key] = None
    if journal_path is not None:
        journal = flow._decode(private_file(Path(journal_path), MAX_BYTES))
        require(journal.get("manifest") == config)
        report["rehearsal"] = journal
    selected = (
        redeployment(config["handoff"]) if report["rehearsal"] else config["handoff"]
    )
    try:
        backend = (
            backend_factory or (lambda value: handoff.Backend(value, environment))
        )(selected)
        rows["route"] = backend.observe(
            selected["plan"]["stage"], selected["plan"]["after"]
        )
        rows["adapter"] = deployment(backend)
    except Exception:
        rows["route"] = None
    report["cases"] = _qualifications(report, config)
    return report


def _qualifications(report, config):
    handoff, _ = helpers()
    rows = report.get("observations", {})
    saved = report.get("rehearsal")
    journal = saved if type(saved) is dict else {}
    require(
        not journal
        or (
            journal.get("schema") == "adapter-release-rehearsal/v1"
            and journal.get("manifest") == config
        )
    )
    require(
        not journal
        or (
            journal.get("owner", {}).get("ADP_SOURCE_REVISION")
            == config["handoff"]["plan"]["after"]["release"]["source_revision"]
            and re.fullmatch(r"[1-9][0-9]*", journal.get("original_run_id", ""))
        )
    )
    events = journal.get("observations", {})
    original = config["handoff"]["plan"]
    final = (
        redeployment(config["handoff"])["plan"]["after"]
        if journal
        else original["after"]
    )
    try:
        current = rows["adapter"]
        replicas = replica_snapshot(current)["replicas"]
        containers = [
            entry
            for entry in current["spec"]["template"]["spec"]["containers"]
            if entry["name"] == "tool-adapters"
        ]
        current_ready = (
            replicas > 0
            and len(containers) == 1
            and containers[0]["image"].endswith("@" + final["release"]["image_digest"])
            and current["status"].get("observedGeneration", 0)
            >= current["metadata"]["generation"]
            and all(
                current["status"].get(key, 0) == replicas
                for key in (
                    "replicas",
                    "updatedReplicas",
                    "readyReplicas",
                    "availableReplicas",
                )
            )
        )
        if journal:
            current_ready = (
                current_ready
                and replicas == events["outage_original"]["replicas"]
                and current["metadata"]["uid"] == events["outage_original"]["uid"]
            )
    except (KeyError, TypeError, ValueError):
        current_ready = False
    checks = {
        "adapter-four-routes": rows.get("route") == final and current_ready,
        "forged-direct-denials": denied(rows.get("denials")),
    }

    def transition(label, expected, phase):
        event = events.get(label, {})
        state = event.get("state", {})
        return (
            event.get("route") == expected
            and healthy(event.get("health"))
            and state.get("status") == expected["status"]
            and state.get("release") == expected["release"]
            and state.get("transition", {}).get("phase") == phase
        )

    interruption = events.get("interruption", {})
    checks["partial-release-resume"] = (
        interruption.get("route") == original["before"]
        and interruption.get("state", {}).get("status") == "fenced"
        and interruption.get("state", {}).get("transition", {}).get("phase") == "fenced"
        and healthy(interruption.get("health"))
        and transition("handoff", original["after"], "active")
    )
    before = events.get("outage_original")
    down, restored = events.get("outage_down", {}), events.get("outage_restored", {})
    try:
        outage = (
            type(before) is dict
            and before["replicas"] > 0
            and down.get("replicas") == 0
            and replica_snapshot(down["deployment"]) == {**before, "replicas": 0}
            and restored.get("replicas") == before["replicas"]
            and replica_snapshot(restored["deployment"]) == before
            and healthy(events.get("outage_health"))
        )
        for observed, count in ((down, 0), (restored, before["replicas"])):
            value = observed["deployment"]
            status = value.get("status", {})
            outage = (
                outage
                and status.get("observedGeneration", 0)
                >= value["metadata"]["generation"]
                and all(
                    status.get(key, 0) == count
                    for key in (
                        "replicas",
                        "updatedReplicas",
                        "readyReplicas",
                        "availableReplicas",
                    )
                )
            )
    except (KeyError, TypeError, ValueError):
        outage = False
    checks["core-health-through-outage"] = outage
    checks["group-rollback-redeploy"] = (
        transition("rollback", handoff.restored(original), "restored")
        and transition("redeploy", final, "active")
        and transition("final", final, "active")
        and journal.get("cleanup") == "confirmed"
        and rows.get("route") == final
    )
    checks["controller-restart-stale-lease"] = (
        checks["partial-release-resume"]
        and checks["group-rollback-redeploy"]
        and events.get("stale")
        == {"path": "/api/tools/adapter-release/transition", "status": 409}
    )
    return [
        {
            "case": name,
            "status": "pass"
            if checks.get(name) and healthy(rows.get("health"))
            else "pending",
            "reason": None
            if checks.get(name) and healthy(rows.get("health"))
            else "original-independent-release-observations-required"
            if name in checks
            else "independent-native-provider-parity-join-required",
        }
        for name in CASES
    ]


def qualify_release(report, *, manifest, binding):
    flow.validate_binding(binding)
    config = globals()["manifest"](
        manifest, account=manifest["account"], region=manifest["region"]
    )
    require(
        report.get("schema") == "adapter-release-observations/v1"
        and report.get("binding") == binding
        and report.get("manifest_sha256") == digest(config)
        and config["handoff"]["plan"]["after"]["release"]["source_revision"]
        == binding["source_revision"]
    )
    expected = _qualifications(report, config)
    require(
        report.get("cases") == expected,
        "Release results differ from original observations",
    )
    return {
        "binding": binding,
        "cases": expected,
        "full_ac06": "pending",
        "evidence_sha256": digest(report),
    }


def qualify_handoff(report, *, manifest, binding, qualified_scenarios):
    require(type(qualified_scenarios) is set and qualified_scenarios <= set(flow.FLOW_SCENARIOS))
    result = qualify_release(report, manifest=manifest, binding=binding)
    dependencies = {f"SCN-8454-{number:02}" for number in range(2, 6)}
    cases = deepcopy(result["cases"])
    return {**result, "cases": cases,
            "qualified_dependencies": sorted(dependencies & qualified_scenarios)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--journal", required=True)
    args = parser.parse_args(argv)
    try:
        rehearse_release(args.config, args.journal)
    except Exception:
        raise SystemExit(
            "Release rehearsal incomplete; retain the original journal and verify cleanup before resuming"
        ) from None
    print(
        "Release infrastructure rehearsal completed; provider/client acceptance remains separately qualified"
    )


if __name__ == "__main__":
    main()
