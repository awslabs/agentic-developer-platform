"""The two-story delivery adapter, using existing operator/provider interfaces."""

from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
import time

from tests.e2e.orchestration.fixtures import FixtureRequest, provision, readback
from tests.e2e.orchestration.report import (
    Artifact,
    Intervention,
    Result,
    ScenarioReport,
    now,
)
from .definitions import (
    CRITERIA,
    DEFINITION_HASH,
    STORIES,
    TESTS,
    definition,
    fixture_test,
)
from .faults import CASES, execute_case, inject
from .http import Client, Unsupported
from .manifest import load_manifest, verify_checkout
from .providers import IssueProvider


def build_plan(config, manifest, qualification_id, issues):
    """A fixed chain. An operator supplies policy/evaluation, never graph edges."""
    prefix = f"{qualification_id}/delivery/qualification/"
    nodes = [
        dict(
            address=prefix + "first",
            kind="story",
            title="Integer discount implementation",
            issue_ref=str(issues[0]),
        ),
        dict(
            address=prefix + "verify",
            kind="eval",
            title="Verify deployed discount and UI/API parity",
            evaluation=manifest.evaluation,
        ),
        dict(
            address=prefix + "release",
            kind="gate",
            title="Planned human release of dependent story",
        ),
        dict(
            address=prefix + "second",
            kind="story",
            title="Dependent quote implementation",
            issue_ref=str(issues[1]),
        ),
    ]
    policy = deepcopy(manifest.execution_policy)
    # The human graph gate is always retained, regardless of machine E2 mode.
    policy["evaluation_acceptance"] = {prefix + "verify": "machine"}
    return dict(
        flow_slug=qualification_id,
        title=f"[{qualification_id}] Bounded delivery qualification",
        org_id=config.org_ref,
        spec_revision=DEFINITION_HASH,
        execution_policy=policy,
        nodes=nodes,
        edges=[
            dict(from_address=a["address"], to_address=b["address"])
            for a, b in zip(nodes, nodes[1:])
        ],
    )


class FlowProvider:
    kind = "qualification-flow"
    isolation = "new-interpreter"

    def __init__(self, client, inventory=None, plan=None):
        self.client, self.inventory, self.plan = client, inventory, plan
        self.native = None
        self.execution_id = None

    def inject(self, name, resource_id):
        if (
            self.native is None
            or self.execution_id is None
            or resource_id != self.native.session.flow_id
        ):
            raise Unsupported("no isolated process bound to this fixture flow")
        return self.native.inject(name, self.execution_id)

    def create(self, *, intended_identity, ownership_tags, idempotency_token):
        qualification_id = ownership_tags["adp:qualification-id"]
        if self.plan is None or self.plan["flow_slug"] not in {
            qualification_id,
            qualification_id + "-refusal",
        }:
            raise ValueError("flow plan is not pinned to inventory")
        status, response = self.client.request(
            "POST", "/orchestration/flows", body=self.plan
        )
        if status not in {200, 201}:
            raise Unsupported(f"plan submission HTTP {status}; intent retained")
        self.response = response
        (
            self.inventory.path.parent / (self.plan["flow_slug"] + "-submission.json")
        ).write_text(json.dumps(response, indent=2))
        return response["flow_id"]

    def find(self, *, intended_identity, idempotency_token):
        # No blind repost following an unknown creation response. A human may
        # reconcile through the published list API and the saved plan.
        raise Unsupported(
            "flow creation outcome needs explicit reconciliation; do not repost"
        )

    def read_tags(self, resource_id):
        graph = self.client.get(f"/orchestration/flows/{resource_id}")
        slug = graph["slug"]
        if not slug.startswith("q-"):
            return None
        plans = self.client.get(f"/orchestration/flows/{resource_id}/plans")
        if not any(
            p["plan_document"].get("spec_revision") == DEFINITION_HASH for p in plans
        ):
            return None
        return self.client.config.ownership_tags(slug.removesuffix("-refusal"))

    def delete(self, resource_id):
        raise Unsupported(
            "accepted flow is durable audit state; no published flow-delete API; retain in cleanup inventory"
        )


class Evidence:
    def __init__(self, inventory):
        self.root = inventory.path.parent
        self.counter = 0

    def save(self, kind, data, source):
        self.counter += 1
        name = f"evidence/{self.counter:04d}-{kind}.json"
        path = self.root / name
        path.parent.mkdir(exist_ok=True)
        raw = json.dumps(data, sort_keys=True, indent=2).encode()
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("evidence size limit exceeded")
        path.write_bytes(raw)
        return Artifact(
            path=name,
            sha256=hashlib.sha256(raw).hexdigest(),
            source=source,
            observed_at=now(),
        )


class LiveSession:
    """Actual service reads and bounded submission. No coordinator retry loop."""

    live = True

    def __init__(self, config, manifest, inventory, providers):
        self.config, self.manifest, self.inventory, self.providers = (
            config,
            manifest,
            inventory,
            providers,
        )
        self.client = Client(config, manifest)
        self.evidence = Evidence(inventory)
        self.started = time.monotonic()
        self.client.deadline = self.started + config.max_duration_seconds
        self.flow_id = None
        self.samples = []
        self.runtime_observations = {}
        self.parity_observation = None
        self.observation_errors = {}
        self.actual_versions = {"engine": "unobserved", "worker": "unobserved"}
        self.interventions = []
        self.fault_observations = {}
        self.fault_errors = {}

    def start(self):
        from .runtime import read_runtime
        from .delivery_resources import plan_resources

        plan_resources(self)

        for component, target in self.manifest.runtime.items():
            observed = read_runtime(self.client, target)
            self.actual_versions[component] = observed["actual_revision"]
            self.evidence.save(
                "prerequisite-runtime", observed, "registered-role:EKS/ECR"
            )
        if self.versions() != self.config.versions:
            raise Unsupported(
                "deployed prerequisite or executing harness differs from approved revisions"
            )
        issues = []
        provider = self.providers[IssueProvider.kind]
        provider.client = self.client
        for index in (1, 2):
            request = FixtureRequest(
                f"story-{index}",
                provider.kind,
                f"{self.inventory.qualification_id}/story-{index}",
            )
            record = provision(self.inventory, self.config, provider, request)
            readback(
                self.inventory,
                provider,
                request.fixture_id,
                self.config.ownership_tags(self.inventory.qualification_id),
            )
            issues.append(record.observed_resource_id)
        plan = build_plan(
            self.config, self.manifest, self.inventory.qualification_id, issues
        )
        # Pin the complete plan and fixed tests BEFORE accepting it at the API.
        self.plan_artifact = self.evidence.save(
            "plan", plan, "reviewed-harness:build_plan"
        )
        flow_provider = FlowProvider(self.client, self.inventory, plan)
        self.providers[flow_provider.kind] = flow_provider
        record = provision(
            self.inventory,
            self.config,
            flow_provider,
            FixtureRequest(
                "flow", flow_provider.kind, self.inventory.qualification_id + "/flow"
            ),
        )
        self.flow_id = record.observed_resource_id
        self.accepted = flow_provider.response
        from .native_probe import NativeProbe

        flow_provider.native = NativeProbe(self)
        readback(
            self.inventory,
            flow_provider,
            "flow",
            self.config.ownership_tags(self.inventory.qualification_id),
        )
        return plan

    def snapshot(self):
        path = f"/orchestration/flows/{self.flow_id}"
        data = {
            "graph": self.client.get(path),
            "plans": self.client.get(path + "/plans"),
            "decisions": self.client.get(path + "/decisions"),
            "cost": self.client.get(path + "/cost"),
        }
        current = [p for p in data["plans"] if p["superseded_at"] is None]
        assert len(current) == 1
        assert (current[0]["version"], current[0]["plan_hash"]) == (
            self.accepted["plan_version"],
            self.accepted["plan_hash"],
        )
        page = self.client.get(path + "/execution?limit=100&offset=0")
        executions = list(page["executions"])
        while len(executions) < page["total"]:
            if len(executions) >= 1000:
                raise Unsupported("execution evidence overflow")
            more = self.client.get(
                path + f"/execution?limit=100&offset={len(executions)}"
            )["executions"]
            if not more:
                raise Unsupported("incomplete execution pagination")
            executions.extend(more)
        if any(row["action_overflow"] for row in executions):
            raise Unsupported("action evidence overflow")
        data["executions"] = executions
        from .delivery_resources import record_resources

        record_resources(self, data)
        data["observed_at"] = now()
        self.samples.append(data)
        self.evidence.save("snapshot", data, self.manifest.api_origin + path)
        return data

    def await_completion(self):
        while True:
            data = self.snapshot()
            nodes = data["graph"]["nodes"]
            by_ref = {n["node_ref"]: n for n in nodes}
            self.observe_faults(data)
            self.observe_worker_loss(data)
            self.observe_wait_and_ci(data)
            from .runtime import read_runtime
            from .parity import observe

            for ref in ("first", "second"):
                if (
                    by_ref[ref]["state"] == "passed"
                    and ref not in self.runtime_observations
                ):
                    try:
                        self.runtime_observations[ref] = read_runtime(
                            self.client, self.manifest.runtime["engine"]
                        )
                    except (Unsupported, AssertionError, KeyError) as exc:
                        self.observation_errors[ref] = type(exc).__name__
            if (
                by_ref["verify"]["state"] == "passed"
                and by_ref["release"]["state"] != "passed"
                and self.parity_observation is None
            ):
                try:
                    runtime = read_runtime(self.client, self.manifest.runtime["engine"])
                    if (
                        "first" not in self.runtime_observations
                        or runtime["actual_revision"]
                        != self.runtime_observations["first"]["actual_revision"]
                    ):
                        raise Unsupported(
                            "parity target differs from the first story deployment"
                        )
                    self.parity_observation = observe(self.client, self.flow_id)
                    after = read_runtime(self.client, self.manifest.runtime["engine"])
                    if (after["actual_revision"], after["digest"]) != (
                        runtime["actual_revision"],
                        runtime["digest"],
                    ):
                        self.parity_observation = None
                        raise Unsupported("runtime changed during UI/API comparison")
                    self.parity_observation["api"] = {
                        "runtime": runtime,
                        "graph": self.parity_observation["api"],
                    }
                except (Unsupported, AssertionError, KeyError) as exc:
                    self.observation_errors["parity"] = type(exc).__name__
            cost = data["cost"]
            if (
                cost["status"] == "known"
                and not cost["partial"]
                and float(cost["amount_usd"]) > self.config.max_usd
            ):
                raise AssertionError("qualification spend bound exceeded")
            if any(
                n["state"] in {"failed", "halted", "rejected_at_gate"} for n in nodes
            ):
                return data
            if nodes and all(n["state"] == "passed" for n in nodes):
                return data
            if (
                time.monotonic() - self.started + self.manifest.poll_seconds
                >= self.config.max_duration_seconds
            ):
                return data
            time.sleep(self.manifest.poll_seconds)

    def pull_requests(self, data):
        result = []
        for ref in ("first", "second"):
            node = next(n for n in data["graph"]["nodes"] if n["node_ref"] == ref)
            binding = node.get("bound_pull_request") or {}
            if binding.get("repo") != self.config.repository:
                raise Unsupported(
                    "bound pull request does not belong to the approved repository"
                )
            number = binding.get("pr_number") or binding.get("number")
            if not isinstance(number, int):
                raise Unsupported(f"{ref}: genuine pull-request binding unavailable")
            path = f"/repos/{self.config.repository}/pulls/{number}"
            pr = self.client.get(path, github=True)
            assert binding["head_sha"] == pr["head"]["sha"]
            test_path = (
                f"modules/gateway/tests/qualification/{self.inventory.qualification_id}/"
                + ("test_pricing.py" if ref == "first" else "test_quote.py")
            )
            content = self.client.get(
                f"/repos/{self.config.repository}/contents/{test_path}?ref={pr['head']['sha']}",
                github=True,
            )
            result.append(
                {
                    "pr": pr,
                    "reviews": self.client.pages(path + "/reviews"),
                    "files": self.client.pages(path + "/files"),
                    "test_definition": base64.b64decode(content["content"]).decode(),
                    "checks": self.client.pages(
                        f"/repos/{self.config.repository}/commits/{pr['head']['sha']}/check-runs",
                        key="check_runs",
                    ),
                }
            )
        return result

    def exercise(self, name):
        from .capabilities import UNAVAILABLE, unavailable

        if name == "human-refusal":
            return self.human_refusal()
        if name in self.fault_observations:
            return self.fault_observations[name]
        if name in self.fault_errors:
            raise Unsupported(self.fault_errors[name])
        if name in UNAVAILABLE:
            return unavailable(name, self)
        # Named adapters need a disposable provider that can prove the current
        # ownership generation at the native boundary. Never target the shared
        # tick, synthesize a webhook, or turn a missing capability into PASS.
        raise Unsupported(
            f"{name}: environment has no registered fixture-scoped injection/evidence adapter"
        )

    def observe_faults(self, data):
        """Inject at live execution boundaries, never after declaring completion."""
        if not self.manifest.native_faults:
            return
        first = next(n for n in data["graph"]["nodes"] if n["node_ref"] == "first")
        entries = [e for e in data["executions"] if e["node_id"] == first["id"]]
        if not entries:
            return
        execution = max(entries, key=lambda e: e["cycle"])
        provider = self.providers[FlowProvider.kind]
        provider.execution_id = execution["id"]
        ready = {
            "competing-launches": first.get("activity", {}).get("liveness") == "live"
            if first.get("activity")
            else False,
            "tick-restart": execution["phase"] == "awaiting_review"
            and execution["status"] == "runnable",
            "missed-wakeup": execution["phase"] == "awaiting_review"
            and bool(execution.get("next_check_at")),
            "timeout-after-success": execution["phase"] == "deployment_pending"
            and execution["status"] == "runnable",
            "duplicate-events": any(
                a["resolved"] and a["receipt_ref"] for a in execution["actions"]
            ),
            "out-of-order": sum(
                a["resolved"] and bool(a["receipt_ref"]) for a in execution["actions"]
            )
            >= 2,
        }
        for name in ("tick-restart", "missed-wakeup", "timeout-after-success"):
            if name in self.fault_observations:
                try:
                    after = provider.native.request(execution["id"], "read")["after"]
                    self.fault_observations[name]["after"] = after
                    if name == "timeout-after-success":
                        observation = self.fault_observations[name]
                        remote = observation["injection"].get("remote")
                        if not remote:
                            continue
                        action = next(
                            a
                            for a in after["actions"]
                            if a["operation_key"] == remote["operation_key"]
                        )
                        if action.get("correlation") and action.get("workflow_run_id"):
                            runs = self.client.pages(
                                f"/repos/{self.config.repository}/actions/runs?head_sha={action['source_revision']}",
                                key="workflow_runs",
                            )
                            observation["remote"] = {
                                "correlation": action["correlation"],
                                "runs": [
                                    r
                                    for r in runs
                                    if r["display_title"]
                                    == "ADP deployment " + action["correlation"]
                                ],
                            }
                except (Unsupported, KeyError, StopIteration):
                    pass  # The saved injection remains; incomplete reconciliation never passes.
        for name, reached in ready.items():
            if (
                not reached
                or name in self.fault_observations
                or name in self.fault_errors
            ):
                continue
            if (
                len(self.fault_observations) + len(self.fault_errors)
                >= self.config.max_runs
            ):
                self.fault_errors[name] = "native fault attempt bound exhausted"
                continue
            intent = self.evidence.save(
                "fault-intent",
                {"name": name, "execution_id": execution["id"]},
                "harness:predeclared-fault",
            )
            self.interventions.append(
                Intervention(
                    at=intent.observed_at,
                    actor="qualification-harness",
                    kind="fault",
                    target=name,
                    evidence=intent,
                )
            )
            try:
                observed = inject(
                    CASES["A6-3." + name],
                    fixture_id="flow",
                    inventory=self.inventory,
                    config=self.config,
                    providers=self.providers,
                )
                self.fault_observations[name] = observed
                self.evidence.save(
                    "fault-result", observed, "native-K1/K2:registered-role"
                )
            except Exception as exc:
                self.fault_errors[name] = (
                    f"{name}: native injection/recovery unverified ({type(exc).__name__})"
                )
            break  # one injection per fresh service snapshot

    def observe_worker_loss(self, data):
        if not (self.manifest.worker_loss and self.manifest.native_faults):
            return
        first = next(n for n in data["graph"]["nodes"] if n["node_ref"] == "first")
        entries = [e for e in data["executions"] if e["node_id"] == first["id"]]
        if not entries or "worker-loss" in self.fault_errors:
            return
        execution = max(entries, key=lambda e: e["cycle"])
        native = self.providers[FlowProvider.kind].native
        if "worker-loss" in self.fault_observations:
            try:
                self.fault_observations["worker-loss"]["after"] = native.request(
                    execution["id"], "read"
                )["after"]
            except Unsupported:
                pass  # Keep the original injection receipt; missing recovery cannot pass.
            return
        if not first.get("activity") or first["activity"]["liveness"] != "live":
            return
        from .workers import WorkerProvider

        provider = WorkerProvider(self)
        provider.node = first
        self.providers[provider.kind] = provider
        try:
            before = native.request(execution["id"], "read")["after"]
            record = provision(
                self.inventory,
                self.config,
                provider,
                FixtureRequest(
                    "worker-loss",
                    provider.kind,
                    self.inventory.qualification_id + "/worker-loss",
                ),
            )
            intent = self.evidence.save(
                "worker-loss-intent",
                {"resource_id": record.observed_resource_id},
                "harness:predeclared-worker-loss",
            )
            self.interventions.append(
                Intervention(
                    at=intent.observed_at,
                    actor="qualification-harness",
                    kind="fault",
                    target="worker-loss",
                    evidence=intent,
                )
            )
            result = inject(
                CASES["A6-3.worker-loss"],
                fixture_id="worker-loss",
                inventory=self.inventory,
                config=self.config,
                providers=self.providers,
            )
            self.fault_observations["worker-loss"] = {
                "before": before,
                "injection": result,
            }
            self.evidence.save(
                "worker-loss",
                result,
                "registered-role:Kubernetes-delete-with-UID-precondition",
            )
        except Exception as exc:
            self.fault_errors["worker-loss"] = (
                f"worker loss unavailable or unverified ({type(exc).__name__})"
            )

    def observe_wait_and_ci(self, data):
        if not self.manifest.native_faults:
            return
        first = next(n for n in data["graph"]["nodes"] if n["node_ref"] == "first")
        entries = [e for e in data["executions"] if e["node_id"] == first["id"]]
        if not entries:
            return
        execution = max(entries, key=lambda e: e["cycle"])
        native = self.providers[FlowProvider.kind].native
        try:
            if "wait-exit" in self.fault_observations:
                self.fault_observations["wait-exit"]["after"] = native.request(
                    execution["id"], "read"
                )["after"]
            elif execution["phase"] == "awaiting_review":
                history = first.get("execution_history") or {}
                completed = [
                    r
                    for r in history.get("runs", [])
                    if r["persona"] == "developer"
                    and r["status"] == "complete"
                    and r["liveness"] == "exited"
                ]
                if history.get("history_complete") and completed:
                    invocation = self.client.get(
                        "/me/agent-invocations/" + completed[0]["invocation_id"]
                    )
                    if (
                        invocation["correlation_id"] != first["run_id"]
                        or invocation["repo"] != self.config.repository
                    ):
                        raise Unsupported("normal worker exit has a foreign lineage")
                    before = native.request(execution["id"], "read")["after"]
                    self.fault_observations["wait-exit"] = {
                        "before": before,
                        "injection": invocation,
                    }
                    self.evidence.save(
                        "normal-worker-exit",
                        invocation,
                        "gateway:authenticated-invocation-read",
                    )
            binding = first.get("bound_pull_request") or {}
            if (
                "failed-ci" not in self.fault_observations
                and binding.get("repo") == self.config.repository
            ):
                pr = self.client.get(
                    f"/repos/{self.config.repository}/pulls/{binding['pr_number']}",
                    github=True,
                )
                checks = self.client.pages(
                    f"/repos/{self.config.repository}/commits/{pr['head']['sha']}/check-runs",
                    key="check_runs",
                )
                if any(
                    c["name"] in self.manifest.required_checks
                    and c["conclusion"] == "failure"
                    for c in checks
                ):
                    current = native.request(execution["id"], "read")["after"]
                    self.fault_observations["failed-ci"] = {
                        "injection": {
                            "head_sha": pr["head"]["sha"],
                            "source": "pinned real-code fixture CI",
                        },
                        "checks": checks,
                        "required_checks": self.manifest.required_checks,
                        "after": {
                            "execution": current,
                            "pull_request": pr,
                            "graph": data["graph"],
                        },
                    }
                    self.evidence.save(
                        "failed-required-ci",
                        self.fault_observations["failed-ci"],
                        "github:check-runs/gateway:execution",
                    )
        except Unsupported:
            pass  # Incomplete observations remain NOT_RUN, not an inferred success.

    def human_refusal(self):
        """Exercise the real gate API on a disposable graph that dispatches no work."""
        slug = self.inventory.qualification_id + "-refusal"
        prefix = slug + "/controls/refusal/"
        nodes = [
            dict(address=prefix + ref, kind="gate", title=title)
            for ref, title in (
                ("refuse", "Planned qualification refusal"),
                ("successor", "Must remain pending after refusal"),
            )
        ]
        policy = deepcopy(self.manifest.execution_policy)
        policy["evaluation_acceptance"] = {}
        plan = dict(
            flow_slug=slug,
            title=f"[{slug}] Human refusal boundary",
            org_id=self.config.org_ref,
            spec_revision=DEFINITION_HASH,
            nodes=nodes,
            execution_policy=policy,
            edges=[
                dict(from_address=nodes[0]["address"], to_address=nodes[1]["address"])
            ],
        )
        provider = FlowProvider(self.client, self.inventory, plan)
        record = provision(
            self.inventory,
            self.config,
            provider,
            FixtureRequest(
                "refusal", provider.kind, self.inventory.qualification_id + "/refusal"
            ),
        )
        readback(
            self.inventory,
            provider,
            "refusal",
            self.config.ownership_tags(self.inventory.qualification_id),
        )
        path = f"/orchestration/flows/{record.observed_resource_id}"
        while True:
            before = self.client.get(path)
            gate = next(n for n in before["nodes"] if n["node_ref"] == "refuse")
            if gate["state"] == "awaiting_gate":
                break
            if (
                time.monotonic() - self.started + self.manifest.poll_seconds
                >= self.config.max_duration_seconds
            ):
                raise Unsupported(
                    "refusal fixture did not reach its human gate within the accepted duration"
                )
            time.sleep(self.manifest.poll_seconds)
        intent = self.evidence.save(
            "refusal-intent",
            {"gate_id": gate["id"], "operation": "reject"},
            "harness:planned-control",
        )
        self.interventions.append(
            Intervention(
                at=intent.observed_at,
                actor=self.config.identity_ref,
                kind="planned_gate",
                target="refusal",
                evidence=intent,
            )
        )
        status, result = self.client.request(
            "POST",
            f"/orchestration/gates/{gate['id']}/reject",
            body={
                "reason": "Predeclared qualification refusal; successor must remain pending"
            },
        )
        assert status == 200 and result["actor_kind"] == "human"
        graph = self.client.get(path)
        executions = self.client.get(path + "/execution")
        decisions = self.client.get(path + "/decisions")
        decision = next(d for d in decisions if d["id"] == result["decision_id"])
        return {
            "before": before,
            "decisions": decision,
            "graph": graph,
            "after": executions,
        }

    def versions(self):
        # Git proves the actual executing harness; deployment revisions require
        # runtime evidence and remain unknown until a scoped adapter supplies it.
        self.checkout_revision = verify_checkout(self.config)
        # Config can be committed after the reviewed harness. Its source must be
        # byte-identical to that pinned code revision; record actual checkout too.
        return {"harness": self.config.versions["harness"], **self.actual_versions}

    def deployments(self, prs):
        observations = []
        for ref, row in zip(("first", "second"), prs):
            pr = row["pr"]
            if ref not in self.runtime_observations:
                raise Unsupported(
                    f"{ref}: actual runtime not observed at the delivery barrier"
                )
            runtime = self.runtime_observations[ref]
            assert runtime["actual_revision"] == pr["merge_commit_sha"]
            runs = self.client.pages(
                f"/repos/{self.config.repository}/actions/runs?head_sha={pr['merge_commit_sha']}",
                key="workflow_runs",
            )
            deployments = [
                r for r in runs if r["path"] in self.manifest.deployment_workflows
            ]
            assert {r["path"] for r in deployments} == set(
                self.manifest.deployment_workflows
            )
            assert all(
                r["head_sha"] == pr["merge_commit_sha"] and r["conclusion"] == "success"
                for r in deployments
            )
            observations.append(
                {
                    "source_revision": pr["merge_commit_sha"],
                    "runtime": runtime,
                    "workflows": deployments,
                }
            )
        return observations


def assert_review_repair(rows, required_checks):
    assert len(rows) == 2
    for row in rows:
        pr = row["pr"]
        assert pr["merged"] and pr["merge_commit_sha"] and pr["merged_at"]
        reviews = sorted(row["reviews"], key=lambda r: r["submitted_at"])
        author = pr["user"]["id"]
        approvals = [
            r
            for r in reviews
            if r["state"] == "APPROVED"
            and r["commit_id"] == pr["head"]["sha"]
            and r["user"]["id"] != author
        ]
        assert approvals and approvals[-1]["submitted_at"] <= pr["merged_at"]
        latest = {
            r["user"]["id"]: r
            for r in reviews
            if r["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
        }
        assert not any(r["state"] == "CHANGES_REQUESTED" for r in latest.values())
        assert any(r in approvals for r in latest.values())
        checks = {c["name"]: c for c in sorted(row["checks"], key=lambda c: c["id"])}
        assert set(required_checks) <= checks.keys()
        assert all(
            checks[name]["head_sha"] == pr["head"]["sha"]
            and checks[name]["conclusion"] == "success"
            for name in required_checks
        )
    first = rows[0]
    changes = [
        r
        for r in first["reviews"]
        if r["state"] == "CHANGES_REQUESTED"
        and r["user"]["id"] != first["pr"]["user"]["id"]
    ]
    assert changes and any(
        r["commit_id"] != first["pr"]["head"]["sha"] for r in changes
    )
    assert any(
        r["submitted_at"] < a["submitted_at"]
        for r in changes
        for a in first["reviews"]
        if a["state"] == "APPROVED"
    )


def assert_dependency(samples):
    assert samples
    saw_blocked = False
    for sample in samples:
        nodes = {n["node_ref"]: n for n in sample["graph"]["nodes"]}
        if not nodes["second"]["run_id"]:
            saw_blocked = True
        else:
            assert (
                nodes["first"]["state"]
                == nodes["verify"]["state"]
                == nodes["release"]["state"]
                == "passed"
            )
    assert saw_blocked and samples[-1]["graph"]["nodes"]
    nodes = {n["node_ref"]: n for n in samples[-1]["graph"]["nodes"]}
    assert nodes["second"]["state"] == "passed" and nodes["second"]["run_id"]
    assert any(
        d["node_id"] == nodes["release"]["id"]
        and d["actor_kind"] == "human"
        and d["to_state"] == "passed"
        for d in samples[-1]["decisions"]
    )


def execute(config, inventory, providers, *, session_factory=LiveSession):
    if not __debug__:
        raise RuntimeError("qualification assertions require Python without -O")
    manifest, manifest_hash = load_manifest(config)
    started_at = now()
    evidence = Evidence(inventory)
    pinned = {
        "definition": definition(),
        "manifest": manifest.model_dump(mode="json"),
        "stories": STORIES,
        "tests": TESTS,
    }
    evidence.save("accepted-inputs", pinned, "reviewed-harness:pre-execution")
    session = session_factory(config, manifest, inventory, providers)
    session.evidence = evidence
    results = {
        c.id: Result(id=c.id, status="NOT_RUN", detail="Not reached") for c in CRITERIA
    }
    policy, plan_version, prs, deployments, spend = {}, None, [], [], None

    def check(criterion_id, operation):
        try:
            artifacts, ids = operation()
            results[criterion_id] = Result(
                id=criterion_id,
                status="PASS",
                detail="Observed required assertions",
                evidence=artifacts,
                **ids,
            )
        except Unsupported as exc:
            results[criterion_id] = Result(
                id=criterion_id,
                status="NOT_RUN",
                detail=str(exc),
                evidence={"capability": exc.evidence}
                if hasattr(exc, "evidence")
                else {},
            )
        except (AssertionError, ValueError, KeyError, StopIteration, TypeError):
            results[criterion_id] = Result(
                id=criterion_id,
                status="FAIL",
                detail="Observed evidence did not satisfy the fixed criterion",
            )

    try:
        session.start()
        data = session.await_completion()
        current = [p for p in data["plans"] if p["superseded_at"] is None]
        if len(current) == 1:
            policy = current[0]["plan_document"].get("execution_policy") or {}
            plan_version = current[0]["version"]
        cost = data["cost"]
        if cost["status"] == "known" and not cost["partial"]:
            spend = float(cost["amount_usd"])

        def development():
            nonlocal prs
            prs = session.pull_requests(data)
            for index, row in enumerate(prs):
                assert row["test_definition"] == fixture_test(
                    index, inventory.qualification_id
                )
                files = {f["filename"] for f in row["files"]}
                assert (
                    f"modules/gateway/src/qualification/{inventory.qualification_id}/"
                    + ("pricing.py" if index == 0 else "quote.py")
                    in files
                )
                assert any(
                    "test" in f
                    and f.startswith(
                        f"modules/gateway/tests/qualification/{inventory.qualification_id}/"
                    )
                    for f in files
                )
            return {
                "plan": session.plan_artifact,
                "code": evidence.save(
                    "files", [r["files"] for r in prs], "github:pull-files"
                ),
                "tests": evidence.save(
                    "tests", [r["checks"] for r in prs], "github:check-runs"
                ),
            }, {}

        check("A6-2.development", development)

        def review():
            assert_review_repair(prs, manifest.required_checks)
            return {
                kind: evidence.save(kind, prs, "github:pulls/reviews/check-runs")
                for kind in ("reviews", "pull_request", "checks")
            }, {}

        check("A6-2.review-repair", review)

        def deploy():
            nonlocal deployments
            assert len(prs) == 2
            deployments = session.deployments(prs)
            return {
                "pull_request": evidence.save(
                    "pull_request", [r["pr"] for r in prs], "github:pulls"
                ),
                "deployment": evidence.save(
                    "deployment", deployments, "github:workflow-runs"
                ),
                "runtime": evidence.save(
                    "runtime", session.runtime_observations, "registered-role:EKS/ECR"
                ),
            }, {}

        check("A6-2.merge-deploy", deploy)

        def parity():
            if session.parity_observation is None:
                raise Unsupported(
                    "UI/API parity was not observed before the dependent-story release"
                )
            return {
                kind: evidence.save(kind, value, "playwright:real-navigation/API")
                for kind, value in session.parity_observation.items()
            }, {}

        check("A6-2.parity", parity)

        def dependency():
            assert_dependency(session.samples)
            return {
                kind: evidence.save(kind, data[key], "gateway:flow-readback")
                for kind, key in (
                    ("graph", "graph"),
                    ("executions", "executions"),
                    ("decisions", "decisions"),
                )
            }, {
                "run_ids": [n["run_id"] for n in data["graph"]["nodes"] if n["run_id"]],
                "action_ids": [
                    a["id"] for e in data["executions"] for a in e["actions"]
                ],
                "decision_ids": [d["id"] for d in data["decisions"]],
            }

        check("A6-2.dependency", dependency)

        def tenant():
            owner = session.client.get("/auth/me")
            foreign = session.client.get("/auth/me", actor="foreign")
            assert (
                owner["org_id"] != foreign["org_id"]
                and owner["user_id"] != foreign["user_id"]
            )
            status, body = session.client.request(
                "GET", f"/orchestration/flows/{session.flow_id}", actor="foreign"
            )
            assert status == 404
            return {
                "owner": evidence.save("owner", owner, "gateway:auth/me"),
                "foreign": evidence.save(
                    "foreign",
                    {"identity": foreign, "status": status, "body": body},
                    "gateway:foreign-flow-read",
                ),
            }, {}

        check("A6-4.tenant", tenant)
        for criterion_id, criterion in CASES.items():

            def fault(c=criterion):
                observation = execute_case(c, session)
                return {
                    kind: evidence.save(
                        kind, observation[kind], "fixture-scoped-service-boundary"
                    )
                    for kind in c.evidence
                }, {}

            check(criterion_id, fault)
    except Exception as exc:
        results["A6-2.development"] = Result(
            id="A6-2.development",
            status="NOT_RUN" if isinstance(exc, Unsupported) else "FAIL",
            detail=f"Delivery stopped ({type(exc).__name__}); inventory and evidence retained",
        )
    interventions_complete = False
    try:
        from .audit import collect

        audit = collect(session, prs)
        interventions_complete = audit["complete"]
        results["A6-6.interventions"] = Result(
            id="A6-6.interventions",
            status="FAIL"
            if any(
                i.kind in {"coordinator_retrigger", "unplanned"}
                for i in session.interventions
            )
            else "PASS"
            if interventions_complete
            else "NOT_RUN",
            detail="Authenticated decision, invocation, provider and harness intervention audit",
            evidence={
                "interventions": evidence.save(
                    "intervention-audit", audit, "gateway/github/harness"
                )
            },
            decision_ids=[d["id"] for d in audit["decisions"]],
        )
    except (Unsupported, AssertionError, ValueError, KeyError, TypeError):
        results["A6-6.interventions"] = Result(
            id="A6-6.interventions",
            status="NOT_RUN",
            detail="Complete authenticated intervention history unavailable; retained evidence cannot establish unattended completion",
        )
    inventory_artifact = evidence.save(
        "inventory",
        [r.to_json() for r in inventory.fixtures],
        "Q1:write-ahead-inventory",
    )
    results["A6-5.isolation"] = Result(
        id="A6-5.isolation",
        status="PASS" if inventory.fixtures else "FAIL",
        detail="Isolated inventory retained; cleanup is a separate recorded operation",
        evidence={"inventory": inventory_artifact},
    )
    return ScenarioReport(
        qualification_id=inventory.qualification_id,
        definition_hash=DEFINITION_HASH,
        manifest_hash=manifest_hash,
        live=session.live,
        started_at=started_at,
        completed_at=now(),
        versions=session.versions(),
        checkout_revision=getattr(session, "checkout_revision", None),
        policy_id=policy.get("policy_id"),
        policy_hash=policy.get("policy_hash"),
        plan_version=plan_version,
        pull_requests=prs,
        deployments=deployments,
        results=list(results.values()),
        spend_usd=spend,
        cleanup_inventory=[r.to_json() for r in inventory.fixtures],
        interventions=session.interventions,
        interventions_complete=interventions_complete,
        planned_gates=manifest.planned_gates,
    )
