"""Machine trust and human MFA trust are separate — Issue #5532 (w6-09), review finding F3.

## The defect this is the regression control for

The workspace admin role had ONE trust statement, requiring
`aws:MultiFactorAuthPresent = true` from every principal, while this module's own
documentation named an automated ADP control-plane role as a thing that assumes it.

A role session cannot satisfy that condition. When a service assumes a role with its own
credentials — an IRSA pod, a CI role, a Lambda execution role — the request context has
`aws:MultiFactorAuthPresent` set to FALSE (the key is present, valued false), so a `Bool`
test for "true" does not match. For the session types where the key is absent instead, a
`Bool` condition on a missing key also fails to match. Either way the documented automation
path was denied on every attempt, presenting as an opaque AccessDenied at the moment the
control plane first reached for a workspace.

So the repair is not "drop the MFA condition" — that would remove the control which makes a
leaked human access key insufficient by itself. It is to give each principal type the
condition that is meaningful for it, and that separation is what this file pins.

## Why these assertions parse source instead of planning

`aws_iam_policy_document` is a data source, and every `.tftest.hcl` suite in this module
mocks it — it must, or each role's `assume_role_policy` is unresolvable at plan time and the
run errors before any assertion. The mock replaces `json` with an empty statement list, so a
plan-time assertion about trust STATEMENTS would be asserting on the mock. The statements
exist only as source text at that point, so source text is what gets parsed. This is the
same limit `test_least_privilege.py` documents, and the same parser is reused.

`admin_trust.tftest.hcl` carries the half that IS checkable in a plan: that an
automation-only workspace creates the role at all, and that the variable validations refuse
foreign principal shapes.

## What "foreign-principal denial" means here, and why it is not a policy statement

IAM's default is deny: a principal named in neither list cannot assume the role, with no
`Deny` statement required. An explicit `Deny` would be the wrong construction — it would
have to enumerate the principals to deny, which is unbounded. So the denial is asserted as
the two properties that actually produce it: each `Allow` statement names exact ARNs drawn
from one specific variable, and neither admits an account root, a wildcard, or the other
list's principals. `test_least_privilege.py` asserts the no-root/no-wildcard rule across
every document in the module; this file asserts the per-statement sourcing that keeps the two
lists from leaking into each other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKSPACES = Path(__file__).resolve().parents[1]
IAM_TF = WORKSPACES / "iam.tf"


def _strip_comments(text: str) -> str:
    """Drop comment lines and heredoc bodies.

    iam.tf's prose discusses the MFA condition, role sessions and the defect at length while
    explaining the repair. Raw text would read the explanation of the defect as the defect.

    Deliberately duplicated from test_least_privilege.py rather than imported from it. These
    directories have no `__init__.py`, so pytest imports each file as a top-level module
    whose importability depends on which directory the run started from — a cross-file import
    works under one invocation and raises ModuleNotFoundError under another, and a collection
    error in the file that guards F3 is a worse outcome than three duplicated helpers.
    """
    out: list[str] = []
    heredoc_terminator: str | None = None

    for line in text.splitlines():
        if heredoc_terminator is not None:
            if line.strip() == heredoc_terminator:
                heredoc_terminator = None
            continue

        opening = re.search(r"<<-?([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if opening:
            heredoc_terminator = opening.group(1)
            continue

        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        out.append(line.split("#", 1)[0])

    return "\n".join(out)


def _block_body(text: str, start: int) -> tuple[str, int]:
    """Body of the brace-delimited block whose opening `{` is at or after `start`."""
    open_index = text.index("{", start)
    depth = 0
    i = open_index
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return text[open_index + 1 : i], i


def _blocks(kind: str, path: Path, labels: int = 2) -> list[tuple[str, ...]]:
    """Return each top-level `kind "a" "b" { ... }` block as (labels..., body).

    Brace-depth delimited rather than regex-delimited: a policy document body contains
    nested `dynamic`, `content`, `principals` and `condition` blocks, and a non-greedy match
    to the first `}` truncates at the first of them — cutting off exactly the statements this
    suite inspects.

    `labels` is 2 for `resource`/`data` and 1 for `output`, so outputs.tf can be parsed with
    the same function instead of a second ad-hoc regex.
    """
    text = _strip_comments(path.read_text())
    label_pattern = r'\s+"([^"]+)"' * labels
    opener = re.compile(rf"^{kind}{label_pattern}\s*\{{", re.MULTILINE)

    found: list[tuple[str, ...]] = []
    for match in opener.finditer(text):
        body, _ = _block_body(text, match.end() - 1)
        found.append((*match.groups(), body))
    return found


def _nested_blocks(body: str, name: str) -> list[str]:
    """Return the bodies of each `name { ... }` block nested inside `body`.

    `name` may include labels, e.g. `dynamic "statement"`.
    """
    found: list[str] = []
    opener = re.compile(rf"^\s*{name}\s*\{{", re.MULTILINE)

    for match in opener.finditer(body):
        nested, _ = _block_body(body, match.end() - 1)
        found.append(nested)
    return found


MFA_CONDITION_KEY = "aws:MultiFactorAuthPresent"

HUMAN_VARIABLE = "var.workspace_admin_principal_arns"
AUTOMATION_VARIABLE = "var.workspace_admin_automation_role_arns"


def _trust_document_body() -> str:
    documents = [
        body
        for t, name, body in _blocks("data", IAM_TF)
        if t == "aws_iam_policy_document" and name == "workspace_admin_assume_role"
    ]
    assert len(documents) == 1, (
        f"expected exactly one data.aws_iam_policy_document.workspace_admin_assume_role in "
        f"iam.tf, found {len(documents)}. If it was renamed, fix this file's target rather "
        f"than dropping the check: it is the regression control for finding F3."
    )
    return documents[0]


def _trust_statements() -> list[str]:
    """Statement bodies, reached through the `dynamic` wrapper each one sits in.

    Both statements are `dynamic "statement"` blocks with a `content { ... }` body, because
    each is omitted entirely when its principal list is empty (an IAM trust statement with
    zero principals is rejected by the API, so emitting an empty one would make an
    automation-only workspace fail to apply). A parser looking only for literal
    `statement {` blocks finds nothing here and would report green on an empty list — so
    this resolves the dynamic form and the premise test below asserts the count.
    """
    body = _trust_document_body()
    statements: list[str] = []

    for dynamic_body in _nested_blocks(body, 'dynamic "statement"'):
        for content in _nested_blocks(dynamic_body, "content"):
            statements.append(content)

    # Any statement written in the plain literal form counts too, so this does not stop
    # noticing a third statement added without the dynamic wrapper.
    statements.extend(_nested_blocks(body, "statement"))

    return statements


TRUST_STATEMENTS = _trust_statements()


def _sid(statement: str) -> str:
    match = re.search(r'^\s*sid\s*=\s*"([^"]+)"', statement, re.MULTILINE)
    return match.group(1) if match else "<no sid>"


def _identifiers(statement: str) -> str:
    principals = _nested_blocks(statement, "principals")
    assert principals, (
        f'trust statement "{_sid(statement)}" declares no principals block, so it names no '
        f"one it trusts."
    )
    return "\n".join(principals)


def _has_mfa_condition(statement: str) -> bool:
    for condition in _nested_blocks(statement, "condition"):
        if MFA_CONDITION_KEY in condition:
            return True
    return False


def test_the_parser_found_both_trust_statements() -> None:
    """Premise check: an empty parse makes every assertion below vacuous.

    The statements are `dynamic` blocks, so this parser is doing real work rather than
    matching a literal — and a parser that silently stopped matching would turn this file
    into a no-op that still reads as enforcement.
    """
    assert len(TRUST_STATEMENTS) == 2, (
        f"expected exactly 2 trust statements on the workspace admin role (one for human "
        f"operators, one for named automation roles), parsed "
        f"{len(TRUST_STATEMENTS)}: {[_sid(s) for s in TRUST_STATEMENTS]}.\n\n"
        f"If a third principal class was genuinely added, extend this file with the "
        f"condition that is meaningful for it. If the count is 0 or 1, either the "
        f"separation this file protects was undone, or the dynamic-block parser stopped "
        f"matching."
    )


def test_exactly_one_statement_requires_mfa_and_exactly_one_does_not() -> None:
    """The separation itself: neither "MFA for everyone" nor "MFA for no one".

    Both failure directions are real and this is the assertion that excludes each. Requiring
    MFA on every statement is the F3 defect. Requiring it on none is the tempting repair that
    silently drops the protection making a leaked human access key insufficient on its own.
    """
    with_mfa = [_sid(s) for s in TRUST_STATEMENTS if _has_mfa_condition(s)]
    without_mfa = [_sid(s) for s in TRUST_STATEMENTS if not _has_mfa_condition(s)]

    assert len(with_mfa) == 1, (
        f"expected exactly one trust statement to carry the {MFA_CONDITION_KEY} condition, "
        f"found {len(with_mfa)}: {with_mfa}.\n\n"
        f"If zero: the MFA requirement on human operators has been dropped, which is the "
        f"wrong repair for finding F3 — a leaked long-lived human access key then suffices "
        f"on its own.\n"
        f"If two or more: the condition has been applied to the automation statement as "
        f"well, which is the original F3 defect. A role session's "
        f"{MFA_CONDITION_KEY} is false, not true, so that statement grants nothing while "
        f"reading as a grant."
    )
    assert len(without_mfa) == 1, (
        f"expected exactly one trust statement WITHOUT the MFA condition (the named "
        f"automation roles, which cannot present MFA), found {len(without_mfa)}: "
        f"{without_mfa}."
    )


def test_the_mfa_statement_trusts_only_the_human_principal_list() -> None:
    """The MFA-protected statement must be the human one, and draw only from that variable.

    Direction matters: a file with one conditioned and one unconditioned statement would pass
    the test above even if the conditions were on the wrong statements — which is exactly the
    F3 defect with an extra statement added.
    """
    human = [s for s in TRUST_STATEMENTS if _has_mfa_condition(s)][0]
    identifiers = _identifiers(human)

    assert HUMAN_VARIABLE in identifiers, (
        f'the MFA-conditioned statement "{_sid(human)}" does not draw its principals from '
        f"`{HUMAN_VARIABLE}`. Its identifiers are:\n{identifiers}\n\n"
        f"The MFA condition is meaningful only for human principals. Applying it to the "
        f"automation list is finding F3."
    )
    assert AUTOMATION_VARIABLE not in identifiers, (
        f'the MFA-conditioned statement "{_sid(human)}" includes '
        f"`{AUTOMATION_VARIABLE}`.\n\n"
        f"Automation roles cannot satisfy an MFA condition — a role session's "
        f"{MFA_CONDITION_KEY} is FALSE, so this denies every automated caller while "
        f"appearing to permit them. That is the exact defect finding F3 reported."
    )

    condition_tests = [
        c for c in _nested_blocks(human, "condition") if MFA_CONDITION_KEY in c
    ]
    assert len(condition_tests) == 1, (
        f"expected one {MFA_CONDITION_KEY} condition on the human statement, found "
        f"{len(condition_tests)}."
    )
    condition = condition_tests[0]

    assert re.search(r'^\s*test\s*=\s*"Bool"', condition, re.MULTILINE), (
        f"the MFA condition's test operator is not `Bool`:\n{condition}\n\n"
        f"{MFA_CONDITION_KEY} is a boolean context key. A `StringEquals` test on it is not "
        f"equivalent, and `Null`/`BoolIfExists` variants change what an absent key means."
    )
    assert re.search(r'^\s*values\s*=\s*\["true"\]', condition, re.MULTILINE), (
        f'the MFA condition does not require the value "true":\n{condition}\n\n'
        f"A condition requiring `false`, or one testing a different value, inverts the "
        f"control into a requirement that MFA be ABSENT."
    )


def test_the_unconditioned_statement_trusts_only_named_automation_roles() -> None:
    """The statement without MFA must be reachable only by explicitly named roles.

    This is where the absence of an MFA condition is made safe. The control is not a
    condition; it is that the principal list is an exact allowlist of role ARNs, so admitting
    a new automated caller is a reviewable change to a named list rather than a side effect.
    """
    automation = [s for s in TRUST_STATEMENTS if not _has_mfa_condition(s)][0]
    identifiers = _identifiers(automation)

    assert AUTOMATION_VARIABLE in identifiers, (
        f'the statement without an MFA condition ("{_sid(automation)}") does not draw its '
        f"principals from `{AUTOMATION_VARIABLE}`. Its identifiers are:\n{identifiers}"
    )
    assert HUMAN_VARIABLE not in identifiers, (
        f'the statement without an MFA condition ("{_sid(automation)}") includes '
        f"`{HUMAN_VARIABLE}`.\n\n"
        f"That lets a human principal assume this role with no MFA — the protection is then "
        f"present in one statement and bypassable through another, which is worse than not "
        f"having it, because the MFA statement still reads as enforcement."
    )

    # No literal principal may be hardcoded into either statement: every trusted ARN must
    # arrive through a variable whose validations refuse `:root`, wildcards and (for the
    # automation list) IAM users. A hardcoded ARN would bypass all of that.
    literal_arns = re.findall(r'"(arn:aws[a-z-]*:iam::[^"]*)"', identifiers)
    assert not literal_arns, (
        f"the automation trust statement hardcodes principal ARNs: {literal_arns}.\n\n"
        f"Trusted principals must arrive through `{AUTOMATION_VARIABLE}`, whose validations "
        f"refuse an account root, a wildcard, and an IAM user ARN. A literal here is subject "
        f"to none of those checks."
    )


@pytest.mark.parametrize("statement_index", range(len(TRUST_STATEMENTS)))
def test_no_trust_statement_grants_more_than_assume_role(statement_index: int) -> None:
    """Each statement grants `sts:AssumeRole` and nothing else.

    `sts:TagSession` or `sts:AssumeRoleWithWebIdentity` appearing here would widen how the
    role can be reached beyond what either statement's condition governs — and the web
    identity path bypasses the MFA context key entirely.
    """
    statement = TRUST_STATEMENTS[statement_index]
    actions_match = re.search(
        r"^\s*actions\s*=\s*\[(.*?)\]", statement, re.MULTILINE | re.DOTALL
    )
    assert actions_match, f'trust statement "{_sid(statement)}" declares no actions.'

    actions = re.findall(r'"([^"]+)"', actions_match.group(1))
    assert actions == ["sts:AssumeRole"], (
        f'trust statement "{_sid(statement)}" grants {actions}, expected only '
        f'["sts:AssumeRole"].\n\n'
        f"`sts:AssumeRoleWithWebIdentity` in particular is not governed by the "
        f"{MFA_CONDITION_KEY} key at all, so adding it would route around the human "
        f"statement's condition."
    )

    assert re.search(r'^\s*effect\s*=\s*"Allow"', statement, re.MULTILINE), (
        f'trust statement "{_sid(statement)}" is not an explicit Allow. Both statements are '
        f"Allows; denial of every other principal comes from IAM's implicit default, not "
        f"from a Deny here (see this file's header)."
    )


def test_each_statement_is_omitted_when_its_principal_list_is_empty() -> None:
    """The dynamic gating, which is what makes an automation-only workspace appliable.

    An IAM trust policy statement with an empty `Principal` is rejected by the AWS API. If
    either statement were emitted unconditionally, a workspace naming only automation roles
    (or only humans) would fail at apply with a policy-validation error — so the separation
    would be correct in source and unusable in practice.
    """
    body = _trust_document_body()
    dynamic_blocks = _nested_blocks(body, 'dynamic "statement"')

    assert len(dynamic_blocks) == 2, (
        f"expected both trust statements to be `dynamic` on their principal list being "
        f"non-empty, found {len(dynamic_blocks)} dynamic block(s)."
    )

    guarded_variables = set()
    for dynamic_body in dynamic_blocks:
        for_each = re.search(r"^\s*for_each\s*=\s*(.+)$", dynamic_body, re.MULTILINE)
        assert for_each, "a dynamic statement block declares no for_each."
        expression = for_each.group(1)

        assert "length(" in expression and "> 0" in expression, (
            f"a dynamic trust statement's for_each is `{expression}`, which does not gate on "
            f"its principal list being non-empty. Expected the form "
            f"`length(var.<list>) > 0 ? [1] : []`."
        )
        for variable in (HUMAN_VARIABLE, AUTOMATION_VARIABLE):
            if variable in expression:
                guarded_variables.add(variable)

    assert guarded_variables == {HUMAN_VARIABLE, AUTOMATION_VARIABLE}, (
        f"the two dynamic statements gate on {sorted(guarded_variables)}, expected one to "
        f"gate on each of {HUMAN_VARIABLE} and {AUTOMATION_VARIABLE}. Both gating on the "
        f"same list means one statement is emitted or omitted for the wrong reason."
    )


def test_the_admin_role_exists_whenever_either_list_names_a_principal() -> None:
    """Role creation must be gated on EITHER list, not only on the human one.

    This is the bug that shipped alongside F3's trust defect: with creation gated on the
    human list alone, a workspace naming only an automation role would create no admin role,
    and `workspace_admin_role_arn` would be null — so the automated caller has nothing to
    assume, and the failure looks like a missing output rather than a gating mistake.
    """
    gated = {
        name: body
        for t, name, body in _blocks("resource", IAM_TF)
        if t in ("aws_iam_role", "aws_iam_role_policy") and "workspace_admin" in name
    }
    assert gated, "iam.tf declares no workspace_admin role or inline policy."

    for name, body in gated.items():
        count_match = re.search(r"^\s*count\s*=\s*(.+)$", body, re.MULTILINE)
        assert count_match, (
            f"{name} declares no count, so it is created unconditionally — including for a "
            f"workspace that has named no operator at all."
        )
        expression = count_match.group(1)
        assert "local.workspace_admin_enabled" in expression, (
            f"{name}'s count is `{expression}`, which does not use "
            f"`local.workspace_admin_enabled`.\n\n"
            f"That local is `length(human) > 0 || length(automation) > 0` — the single "
            f"expression that makes the role exist for an automation-only workspace as well "
            f"as a human-operated one. Gating directly on one list reintroduces the case "
            f"where a named automation role has no role to assume."
        )


def test_the_module_publishes_which_principals_are_trusted_how() -> None:
    """The distinction must be readable from state, not only from this source file.

    An automated caller that gets AccessDenied cannot otherwise tell whether its role was
    never named or was named in the human list, where the MFA condition it cannot satisfy
    applies. Those need different fixes, and the role ARN alone does not distinguish them.
    """
    outputs = {
        name: body
        for name, body in _blocks("output", WORKSPACES / "outputs.tf", labels=1)
    }
    assert len(outputs) >= 10, (
        f"parsed only {len(outputs)} outputs from outputs.tf, which suggests the parser "
        f"stopped matching and would make the assertion below vacuous."
    )

    assert "workspace_admin_trust" in outputs, (
        f"outputs.tf publishes no `workspace_admin_trust` output. Present outputs: "
        f"{sorted(outputs)}.\n\n"
        f"Without it, the two trust classes are distinguishable only by reading iam.tf, and "
        f"an automated caller debugging an AccessDenied has no way to tell 'my role was "
        f"never named' from 'my role was named in the MFA-required list'."
    )

    body = outputs["workspace_admin_trust"]
    for variable in (HUMAN_VARIABLE, AUTOMATION_VARIABLE):
        assert variable in body, (
            f"`workspace_admin_trust` does not report `{variable}`, so a caller cannot tell "
            f"which list its principal is in — which is the question the output exists to "
            f"answer."
        )
