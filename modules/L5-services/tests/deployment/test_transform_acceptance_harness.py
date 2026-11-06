"""Original result and retained-resource transports feed strict partial acceptance."""
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "modules/L5-services/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "modules/L5-services/tests/transform"))
import qualification_transform_acceptance as acceptance
from qualification_transform_collectors import NativeResults
from qualification_transform_journal import ReleaseJournal, digest
from qualification_transform_retained import InstalledRetained
import qualification_transform_release as release
from l5_services.adapters.transform import qualification_release as native
import test_qualification_release as existing

observations = existing.observations
published = existing.published
assembled = existing.assembled

SPEC = importlib.util.spec_from_file_location("transform_test_publisher", SCRIPTS / "publish-release-reports.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
CONTEXT = {"schema_version": 1, "phase": "artifacts", "service": "transform", "environment": "dev",
           "repository_id": 1, "run_id": 2, "run_attempt": 1, "source_revision": "a" * 40,
           "definition_revision": "b" * 40, "correlation": "orch:00000000-0000-0000-0000-000000000001",
           "contract_digest": "c" * 64}


@pytest.fixture
def journal(tmp_path):
    tmp_path.chmod(0o700)
    value = ReleaseJournal(tmp_path, CONTEXT, "d" * 64, acceptance.catalog()[1])
    with value.locked():
        yield value


class MetadataTransport:
    def __init__(self, present=False):
        self.calls, self.present = [], present

    def request(self, method, path, **options):
        assert method == "GET" and options == {"metadata_only": True, "maximum": 65536, "timeout_seconds": 2}
        assert path.startswith(("/api/v1/namespaces/provider/", "/api/v1/namespaces/checks/"))
        self.calls.append(path)
        return {"kind": "PartialObjectMetadata", "metadata": {"uid": "replaced"}} if self.present else None


def complete_checkpoint_inventory(s3):
    for (key, _), saved in s3.objects.items():
        if "/checkpoints/" not in key:
            continue
        value = json.loads(saved["Body"])
        cleanup = value["cleanup"]
        provider = "observation" in value
        cleanup["namespace"] = "provider" if provider else "checks"
        cleanup["resources"] = [{"kind": "configmaps", "name": cleanup["name"] + "-part-0000", "uid": "input-" + cleanup["uid"]}]
        if provider:
            cleanup["resources"].append({"kind": "secrets", "name": cleanup["name"], "uid": "credential-" + cleanup["uid"]})
        saved["Body"] = json.dumps(value).encode()
        saved["Metadata"]["sha256"] = sha256(saved["Body"]).hexdigest()


async def collect_case(journal, assembled, *, present=False, mutate=None):
    fixture, sessions, client, artifacts, result, receipt, s3, calls, source_status = assembled
    complete_checkpoint_inventory(s3)
    evidence = acceptance.EvidenceSet(journal)
    cases = NativeResults(evidence)
    cases.select(fixture["runs"][0])
    retained = InstalledRetained(evidence, cases, SimpleNamespace(series=object()), "example-artifacts")
    retained.api = MetadataTransport(present)
    retained.namespaces = {"provider": "provider", "check": "checks"}
    async def after_verified(**values):
        if mutate is not None:
            mutate(values)
        await retained.collect(**values)
    baseline = deepcopy(s3.objects)
    evaluated = await native.qualify(fixture, sessions, client, artifacts, clock=lambda: existing.NOW,
                                     on_verified_case=cases.verified, on_retained_case=after_verified)
    cases.finish()
    retained.finish()
    assert s3.objects == baseline
    assert evaluated["status"] == "evaluated"
    return evidence, cases, retained


@pytest.mark.asyncio
async def test_real_result_sql_and_retained_transports_collect_without_effects(journal, assembled):
    evidence, cases, retained = await collect_case(journal, assembled)
    assert len(cases.cases) == len(retained.completed) == 1, evidence.progress
    assert len(retained.api.calls) == 7  # provider Pod/input/secret plus two check Pod/input pairs
    observed = [row for row in journal.records if row["kind"] == "retained-after-cleanup"]
    assert len(observed) == 1 and len(observed[0]["value"]["retained_artifacts"]) == 5
    histories = [row["value"] for row in journal.records if row["kind"] == "durable-progress-readback"]
    assert len(histories) == 1 and len(histories[0]["attempts"]) == 1
    assert "independent-native-effect-ledger" in histories[0]["missing"]
    # One real case and post-cleanup bytes do not stand in for the two-case
    # requirement, fault injection, UI journey, adopted inventory or SCM proof.
    report = acceptance.evaluate(evidence)
    acceptance.validate_catalog(report)
    normalized = publisher.normalized_report(report, CONTEXT, "e" * 64)
    assert not publisher.complete(normalized)
    assert [len(report[k]) for k in publisher.SECTIONS] == [8, 29, 29, 15]
    assert report["cleanup"]["outcome"] == "incomplete"
    assert len(report["unresolved"]) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["present", "expired", "revision", "different-case", "wrong-namespace"])
async def test_retention_failure_has_action_and_never_credits_acceptance(journal, assembled, failure):
    def mutate(values):
        if failure == "expired":
            values["authority"].clock = lambda: datetime(2028, 1, 1, tzinfo=UTC)
        elif failure == "revision":
            values["result"]["revision"] += 1
        elif failure == "different-case":
            values["authority"].scope = {**values["authority"].scope, "project_id": "foreign"}
        elif failure == "wrong-namespace":
            original = values["observer"].verify_checks
            async def wrong(**arguments):
                checkpoints = await original(**arguments)
                checkpoints[0]["checkpoint"]["cleanup"]["namespace"] = "foreign"
                return checkpoints
            values["observer"].verify_checks = wrong
    evidence, cases, retained = await collect_case(journal, assembled, present=failure == "present", mutate=mutate)
    assert not retained.completed
    failures = [entry for entry in evidence.progress if entry.get("outcome") == "incomplete"]
    assert len(failures) == 1 and failures[0]["next_action"]
    report = acceptance.evaluate(evidence)
    assert "retained-results" not in report["observed_criteria"]
    normalized = publisher.normalized_report(report, CONTEXT, "e" * 64)
    published = [item for item in normalized["diagnostics"] if item["code"].startswith("transform-collection:")]
    assert len(published) == 1 and published[0]["next_action"] == failures[0]["next_action"]
    report["progress"][-1]["next_action"] = "UNTRUSTED-PRIVATE-DETAIL"
    assert "UNTRUSTED-PRIVATE-DETAIL" not in json.dumps(publisher.normalized_report(report, CONTEXT, "e" * 64))


def test_partial_catalog_and_fixture_verdicts_cannot_pass(journal):
    report = acceptance.evaluate(acceptance.EvidenceSet(journal))
    normalized = publisher.normalized_report(report, CONTEXT, "e" * 64)
    assert not publisher.complete(normalized)
    assert len(normalized["diagnostics"]) == 8
    for mutate in (lambda x: x["story_criteria"].pop(), lambda x: x["scenarios"].append(x["scenarios"][0]),
                   lambda x: x["live_checks"][0].update(outcome="passed", evidence_verified=True),
                   lambda x: x.update(fixture_sha256="0" * 64), lambda x: x["release_context"].update(run_attempt=2)):
        changed = deepcopy(report)
        mutate(changed)
        with pytest.raises(ValueError):
            publisher.normalized_report(changed, CONTEXT, "e" * 64)
    with pytest.raises(ValueError):
        acceptance.evaluate({"EC-01": True})
    for supplied in ({"verdicts": {"EC-01": "passed"}}, {"human_observation": {"provenance": "authenticated-human-observation"}},
                     {"collector": "external.python.object"}):
        with pytest.raises(ValueError):
            release.options({"release_acceptance": supplied})


@pytest.mark.asyncio
async def test_wrong_original_selection_cannot_receive_evaluator_callback(journal, assembled):
    fixture, sessions, client, artifacts, *_ = assembled
    evidence = acceptance.EvidenceSet(journal)
    cases = NativeResults(evidence)
    cases.select({**fixture["runs"][0], "result_revision": 2})
    with pytest.raises(ValueError, match="selected original"):
        await native.qualify(fixture, sessions, client, artifacts, clock=lambda: existing.NOW,
                             on_verified_case=cases.verified)
    assert not cases.cases


def test_journal_rejects_parallel(journal, tmp_path):
    competing = ReleaseJournal(tmp_path, CONTEXT, "d" * 64, acceptance.catalog()[1])
    with pytest.raises(ValueError, match="already running"):
        with competing.locked():
            pass


def test_journal_resume_preserves_history_and_refuses_rebinding(tmp_path):
    tmp_path.chmod(0o700)
    def make(**kwargs):
        return ReleaseJournal(tmp_path, kwargs.get("context", CONTEXT), kwargs.get("fixture", "d" * 64), acceptance.catalog()[1])
    with make().locked() as value:
        value.append("original-run", {"run_id": "retained"})
    with make().locked() as value:
        assert value.records[-1]["value"] == {"run_id": "retained"}
    for changed in ({"context": {**CONTEXT, "run_attempt": 2}}, {"fixture": "f" * 64}):
        with pytest.raises(ValueError, match="another attempt"):
            with make(**changed).locked():
                pass
    path = tmp_path / "release-journal.jsonl"
    path.write_text(path.read_text().rstrip())
    with pytest.raises(ValueError, match="incomplete"):
        with make().locked():
            pass


@pytest.mark.parametrize("representation", ["metadata", "full-secret", "absent"])
def test_cluster_transport_requests_only_metadata_and_rejects_full_secret(representation):
    import httpx
    from botocore.credentials import Credentials
    from l5_services.adapters.transform.kubernetes import TransformKubernetesAPI

    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers["accept"] == "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
        if representation == "absent":
            return httpx.Response(404)
        if representation == "full-secret":
            return httpx.Response(200, json={"apiVersion": "v1", "kind": "Secret", "data": {"key": "REJECT"}})
        return httpx.Response(200, json={"apiVersion": "meta.k8s.io/v1", "kind": "PartialObjectMetadata",
                                        "metadata": {"uid": "owned"}})
    api = TransformKubernetesAPI.__new__(TransformKubernetesAPI)
    api.endpoint, api.cluster, api.region = "https://example.eks.amazonaws.com", "example", "us-east-1"
    api.session = SimpleNamespace(get_credentials=lambda: Credentials("offline", "offline"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as api.client:
        if representation == "full-secret":
            with pytest.raises(ValueError, match="unsupported resource contents"):
                api.request("GET", "/api/v1/namespaces/provider/secrets/example", metadata_only=True)
        else:
            result = api.request("GET", "/api/v1/namespaces/provider/secrets/example", metadata_only=True)
            assert result is None if representation == "absent" else result["metadata"]["uid"] == "owned"
        with pytest.raises(ValueError, match="read-only"):
            api.request("DELETE", "/api/v1/namespaces/provider/secrets/example", metadata_only=True)
    assert len(requests) == 1


@pytest.mark.parametrize("failure", [None, "role", "worker", "cluster", "bucket", "namespace"])
def test_cleanup_target_comes_from_pinned_installed_worker(monkeypatch, failure):
    import boto3
    from qualification_transform_retained import installed_inventory
    from l5_services.adapters.transform import kubernetes
    from l5_services.runtime_names import functions

    account = "123456789012"
    worker, _ = functions("dev", "transform")
    release_pin = SimpleNamespace(queue_arn=f"arn:aws:sqs:us-east-1:{account}:adp-dev-l5-transform",
        account_id=account, role_arn=f"arn:aws:iam::{account}:role/release", source_revision="a" * 40,
        worker_code_sha256="worker-pin")
    # Use the exact runtime queue naming contract.
    release_pin.queue_arn = f"arn:aws:sqs:us-east-1:{account}:adp-dev-l5-transform-work"
    principal = {"Account": account, "Arn": f"arn:aws:sts::{account}:assumed-role/release/test"}
    cluster = {"arn": f"arn:aws:eks:us-east-1:{account}:cluster/example", "status": "ACTIVE",
               "endpoint": "https://example.eks.amazonaws.com", "certificateAuthority": {"data": "example-ca"}}
    values = {"L5_TRANSFORM_CLUSTER_NAME": "example", "L5_TRANSFORM_CLUSTER_ENDPOINT": cluster["endpoint"],
              "L5_TRANSFORM_CLUSTER_CA": "example-ca", "L5_TRANSFORM_ARTIFACT_BUCKET": "example-artifacts",
              "L5_TRANSFORM_PROVIDER_NAMESPACE": "provider", "L5_TRANSFORM_CHECK_NAMESPACE": "checks"}
    function = {"CodeSha256": "worker-pin", "State": "Active", "LastUpdateStatus": "Successful", "RevisionId": "r1",
                "FunctionArn": f"arn:aws:lambda:us-east-1:{account}:function:{worker}", "Environment": {"Variables": values}}
    if failure == "role":
        principal["Arn"] = f"arn:aws:sts::{account}:assumed-role/foreign/test"
    elif failure == "worker":
        function["CodeSha256"] = "foreign"
    elif failure == "cluster":
        cluster["arn"] += "-foreign"
    elif failure == "bucket":
        values["L5_TRANSFORM_ARTIFACT_BUCKET"] = "foreign"
    elif failure == "namespace":
        values["L5_TRANSFORM_CHECK_NAMESPACE"] = "../../foreign"
    calls = []
    class Client:
        def get_caller_identity(self):
            calls.append("identity")
            return principal
        def get_function_configuration(self, **request):
            assert request == {"FunctionName": worker}
            calls.append("worker")
            return function
        def describe_cluster(self, **request):
            assert request == {"name": "example"}
            calls.append("cluster")
            return {"cluster": cluster}
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: SimpleNamespace(client=lambda *args, **kwargs: Client()))
    monkeypatch.setattr(kubernetes, "TransformKubernetesAPI", lambda **kwargs: SimpleNamespace(**kwargs))
    if failure:
        with pytest.raises(ValueError):
            installed_inventory({"cluster_name": "example"}, release_pin, CONTEXT, "example-artifacts")
    else:
        api, namespaces, target = installed_inventory({"cluster_name": "example"}, release_pin, CONTEXT, "example-artifacts")
        assert namespaces == {"provider": "provider", "check": "checks"}
        assert target["worker_revision"] == "r1" and api.cluster == "example"
        assert calls == ["identity", "worker", "cluster"]


def test_journal_rejects_altered_prior_facts(tmp_path):
    tmp_path.chmod(0o700)
    def make():
        return ReleaseJournal(tmp_path, CONTEXT, "d" * 64, acceptance.catalog()[1])
    with make().locked() as journal:
        journal.append("readback", {"result_revision": 1})
        journal.append("checkpoint", {"original": True})
    path = tmp_path / "release-journal.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    records[1]["value"]["result_revision"] = 2
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    with pytest.raises(ValueError, match="changed"):
        with make().locked():
            pass


@pytest.mark.asyncio
async def test_bootstrap_flat_selection_is_bound_to_installed_checkpoint(journal):
    installed = release.Installed.__new__(release.Installed)
    calls = []
    installed.series = SimpleNamespace(checkpoint=lambda stage, **kwargs: calls.append((stage, kwargs)))
    selected = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
                "run_id": "run-a", "result_id": "result-a", "result_revision": 1}
    await installed.selected(selected, 1, "run")
    assert calls == [("after-native-case-1", {"completed_case": {
        "scope": {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a"},
        "run_id": "run-a", "result_revision": 1}})]


def test_missing_baseline_cannot_be_recreated_after_original_intent(journal):
    installed = release.Installed.__new__(release.Installed)
    installed.journal, installed.bootstrap = journal, True
    calls = []
    installed.series = SimpleNamespace(checkpoint=lambda *args: calls.append(args))
    (journal.directory / "transform-bootstrap-original.jsonl").write_text("retained intent")
    with pytest.raises(ValueError, match="do not rebaseline"):
        installed.before("run")
    assert calls == []


def test_publisher_requires_exact_latest_report_and_private_observation_journal(tmp_path):
    tmp_path.chmod(0o700)
    journal = ReleaseJournal(tmp_path, CONTEXT, "d" * 64, acceptance.catalog()[1])
    with journal.locked():
        evidence = acceptance.EvidenceSet(journal)
        evidence._collected("runtime-revisions", {"test_transport": "fixed-runtime-read"})
        report = acceptance.evaluate(evidence)
        release.publish_local_report(journal, report)
    report_sha256 = sha256((tmp_path / "qualification.json").read_bytes()).hexdigest()
    acceptance.validate_journal(report, tmp_path, report_sha256)
    changed = deepcopy(report)
    changed["collector_receipts"]["runtime-revisions"]["facts_sha256"] = "e" * 64
    # A report document cannot supply a new private observation, even if its
    # catalog coverage and criterion outcome otherwise look self-consistent.
    with pytest.raises(ValueError, match="preserved collector"):
        acceptance.validate_journal(changed, tmp_path, report_sha256)
    with journal.locked():
        journal.append("phase-started", {"phase": "emit"})
    with pytest.raises(ValueError, match="latest completed"):
        acceptance.validate_journal(report, tmp_path, report_sha256)


@pytest.mark.parametrize("failure", [None, "reported-version", "artifact-pins", "write-scope"])
def test_native_acceptance_recomputes_dependency_and_write_scope(failure):
    from test_dependency_oracle_uv_lock import inputs, TARGET
    from l5_services.adapters.transform.dependency_oracle import compare_dependency_upgrade
    from l5_services.adapters.transform.provenance import compare_source_trees
    from l5_services.adapters.transform.qualification_readback import verify_candidate_semantics

    base, candidate, pins = inputs()
    modes = dict.fromkeys(base, "100644")
    run = SimpleNamespace(admission={"transform_artifact_hashes": pins,
        "transform_settings": {"write_paths": list(base), "excluded_paths": [".git"]}},
        manifest={"files": [{"path": path, "mode": "100644"} for path in base]},
        input={"transform_input": {"goal": {"kind": "dependency_upgrade", "target": TARGET}}})
    result = {"source_trees": compare_source_trees(base, candidate, base_modes=modes, candidate_modes=modes),
              "scope_status": "passed", "dependency_comparison": {"status": "passed",
                  **compare_dependency_upgrade(base, candidate, TARGET, pins)}}
    if failure == "reported-version":
        result["dependency_comparison"]["candidate_version"] = "invented"
    elif failure == "artifact-pins":
        run.admission["transform_artifact_hashes"] = ["sha256:" + "0" * 64]
    elif failure == "write-scope":
        run.admission["transform_settings"]["write_paths"] = ["different-directory"]
    if failure:
        with pytest.raises(ValueError):
            verify_candidate_semantics(run, result, base, candidate, modes)
    else:
        verify_candidate_semantics(run, result, base, candidate, modes)


@pytest.mark.asyncio
async def test_result_changed_after_independent_semantics_cannot_be_sealed(assembled):
    import httpx
    fixture, sessions, client, artifacts, result, *_ = assembled
    reads, sealed = [], []
    async def respond(request):
        if "/results/" in request.url.path:
            reads.append(request.url.path)
            if len(reads) == 2:
                result["dependency_comparison"] = {"status": "passed"}
        return await client.get(request.url.path)
    async def verified(*values):
        sealed.append(values)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://example.invalid") as changing:
        with pytest.raises(ValueError, match="changed after independent"):
            await native.qualify(fixture, sessions, changing, artifacts, clock=lambda: existing.NOW,
                                 on_verified_case=verified)
    assert reads and not sealed
