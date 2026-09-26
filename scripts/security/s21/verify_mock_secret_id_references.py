"""Prove exact mock-provider SecretId literals without resolving any resource."""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

from verify_model_reference_fixtures import imported, is_attribute, is_name
from verify_secret_git_objects import git, require

TEST_FILE = "modules/gateway/tests/vault/test_secrets_manager.py"
SERVICE_FILE = "modules/gateway/src/shared/services/secrets_manager.py"


def reject_rebinding(tree, names):
    for node in ast.walk(tree):
        require(
            not (
                isinstance(node, ast.Name)
                and node.id in names
                and isinstance(node.ctx, ast.Store)
            ),
            "Imported constructor is rebound",
        )
        require(
            not (isinstance(node, ast.arg) and node.arg in names),
            "Constructor is shadowed",
        )
        require(
            not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in names
            ),
            "Constructor is redefined",
        )


def verify_context(text, record, candidate):
    require(
        re.fullmatch(r"arn:[a-z0-9-]+", candidate), "Outside synthetic reference cohort"
    )
    tree = ast.parse(text)
    require(
        len(imported(tree, "unittest.mock", "MagicMock")) == 1,
        "Mock constructor import mismatch",
    )
    require(
        len(
            imported(
                tree, "src.shared.services.secrets_manager", "SecretsManagerHelper"
            )
        )
        == 1,
        "Helper constructor import mismatch",
    )
    reject_rebinding(tree, {"MagicMock", "SecretsManagerHelper"})
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                require(alias.name != "*", "Wildcard import obscures bindings")
                if bound in {"MagicMock", "SecretsManagerHelper"}:
                    require(
                        isinstance(node, ast.ImportFrom)
                        and alias.asname is None
                        and node.module
                        == {
                            "MagicMock": "unittest.mock",
                            "SecretsManagerHelper": "src.shared.services.secrets_manager",
                        }[bound],
                        "Conflicting constructor import",
                    )
    fixtures = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    mock = fixtures["mock_sm_client"]
    require(
        any(is_attribute(d, "pytest", "fixture") for d in mock.decorator_list),
        "Mock fixture missing",
    )
    require(
        any(
            isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and is_name(n.targets[0], "client")
            and isinstance(n.value, ast.Call)
            and is_name(n.value.func, "MagicMock")
            and not n.value.args
            and not n.value.keywords
            for n in mock.body
        ),
        "Client is not a plain mock",
    )
    require(
        sum(
            isinstance(n, ast.Name)
            and n.id == "client"
            and isinstance(n.ctx, ast.Store)
            for n in ast.walk(mock)
        )
        == 1,
        "Mock client rebound",
    )
    require(
        isinstance(mock.body[-1], ast.Return)
        and is_name(mock.body[-1].value, "client"),
        "Fixture does not return mock",
    )
    helper = fixtures["helper"]
    require(
        any(is_attribute(d, "pytest", "fixture") for d in helper.decorator_list),
        "Helper fixture missing",
    )
    require(
        [a.arg for a in helper.args.args] == ["mock_sm_client"],
        "Fixture injection mismatch",
    )
    require(
        len(helper.body) == 1 and isinstance(helper.body[0], ast.Return),
        "Helper fixture changed",
    )
    constructor = helper.body[0].value
    require(
        isinstance(constructor, ast.Call)
        and is_name(constructor.func, "SecretsManagerHelper")
        and not constructor.args
        and len(constructor.keywords) == 1
        and constructor.keywords[0].arg == "client"
        and is_name(constructor.keywords[0].value, "mock_sm_client"),
        "Mock client is not injected",
    )
    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == record["test_function"]
    ]
    require(len(functions) == 1, "Exact test function mismatch")
    function = functions[0]
    require(
        {"helper", "mock_sm_client"}.issubset({a.arg for a in function.args.args}),
        "Test fixture arguments missing",
    )
    require(
        not any(
            isinstance(n, ast.Name)
            and n.id in {"helper", "mock_sm_client"}
            and isinstance(n.ctx, ast.Store)
            for n in ast.walk(function)
        ),
        "Fixture reference rebound",
    )
    matches = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "assert_called_once_with" or not is_attribute(
            node.func.value, "mock_sm_client", record["provider_method"]
        ):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "SecretId"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == candidate
            ):
                require(
                    keyword.value.lineno == keyword.value.end_lineno == record["line"],
                    "Original literal line mismatch",
                )
                matches.append(node)
    require(len(matches) == 1, "Complete literal is not exact SecretId expectation")
    calls = [
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Call)
        and is_attribute(n.func, "helper", record["helper_method"])
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == candidate
    ]
    require(
        len(calls) == 1 and calls[0].lineno < matches[0].lineno,
        "Matching helper call missing",
    )


def verify_consumer(text, method):
    tree = ast.parse(text)
    helper = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "SecretsManagerHelper"
    )
    initializer = next(
        n
        for n in helper.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    assignments = [
        n
        for n in ast.walk(initializer)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and is_attribute(n.targets[0], "self", "_client")
    ]
    require(
        len(assignments) == 1
        and isinstance(assignments[0].value, ast.BoolOp)
        and isinstance(assignments[0].value.op, ast.Or)
        and is_name(assignments[0].value.values[0], "client"),
        "Injected client is not retained",
    )
    function = next(
        n for n in helper.body if isinstance(n, ast.FunctionDef) and n.name == method
    )
    require(
        [a.arg for a in function.args.args][:2] == ["self", "secret_arn"],
        "Helper identifier parameter mismatch",
    )
    require(
        not any(
            isinstance(n, ast.Name)
            and n.id == "secret_arn"
            and isinstance(n.ctx, ast.Store)
            for n in ast.walk(function)
        ),
        "Identifier parameter rebound",
    )
    if method == "get_secret_at_version":
        calls = [
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get_secret_value"
            and is_attribute(n.func.value, "self", "_client")
        ]
        require(
            len(calls) == 1
            and any(
                k.arg == "SecretId" and is_name(k.value, "secret_arn")
                for k in calls[0].keywords
            ),
            "Provider SecretId binding mismatch",
        )
    elif method == "delete_secret":
        bindings = [
            n
            for n in ast.walk(function)
            if isinstance(n, ast.AnnAssign) and is_name(n.target, "kwargs")
        ]
        require(
            len(bindings) == 1 and isinstance(bindings[0].value, ast.Dict),
            "Delete kwargs binding mismatch",
        )
        require(
            len(bindings[0].value.keys) == 1
            and isinstance(bindings[0].value.keys[0], ast.Constant)
            and bindings[0].value.keys[0].value == "SecretId"
            and is_name(bindings[0].value.values[0], "secret_arn"),
            "Delete identifier is not SecretId",
        )
        writes = [
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Subscript)
            and is_name(n.value, "kwargs")
            and isinstance(n.ctx, ast.Store)
        ]
        require(
            all(
                isinstance(n.slice, ast.Constant)
                and n.slice.value == "ForceDeleteWithoutRecovery"
                for n in writes
            ),
            "Identifier kwargs may be overwritten",
        )
        calls = [
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "delete_secret"
            and is_attribute(n.func.value, "self", "_client")
        ]
        require(
            len(calls) == 1
            and not calls[0].args
            and len(calls[0].keywords) == 1
            and calls[0].keywords[0].arg is None
            and is_name(calls[0].keywords[0].value, "kwargs"),
            "Delete provider kwargs mismatch",
        )
    else:
        raise ValueError("Unreviewed helper method")


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    require(
        len(records) == receipt["verified_delta"] and records, "Receipt count mismatch"
    )
    require(len({r["selector"] for r in records}) == len(records), "Duplicate selector")
    revision = receipt["source_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Invalid frozen revision")
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        digest = hashlib.sha1(group["secrets"].encode()).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), digest] = group["secrets"]
    fixture = git(source, "show", revision + ":" + TEST_FILE)
    service = git(source, "show", revision + ":" + SERVICE_FILE)
    for record in records:
        require(record["file"] == TEST_FILE, "Unreviewed fixture file")
        prefix, index = record["selector"].rsplit("|ri=", 1)
        require(
            prefix == "detect-secrets|" + TEST_FILE and index.isdecimal(),
            "Original selector mismatch",
        )
        original = scan[TEST_FILE][int(index)]
        require(
            original["line_number"] == record["line"]
            and original["type"] == record["detector"]
            and len(record["candidate_hash_prefix"]) == 16
            and original["hashed_secret"].startswith(record["candidate_hash_prefix"]),
            "Original identity mismatch",
        )
        candidate = candidates[TEST_FILE, record["line"], original["hashed_secret"]]
        verify_context(fixture, record, candidate)
        verify_consumer(service, record["helper_method"])
    print(
        f"Verified {len(records)} exact mock SecretId references; no values emitted or resources resolved"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.audit, args.receipt)
    except Exception:  # noqa: BLE001 - private inputs must never appear in errors
        parser.exit(1, "Verification failed; private values withheld\n")
