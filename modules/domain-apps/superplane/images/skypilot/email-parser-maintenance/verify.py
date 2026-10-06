"""Check reviewed source and small ordinary address examples; no depth probes."""

import ast
import email._parseaddr as parser_module
import email.utils
import hashlib
import json
import marshal
from pathlib import Path
import sys


def normalized(value):
    return json.loads(json.dumps(value))


def main():
    root = Path(__file__).resolve().parent
    lock = json.loads((root / "source-lock.json").read_text())
    baseline = json.loads((root / "ordinary-baseline.json").read_text())
    source = Path(parser_module.__file__)
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == lock["after_sha256"]
    assert baseline["module_sha256"] == lock["before_sha256"]

    # Verify loaded function code, not merely the source file next to a stale
    # bytecode cache. Compilation here does not write a cache file.
    namespace = {}
    exec(
        compile(
            raw,
            str(source),
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        ),
        namespace,
    )
    for name in ("getaddress", "getdelimited"):
        installed = getattr(parser_module.AddrlistClass, name).__code__
        expected = getattr(namespace["AddrlistClass"], name).__code__
        assert marshal.dumps(installed) == marshal.dumps(expected), name

    tree = ast.parse(raw)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AddrlistClass"
    )
    calls = {
        method.name: {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        }
        for method in cls.body
        if isinstance(method, ast.FunctionDef)
    }

    def visit(name, active):
        assert name not in active, ("recursive parser call graph", name)
        for child in calls.get(name, ()):
            visit(child, active | {name})

    visit("getaddrlist", set())
    for case in baseline["cases"]:
        value = case["value"]
        actual = {
            "legacy": parser_module.AddressList(value).addresslist,
            "strict_getaddresses": email.utils.getaddresses([value]),
            "compat_getaddresses": email.utils.getaddresses([value], strict=False),
            "strict_parseaddr": email.utils.parseaddr(value),
            "compat_parseaddr": email.utils.parseaddr(value, strict=False),
        }
        for key, result in actual.items():
            assert normalized(result) == case[key], (case["name"], key, result)
        parser = parser_module.AddrlistClass(value)
        states = []
        while parser.pos < len(parser.field):
            before = parser.pos
            result = parser.getaddress()
            assert parser.pos > before, case["name"]
            states.append(
                {
                    "result": result,
                    "pos": parser.pos,
                    "comments": parser.commentlist[:],
                }
            )
        assert normalized(states) == case["address_states"], case["name"]

    methods = {
        "comment": "getcomment",
        "quote": "getquote",
        "domainliteral": "getdomainliteral",
    }
    for case in baseline["fragments"]:
        parser = parser_module.AddrlistClass(case["value"])
        result = getattr(parser, methods[case["kind"]])()
        assert (result, parser.pos, parser.commentlist) == (
            case["result"],
            case["pos"],
            case["comments"],
        ), case["value"]
    print(
        json.dumps(
            {
                "source_sha256": lock["after_sha256"],
                "ordinary_address_cases": len(baseline["cases"]),
                "ordinary_fragment_cases": len(baseline["fragments"]),
                "public_modes_and_cursor_comment_states": "passed",
                "loaded_code_matches_source": True,
                "parser_call_graph_acyclic": True,
            }
        )
    )


if __name__ == "__main__":
    main()
