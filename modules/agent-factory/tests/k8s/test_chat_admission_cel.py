"""Offline CEL checks for the chat/Crew ValidatingAdmissionPolicies.

The kube-apiserver compiles every policy expression with
``cel.HomogeneousAggregateLiterals()`` (k8s.io/apiserver/pkg/cel/environment/base.go),
so a map or list literal whose entries mix a static literal with a ``dyn`` value read
from the request (``object...``) is rejected when the policy is applied:
``expected type 'string' but found 'dyn'``. ``deploy-chat-scaledjob.sh`` failed on
exactly that construct in ``adp-crew-checkpoint-helper-template``.

The apiserver also type-checks ``spec.validations`` against the OpenAPI schema of
every matched kind and publishes the result to ``status.typeChecking``. Fields it
cannot type are dropped from the CEL declaration: ``resource.Quantity`` (``oneOf``
string|number, no ``type``) is one, so ``v.emptyDir.sizeLimit == '512Mi'`` or
``resources.requests.cpu == '500m'`` is reported as ``undefined field`` (observed on
EKS 1.35 for every chat/Crew pod policy). The policies read those fields through
``dyn()`` and compare with the CEL quantity library instead.

These tests keep both constructs out of every policy and, when a Go toolchain is on
PATH, compile and type-check each expression with the real apiserver code
(``celcheck/``). Without Go, run the same check through Docker from this directory::

    docker run --rm -v "$PWD/celcheck:/src" -v "$PWD/../../agent/k8s:/k8s:ro" -w /src \
      golang:1.25-bookworm sh -c 'go run . /k8s/*.yaml'
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


K8S = Path(__file__).resolve().parents[2] / "agent/k8s"
CELCHECK = Path(__file__).resolve().parent / "celcheck"
QUANTITY_FIXTURE = Path(__file__).resolve().parent / "fixtures/quantity_string_comparison.vap.yaml"
QUANTITY_FIELD = re.compile(r"\.(sizeLimit|requests|limits)\b")
CONSTANT = re.compile(r"""^\s*('[^']*'|"[^"]*"|-?\d+(\.\d+)?[uU]?|true|false|null)\s*$""")


def policy_documents(path: Path):
    return [doc for doc in yaml.safe_load_all(path.read_text())
            if doc and doc.get("kind") == "ValidatingAdmissionPolicy"]


POLICY_FILES = sorted(path for path in K8S.glob("*.yaml") if policy_documents(path))


def policy_expressions(path: Path):
    """Yield (location, expression) for every CEL expression the apiserver compiles."""
    for doc in policy_documents(path):
        name, spec = doc["metadata"]["name"], doc["spec"]
        for index, condition in enumerate(spec.get("matchConditions") or []):
            yield f"{name} spec.matchConditions[{index}].expression", condition["expression"]
        for index, variable in enumerate(spec.get("variables") or []):
            yield f"{name} spec.variables[{index}].expression", variable["expression"]
        for index, rule in enumerate(spec.get("validations") or []):
            yield f"{name} spec.validations[{index}].expression", rule["expression"]
            if rule.get("messageExpression"):
                yield f"{name} spec.validations[{index}].messageExpression", rule["messageExpression"]
        for index, annotation in enumerate(spec.get("auditAnnotations") or []):
            yield f"{name} spec.auditAnnotations[{index}].valueExpression", annotation["valueExpression"]


def split_top_level(text: str, separator: str) -> list[str]:
    parts, depth, quote, start = [], 0, None, 0
    for index, char in enumerate(text):
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part for part in parts if part.strip()]


def aggregate_literals(expression: str) -> list[str]:
    """Return the source of every map ``{...}`` and list ``[...]`` literal (indexing excluded)."""
    literals, stack, quote = [], [], None
    for index, char in enumerate(expression):
        if quote:
            quote = None if char == quote else quote
            continue
        if char in "'\"":
            quote = char
        elif char == "{":
            stack.append((char, index))
        elif char == "[":
            # `x[...]`, `x()[...]` and `x['k'][...]` index; `in [...]`, `== [...]`, `, [...]` are literals.
            before = expression[:index].rstrip()
            word = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", before)
            is_index = bool(before) and (before[-1] in "]')\"" or (word is not None and word.group() != "in"))
            stack.append((None if is_index else char, index))
        elif char in "}]" and stack:
            opening, start = stack.pop()
            if opening:
                literals.append(expression[start:index + 1])
    return literals


def mixed_literals(expression: str) -> list[str]:
    """Literals whose entries mix constants with request-derived (dyn) values."""
    mixed = []
    for literal in aggregate_literals(expression):
        entries = split_top_level(literal[1:-1], ",")
        values = [split_top_level(entry, ":")[-1] if literal[0] == "{" else entry for entry in entries]
        if len({bool(CONSTANT.match(value)) for value in values}) > 1:
            mixed.append(literal)
    return mixed


def quantity_reads_without_dyn(expression: str) -> list[str]:
    """Reads of Quantity-typed Pod fields whose receiver is not wrapped in ``dyn(...)``."""
    direct = []
    for match in QUANTITY_FIELD.finditer(expression):
        before = expression[:match.start()]
        if before.endswith(")"):
            depth, index = 0, len(before) - 1
            while index >= 0:
                depth += {")": 1, "(": -1}.get(before[index], 0)
                if depth == 0:
                    break
                index -= 1
            if before[:index].endswith("dyn"):
                continue
        receiver = re.search(r"[\w\[\]'./-]*$", before).group()
        direct.append(receiver + match.group())
    return direct


def test_literal_scanner_flags_the_construct_the_apiserver_rejected():
    rejected = ("x[0].labelSelector.matchLabels ==\n  {'adp.io/crew-pod': 'true', "
                "'adp.io/owner-key': object.metadata.labels['adp.io/owner-key']} &&\n!has(x[0].namespaces)")
    assert mixed_literals(rejected) == [
        "{'adp.io/crew-pod': 'true', 'adp.io/owner-key': object.metadata.labels['adp.io/owner-key']}"]
    accepted = ("e.name in ['A', 'B'] && c.command == ['/app/x'] && labels['k'] == 'v' && "
                "m == {'a': 'b'} && m == {'a': string(object.metadata.labels['k'])}")
    assert mixed_literals(accepted) == []
    assert aggregate_literals(accepted) == ["['A', 'B']", "['/app/x']", "{'a': 'b'}",
                                            "{'a': string(object.metadata.labels['k'])}"]


def test_quantity_scanner_flags_the_reads_the_apiserver_cannot_type():
    policy, = policy_documents(QUANTITY_FIXTURE)
    flagged = [quantity_reads_without_dyn(rule["expression"]) for rule in policy["spec"]["validations"]]
    assert flagged == [["v.emptyDir.sizeLimit"],
                       ["object.spec.containers[0].resources.requests",
                        "object.spec.containers[0].resources.limits"]]
    accepted = ("has(dyn(v.emptyDir).sizeLimit) && quantity(dyn(v.emptyDir).sizeLimit) == quantity('512Mi') && "
                "quantity(dyn(object.spec.containers[0].resources).requests['ephemeral-storage']) == quantity('256Mi')")
    assert quantity_reads_without_dyn(accepted) == []


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda path: path.name)
def test_policy_quantity_fields_are_read_through_dyn(path):
    for location, expression in policy_expressions(path):
        assert quantity_reads_without_dyn(expression) == [], location


@pytest.mark.parametrize("path", POLICY_FILES, ids=lambda path: path.name)
def test_policy_literals_are_homogeneous(path):
    assert policy_documents(path)
    for location, expression in policy_expressions(path):
        assert mixed_literals(expression) == [], location


def test_policy_set_covers_every_crew_and_sandbox_admission_manifest():
    assert [path.name for path in POLICY_FILES] == [
        "chat-crew-helper-admission.yaml", "chat-crew-pod-admission.yaml",
        "chat-crew-restore-admission.yaml", "chat-crew-service-account-admission.yaml",
        "chat-sandbox-admission.yaml", "chat-supervisor-pod-policy.yaml",
    ]


def test_checkpoint_helper_affinity_pins_only_the_owner_crew_pod():
    policy, = policy_documents(K8S / "chat-crew-helper-admission.yaml")
    rule, = [rule for rule in policy["spec"]["validations"] if "podAffinity" in rule["expression"]]
    expression = " ".join(rule["expression"].split())
    assert rule["message"] == "Helper must share the owner Crew pod's node for the EBS volume"
    for fragment in (
        "has(object.spec.affinity) && has(object.spec.affinity.podAffinity)",
        "!has(object.spec.affinity.nodeAffinity) && !has(object.spec.affinity.podAntiAffinity)",
        "!has(object.spec.affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution)",
        "size(object.spec.affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution) == 1",
        "object.spec.affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution.all(t, "
        "t.topologyKey == 'kubernetes.io/hostname'",
        "has(t.labelSelector) && has(t.labelSelector.matchLabels) && !has(t.labelSelector.matchExpressions)",
        "size(t.labelSelector.matchLabels) == 2",
        "'adp.io/crew-pod' in t.labelSelector.matchLabels && t.labelSelector.matchLabels['adp.io/crew-pod'] == 'true'",
        "'adp.io/owner-key' in t.labelSelector.matchLabels && "
        "t.labelSelector.matchLabels['adp.io/owner-key'] == object.metadata.labels['adp.io/owner-key']",
        "!has(t.namespaces) && !has(t.namespaceSelector))",
    ):
        assert fragment in expression
    assert "{" not in expression
    restore, = policy_documents(K8S / "chat-crew-restore-admission.yaml")
    assert {"expression": "!has(object.spec.affinity)",
            "message": "Restore placement follows the owner claim's volume zone"} in restore["spec"]["validations"]


@pytest.mark.skipif(shutil.which("go") is None,
                    reason="Go toolchain unavailable; run celcheck through Docker (module docstring)")
def test_policy_expressions_compile_with_the_apiserver_compiler():
    result = subprocess.run(["go", "run", ".", *map(str, POLICY_FILES)], cwd=CELCHECK,
                            capture_output=True, text=True, timeout=900, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"OK: {len(POLICY_FILES)} policies compiled and type-checked" in result.stdout
    assert result.stdout.count("(type check against [/v1, Kind=Pod])") == sum(
        len(doc["spec"]["validations"]) for path in POLICY_FILES for doc in policy_documents(path)
        if doc["spec"]["matchConstraints"]["resourceRules"][0]["resources"] == ["pods"])


@pytest.mark.skipif(shutil.which("go") is None,
                    reason="Go toolchain unavailable; run celcheck through Docker (module docstring)")
def test_celcheck_reports_quantity_string_comparisons_as_type_errors():
    result = subprocess.run(["go", "run", ".", str(QUANTITY_FIXTURE)], cwd=CELCHECK,
                            capture_output=True, text=True, timeout=900, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "spec.validations[0].expression (type check): /v1, Kind=Pod: ERROR: <input>:1:87: undefined field 'sizeLimit'" in result.stderr
    assert "spec.validations[1].expression (type check): /v1, Kind=Pod: ERROR: <input>:1:36: undefined field 'requests'" in result.stderr
    assert "undefined field 'limits'" in result.stderr
    assert "2 expression(s) failed to compile or type-check" in result.stderr
