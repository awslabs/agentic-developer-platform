"""Local S13 access scenarios; deployed authority evidence is collected separately."""

import copy
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SPEC = importlib.util.spec_from_file_location(
    "s13_local_gateway_fixture", Path(__file__).resolve().parents[1] / "test_i23_registry.py"
)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_source_policy_reads():
    shared, _, _, _ = fixture.fixed_snapshot()
    shared_policy = fixture.qualified_shared_policy(shared)
    state = {
        "snapshot": shared, "policy": shared_policy, "granted": True,
        "source_granted": True, "evidence_granted": True,
    }
    with fixture.installed_gateway(shared, state) as (binary, environment, token, gateway_url):
        authorized = fixture.read_result(binary, environment, token, gateway_url, fixture.registry.EVENT_TIME)
        assert authorized["work_count"] == 1
        assert authorized["cost_by_currency"] == {"USD": "10.00"}
        assert authorized["coverage"] == "complete"
        assert {item["subject_ref"]["record_id"] for item in authorized["contexts"]} == {
            "example-work", "example-cost"
        }
        assert len(fixture.LocalGateway.requests) == 2

        for grant in ("source_granted", "evidence_granted"):
            state[grant] = False
            cli_exit, cli = fixture.error_command(binary, environment, token)
            api_status, api = fixture.error_readback(gateway_url, token)
            assert (cli_exit, cli["error"]["code"]) == (5, "not_found")
            assert (api_status, api) == (
                404, {"error": {"code": "not_found", "message": "Result unavailable"}}
            )
            assert not any(hidden in json.dumps(cli) + json.dumps(api) for hidden in (
                "example-cost", "example-work", "example-source", "10.00", "work_count"
            ))
            state[grant] = True

        restricted = copy.deepcopy(shared)
        private_cost = copy.deepcopy(shared["facts"][1])
        private_cost["subject_ref"]["record_id"] = "example-private-cost"
        private_cost["amount"] = "900.00"
        restricted["facts"].append(private_cost)
        private_link = copy.deepcopy(shared["relationships"][1])
        private_link["relationship_id"] = "example-private-link"
        private_link["from_ref"] = private_cost["subject_ref"]
        restricted["relationships"].append(private_link)
        private_policy = fixture.qualified_shared_policy(restricted)
        private_policy["envelopes"][0]["required_policies"][1]["visibility"] = "private"
        state["snapshot"] = restricted
        state["policy"] = private_policy
        cli_exit, cli = fixture.error_command(binary, environment, token)
        api_status, api = fixture.error_readback(gateway_url, token)
        assert (cli_exit, cli["error"]["code"]) == (4, "projection_incomplete")
        assert (api_status, api) == (
            409, {"error": {"code": "projection_incomplete", "message": "Result unavailable"}}
        )
        assert "900.00" not in json.dumps(cli) + json.dumps(api)
        assert "example-private-cost" not in json.dumps(cli) + json.dumps(api)

        state["snapshot"] = shared
        state["policy"] = shared_policy
        current = fixture.read_result(binary, environment, token, gateway_url, fixture.registry.EVENT_TIME)
        assert (current["work_count"], current["cost_by_currency"], current["coverage"]) == (
            1, {"USD": "10.00"}, "complete"
        )


def test_live_revocation():
    shared, _, _, _ = fixture.fixed_snapshot()
    state = {
        "snapshot": shared, "policy": fixture.qualified_shared_policy(shared),
        "granted": True, "source_granted": True, "evidence_granted": True,
    }
    with fixture.installed_gateway(shared, state) as (_, _, token, gateway_url):
        endpoint = f"{gateway_url}/engineering-insights/v1/projects/{fixture.registry.PROJECT}/results"

        def request(*, limit=100, cursor=None):
            params = [
                ("view", "business_context"), ("product_id", "example-product"),
                ("as_of", fixture.registry.EVENT_TIME), ("limit", limit),
            ]
            if cursor is not None:
                params.append(("cursor", cursor))
            call = Request(f"{endpoint}?{urlencode(params)}", headers={"Authorization": f"Bearer {token}"})
            try:
                with urlopen(call, timeout=15) as response:
                    return response.status, response.read()
            except HTTPError as error:
                with error:
                    return error.code, error.read()

        status, body = request(limit=1)
        assert status == 200
        cursor = json.loads(body)["next_cursor"]
        assert cursor
        loaded = threading.Event()
        release = threading.Event()
        reads = 0
        pause = False

        def on_snapshot_load():
            nonlocal reads
            reads += 1
            if pause:
                loaded.set()
                assert release.wait(timeout=10), "snapshot load barrier timed out"

        state["on_snapshot_load"] = on_snapshot_load
        for grant in ("source_granted", "evidence_granted", "granted"):
            loaded.clear()
            release.clear()
            pause = True
            before = reads
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(request)
                try:
                    assert loaded.wait(timeout=5), "snapshot load was not reached"
                    state[grant] = False
                finally:
                    release.set()
                status, body = pending.result(timeout=15)
            pause = False
            assert reads == before + 1
            assert status == 404
            assert json.loads(body) == {"error": {"code": "not_found", "message": "Result unavailable"}}
            assert not any(value in body for value in (b"example-work", b"example-cost", b"10.00"))

            for options in ({}, {"limit": 1, "cursor": cursor}):
                status, body = request(**options)
                assert status == 404
                assert json.loads(body) == {"error": {"code": "not_found", "message": "Result unavailable"}}
                assert reads == before + 1

            state[grant] = True
            status, body = request(limit=1, cursor=cursor)
            assert status == 200
            assert len(json.loads(body)["contexts"]) == 1


def test_scope_boundaries():
    shared, _, _, _ = fixture.fixed_snapshot()
    policy = fixture.qualified_shared_policy(shared)
    state = {"snapshot": shared, "policy": policy, "granted": True}
    trusted_realm = tuple(policy["envelopes"][0][key] for key in (
        "realm_id", "realm_revision", "descriptor_digest"
    ))
    outage = [False]

    def current_authority(descriptor):
        if outage[0]:
            raise OSError("authority unavailable")
        return descriptor["policy_epoch"] == policy["policy_epoch"] and all(
            tuple(envelope[key] for key in ("realm_id", "realm_revision", "descriptor_digest")) == trusted_realm
            for envelope in descriptor["envelopes"]
        )

    state["policy_authority"] = current_authority
    reads = []
    state["on_snapshot_load"] = lambda: reads.append((state["snapshot"]["dataset_version"]))
    with fixture.installed_gateway(shared, state) as (_, _, token, gateway_url):
        def request(*, project=fixture.registry.PROJECT, bearer=None, cursor=None, limit=1, extra=()):
            query = [
                ("view", "business_context"), ("product_id", "example-product"),
                ("as_of", fixture.registry.EVENT_TIME), ("limit", limit), *extra,
            ]
            if cursor is not None:
                query.append(("cursor", cursor))
            url = (f"{gateway_url}/engineering-insights/v1/projects/{project}/results"
                   f"?{urlencode(query)}")
            call = Request(url, headers={"Authorization": f"Bearer {token if bearer is None else bearer}"})
            try:
                with urlopen(call, timeout=5) as response:
                    return response.status, response.read(), response.headers
            except HTTPError as error:
                with error:
                    return error.code, error.read(), error.headers

        def denied(expected_status, expected_code, *, no_snapshot=True, **kwargs):
            before = len(reads)
            status, body, headers = request(**kwargs)
            assert status == expected_status
            assert json.loads(body) == {"error": {"code": expected_code, "message": "Result unavailable"}}
            assert headers["Cache-Control"] == "no-store"
            assert not any(hidden in body for hidden in (
                b"example-work", b"example-cost", b"example-source", b"10.00", b"work_count",
                b"dataset_version",
            ))
            if no_snapshot:
                assert len(reads) == before

        status, body, _ = request()
        assert status == 200
        assert json.loads(body)["cost_by_currency"] == {"USD": "10.00"}
        old_cursor = json.loads(body)["next_cursor"]
        assert old_cursor

        denied(404, "not_found", project="other-project")
        denied(401, "unauthenticated", bearer=token + ".other-tenant")
        denied(400, "invalid_request", project="..")
        denied(400, "invalid_request", extra=(("path", "results/v1/other-project/published.json"),))
        denied(400, "invalid_request", no_snapshot=False, cursor="0." + "a" * 513)
        denied(400, "invalid_request", limit=101)

        state["policy"] = None
        denied(409, "projection_incomplete")
        denied(409, "projection_incomplete", cursor=old_cursor)
        for field, value in (("tenant_id", "other-tenant"), ("project_id", "other-project")):
            foreign = copy.deepcopy(policy)
            foreign[field] = value
            state["policy"] = foreign
            denied(409, "projection_incomplete")
        foreign = copy.deepcopy(policy)
        foreign["envelopes"][0]["realm_id"] = "other-realm"
        state["policy"] = foreign
        denied(404, "not_found")
        unqualified = copy.deepcopy(policy)
        unqualified["envelopes"][0]["audience"] = "private"
        state["policy"] = unqualified
        denied(409, "projection_incomplete")
        unqualified = copy.deepcopy(policy)
        unqualified["schema_version"] = "results-policy/2.0"
        state["policy"] = unqualified
        denied(409, "projection_incomplete")
        foreign = copy.deepcopy(policy)
        foreign["policy_epoch"] += 1
        state["policy"] = foreign
        denied(404, "not_found", cursor=old_cursor)
        state["policy"] = policy

        outage[0] = True
        denied(409, "projection_incomplete")
        denied(409, "projection_incomplete", cursor=old_cursor)
        outage[0] = False
        status, body, _ = request(cursor=old_cursor)
        assert status == 200
        assert len(json.loads(body)["contexts"]) == 1

        other_version = copy.deepcopy(shared)
        other_version["dataset_version"] = "example-dataset-2"
        state["snapshot"] = other_version
        state["policy"] = fixture.qualified_shared_policy(other_version)
        status, body, _ = request()
        assert status == 200
        foreign_cursor = json.loads(body)["next_cursor"]
        assert foreign_cursor
        state["snapshot"] = shared
        state["policy"] = policy
        denied(400, "invalid_request", no_snapshot=False, cursor=foreign_cursor)
        status, body, _ = request(cursor=old_cursor)
        assert status == 200
        assert json.loads(body)["dataset_version"] == shared["dataset_version"]
