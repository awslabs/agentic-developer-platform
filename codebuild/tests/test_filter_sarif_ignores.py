"""Exercise positive scope evidence and conservative retention on malformed input."""

import importlib.util
import json
import sys
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location(
    "scoped_filter", Path(__file__).parents[1] / "filter-sarif-ignores.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


@pytest.mark.parametrize(
    "name,kind,expected",
    [
        ("stdlib", "go-module", True),
        ("stdlib", "deb", False),
        ("stdlib", None, False),
        (None, "go-module", False),
        ("other", "go-module", False),
    ],
)
def test_all_constraints_need_evidence(name, kind, expected):
    sel = m.IgnoreSelector("CVE-2026-1234", "stdlib", "go-module")
    assert (
        m.match_result("CVE-2026-1234-stdlib", [sel], name, kind).suppressed is expected
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"vulnerability": "CVE-2026-1234"},
        {
            "vulnerability": "CVE-2026-1234",
            "package": {"name": "stdlib", "version": "1.0"},
        },
        {
            "vulnerability": "CVE-2026-1234",
            "package": {"name": "stdlib"},
            "image": "only-this-image",
        },
        {"vulnerability": "CVE-2026-1234", "package": {"type": "go-module"}},
    ],
)
def test_unscoped_or_unsupported_rules_do_not_broaden(entry):
    sel = m.IgnoreSelector.from_config_entry(entry)
    assert not m.match_result(
        "CVE-2026-1234-stdlib", [sel], "stdlib", "go-module"
    ).suppressed


@pytest.mark.parametrize(
    "rule_id", ["CVE-2026-12345-stdlib", "CVE-2026-1234x", "GHSA-abcd-efgh-ijkl-stdlib"]
)
def test_no_prefix_or_unreviewed_alias_match(rule_id):
    assert not m.match_result(
        rule_id, [m.IgnoreSelector("CVE-2026-1234", "stdlib")], "stdlib", "go-module"
    ).suppressed


def test_real_grype_metadata_multiple_runs_and_ambiguity(tmp_path):
    rid = "CVE-2026-1234-stdlib"
    rule = {"id": rid, "help": {"text": "Package: stdlib\nType: go-module\n"}}
    runs = [
        {"tool": {"driver": {"rules": rules}}, "results": [{"ruleId": rid}]}
        for rules in [
            [rule],
            [],
            [rule, rule],
            [dict(rule, help={"text": "Package: other\nType: go-module\n"})],
        ]
    ]
    p = tmp_path / "input.json"
    p.write_text(json.dumps({"runs": runs}))
    result, removed = m.filter_sarif(
        str(p), [m.IgnoreSelector("CVE-2026-1234", "stdlib", "go-module")]
    )
    assert [len(x["results"]) for x in result["runs"]] == [0, 1, 1, 1]
    assert len(removed) == 1 and removed[0]["scope_verified"]
