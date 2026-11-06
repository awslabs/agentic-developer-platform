"""Validate proposed catalog wire and storage examples, not a deployed runtime."""

import copy
import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


HOME = Path(__file__).resolve().parent
CONTEXT = json.loads((HOME / "context-model-v1.schema.json").read_text())
SCHEMAS = json.loads((HOME / "catalog-schemas.json").read_text())
FIXTURES = json.loads((HOME / "catalog-contract-examples.json").read_text())
REGISTRY = Registry().with_resource(
    "https://adp.example.com/contracts/context-model-v1.schema.json",
    Resource.from_contents(CONTEXT),
)
FORMAT_CHECKER = FormatChecker()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def validator(schema):
    return Draft202012Validator(
        {"$ref": f"#/$defs/{schema}", "$defs": SCHEMAS["$defs"], "$id": SCHEMAS["$id"]},
        registry=REGISTRY,
        format_checker=FORMAT_CHECKER,
    )


def example(cases, name):
    case = cases[name]
    value = copy.deepcopy(case.get("value", cases[case["from"]]["value"])) if "from" in case else copy.deepcopy(case["value"])
    for path, replacement in case.get("changes", {}).items():
        target = value
        parts = path.split(".")
        for key in parts[:-1]:
            target = target[key]
        target[parts[-1]] = replacement
    return value


def time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def identity(ref):
    return ref["source_connection_id"], ref["record_id"]


def outcome(record, kind, memberships, contributions):
    if kind == "membership":
        if record["portfolio_ref"]["tenant_id"] != record["tenant_id"] or record["product_ref"]["tenant_id"] != record["tenant_id"]:
            return "invalid_request"
        key = (record["tenant_id"], identity(record["portfolio_ref"]), identity(record["product_ref"]))
        earlier = memberships.get(key)
        if earlier:
            if record["relationship_id"] != earlier["relationship_id"] or record.get("supersedes_revision") != earlier["relationship_revision"]:
                return "conflict"
            if record["valid_from"] != earlier["valid_from"] or time(record["recorded_at"]) <= time(earlier["recorded_at"]):
                return "invalid_request"
        elif "supersedes_revision" in record:
            return "conflict"
        if "valid_to" in record and time(record["valid_to"]) <= time(record["valid_from"]):
            return "invalid_request"
        memberships[key] = copy.deepcopy(record)
        return "accepted"

    data = record["data"]
    if data["product_ref"]["project_id"] != record["project_id"] or any(
        ref["tenant_id"] != record["tenant_id"] for ref in (data["product_ref"], data["initiative_ref"])
    ):
        return "invalid_request"
    if "valid_to" in data and time(data["valid_to"]) <= time(data["valid_from"]):
        return "invalid_request"
    key = (record["tenant_id"], identity(data["initiative_ref"]), identity(data["product_ref"]), record["project_id"])
    earlier = contributions.get(key)
    if earlier:
        if record["id"] != earlier["id"] or record["revision"] == earlier["revision"]:
            return "conflict"
        if time(record["recorded_at"]) <= time(earlier["recorded_at"]):
            return "invalid_request"
    contributions[key] = copy.deepcopy(record)
    return "accepted"


def main():
    Draft202012Validator.check_schema(SCHEMAS)
    cases = {case["name"]: case for case in FIXTURES["cases"]}
    require(len(cases) == len(FIXTURES["cases"]), "Duplicate case names")
    valid = invalid = 0
    for case in cases.values():
        record = example(cases, case["name"])
        errors = list(validator(case["schema"]).iter_errors(record))
        require(bool(errors) != case["valid"], f"Wrong schema result: {case['name']}: {errors}")
        valid += case["valid"]
        invalid += not case["valid"]
    memberships = {}
    contributions = {}
    accepted = rejected = 0
    for transaction in FIXTURES["transactions"]:
        record = example({**cases, transaction["name"]: transaction}, transaction["name"])
        schema = "membership_version" if transaction["type"] == "membership" else "contribution"
        require(not list(validator(schema).iter_errors(record)), transaction["name"])
        before = copy.deepcopy((memberships, contributions))
        actual = outcome(record, transaction["type"], memberships, contributions)
        require(actual == transaction["expect"], f"Wrong transaction result: {transaction['name']}: {actual}")
        if actual == "accepted":
            accepted += 1
        else:
            rejected += 1
            require(before == (memberships, contributions), f"Failed mutation changed state: {transaction['name']}")
    require(len(memberships) == 3 and len(contributions) == 1, "Fixture lost many-to-many membership or commitment")
    require(valid >= 7 and invalid >= 3 and accepted >= 5 and rejected >= 3, "Positive/negative fixture coverage missing")
    print(f"PASS: AC-01 {valid} valid, {invalid} invalid wire cases; {accepted} accepted, {rejected} rejected transactions")


if __name__ == "__main__":
    main()
