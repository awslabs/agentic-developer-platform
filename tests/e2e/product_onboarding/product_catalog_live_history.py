"""Check served dated catalog relationships with separately authenticated actors."""

from urllib.parse import quote, urlencode

from tests.e2e.new_ui.product_fixture_ports import checked_json
from tests.e2e.new_ui.product_release import check_served_assets
from tests.e2e.product_onboarding.product_catalog_history import (
    pinned_history_scenario, record_history_expectations, verify_history_sessions,
)


def verify_live_history(provider, sessions, source_revision, native_reader):
    state = provider.state
    if not callable(native_reader):
        raise ValueError("Live history requires an independent native-work reader")
    history = state.document.get("catalog_history")
    if not isinstance(history, dict) or not history.get("cleanup_pending"):
        raise ValueError("Live history requires an active protected run-owned fixture")
    plan = history["plan"]
    if (plan != pinned_history_scenario(source_revision, plan["requirements_revision"], provider.run_id,
                                        plan["actors"], plan["products"], plan["timeline"], plan["selectors"])
            or state.binding["actor_id"] != plan["actors"]["editor"]["actor_id"]
            or state.binding["tenant_id"] != plan["actors"]["editor"]["tenant_id"]):
        raise ValueError("Live history differs from original protected Product scopes")
    expected = history.get("expected")
    if (not isinstance(expected, dict) or set(expected.get("contributions", {})) != {
        product["project_id"] for product in plan["products"]
    } or not isinstance(expected.get("membership"), dict)):
        raise ValueError("Live history requires independently specified protected expectations")
    record_history_expectations(state, expected)
    check_served_assets(provider.request, provider.base_url, provider.frontend, source_revision)
    verify_history_sessions(provider.request, provider.base_url, plan, sessions)
    headers = {role: {"Authorization": "Bearer " + session["access_token"]} for role, session in sessions.items()}

    def read(path, actor="editor", **times):
        suffix = "?" + urlencode(times) if times else ""
        return provider.request.get(provider.base_url + path + suffix, headers=headers[actor], timeout=30_000)

    timeline = plan["timeline"]
    initiative = checked_json(read("/api/v1/catalog/initiatives/" + quote(expected["initiative_id"], safe="")))
    if initiative["id"] != expected["initiative_id"]:
        raise AssertionError("Cross-product Initiative differs from the selected shared receipt")
    membership = expected["membership"]
    path = "/api/v1/catalog/portfolios/" + quote(membership["portfolio_id"], safe="") + "/memberships"
    before = read(path, effective_at=timeline["valid_from"], decision_at=timeline["before_correction"])
    after = read(path, effective_at=timeline["switch"], decision_at=timeline["after_correction"])
    old_rows, new_rows = checked_json(before)["items"], checked_json(after)["items"]
    if (before.headers.get("X-Catalog-View") != "historical"
            or after.headers.get("X-Catalog-View") != "historical"
            or len(old_rows) != 1 or new_rows != []
            or (old_rows[0]["relationship_id"], old_rows[0]["relationship_revision"], old_rows[0]["valid_from"]) != (
                membership["relationship_id"], membership["revision"], membership["valid_from"]
            ) or (old_rows[0]["product_ref"]["record_id"], old_rows[0]["product_ref"]["project_id"],
                  old_rows[0]["product_ref"]["tenant_id"]) != (
                plan["products"][0]["product_id"], plan["products"][0]["project_id"], state.binding["tenant_id"]
            )):
        raise AssertionError("Served effective-time membership differs from the protected dated receipt")

    observed = {}
    for product in plan["products"]:
        project_id = product["project_id"]
        product_id = product["product_id"]
        profile = checked_json(read(f"/api/v1/products/{quote(product_id, safe='')}/projects/{quote(project_id, safe='')}"))
        if (profile["product_id"], profile["project_id"]) != (product_id, project_id):
            raise AssertionError("Catalog corrections changed Product native identity")
        receipt = expected["contributions"][project_id]
        path = f"/api/v1/catalog/projects/{quote(project_id, safe='')}/contributions"
        for time, effective_at, decision_at in (
            ("before", timeline["valid_from"], timeline["before_correction"]),
            ("after", timeline["switch"], timeline["after_correction"]),
        ):
            response = read(path, effective_at=effective_at, decision_at=decision_at)
            rows = checked_json(response)["items"]
            if response.headers.get("X-Catalog-View") != "historical" or len(rows) != 1:
                raise AssertionError("Served dated Contribution has missing or duplicate relationships")
            row = rows[0]
            actual = (row["id"], row["revision"], row["data"]["objective"],
                      [link["work_ref"]["record_id"] for link in row["data"]["work_links"]],
                      row["data"]["product_ref"]["record_id"],
                      row["data"]["initiative_ref"]["record_id"])
            wanted = receipt[time]
            if (actual != (receipt["id"], wanted["revision"], wanted["objective"], wanted["work_ids"],
                           product_id, expected["initiative_id"])
                    or any(link["work_ref"]["project_id"] != project_id
                           or link["work_ref"]["tenant_id"] != state.binding["tenant_id"]
                           for link in row["data"]["work_links"])):
                raise AssertionError("Served Contribution differs from independently pinned dated work links")
        observed[project_id] = receipt["id"]

    for product in plan["products"]:
        native = native_reader(product, sessions["editor"])
        if (not isinstance(native, list) or len(native) != 1 or not isinstance(native[0], dict)
                or native[0].get("id") != product["work_id"]
                or native[0].get("project_id") != product["project_id"]):
            raise AssertionError("Catalog correction duplicated or changed native project work")

    visible = checked_json(read("/api/v1/catalog/projects/" + quote(plan["products"][0]["project_id"], safe="")
                                + "/contributions", "authorized", effective_at=timeline["clock"],
                                decision_at=timeline["clock"]))
    if (len(visible["items"]) != 1 or visible["items"][0]["id"] != observed[plan["products"][0]["project_id"]]
            or visible["items"][0]["data"]["product_ref"]["record_id"] != plan["products"][0]["product_id"]):
        raise AssertionError("Authorized Product scope lost its own Contribution")
    for actor, product in (("authorized", plan["products"][1]), ("revoked", plan["products"][0])):
        denied = read("/api/v1/catalog/projects/" + quote(product["project_id"], safe="") + "/contributions",
                      actor, effective_at=timeline["clock"], decision_at=timeline["clock"])
        body = denied.json()
        if (denied.status not in ((404,) if actor == "authorized" else (403, 404))
                or not isinstance(body, dict) or set(body) != {"detail"}
                or any(item["work_id"] in str(body) for item in plan["products"])):
            raise AssertionError("Cross-product Initiative exposed another or revoked Product scope")
        if actor == "authorized":
            unknown = read("/api/v1/catalog/projects/unknown/contributions", "authorized",
                           effective_at=timeline["clock"], decision_at=timeline["clock"])
            if unknown.status != 404 or unknown.json() != body:
                raise AssertionError("Foreign Product disclosure differs from the absent-scope oracle")
    return {"run_id": provider.run_id, "source_revision": source_revision,
            "contributions": observed, "dated_membership": membership["relationship_id"],
            "denials_verified": True, "native_work_verified": True}
