"""Prove synthetic ORM resource references without resolving any resource."""

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

from verify_secret_git_objects import git, require

MODEL_MODULE = "src.shared.models.vault"
MODEL_FILE = "modules/gateway/src/shared/models/vault.py"
DELIVERY_FILE = "modules/gateway/src/auth/vault_delivery.py"
SERVICE_FILE = "modules/gateway/src/shared/services/secrets_manager.py"


def is_name(node, name):
    return isinstance(node, ast.Name) and node.id == name


def is_attribute(node, name, attribute):
    return (
        isinstance(node, ast.Attribute)
        and is_name(node.value, name)
        and node.attr == attribute
    )


def imported(tree, module, name):
    return [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == module
        and any(alias.name == name and alias.asname is None for alias in node.names)
    ]


def verify_context(text, record, candidate):
    # This grammar alone never classifies a candidate: it only limits this
    # reviewed cohort to deliberately incomplete ARN fixtures or AWS SecretId names.
    require(
        re.fullmatch(
            r"(?:arn(?::[a-z0-9-]+|[0-9]+)|[A-Za-z0-9/_+=.@-]{1,512})", candidate
        ),
        "Not a reviewed synthetic identifier",
    )
    tree = ast.parse(text)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == MODEL_MODULE
        and any(
            alias.name == "UserCredential" and alias.asname is None
            for alias in node.names
        )
    ]
    require(
        len(imports) == 1 and imports[0].lineno == record["import_line"],
        "Model import binding mismatch",
    )
    for node in ast.walk(tree):
        require(
            not (
                isinstance(node, ast.Name)
                and node.id == "UserCredential"
                and isinstance(node.ctx, ast.Store)
            ),
            "Model constructor is rebound",
        )
        require(
            not (isinstance(node, ast.arg) and node.arg == "UserCredential"),
            "Model constructor is shadowed",
        )
        require(
            not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == "UserCredential"
            ),
            "Model constructor is redefined",
        )
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node is not imports[0]:
            require(
                not any(
                    (alias.asname or alias.name.split(".")[0]) == "UserCredential"
                    for alias in node.names
                ),
                "Conflicting constructor import",
            )
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and node.value == candidate
        and node.lineno == record["line"] == node.end_lineno
    ]
    require(len(literals) == 1, "Exact complete source literal missing or ambiguous")
    literal = literals[0]
    keyword = parents[literal]
    require(
        isinstance(keyword, ast.keyword) and keyword.arg == "secret_arn",
        "Literal is not a resource-reference argument",
    )
    call = parents[keyword]
    require(
        isinstance(call, ast.Call)
        and is_name(call.func, "UserCredential")
        and call.lineno == record["constructor_line"],
        "Imported model constructor mismatch",
    )
    import_scope = parents[imports[0]]
    require(
        isinstance(import_scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)),
        "Constructor import is conditional or in an unreviewed scope",
    )
    require(imports[0].lineno < call.lineno, "Constructor import follows use")
    if not isinstance(import_scope, ast.Module):
        cursor = call
        while cursor is not import_scope and cursor in parents:
            cursor = parents[cursor]
        require(
            cursor is import_scope,
            "Function-local constructor import is outside call scope",
        )
    require(
        not any(k.arg is None for k in call.keywords),
        "Dynamic constructor arguments not reviewed",
    )


def verify_consumers(blob):
    """Bind the ORM attribute through the typed delivery call to SecretId."""
    model = ast.parse(blob(MODEL_FILE))
    classes = [
        n
        for n in model.body
        if isinstance(n, ast.ClassDef) and n.name == "UserCredential"
    ]
    require(len(classes) == 1, "UserCredential model missing")
    fields = [
        n
        for n in classes[0].body
        if isinstance(n, ast.AnnAssign) and is_name(n.target, "secret_arn")
    ]
    require(
        len(fields) == 1
        and isinstance(fields[0].value, ast.Call)
        and is_name(fields[0].value.func, "mapped_column"),
        "ORM reference column missing",
    )
    delivery = ast.parse(blob(DELIVERY_FILE))
    require(
        imported(delivery, MODEL_MODULE, "UserCredential")
        and imported(
            delivery, "src.shared.services.secrets_manager", "SecretsManagerHelper"
        ),
        "Delivery type imports missing",
    )
    functions = {
        n.name: n
        for n in delivery.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    authorization = functions["authorize_delivery"]
    require(
        is_name(authorization.returns, "UserCredential"),
        "Authorization return model mismatch",
    )
    function = functions["deliver_credential"]
    require(
        any(
            a.arg == "sm" and is_name(a.annotation, "SecretsManagerHelper")
            for a in function.args.args + function.args.kwonlyargs
        ),
        "Delivery helper type mismatch",
    )
    bindings = [
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and is_name(n.targets[0], "credential")
    ]
    require(
        len(bindings) == 1
        and isinstance(bindings[0].value, ast.Await)
        and isinstance(bindings[0].value.value, ast.Call)
        and is_name(bindings[0].value.value.func, "authorize_delivery"),
        "Credential model result is not bound",
    )
    calls = [
        n
        for n in ast.walk(function)
        if isinstance(n, ast.Call)
        and is_attribute(n.func, "asyncio", "to_thread")
        and len(n.args) == 3
        and is_attribute(n.args[0], "sm", "get_secret_at_version")
        and is_attribute(n.args[1], "credential", "secret_arn")
        and is_name(n.args[2], "version_id")
    ]
    require(
        len(calls) == 1, "Model reference does not reach helper identifier argument"
    )
    service = ast.parse(blob(SERVICE_FILE))
    helpers = [
        n
        for n in service.body
        if isinstance(n, ast.ClassDef) and n.name == "SecretsManagerHelper"
    ]
    require(len(helpers) == 1, "Secrets Manager helper missing")
    methods = [
        n
        for n in helpers[0].body
        if isinstance(n, ast.FunctionDef) and n.name == "get_secret_at_version"
    ]
    require(
        len(methods) == 1
        and [a.arg for a in methods[0].args.args]
        == ["self", "secret_arn", "version_id"],
        "Helper argument binding mismatch",
    )
    require(
        not any(
            isinstance(n, ast.Name)
            and n.id == "secret_arn"
            and isinstance(n.ctx, ast.Store)
            for n in ast.walk(methods[0])
        ),
        "Helper identifier argument is rebound",
    )
    calls = [
        n
        for n in ast.walk(methods[0])
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
        "Reference is not bound to AWS SecretId",
    )


def verify(source, scan_path, audit_path, receipt_path):
    receipt = json.loads(receipt_path.read_text())
    records = receipt["verified_records"]
    require(
        records and len(records) == receipt["verified_delta"], "Receipt count mismatch"
    )
    require(
        len({r["selector"] for r in records}) == len(records),
        "Duplicate original selector",
    )
    revision = receipt["source_revision"]
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Invalid frozen revision")
    scan = json.loads(scan_path.read_text())["results"]
    audit = json.loads(audit_path.read_text())["results"]
    candidates = {}
    for group in audit:
        digest = hashlib.sha1(group["secrets"].encode(), usedforsecurity=False).hexdigest()
        for line in group["lines"]:
            candidates[group["filename"], int(line), digest] = group["secrets"]
    blobs = {}

    def blob(path):
        if path not in blobs:
            blobs[path] = git(source, "show", revision + ":" + path)
        return blobs[path]

    require(
        receipt["consumer_files"] == [MODEL_FILE, DELIVERY_FILE, SERVICE_FILE],
        "Consumer manifest mismatch",
    )
    verify_consumers(blob)
    for record in records:
        prefix, index = record["selector"].rsplit("|ri=", 1)
        require(
            prefix == "detect-secrets|" + record["file"] and index.isdecimal(),
            "Original selector mismatch",
        )
        original = scan[record["file"]][int(index)]
        require(
            original["line_number"] == record["line"]
            and original["type"] == record["detector"]
            and len(record["candidate_hash_prefix"]) == 16
            and original["hashed_secret"].startswith(record["candidate_hash_prefix"]),
            "Original scan identity mismatch",
        )
        candidate = candidates[
            record["file"], record["line"], original["hashed_secret"]
        ]
        verify_context(blob(record["file"]), record, candidate)
    print(
        f"Verified {len(records)} original model-reference fixtures; no values emitted or resources resolved"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "scan", "audit", "receipt"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(args.source, args.scan, args.audit, args.receipt)
    except Exception:  # noqa: BLE001 - redact every private-input failure
        parser.exit(1, "Verification failed; private inputs and values withheld\n")
