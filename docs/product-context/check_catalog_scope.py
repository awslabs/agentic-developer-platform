"""Independent fixture check of the proposed catalog authorization contract."""

import json
from pathlib import Path

from openapi_spec_validator import validate

from check_catalog_contract import HOME, FIXTURES, require, validator


SPEC_PATH = HOME / "catalog-openapi.json"
SPEC = json.loads(SPEC_PATH.read_text())
SCOPE = json.loads((HOME / "catalog-scope-examples.json").read_text())


def decide(case):
    grants = set(case["grants"])
    sources = set(case.get("sources", ["adp.example-catalog"]))
    visible = {
        row["id"] for row in SCOPE["projections"]
        if row["project_id"] in grants and row["source"] in sources
    }
    cursor = case.get("cursor")
    if cursor and (
        cursor["actor"] != "example-actor" or set(cursor["grants"]) != grants
        or set(cursor["sources"]) != sources or cursor["filter"] != "portfolios"
        or cursor["source_revision"] != SCOPE["source_revision"]
        or cursor["limit"] != case.get("limit", 50)
        or cursor["expires"] <= SCOPE["clock"]
    ):
        return {"status": 400, "code": "invalid_cursor"}
    if case["action"] == "list":
        ordered = sorted(visible)
        if cursor:
            ordered = [item for item in ordered if item > cursor["last"]]
        limit = case.get("limit", 50)
        items = ordered[:limit]
        return {"status": 200, "items": items, **({"next_cursor": True} if len(ordered) > limit else {})}
    if case["id"] not in visible:
        return {"status": 404, "code": "not_found"}
    if case["action"] == "members":
        items = sorted({row["product_id"] for row in SCOPE["memberships"] if row["portfolio_id"] == case["id"] and row["project_id"] in grants})
        return {"status": 200, "items": items}
    if case["action"] == "mutate":
        project = case.get("product_project")
        if project and project not in grants:
            return {"status": 404, "code": "not_found"}
        if not case.get("editor") or project and project not in case.get("writes", []):
            return {"status": 403, "code": "permission_denied"}
        if case.get("expected_revision", "1") != case.get("current_revision", "1"):
            return {"status": 412, "code": "stale_revision"}
        return {"status": 201, "receipt": True}
    return {"status": 200, "items": [case["id"]]}


def main():
    validate(SPEC, base_uri=SPEC_PATH.as_uri())
    operations = [operation for path in SPEC["paths"].values() for method, operation in path.items() if method in ("get", "post", "patch")]
    require(len(operations) == 15, "Catalog lookup and write routes lost an operation")
    require(len({operation["operationId"] for operation in operations}) == len(operations), "Duplicate operation ID")
    for path in SPEC["paths"].values():
        for method in ("post", "patch"):
            if method not in path:
                continue
            parameters = [parameter["$ref"] for parameter in path[method]["parameters"]]
            require("#/components/parameters/key" in parameters, "Write missing idempotency key")
            if method == "patch":
                require("#/components/parameters/revision" in parameters, "Revision write missing If-Match")
    examples = {case["name"]: case["value"] for case in FIXTURES["cases"] if "value" in case}
    record = examples["commerce-portfolio"]
    require(not list(validator("portfolio_page").iter_errors({"items": [record]})), "Authorized page shape invalid")
    require(list(validator("portfolio_page").iter_errors({"items": [record], "total_count": 4})), "Hidden count field admitted")
    require(not list(validator("error").iter_errors({"code": "not_found", "message": "Not found"})), "Error shape invalid")
    require(list(validator("error").iter_errors({"code": "not_found", "message": "Not found", "foreign_id": "hidden"})), "Foreign ID disclosure admitted")
    names = set()
    for case in SCOPE["cases"]:
        require(case["name"] not in names, "Duplicate scope fixture")
        names.add(case["name"])
        actual = decide(case)
        require(actual == case["expect"], f"Scope result mismatch: {case['name']}: {actual}")
    require(decide(next(case for case in SCOPE["cases"] if case["name"] == "foreign-id")) == decide(next(case for case in SCOPE["cases"] if case["name"] == "nonexistent-id")), "Foreign and absent reveal different errors")
    print(f"PASS: AC-02 OpenAPI and {len(names)} scoped lookup/write/cursor fixture cases; hidden fields rejected")


if __name__ == "__main__":
    main()
