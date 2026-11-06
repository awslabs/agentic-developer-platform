"""Local S06 candidate checks; real-source readback requires deployed authority."""

import copy
import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from insights import projection, results
from insights.poc.devlake.dataset import build_candidate


TIME = "2026-10-20T00:00:00Z"


def reference(kind, identifier):
    return {"tenant_id": "example-tenant", "project_id": "example-project",
            "source_connection_id": "example-source", "record_type": kind,
            "record_id": identifier, "record_revision": "1"}


def candidate_inputs():
    work = {"subject_ref": reference("work", "example-work"), "kind": "work",
            "effective_at": TIME, "recorded_at": TIME}
    charge = {"subject_ref": reference("cost", "example-charge"), "kind": "cost",
              "work_ref": work["subject_ref"], "cost_basis": "actual_charge",
              "amount": "12.50", "currency": "USD", "effective_at": TIME, "recorded_at": TIME}
    product = reference("product", "example-product")
    links = [{"relationship_type": "work_product", "relationship_id": f"example-{kind}-link",
              "relationship_revision": "1", "source_connection_id": "example-source",
              "from_ref": fact["subject_ref"], "to_ref": product,
              "valid_from": TIME, "recorded_at": TIME, "mapping_version": "example-map-1"}
             for kind, fact in (("work", work), ("charge", charge))]
    return {"facts": [work, charge], "relationships": links, "allocations": [],
            "tenant_id": "example-tenant", "project_id": "example-project",
            "dataset_version": "example-dataset-1", "mapping_versions": ["example-map-1"],
            "as_of": TIME, "source_scope": "example-source-policy",
            "evidence_scope": "example-evidence-policy"}


def test_candidate_has_reader_shape_and_independent_ledger_total():
    snapshot, quality = build_candidate(**candidate_inputs())
    assert (snapshot["status"], snapshot["dataset_version"], len(snapshot["facts"])) == (
        "complete", "example-dataset-1", 2)
    assert quality["work_count"] == 1
    assert quality["cost_by_currency"] == {"USD": "12.50"}
    assert quality["cost_basis"] == "actual_charge"
    assert "quality" not in snapshot

    result = projection.query_authorized_projection(
        snapshot["facts"], snapshot["relationships"], snapshot["allocations"],
        tenant_id="example-tenant", project_ids=["example-project"],
        can_read_project=lambda tenant, project: (tenant, project) == (
            "example-tenant", "example-project"))
    assert result["coverage"] == "complete"
    assert result["work_count"] == 1
    assert result["cost_by_currency"] == {"USD": 12.50}
    assert result["unallocated_by_currency"] == {"USD": 12.50}


def test_candidate_rejects_estimates_unbound_charges_and_foreign_references():
    for mutation in (lambda data: data["facts"][1].update(cost_basis="estimated_usage"),
                     lambda data: data["facts"][1].update(work_ref=reference("work", "absent")),
                     lambda data: data["relationships"][0]["to_ref"].update(project_id="foreign")):
        inputs = copy.deepcopy(candidate_inputs())
        mutation(inputs)
        with pytest.raises(ValueError):
            build_candidate(**inputs)


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class ConditionalConflict(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class MemoryObjects:
    def __init__(self):
        self.objects = {}
        self.fail_key = None
        self.fail_read_key = None
        self.fail_after_pointer = False
        self.pointer_writes = 0

    def get_object(self, *, Bucket, Key):
        import hashlib
        import io

        if self.fail_read_key == Key:
            self.fail_read_key = None
            raise OSError("readback unavailable")
        if Key not in self.objects:
            raise MissingObject()
        body = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": hashlib.md5(body).hexdigest()}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None):
        import hashlib

        if self.fail_key == Key:
            self.fail_key = None
            raise OSError("interrupted pointer write")
        current = self.objects.get(Key)
        if (IfNoneMatch == "*" and current is not None or
                IfMatch is not None and (current is None or hashlib.md5(current).hexdigest() != IfMatch)):
            raise ConditionalConflict()
        self.objects[Key] = Body
        if Key.endswith("published.json"):
            self.pointer_writes += 1
            if self.fail_after_pointer:
                self.fail_after_pointer = False
                self.fail_read_key = Key


def test_api_cli_projection():
    import importlib.util
    import json

    from insights.poc.devlake.results_publisher import publish_candidate

    spec = importlib.util.spec_from_file_location(
        "s06_i23_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    inputs = candidate_inputs()
    inputs["source_scope"] = "source-policy"
    inputs["evidence_scope"] = "evidence-policy"
    snapshot, quality = build_candidate(**inputs)
    policy = fixture.qualified_shared_policy(snapshot)
    storage = MemoryObjects()
    calls = []

    def authorize(descriptor):
        calls.append(descriptor["policy_epoch"])
        return True

    pointer = publish_candidate(storage=storage, bucket="example-bucket",
                                tenant_id="example-tenant", project_id="example-project",
                                snapshot=snapshot, policy=policy, quality=quality, authorize=authorize)
    assert calls == [1, 1]
    prefix = "results/v1/example-tenant/example-project/"
    assert json.loads(storage.objects[prefix + "published.json"]) == pointer
    assert json.loads(storage.objects[
        prefix + f"datasets/example-dataset-1/snapshots/{pointer['snapshot_sha256']}.json"]) == snapshot
    assert json.loads(storage.objects[
        prefix + f"datasets/example-dataset-1/quality/{pointer['quality_sha256']}.json"]) == quality
    stored_snapshot = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-1/snapshots/{pointer['snapshot_sha256']}.json"])
    stored_policy = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-1/policies/{pointer['policy_sha256']}.json"])
    stored_quality = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-1/quality/{pointer['quality_sha256']}.json"])
    assert results.snapshot_digest(stored_snapshot) == stored_policy["snapshot_sha256"]
    state = {"snapshot": stored_snapshot, "policy": stored_policy, "granted": True}
    with fixture.installed_gateway(stored_snapshot, state) as (binary, environment, token, gateway_url):
        result = fixture.read_result(binary, environment, token, gateway_url, TIME)
    assert (result["dataset_version"], result["as_of"], result["product_id"]) == (
        "example-dataset-1", TIME, "example-product")
    assert (result["coverage"], result["work_count"], result["cost_by_currency"]) == (
        "complete", 1, {"USD": "12.50"})
    assert result["allocated_by_currency"] == {}
    assert result["unallocated_by_currency"] == {"USD": "12.50"}
    assert result["mapping_versions"] == ["example-map-1"]
    assert {context["subject_ref"]["record_id"] for context in result["contexts"]} == {
        "example-work", "example-charge"}
    assert (stored_quality["cost_basis"], stored_quality["source_scope"],
            stored_quality["evidence_scope"]) == ("actual_charge", "source-policy", "evidence-policy")
    assert "actor" not in json.dumps(result)


def test_private_inputs_cannot_change_published_totals_coverage_or_status():
    import importlib.util
    import json

    from insights.poc.devlake.results_publisher import publish_candidate

    spec = importlib.util.spec_from_file_location(
        "s06_i23_private_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    inputs = candidate_inputs()
    inputs["source_scope"] = "source-policy"
    inputs["evidence_scope"] = "evidence-policy"
    snapshot, quality = build_candidate(**inputs)
    policy = fixture.qualified_shared_policy(snapshot)
    storage = MemoryObjects()
    options = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
               "project_id": "example-project", "authorize": lambda descriptor: True}
    publish_candidate(**options, snapshot=snapshot, policy=policy, quality=quality)
    baseline = dict(storage.objects)

    def private_fact(data):
        cost = copy.deepcopy(data["facts"][1])
        cost["subject_ref"]["record_id"] = "private-charge"
        cost.update(visibility="private", amount="900.00")
        data["facts"].append(cost)

    def private_link(data):
        link = copy.deepcopy(data["relationships"][1])
        link["relationship_id"] = "private-mapping"
        link["to_ref"]["record_id"] = "private-product"
        link["visibility"] = "private"
        data["relationships"].append(link)

    def private_allocation(data):
        cost = data["facts"][1]
        data["allocations"].append({"source_ref": cost["subject_ref"],
                                    "target_ref": data["relationships"][1]["to_ref"],
                                    "allocation_id": "private-allocation", "allocation_policy_version": "1",
                                    "allocation_basis": "work", "amount": "12.50", "currency": "USD",
                                    "visibility": "private"})

    for mutation in (private_fact, private_link, private_allocation):
        altered = copy.deepcopy(inputs)
        mutation(altered)
        with pytest.raises(ValueError):
            changed, changed_quality = build_candidate(**altered)
            changed_policy = fixture.qualified_shared_policy(changed)
            publish_candidate(**options, snapshot=changed, policy=changed_policy,
                              quality=changed_quality)
        assert storage.objects == baseline

    private_snapshot = copy.deepcopy(snapshot)
    private_snapshot["facts"][1]["visibility"] = "private"
    with pytest.raises(ValueError):
        publish_candidate(**options, snapshot=private_snapshot,
                          policy=fixture.qualified_shared_policy(private_snapshot), quality=quality)
    assert storage.objects == baseline

    private_policy = copy.deepcopy(policy)
    private_policy["envelopes"][0]["required_policies"][0]["visibility"] = "private"
    with pytest.raises(results.ReadError):
        publish_candidate(**options, snapshot=snapshot, policy=private_policy, quality=quality)
    assert storage.objects == baseline
    assert json.loads(storage.objects["results/v1/example-tenant/example-project/published.json"])["dataset_version"] == "example-dataset-1"
    state = {"snapshot": snapshot, "policy": policy, "granted": True}
    with fixture.installed_gateway(snapshot, state) as (binary, environment, token, gateway_url):
        result = fixture.read_result(binary, environment, token, gateway_url, TIME)
    assert (result["coverage"], result["work_count"], result["cost_by_currency"]) == (
        "complete", 1, {"USD": "12.50"})


LATER = "2026-10-23T00:00:00Z"


def revised_inputs():
    inputs = candidate_inputs()
    work = copy.deepcopy(inputs["facts"][0])
    work["subject_ref"]["record_revision"] = "2"
    work["recorded_at"] = LATER
    charge = copy.deepcopy(inputs["facts"][1])
    charge["subject_ref"]["record_revision"] = "2"
    charge["work_ref"] = work["subject_ref"]
    charge["recorded_at"] = LATER
    charge["amount"] = "9.25"
    inputs["facts"] += [work, charge, copy.deepcopy(work), copy.deepcopy(charge)]
    for link, fact in zip(inputs["relationships"][:2], (work, charge)):
        revised_link = copy.deepcopy(link)
        revised_link["relationship_revision"] = "2"
        revised_link["from_ref"] = fact["subject_ref"]
        revised_link["recorded_at"] = LATER
        inputs["relationships"].append(revised_link)
    product = inputs["relationships"][0]["to_ref"]
    for portfolio in ("example-commerce", "example-platform"):
        inputs["relationships"].append({
            "relationship_type": "portfolio_product", "relationship_id": portfolio,
            "relationship_revision": "1", "source_connection_id": "example-source",
            "from_ref": reference("portfolio", portfolio), "to_ref": product,
            "valid_from": TIME, "recorded_at": TIME, "mapping_version": "example-map-1"})
    inputs["as_of"] = LATER
    inputs["dataset_version"] = "example-dataset-2"
    inputs["source_scope"] = "source-policy"
    inputs["evidence_scope"] = "evidence-policy"
    return inputs


def test_revision_selection():
    inputs = revised_inputs()
    old_inputs = candidate_inputs()
    old_inputs["source_scope"] = "source-policy"
    old_inputs["evidence_scope"] = "evidence-policy"
    old_snapshot, old_quality = build_candidate(**old_inputs)
    historical_inputs = copy.deepcopy(inputs)
    historical_inputs["as_of"] = TIME
    historical_inputs["dataset_version"] = "example-historical-cutoff"
    historical, historical_quality = build_candidate(**historical_inputs)
    assert {fact["subject_ref"]["record_revision"] for fact in historical["facts"]} == {"1"}
    assert historical_quality["cost_by_currency"] == {"USD": "12.50"}
    snapshot, quality = build_candidate(**inputs)
    assert len(snapshot["facts"]) == 2
    assert {fact["subject_ref"]["record_revision"] for fact in snapshot["facts"]} == {"2"}
    assert quality["work_count"] == 1
    assert quality["cost_by_currency"] == {"USD": "9.25"}
    assert old_quality["cost_by_currency"] == {"USD": "12.50"}
    assert all(link["from_ref"]["record_revision"] == "2"
               for link in snapshot["relationships"] if link["relationship_type"] == "work_product")
    result = projection.query_authorized_projection(
        snapshot["facts"], snapshot["relationships"], [], tenant_id="example-tenant",
        project_ids=["example-project"], can_read_project=lambda *args: True)
    assert (result["coverage"], result["work_count"], result["cost_by_currency"]) == (
        "complete", 1, {"USD": 9.25})
    assert all(set(item["portfolio_ids"]) == {"example-commerce", "example-platform"}
               for item in result["associations"])

    import importlib.util
    import json
    from insights.poc.devlake.results_publisher import publish_candidate

    spec = importlib.util.spec_from_file_location(
        "s06_i23_revision_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    storage = MemoryObjects()
    options = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
               "project_id": "example-project", "authorize": lambda descriptor: True}
    publish_candidate(**options, snapshot=old_snapshot, quality=old_quality,
                      policy=fixture.qualified_shared_policy(old_snapshot))
    old_key = next(key for key in storage.objects if "/example-dataset-1/snapshots/" in key)
    old_bytes = storage.objects[old_key]
    pointer = publish_candidate(**options, snapshot=snapshot, quality=quality,
                                policy=fixture.qualified_shared_policy(snapshot))
    after = dict(storage.objects)
    assert pointer["dataset_version"] == "example-dataset-2"
    assert json.loads(storage.objects[old_key]) == old_snapshot
    assert storage.objects[old_key] == old_bytes
    prefix = "results/v1/example-tenant/example-project/"
    published = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-2/snapshots/{pointer['snapshot_sha256']}.json"])
    published_policy = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-2/policies/{pointer['policy_sha256']}.json"])
    with fixture.installed_gateway(published, {"snapshot": published, "policy": published_policy,
                                             "granted": True}) as (binary, environment, token, gateway_url):
        observed = fixture.read_result(binary, environment, token, gateway_url, LATER,
                                       portfolios=("example-commerce", "example-platform"))
    assert (observed["dataset_version"], observed["work_count"], observed["cost_by_currency"]) == (
        "example-dataset-2", 1, {"USD": "9.25"})
    assert observed["unallocated_by_currency"] == {"USD": "9.25"}
    assert {context["subject_ref"]["record_revision"] for context in observed["contexts"]} == {"2"}
    assert all(set(row["portfolio_ids"]) == {"example-commerce", "example-platform"}
               for row in observed["associations"])
    with fixture.installed_gateway(json.loads(old_bytes), {"snapshot": json.loads(old_bytes),
                                                      "policy": fixture.qualified_shared_policy(old_snapshot),
                                                      "granted": True}) as (binary, environment, token, gateway_url):
        historical_readback = fixture.read_result(binary, environment, token, gateway_url, TIME)
    assert (historical_readback["dataset_version"], historical_readback["work_count"],
            historical_readback["cost_by_currency"]) == ("example-dataset-1", 1, {"USD": "12.50"})
    assert publish_candidate(**options, snapshot=snapshot, quality=quality,
                             policy=fixture.qualified_shared_policy(snapshot)) == pointer
    assert storage.objects == after
    changed = copy.deepcopy(snapshot)
    changed["facts"][1]["amount"] = "100.00"
    changed_quality = {**quality, "cost_by_currency": {"USD": "100.00"}}
    with pytest.raises(ValueError, match="dataset version already published"):
        publish_candidate(**options, snapshot=changed, quality=changed_quality,
                          policy=fixture.qualified_shared_policy(changed))
    assert storage.objects["results/v1/example-tenant/example-project/published.json"] == after[
        "results/v1/example-tenant/example-project/published.json"]
    assert storage.objects[old_key] == old_bytes


def test_ambiguous_corrections_fail_instead_of_counting_both_revisions():
    inputs = revised_inputs()
    conflicting = copy.deepcopy(inputs["facts"][2])
    conflicting["subject_ref"]["record_revision"] = "3"
    inputs["facts"].append(conflicting)
    with pytest.raises(ValueError, match="ambiguous"):
        build_candidate(**inputs)


@pytest.fixture
def reader_fixture():
    spec = importlib.util.spec_from_file_location(
        "s06_repair_reader_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


def test_corrected_snapshot_rejects_unavailable_historical_cutoffs(reader_fixture):
    from urllib.parse import urlencode

    from insights.poc.devlake.results_publisher import publish_candidate

    storage = MemoryObjects()
    options = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
               "project_id": "example-project", "authorize": lambda descriptor: True}
    original = candidate_inputs()
    original.update(source_scope="source-policy", evidence_scope="evidence-policy")
    for inputs in (original, revised_inputs()):
        snapshot, quality = build_candidate(**inputs)
        policy = reader_fixture.qualified_shared_policy(snapshot)
        publish_candidate(**options, snapshot=snapshot, quality=quality, policy=policy)
    reader = results.ResultsReader(
        lambda *args: snapshot, lambda *args: True, "example-secret",
        read_policy=lambda *args: policy, can_read_policy=lambda *args: True)
    adapter = results.ResultsApp(reader, lambda environ: ("example-tenant", "example-viewer"))
    for cutoff, expected_status in ((TIME, "409 Conflict"), (LATER, "200 OK"),
                                    ("2026-10-23T01:00:00+01:00", "200 OK"),
                                    ("2026-10-24T00:00:00Z", "409 Conflict")):
        status = []
        body = json.loads(b"".join(adapter({
            "PATH_INFO": "/engineering-insights/v1/projects/example-project/results",
            "REQUEST_METHOD": "GET", "QUERY_STRING": urlencode({
                "view": "business_context", "product_id": "example-product", "as_of": cutoff}),
        }, lambda value, headers: status.append(value))))
        assert status == [expected_status]
        if expected_status == "200 OK":
            assert (body["work_count"], body["cost_by_currency"]) == (1, {"USD": "9.25"})
        else:
            assert body == {"error": {"code": "projection_incomplete", "message": "Result unavailable"}}


@pytest.mark.parametrize("changed_document", ["snapshot", "policy", "quality"])
def test_dataset_version_binding_survives_pointer_changes(reader_fixture, changed_document):
    from insights.poc.devlake.results_publisher import publish_candidate

    storage = MemoryObjects()
    options = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
               "project_id": "example-project", "authorize": lambda descriptor: True}
    inputs = candidate_inputs()
    inputs.update(source_scope="source-policy", evidence_scope="evidence-policy")
    snapshot, quality = build_candidate(**inputs)
    policy = reader_fixture.qualified_shared_policy(snapshot)
    original = publish_candidate(**options, snapshot=snapshot, policy=policy, quality=quality)
    revised, revised_quality = build_candidate(**revised_inputs())
    publish_candidate(**options, snapshot=revised, quality=revised_quality,
                      policy=reader_fixture.qualified_shared_policy(revised))
    baseline = dict(storage.objects)
    changed = copy.deepcopy({"snapshot": snapshot, "quality": quality, "policy": policy})
    if changed_document == "snapshot":
        changed["snapshot"]["facts"][1]["amount"] = "100.00"
        changed["quality"]["cost_by_currency"] = {"USD": "100.00"}
        changed["policy"] = reader_fixture.qualified_shared_policy(changed["snapshot"])
    elif changed_document == "policy":
        changed["policy"]["policy_epoch"] = 2
    else:
        changed["quality"]["freshness"] = "changed"
    with pytest.raises(ValueError, match="dataset version already published"):
        publish_candidate(**options, **changed)
    assert storage.objects == baseline
    prefix = "results/v1/example-tenant/example-project/"
    assert json.loads(storage.objects[prefix + "datasets/example-dataset-1/manifest.json"]) == original
    assert publish_candidate(**options, snapshot=snapshot, quality=quality, policy=policy) == original
    assert json.loads(storage.objects[prefix + "published.json"]) == original
    replayed = dict(storage.objects)
    assert publish_candidate(**options, snapshot=snapshot, quality=quality, policy=policy) == original
    assert storage.objects == replayed


@pytest.mark.parametrize("selection_as_of", [None, "not-a-time", "2026-10-23T00:00:00", 123])
def test_invalid_selection_cutoff_fails_closed(reader_fixture, selection_as_of):
    snapshot, _ = build_candidate(**revised_inputs())
    snapshot["selection_as_of"] = selection_as_of
    policy = reader_fixture.qualified_shared_policy(snapshot)
    reader = results.ResultsReader(
        lambda *args: snapshot, lambda *args: True, "example-secret",
        read_policy=lambda *args: policy, can_read_policy=lambda *args: True)
    with pytest.raises(results.ReadError) as error:
        reader.read(tenant_id="example-tenant", viewer_id="example-viewer", project_id="example-project",
                    product_id="example-product", as_of=LATER)
    assert (error.value.status, error.value.code) == (409, "projection_incomplete")


def test_revision_ambiguity_depends_only_on_selected_timestamp():
    from insights.poc.devlake.revision import select_logical_revisions

    oldest = candidate_inputs()["facts"][0]
    superseded = copy.deepcopy(oldest)
    superseded["subject_ref"]["record_revision"] = "2"
    newest = copy.deepcopy(oldest)
    newest["subject_ref"]["record_revision"] = "3"
    newest["recorded_at"] = LATER
    for ordered in itertools.permutations((oldest, superseded, newest)):
        assert select_logical_revisions(iter(ordered), LATER) == [newest]
        with pytest.raises(ValueError, match="ambiguous"):
            select_logical_revisions(iter(ordered), TIME)
    superseded["recorded_at"] = LATER
    for ordered in itertools.permutations((oldest, superseded, newest)):
        with pytest.raises(ValueError, match="ambiguous"):
            select_logical_revisions(iter(ordered), LATER)


def test_publication_boundaries():
    import importlib.util
    import json

    from insights.poc.devlake.dataset import SNAPSHOT_LIMIT
    from insights.poc.devlake.results_publisher import publish_candidate, reconcile_pending

    spec = importlib.util.spec_from_file_location(
        "s06_i23_boundary_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    inputs = candidate_inputs()
    inputs["source_scope"] = "source-policy"
    inputs["evidence_scope"] = "evidence-policy"
    snapshot, quality = build_candidate(**inputs)
    storage = MemoryObjects()
    options = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
               "project_id": "example-project", "authorize": lambda descriptor: True}
    publish_candidate(**options, snapshot=snapshot, quality=quality,
                      policy=fixture.qualified_shared_policy(snapshot))
    prefix = "results/v1/example-tenant/example-project/"
    pointer = storage.objects[prefix + "published.json"]
    original = dict(storage.objects)

    partial = copy.deepcopy(snapshot)
    partial["dataset_version"] = "example-dataset-2"
    partial["facts"] = partial["facts"][:1]
    partial["relationships"] = partial["relationships"][:1]
    new_quality = {**quality, "dataset_version": "example-dataset-2"}
    with pytest.raises(ValueError, match="complete|totals"):
        publish_candidate(**options, snapshot=partial, quality=new_quality,
                          policy=fixture.qualified_shared_policy(partial))
    assert storage.objects == original

    mismatched = copy.deepcopy(snapshot)
    mismatched["relationships"][1]["to_ref"] = {
        **mismatched["relationships"][1]["to_ref"], "record_id": "other-product"}
    with pytest.raises(ValueError, match="Product attribution"):
        publish_candidate(**options, snapshot=mismatched, quality=quality,
                          policy=fixture.qualified_shared_policy(mismatched))
    assert storage.objects == original

    oversized = copy.deepcopy(snapshot)
    oversized["facts"][0]["payload"] = "x" * SNAPSHOT_LIMIT
    with pytest.raises(ValueError, match="limit"):
        publish_candidate(**options, snapshot=oversized, quality=quality,
                          policy=fixture.qualified_shared_policy(oversized))
    assert storage.objects == original

    with pytest.raises(ValueError, match="foreign Project"):
        publish_candidate(**{**options, "project_id": "foreign-project"},
                          snapshot=snapshot, quality=quality,
                          policy=fixture.qualified_shared_policy(snapshot))
    assert storage.objects == original

    with pytest.raises(PermissionError):
        publish_candidate(**{**options, "authorize": lambda descriptor: False},
                          snapshot=snapshot, quality=quality,
                          policy=fixture.qualified_shared_policy(snapshot))
    assert storage.objects == original

    private_policy = fixture.qualified_shared_policy(snapshot)
    private_policy["envelopes"][0]["required_policies"][0]["visibility"] = "private"
    with pytest.raises(results.ReadError):
        publish_candidate(**options, snapshot=snapshot, quality=quality, policy=private_policy)
    assert storage.objects == original

    pending = copy.deepcopy(snapshot)
    pending["dataset_version"] = "example-dataset-3"
    pending_quality = {**quality, "dataset_version": "example-dataset-3"}
    calls = []

    def revoked(descriptor):
        calls.append(descriptor["policy_epoch"])
        return len(calls) == 1

    with pytest.raises(PermissionError):
        publish_candidate(**{**options, "authorize": revoked},
                          snapshot=pending, quality=pending_quality,
                          policy=fixture.qualified_shared_policy(pending))
    assert calls == [1, 1]
    assert storage.objects[prefix + "published.json"] == pointer
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "pending"
    assert all(storage.objects[key] == value for key, value in original.items()
               if key != prefix + "pending.json")

    state = {"snapshot": snapshot, "policy": fixture.qualified_shared_policy(snapshot),
             "granted": False}
    with fixture.installed_gateway(snapshot, state) as (binary, environment, token, gateway_url):
        cli_code, cli = fixture.error_command(binary, environment, token)
        api_status, api = fixture.error_readback(gateway_url, token)
    assert (cli_code, cli["error"]["code"], api_status, api["error"]["code"]) == (
        5, "not_found", 404, "not_found")
    assert storage.objects[prefix + "published.json"] == pointer
    assert all(hidden not in json.dumps(cli) + json.dumps(api)
               for hidden in ("example-work", "example-charge", "12.50"))

    storage.fail_key = prefix + "published.json"
    with pytest.raises(OSError, match="interrupted"):
        reconcile_pending(**options)
    assert storage.objects[prefix + "published.json"] == pointer
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "pending"
    recovered = reconcile_pending(**options)
    assert recovered["dataset_version"] == "example-dataset-3"
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "completed"
    assert storage.pointer_writes == 2
    retained = next(value for key, value in original.items()
                    if "/example-dataset-1/snapshots/" in key)
    assert retained in storage.objects.values()
    after = dict(storage.objects)
    assert reconcile_pending(**options) is None
    assert storage.objects == after

    published = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-3/snapshots/{recovered['snapshot_sha256']}.json"])
    stored_policy = json.loads(storage.objects[
        prefix + f"datasets/example-dataset-3/policies/{recovered['policy_sha256']}.json"])
    with fixture.installed_gateway(published, {"snapshot": published, "policy": stored_policy,
                                             "granted": True}) as (binary, environment, token, gateway_url):
        result = fixture.read_result(binary, environment, token, gateway_url, TIME)
    assert (result["dataset_version"], result["coverage"], result["work_count"],
            result["cost_by_currency"]) == ("example-dataset-3", "complete", 1, {"USD": "12.50"})


def test_interrupted_pointer_write_reconciles_pinned_version_once():
    import importlib.util
    import json

    from insights.poc.devlake.results_publisher import publish_candidate, reconcile_pending

    spec = importlib.util.spec_from_file_location(
        "s06_i23_recovery_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    first = candidate_inputs()
    first["source_scope"] = "source-policy"
    first["evidence_scope"] = "evidence-policy"
    original, original_quality = build_candidate(**first)
    revised, revised_quality = build_candidate(**revised_inputs())
    storage = MemoryObjects()
    args = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
            "project_id": "example-project", "authorize": lambda descriptor: True}
    publish_candidate(**args, snapshot=original, quality=original_quality,
                      policy=fixture.qualified_shared_policy(original))
    prefix = "results/v1/example-tenant/example-project/"
    old_pointer = storage.objects[prefix + "published.json"]
    old_snapshot = next(value for key, value in storage.objects.items()
                        if "/example-dataset-1/snapshots/" in key)
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "completed"

    storage.fail_key = prefix + "published.json"
    with pytest.raises(OSError, match="interrupted"):
        publish_candidate(**args, snapshot=revised, quality=revised_quality,
                          policy=fixture.qualified_shared_policy(revised))
    assert storage.objects[prefix + "published.json"] == old_pointer
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "pending"
    assert old_snapshot in storage.objects.values()

    with pytest.raises(PermissionError):
        reconcile_pending(**{**args, "authorize": lambda descriptor: False})
    assert storage.objects[prefix + "published.json"] == old_pointer
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "pending"

    manifest_key = prefix + "datasets/example-dataset-2/manifest.json"
    manifest_body = storage.objects[manifest_key]
    conflicting_manifest = json.loads(manifest_body)
    conflicting_manifest["snapshot_sha256"] = "b" * 64
    storage.objects[manifest_key] = json.dumps(conflicting_manifest).encode()
    before_recovery = dict(storage.objects)
    with pytest.raises(ValueError, match="dataset version already published"):
        reconcile_pending(**args)
    assert storage.objects == before_recovery
    storage.objects[manifest_key] = manifest_body

    pointer = reconcile_pending(**args)
    assert pointer["dataset_version"] == "example-dataset-2"
    assert json.loads(storage.objects[prefix + "published.json"]) == pointer
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "completed"
    assert old_snapshot in storage.objects.values()
    recovered = dict(storage.objects)
    assert reconcile_pending(**args) is None
    assert storage.objects == recovered


def test_readback_outage_after_pointer_write_recovers_without_republishing():
    import importlib.util
    import json

    from insights.poc.devlake.results_publisher import publish_candidate, reconcile_pending

    spec = importlib.util.spec_from_file_location(
        "s06_i23_readback_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    first = candidate_inputs()
    first["source_scope"] = "source-policy"
    first["evidence_scope"] = "evidence-policy"
    original, original_quality = build_candidate(**first)
    revised, revised_quality = build_candidate(**revised_inputs())
    storage = MemoryObjects()
    options = {"storage": storage, "bucket": "example-bucket", "tenant_id": "example-tenant",
               "project_id": "example-project", "authorize": lambda descriptor: True}
    publish_candidate(**options, snapshot=original, quality=original_quality,
                      policy=fixture.qualified_shared_policy(original))
    storage.fail_after_pointer = True
    with pytest.raises(OSError, match="readback"):
        publish_candidate(**options, snapshot=revised, quality=revised_quality,
                          policy=fixture.qualified_shared_policy(revised))
    prefix = "results/v1/example-tenant/example-project/"
    assert json.loads(storage.objects[prefix + "published.json"])["dataset_version"] == "example-dataset-2"
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "pending"
    assert storage.pointer_writes == 2
    assert reconcile_pending(**options)["dataset_version"] == "example-dataset-2"
    assert storage.pointer_writes == 2
    assert json.loads(storage.objects[prefix + "pending.json"])["state"] == "completed"
    assert reconcile_pending(**options) is None
