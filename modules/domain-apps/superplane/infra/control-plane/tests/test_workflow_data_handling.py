"""No external value reaches a script body as source — Issue #5042 (U3), PR #5283.

## The rule

`${{ }}` is textual substitution into the script **before** the shell parses it. At that
moment the value is not data — it is source code. So:

    if [ "${{ inputs.confirm }}" != "superplane" ]; then     # the value can close the quote
    IMAGE='${{ steps.config.outputs.skypilot_image }}'       # a single quote ends the literal
    sed -i "s/ACCOUNT_ID/${{ steps.account.outputs.account_id }}/g"   # a `/` ends the s-expr

The same values passed with `env:` are data at every layer: GitHub sets an environment
variable, the shell expands it as a quoted parameter, and nothing re-parses it.

## Why this is a sweep rather than five hand-written assertions

The review asked for all five workflows' data interpolation to be checked. Auditing them by
hand found violations in four — including `IMAGE='${{ … }}'` inside the guard whose whole
purpose is to validate that value. A hand-written test per known site would not have found
the fifth, and will not find the sixth: the failure mode is a step ADDED later that copies the
shape of an existing one.

So this parses every Superplane workflow and fails on any `inputs.*`, `steps.*` or `github.*`
expression inside a `run:` body.

## Which expressions are exempt, and why that is not a loophole

`env.*` and `vars.*` referring to workflow-level literals are allowed. Those values are
declared in the workflow file itself, so an attacker who can change them can already change
the script — the substitution adds no capability. `inputs.*`, `steps.*.outputs.*` and
`github.*` are different in kind: operator-typed text, SSM parameters an operator can edit by
hand, and event payload fields.

The distinction is provenance, not syntax — the same reasoning as the account-id provenance
rule in `variables.tf`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".github" / "workflows").is_dir():
            return candidate
    raise AssertionError("could not locate the repository root from this test file")


REPO_ROOT = _repo_root()
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("superplane-*.yml"))

EXPRESSION_RE = re.compile(r"\$\{\{[^}]*\}\}")

# Contexts whose values do not originate in the workflow file.
UNTRUSTED_CONTEXTS = (
    "inputs.",
    "steps.",
    "github.event",
    "github.head_ref",
    "github.ref_name",
)


def _steps(workflow: dict) -> list[tuple[str, dict]]:
    found = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            found.append((job_name, step))
    return found


def _script_body(step: dict) -> str:
    """The step's script with comments removed.

    Comments are excluded because several of these workflows document the defect they replaced
    by quoting it — `${{ inputs.confirm }}` appears in a header explaining why it must not
    appear in a script. A check that cannot tell the difference forces the next person to
    delete the explanation.
    """
    return "\n".join(
        line
        for line in (step.get("run") or "").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_there_are_workflows_to_check() -> None:
    """Guard against the sweep passing by iterating zero times.

    A rename of the workflow prefix would make every test below vacuous, and "no workflows
    matched" and "all workflows are clean" are otherwise the same green tick.
    """
    assert len(WORKFLOWS) >= 5, (
        f"expected the Superplane workflow set; found {[w.name for w in WORKFLOWS]}"
    )


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_no_untrusted_expression_is_interpolated_into_a_script(
    workflow_path: Path,
) -> None:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    offenders = []
    for job_name, step in _steps(workflow):
        for expression in EXPRESSION_RE.findall(_script_body(step)):
            if any(context in expression for context in UNTRUSTED_CONTEXTS):
                offenders.append(f"{job_name} / {step.get('name')}: {expression}")

    assert not offenders, (
        f"{workflow_path.name} interpolates externally-sourced values into a script body, "
        f"where they are source code rather than data. Pass them with `env:` and reference "
        f"them as quoted shell variables:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_every_confirmation_gate_compares_an_environment_variable(
    workflow_path: Path,
) -> None:
    """The confirmation gate is the one step guaranteed to receive operator-typed text.

    Checked specifically as well as by the sweep, because it is the highest-value target: it
    runs first, before any other validation, in workflows that destroy state.
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    for job_name, step in _steps(workflow):
        name = (step.get("name") or "").lower()
        if "confirm" not in name:
            continue
        body = _script_body(step)
        assert "${{" not in body, (
            f"{workflow_path.name}: the confirmation gate '{step.get('name')}' interpolates "
            f"into its script"
        )
        if "!=" in body or "=" in body:
            assert re.search(r"\$\{?SP_[A-Z_]+", body), (
                f"{workflow_path.name}: the confirmation gate compares something other than an "
                f"env var; the operator's typing must arrive as data"
            )


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_every_referenced_input_is_declared(workflow_path: Path) -> None:
    """`inputs.nope` is not an error — it is the empty string.

    Found in `superplane-k8s-deploy.yml`, which referenced `inputs.mode` while declaring only
    `dry_run`. The summary printed "**Mode**:" followed by nothing, and nothing anywhere
    complained. The same silence is dangerous rather than merely untidy when the reference sits
    in a condition: `if: inputs.skip_guard == 'yes'` against an undeclared input is a condition
    that is simply always false, and `if: inputs.require_backup != 'yes'` is always TRUE —
    a gate that appears to consult an operator's choice while consulting nothing.
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    # `on` is parsed as the boolean True by YAML 1.1, which is why this is not `workflow["on"]`.
    triggers = workflow.get("on") or workflow.get(True) or {}
    declared = set((triggers.get("workflow_dispatch") or {}).get("inputs") or {})
    declared |= set((triggers.get("workflow_call") or {}).get("inputs") or {})

    # Comment lines are excluded for the reason given in `_script_body`: this very defect is
    # documented, by name, in a comment directly above its fix.
    body = "\n".join(
        line
        for line in workflow_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    referenced = set(re.findall(r"inputs\.([A-Za-z_][A-Za-z0-9_-]*)", body))
    missing = referenced - declared
    assert not missing, (
        f"{workflow_path.name} references input(s) {sorted(missing)} that no trigger declares. "
        f"These resolve to the empty string rather than failing, so the reference reads as a "
        f"value the operator supplied when it is nothing at all. Declared: {sorted(declared)}"
    )


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda p: p.name)
def test_scripts_referencing_sp_variables_declare_them(workflow_path: Path) -> None:
    """A typo'd env name expands to the empty string under `set -u`… or silently, in a
    substitution.

    This is the failure the `env:`-passing convention introduces if it is applied carelessly:
    `"$SP_ACOUNT_ID"` is empty, and an empty account id in a guard's `--account-id` is a guard
    comparing against nothing. Job- and workflow-level `env:` both count as declarations.

    Both real cases this found were undeclared references left behind by moving values out of
    `${{ }}` — the `env:` block was added to one step and the reference to another. Under
    `set -u` the step aborts, so the guard does fail closed; but a guard that never runs is not
    a guard that passed, and the workflow fails at deploy time with `unbound variable` rather
    than here.

    All offenders are collected before asserting: the first version stopped at the first
    undeclared name, which hid a SECOND one in the same workflow behind the fix for the first.
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    workflow_env = set(workflow.get("env") or {})
    offenders = []

    for job_name, job in (workflow.get("jobs") or {}).items():
        job_env = workflow_env | set(job.get("env") or {})
        for step in job.get("steps") or []:
            body = _script_body(step)
            declared = job_env | set(step.get("env") or {})
            referenced = set(re.findall(r"\$\{?(SP_[A-Z0-9_]+)\}?", body))
            # Names the script assigns itself are not env inputs.
            assigned = set(re.findall(r"^\s*(SP_[A-Z0-9_]+)=", body, re.MULTILINE))
            for name in sorted(referenced - declared - assigned):
                offenders.append(f"{job_name} / {step.get('name')}: ${name}")

    assert not offenders, (
        f"{workflow_path.name} references env variables no `env:` block declares — they expand "
        f"to the empty string, and an empty value passed to a guard is a guard checking "
        f"nothing:\n  " + "\n  ".join(offenders)
    )
