"""Offline recovery selector checks; no worker is stopped in these tests."""

from copy import deepcopy
import httpx
import pytest

from recovery_live import LiveRecoveryDriver, validate_recovery_fixture
from recovery_scenarios import isolation_recovery_cleanup
from test_recovery import recovery


@pytest.fixture
def recovery_case(recovery):
    before, after, attempts, jobs, prior, later, repair = deepcopy(recovery)
    scope = before["scope"]
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "record_type": "document", "record_revision": "pinned-example"}
    before["input"].update(binding_id="binding-example", capability="threat_model", sources=[source])
    after["input"].update(binding_id="binding-example", capability="threat_model", sources=[source])
    selection = {"scope": scope, "sources": before["input"]["sources"],
                 "input": before["input"], "binding_id": "binding-example", "owned": ["owned-example"],
                 "linked": ["linked-example"], "max_attempts": 3}
    case = {"scope": scope, "selected": {"sources": [source], "binding_id": "binding-example"},
            "recovery": selection, "max_attempts": 3}
    foreign_case = {"scope": {**scope, "tenant_id": "foreign-tenant"}}
    return case, foreign_case, (before, after, attempts, jobs, prior, later, repair)


class RecordingRecovery:
    def __init__(self, evidence):
        self.before, self.after, self.attempts, self.jobs, self.prior, self.later, self.repair = evidence
        self.calls = []

    def check_selection(self, selection):
        self.calls.append("selection")

    def arm_faults(self, selection):
        self.calls.append("arm")

    def submit(self, selection):
        self.calls.append("submit")

    def read_faulted(self, selection):
        self.calls.append("before")
        return self.before, self.prior

    def retain_attempts(self, attempts):
        self.calls.append("journal")

    def restart_workers(self, selection):
        self.calls.append("restart")

    def read_recovered(self, selection):
        self.calls.append("after")
        return self.after, self.later, self.repair

    def scope_probes(self, run_id, selection, foreign_case):
        self.calls.append("denials")
        return [{"case": name, "status": 404, "disclosed_resources": []} for name in (
            "foreign_product", "foreign_tenant", "revoked_access")]

    def native_jobs(self, after):
        self.calls.append("native")
        return self.jobs

    def recorded_attempts(self):
        self.calls.append("recorded")
        return self.attempts

    def validate_cleanup_ownership(self, selection):
        self.calls.append("ownership")

    def cleanup_owned(self, selection):
        self.calls.append("cleanup")

    def cleanup_readback(self, selection):
        self.calls.append("cleanup_read")
        return {"owned-example": "absent", "linked-example": "retained"}


def test_scenario_orders_fault_restart_denials_and_owned_cleanup(recovery_case):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    summary = isolation_recovery_cleanup(case, foreign, driver)
    assert summary["recovery"]["run_id"] == "run-example"
    assert summary["cleanup"] == {"owned_absent": 1, "linked_retained": 1}
    assert driver.calls == ["selection", "arm", "submit", "before", "journal", "restart", "after", "journal",
                            "denials", "native", "recorded", "ownership", "cleanup", "cleanup_read"]


@pytest.mark.parametrize("defect", ["wrong_scope", "wrong_source", "overlapping_cleanup", "wrong_tenant"])
def test_invalid_fixture_never_arms_paid_work(recovery_case, defect):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    if defect == "wrong_scope":
        case["recovery"]["scope"] = {**case["scope"], "product_id": "foreign"}
    elif defect == "wrong_source":
        case["recovery"]["sources"] = [{**case["selected"]["sources"][0], "record_revision": "other-revision"}]
    elif defect == "overlapping_cleanup":
        case["recovery"]["owned"] = ["linked-example"]
    else:
        foreign["scope"]["tenant_id"] = case["scope"]["tenant_id"]
    with pytest.raises(AssertionError):
        isolation_recovery_cleanup(case, foreign, driver)
    assert driver.calls == []


@pytest.mark.parametrize("defect", ["missing_attempt", "foreign_result", "extra_paid_job", "false_publication"])
def test_recovery_fault_cannot_trigger_cleanup_on_unverified_state(recovery_case, defect):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    if defect == "missing_attempt":
        driver.after["attempts"] = []
    elif defect == "foreign_result":
        driver.after["scope"] = {**case["scope"], "tenant_id": "foreign"}
    elif defect == "extra_paid_job":
        driver.jobs.append(deepcopy(driver.jobs[0]))
    else:
        driver.later["receipt"] = None
    with pytest.raises(AssertionError):
        isolation_recovery_cleanup(case, foreign, driver)
    assert "cleanup" not in driver.calls


def test_disclosure_probe_rejects_denied_status_with_private_metadata(recovery_case):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    driver.scope_probes = lambda *args: [{"case": name, "status": 404,
                                         "disclosed_resources": ["run-example"] if name == "foreign_tenant" else []}
                                        for name in ("foreign_product", "foreign_tenant", "revoked_access")]
    with pytest.raises(AssertionError, match="disclosed private evidence"):
        isolation_recovery_cleanup(case, foreign, driver)
    assert "cleanup" not in driver.calls


def test_owned_cleanup_never_accepts_removed_linked_resource(recovery_case):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    driver.cleanup_readback = lambda selection: {"owned-example": "absent", "linked-example": "absent"}
    with pytest.raises(AssertionError, match="linked resource"):
        isolation_recovery_cleanup(case, foreign, driver)


def test_private_fault_fixture_refuses_checkout_journal_and_shared_authority(tmp_path, monkeypatch):
    import recovery_live

    common = {"fault_url": "https://private.example/fault", "authorized_faults": True,
              "exclusive_agent_space": True, "exclusive_worker_pool": True,
              "binding_id": "linked-binding", "owned_binding_id": "owned-binding",
              "owned": ["owned-example"], "linked": ["linked-example"],
              "max_attempts": 3, "receipt": "receipt-example-01", "foreign_product_id": "foreign-product"}
    names = ("fault", "revoked")
    for name in names:
        common[f"{name}_token_env"] = f"TEST_{name.upper()}_TOKEN"
        monkeypatch.setenv(common[f"{name}_token_env"], f"synthetic-{name}")
    case = {"scope": {"tenant_id": "tenant-example", "product_id": "product-example"},
            "api_token_env": "TEST_API_TOKEN",
            "recovery": {**common, "journal_file": str(tmp_path / "recovery.jsonl")}, "max_attempts": 3}
    monkeypatch.setenv("TEST_API_TOKEN", "synthetic-api")
    foreign = {"scope": {"tenant_id": "other-tenant"}}
    validate_recovery_fixture(case, foreign)
    snapshot = tmp_path / "recovery.jsonl.fault.json"
    snapshot.write_text('{}')
    snapshot.chmod(0o600)
    with pytest.raises(ValueError, match="private journal"):
        validate_recovery_fixture(case, foreign)
    snapshot.unlink()
    case["recovery"]["journal_file"] = str(recovery_live.ROOT / "private.jsonl")
    with pytest.raises(ValueError, match="private journal"):
        validate_recovery_fixture(case, foreign)
    case["recovery"]["journal_file"] = str(tmp_path / "recovery.jsonl")
    case["recovery"]["owned_binding_id"] = "linked-binding"
    with pytest.raises(ValueError, match="separate owned cleanup"):
        validate_recovery_fixture(case, foreign)



@pytest.mark.parametrize("fault", ["accepted", "missing_receipt", "missing_product", "changed_source",
                                     "changed_job", "missing_snapshot", "duplicate_attempt", "wrong_work"])
def test_recovery_reads_receipt_run_retained_sources_and_durable_attempts(monkeypatch, fault):
    import recovery_live

    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    source = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
              "record_type": "document", "record_id": "document-example", "record_revision": "revision-example"}
    observed = {"run_id": "run-example", "scope": scope, "state": "succeeded", "provider_state": "completed",
                "native_job_ref": {"record_id": "native-example"},
                "input": {"binding_id": "binding-example", "sources": [source]},
                "admission": {"scope": scope, "source_snapshots": [{"source_ref": source}]}}
    selected_run = deepcopy(observed)
    durable = {"run": selected_run, "attempts": [{"attempt_id": "attempt-example", "operation": "submit",
                "first_work_id": "work-example", "native_job_ref": observed["native_job_ref"],
                "started_at": "2026-10-29T12:00:00Z"}],
               "work": [{"work_id": "work-example", "completed_at": "2026-10-29T12:00:00Z"}]}
    if fault == "changed_source":
        observed["input"]["sources"] = [{**source, "record_revision": "other-revision"}]
    elif fault == "changed_job":
        durable["run"]["native_job_ref"]["record_id"] = "other-job"
    elif fault == "duplicate_attempt":
        durable["attempts"].append(deepcopy(durable["attempts"][0]))
    elif fault == "wrong_work":
        durable["work"][0]["work_id"] = "invented-work"
    selection = {"receipt": "receipt-example-01", "sources": [source], "binding_id": "binding-example",
                 "input": {"binding_id": "binding-example", "sources": [source]}}
    driver = object.__new__(LiveRecoveryDriver)
    driver.case = {"scope": scope}
    driver.run_id = None
    paths = []

    def product_read(path):
        paths.append(path)
        if path.endswith("/receipts/receipt-example-01"):
            if fault == "missing_receipt":
                raise ValueError("Product receipt unavailable")
            return {"run_id": "run-example"}
        if fault == "missing_product":
            raise ValueError("Product run unavailable")
        return durable if path.endswith("/recovery-state") else observed

    driver.read_api = product_read
    driver.api = object()
    if fault == "missing_snapshot":
        monkeypatch.setattr(recovery_live, "product_sources", lambda *args: [])
    else:
        monkeypatch.setattr(recovery_live, "product_sources", lambda *args: [
            {"source_ref": source, "content": b"pinned source"}])
    if fault == "accepted":
        run = driver._worker(selection)["run"]
        assert run["run_id"] == "run-example" and run["attempts"][0]["work_id"] == "work-example"
        assert driver.run_id == "run-example"
    else:
        with pytest.raises((AssertionError, ValueError)):
            driver._worker(selection)
        assert driver.run_id is None
    assert paths[0].endswith("/runs/receipts/receipt-example-01")
    assert all(path.startswith("/v1/products/product-example/projects/project-example/services/security/")
               for path in paths)
    assert len(paths) == {"missing_receipt": 1, "missing_product": 2,
                          "changed_source": 2, "missing_snapshot": 2}.get(fault, 3)


@pytest.mark.parametrize("fault", ["accepted", "missing_product", "wrong_result", "wrong_receipt"])
def test_recovery_publication_packet_requires_product_status(fault):
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    publication = {"publication_id": "publication-example", "result_id": "result-example",
                   "state": "published", "receipt": {"receipt_id": "receipt-example",
                   "consumer_revision": "consumer-example"}}
    observed = {"publication_id": publication["publication_id"], "result_id": publication["result_id"],
                "state": "published", "receipt_id": "receipt-example", "consumer_revision": "consumer-example"}
    if fault == "wrong_result":
        observed["result_id"] = "other-result"
    elif fault == "wrong_receipt":
        observed["receipt_id"] = "other-receipt"
    selection = {}
    driver = object.__new__(LiveRecoveryDriver)
    driver.case = {"scope": scope}
    driver.run_id = "run-example"
    paths = []

    def product_read(path):
        paths.append(path)
        if fault == "missing_product":
            raise ValueError("Product publication unavailable")
        if path.endswith("/recovery-state"):
            return {"run": {"run_id": driver.run_id}, "publications": [publication]}
        return observed

    driver.read_api = product_read
    if fault == "accepted":
        assert driver._publication(selection) == publication
    else:
        with pytest.raises((AssertionError, ValueError)):
            driver._publication(selection)
    assert paths == (["/v1/products/product-example/projects/project-example/services/security/runs/"
                      "run-example/recovery-state"] if fault == "missing_product" else
                     ["/v1/products/product-example/projects/project-example/services/security/runs/"
                      "run-example/recovery-state",
                      "/v1/products/product-example/projects/project-example/services/security/publications/"
                      "publication-example"])

def test_live_denial_adapter_inspects_nested_leaks_without_logging(monkeypatch):
    driver = object.__new__(LiveRecoveryDriver)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    driver.case = {"scope": scope, "api_token_env": "TEST_API"}
    driver.remaining = lambda: 20
    response = {"detail": {"reason": "run-example"}}
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(404, json=response)

    driver.api = httpx.Client(transport=httpx.MockTransport(handle), base_url="https://example.test")
    monkeypatch.setenv("TEST_API", "synthetic")
    monkeypatch.setenv("TEST_FOREIGN", "foreign-synthetic")
    monkeypatch.setenv("TEST_REVOKED", "revoked-synthetic")
    try:
        probes = driver.scope_probes("run-example", {"foreign_product_id": "foreign-product",
                                                     "revoked_token_env": "TEST_REVOKED"},
                                     {"api_token_env": "TEST_FOREIGN"})
    finally:
        driver.api.close()
    assert set(probes[0]["disclosed_resources"]) == {"run-example", "unexpected_denial_payload"}
    assert len(paths) == 3 and "/products/foreign-product/" in paths[0]


def test_live_attempt_journal_retains_worker_history_and_rejects_changed_native_job(tmp_path):
    from evidence import AttemptJournal

    driver = object.__new__(LiveRecoveryDriver)
    driver.journal = AttemptJournal(tmp_path / "private-attempts.jsonl")
    driver.run_id = "run-example"
    attempt = {"attempt_id": "worker-attempt-example", "operation": "submit", "status": "accepted",
               "work_id": "worker-example", "observed_at": "2026-10-28T03:00:00Z",
               "native_job_ref": {"record_id": "job-example"}}
    driver.retain_attempts([attempt])
    driver.retain_attempts([attempt])
    assert len(driver.recorded_attempts()) == 1
    with pytest.raises(AssertionError, match="changed"):
        driver.retain_attempts([{**attempt, "native_job_ref": {"record_id": "different-job"}}])


def test_live_denial_adapter_refuses_unexpected_body_even_at_404(monkeypatch):
    driver = object.__new__(LiveRecoveryDriver)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    driver.case = {"scope": scope, "api_token_env": "TEST_API"}
    driver.remaining = lambda: 20
    monkeypatch.setenv("TEST_API", "fictional")
    monkeypatch.setenv("TEST_FOREIGN", "fictional-foreign")
    monkeypatch.setenv("TEST_REVOKED", "fictional-revoked")
    driver.api = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, json={"detail": {"code": "not_found"}, "secret": "private"})),
        base_url="https://example.test")
    try:
        probes = driver.scope_probes("run-example", {"foreign_product_id": "foreign-product",
                                                     "revoked_token_env": "TEST_REVOKED"},
                                     {"api_token_env": "TEST_FOREIGN"})
    finally:
        driver.api.close()
    assert all("unexpected_denial_payload" in probe["disclosed_resources"] for probe in probes)


def test_live_cleanup_refuses_unowned_native_resource(recovery_case):
    case, foreign, evidence = recovery_case
    driver = object.__new__(LiveRecoveryDriver)
    driver.run_id = "run-example"
    driver._owned_binding = lambda selection: {"state": "ready"}
    driver._cleanup_packet = lambda selection: (_ for _ in ()).throw(AssertionError("Product native ownership differs"))
    with pytest.raises(AssertionError, match="ownership"):
        driver.validate_cleanup_ownership(case["recovery"])


def test_recovered_attempt_may_advance_without_losing_identity(recovery_case):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    driver.before["attempts"][0]["native_job_ref"] = None
    driver.before["attempts"][0]["status"] = "outcome_unknown"
    assert isolation_recovery_cleanup(case, foreign, driver)["recovery"]["attempts"] == 1


def test_owner_attempt_bound_not_bypassed_by_worker_report(recovery_case):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    driver.repair["max_attempts"] = 100
    driver.attempts.append({**driver.attempts[0], "attempt_id": "second-attempt"})
    driver.attempts.append({**driver.attempts[0], "attempt_id": "third-attempt"})
    driver.attempts.append({**driver.attempts[0], "attempt_id": "fourth-attempt"})
    driver.after["attempts"] = [{**driver.after["attempts"][0], "attempt_id": row["attempt_id"]}
                                for row in driver.attempts]
    with pytest.raises(AssertionError, match="attempt bound"):
        isolation_recovery_cleanup(case, foreign, driver)
    assert "cleanup" not in driver.calls


def test_duplicate_worker_attempt_identity_does_not_count_as_recovery(recovery_case):
    case, foreign, evidence = recovery_case
    driver = RecordingRecovery(evidence)
    driver.after["attempts"].append(deepcopy(driver.after["attempts"][0]))
    with pytest.raises(AssertionError, match="every durable attempt"):
        isolation_recovery_cleanup(case, foreign, driver)
    assert "cleanup" not in driver.calls


def test_fault_time_worker_and_publication_are_saved_before_restart(tmp_path):
    import json

    from evidence import AttemptJournal

    driver = object.__new__(LiveRecoveryDriver)
    driver.journal = AttemptJournal(tmp_path / "selected.jsonl")
    run = {"run_id": "run-example", "scope": {"tenant_id": "tenant-example"},
           "attempts": [{"attempt_id": "attempt-example"}]}
    publication = {"publication_id": "publication-example", "state": "retry_wait"}
    driver._worker = lambda selection: {"run": run}
    driver._publication = lambda selection: publication
    selected = {"receipt": "receipt-example"}
    assert driver.read_faulted(selected) == (run, publication)
    snapshot = tmp_path / "selected.jsonl.fault.json"
    observed = json.loads(snapshot.read_text())
    assert observed["receipt"] == selected["receipt"]
    assert observed["run"] == run and observed["publication"] == publication
    assert observed["observed_at"] and snapshot.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        driver.read_faulted(selected)
    assert json.loads(snapshot.read_text()) == observed
    snapshot.unlink()
    run["padding"] = "x" * 262144
    with pytest.raises(ValueError, match="snapshot bound"):
        driver.read_faulted(selected)
    assert not snapshot.exists()


@pytest.mark.parametrize("fault", ["accepted", "stale_work", "wrong_result", "foreign_receipt"])
def test_product_recovery_progress_requires_pinned_snapshot_and_publication(tmp_path, fault):
    from evidence import AttemptJournal
    from continuation import verify_recovery

    driver = object.__new__(LiveRecoveryDriver)
    driver.journal = AttemptJournal(tmp_path / "recovery.jsonl")
    driver.journal.append({"attempt_id": "run_submit", "operation": "run_submit", "status": "prepared",
                           "work_id": "receipt-example", "observed_at": "2026-10-29T12:00:00Z"})
    before = {"run_id": "run-example", "scope": {"tenant_id": "tenant-example"},
              "input": {"sources": [{"record_revision": "pinned-example"}]},
              "admission": {"source_snapshots": [{"record_revision": "pinned-example"}]},
              "attempts": [{"attempt_id": "attempt-example", "operation": "submit"}],
              "work": {"work_id": "work-example", "attempts": 1, "completed_at": None,
                       "last_enqueued_at": "2026-10-29T12:00:00Z"}}
    after = deepcopy(before)
    after.update(state="succeeded", provider_state="completed",
                 native_job_ref={"record_id": "native-example"})
    after["work"].update(attempts=2, completed_at="2026-10-29T12:02:00Z",
                         last_enqueued_at="2026-10-29T12:01:00Z")
    if fault == "stale_work":
        after["work"]["last_enqueued_at"] = before["work"]["last_enqueued_at"]
    prior = {"publication_id": "publication-example", "result_id": "result-example", "state": "retry_wait"}
    later = {**prior, "state": "published"}
    if fault == "wrong_result":
        later["result_id"] = "fabricated-result"
    if fault == "foreign_receipt":
        later["publication_id"] = "another-publication"
    packets = iter(({"run": before}, {"run": after}))
    publications = iter((prior, later))
    driver._worker = lambda selection: next(packets)
    driver._publication = lambda selection: next(publications)
    selected = {"receipt": "receipt-example"}
    assert driver.read_faulted(selected) == (before, prior)
    recovered, publication, repair = driver.read_recovered(selected)
    assert recovered == after and publication == later
    assert repair["operator_command"] is False
    assert repair["work_progressed"] is (fault != "stale_work")
    if fault in {"wrong_result", "foreign_receipt"}:
        with pytest.raises(AssertionError, match="publication remains"):
            verify_recovery(before, after, [{"attempt_id": "attempt-example"}],
                            [{"native_job_ref": after["native_job_ref"]}],
                            {**prior, "result_version": 1, "schema_version": "1.0", "attempts": 1},
                            {**publication, "result_version": 1, "schema_version": "1.0", "attempts": 2,
                             "receipt": {"receipt_id": "receipt-example", "contribution_id": publication["publication_id"],
                                         "admitted_schema": "1.0"}},
                            {**repair, "max_attempts": 3})

@pytest.mark.parametrize("fault", ["accepted", "linked_binding", "wrong_ownership", "wrong_operation",
                                   "stale_operation", "failed_operation", "native_still_present"])
def test_cleanup_uses_product_owned_binding_operation_and_native_absence(tmp_path, fault):
    from evidence import AttemptJournal

    driver = object.__new__(LiveRecoveryDriver)
    driver.case = {"scope": {"tenant_id": "tenant-example", "product_id": "product-example",
                             "project_id": "project-example"},
                   "selected": {"configuration_id": "configuration-example"}}
    driver.run_id = "run-example"
    driver.journal = AttemptJournal(tmp_path / "owned.jsonl")
    driver.deadline = __import__("time").monotonic() + 10
    selection = {"owned_binding_id": "owned-binding", "binding_id": "linked-binding",
                 "owned": ["owned-example"], "linked": ["linked-example"],
                 "input": {"configuration_revision": 3}}
    binding = {"binding_id": "owned-binding", "scope": driver.case["scope"],
               "configuration_id": "configuration-example", "ownership": "adp_created",
               "native_resource_ref": {"record_id": "owned-example"}, "revision": 2, "state": "ready"}
    if fault == "linked_binding":
        selection["owned_binding_id"] = selection["binding_id"]
    if fault == "wrong_ownership":
        binding["ownership"] = "customer_linked"
    reads = []
    def read_api(path):
        reads.append(path)
        if "/bindings/" in path:
            return {**binding, "state": "cleaned" if len([item for item in reads if "/bindings/" in item]) > 1 else binding["state"]}
        if "/native-readback/spaces/" in path:
            return {"run_id": "run-example", "ownership": {"owned-example": "scenario_owned",
                    "linked-example": "customer_linked"}, "resources": {
                        "owned-example": "present" if fault == "native_still_present" else "absent",
                        "linked-example": "retained"}}
        return {"operation_id": "operation-example" if fault != "wrong_operation" else "foreign",
                "action": "cleanup", "state": "failed" if fault == "failed_operation" else
                "pending" if fault == "stale_operation" else "succeeded"}
    driver.read_api = read_api
    def post(path, *, json, headers):
        assert path.endswith("/binding-operations") and json == {
            "action": "cleanup", "configuration_revision": 3, "binding_id": selection["owned_binding_id"],
            "owned_cleanup_confirmed": True}
        assert headers["If-Match"] == '"2"' and headers["Idempotency-Key"].startswith("security-cleanup-")
        return type("Response", (), {"status_code": 202, "raise_for_status": lambda self: None,
                                      "json": lambda self: {"action": "cleanup", "operation_id": "operation-example"}})()
    driver.api = type("Client", (), {"post": staticmethod(post)})()
    if fault == "stale_operation":
        driver.remaining = lambda: (_ for _ in ()).throw(AssertionError("deadline exceeded"))
    if fault == "accepted":
        driver.cleanup_owned(selection)
        assert driver.cleanup_readback(selection) == {"owned-example": "absent", "linked-example": "retained"}
        assert [entry["status"] for entry in driver.journal.read()] == ["prepared", "acknowledged"]
    else:
        with pytest.raises((AssertionError, TypeError)):
            driver.cleanup_owned(selection)
        assert not driver.journal.path.exists() or all(
            entry["status"] != "acknowledged" for entry in driver.journal.read())
