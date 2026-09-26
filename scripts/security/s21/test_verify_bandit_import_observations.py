"""Import review cannot migrate acceptance to an execution observation."""

import hashlib
import json

import pytest
import verify_bandit_import_observations as verifier


@pytest.mark.parametrize(
    "source,bindings",
    [
        ("import subprocess", ["subprocess"]),
        ("import subprocess as child_process", ["child_process"]),
        ("from subprocess import run, Popen as Child", ["run", "Child"]),
    ],
)
def test_exact_import_statement(source, bindings):
    assert verifier.import_context(source, 1)["bound_names"] == bindings


@pytest.mark.parametrize(
    "source,line",
    [
        ("subprocess.run(['synthetic'])", 1),
        ("import other_module", 1),
        ("from .subprocess import run", 1),
        ("from subprocess_helpers import run", 1),
        ("from subprocess import *", 1),
        ("import subprocess\nsubprocess.run(['synthetic'])", 2),
        ("import subprocess; import subprocess as alternate", 1),
    ],
)
def test_calls_relative_imports_or_wrong_lines_are_not_import_evidence(source, line):
    with pytest.raises(ValueError):
        verifier.import_context(source, line)


@pytest.fixture
def case(tmp_path, monkeypatch):
    selector = lambda index: f"bandit|bandit-results.sarif|run=0|ri={index}"
    source = "import subprocess\nsubprocess.run(['synthetic'])\n"
    results = [
        {
            "ruleId": rule,
            "level": "note",
            "properties": {"issue_severity": "LOW"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": "source.py"},
                        "region": {"startLine": line},
                    }
                }
            ],
        }
        for rule, line in [("B404", 1), ("B603", 2)]
    ]
    sarif = {"runs": [{"results": results}]}
    inventory = {
        "original_count": 1470,
        "selectors": [
            {
                "selector": selector(index),
                "files": ["source.py" if index < 2 else "other.py"],
                "rule": ["B404", "B603"][index] if index < 2 else "B101",
                "line": index + 1,
                "severity": "LOW",
                "owner_issue": 6108,
                "disposition": "pending-source-review",
            }
            for index in range(1470)
        ],
    }
    record = {
        "selector": selector(0),
        "file": "source.py",
        "line": 1,
        "rule": "B404",
        "severity": "LOW",
        "sarif_level": "note",
        "import_evidence": verifier.import_context(source, 1),
        "execution_selectors": [selector(1)],
        "execution_dispositions": {selector(1): "pending-source-review"},
    }
    receipt = {
        "source_revision": "1" * 40,
        "reviewed_delta": 1,
        "reviewed_records": [record],
    }
    paths = [tmp_path / (name + ".json") for name in ("scan", "inventory", "receipt")]

    def frozen_git(directory, *args):
        assert directory == tmp_path and args == ("show", "1" * 40 + ":source.py")
        return source.encode()

    monkeypatch.setattr(verifier, "git", frozen_git)

    def run():
        raw = json.dumps(sarif).encode()
        receipt["original_sarif_sha256"] = hashlib.sha256(raw).hexdigest()
        for path, document in zip(paths, (sarif, inventory, receipt)):
            path.write_text(json.dumps(document))
        verifier.verify(tmp_path, *paths)

    return sarif, inventory, record, receipt, run


def test_import_and_separate_execution_ownership_pass(case):
    case[-1]()


@pytest.mark.parametrize(
    "mutation",
    [
        "import_index",
        "import_line",
        "native_severity",
        "missing_calls",
        "call_line",
        "call_file",
        "call_owner",
        "call_disposition",
        "duplicate",
    ],
)
def test_inexact_import_or_execution_transfer_is_rejected(case, mutation):
    _, inventory, record, receipt, run = case
    call = inventory["selectors"][1]
    if mutation == "import_index":
        record["selector"] = call["selector"]
    elif mutation == "import_line":
        record["line"] = 2
    elif mutation == "native_severity":
        record["severity"] = "MEDIUM"
    elif mutation == "missing_calls":
        record["execution_selectors"] = []
    elif mutation == "call_line":
        call["line"] = 8
    elif mutation == "call_file":
        call["files"] = ["unrelated.py"]
    elif mutation == "call_owner":
        call["owner_issue"] = 1
    elif mutation == "call_disposition":
        call["disposition"] = "fixed"
    else:
        receipt["reviewed_records"].append(dict(record))
        receipt["reviewed_delta"] = 2
    with pytest.raises(ValueError):
        run()
