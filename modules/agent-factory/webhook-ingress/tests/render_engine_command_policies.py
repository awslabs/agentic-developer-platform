"""Render the real engine-command signing policy expressions (issue #4539).

Same technique as `render_worker_boundary.py` — evaluate the actual Terraform
expressions with fixture values, no providers and no state — but pointed at policies
written inline in resources rather than in `locals` blocks. The expressions are
extracted from the file and evaluated, so a test asserting on them is asserting on
what would really be applied. Rewording a comment cannot break it; widening a
principal list or dropping a condition will.

Why not read the file as text and grep it: a text assertion passes on a policy that
never renders (an unbalanced `compact()`, a reference to a resource that does not
exist), and it cannot see what `compact()` actually drops. Evaluation can.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1] / "infra"
SOURCE = INFRA / "engine-command-signing.tf"

ACCOUNT = "879318057152"
REGION = "us-east-1"

# Fixture stand-ins for everything the expressions reference. Deliberately distinct,
# recognisable strings: a test asserting "the verifier and not the worker" must be able
# to fail if two of these ever collapse into the same value.
FIXTURES = {
    "var.aws_region": f'"{REGION}"',
    "var.environment": '"dev"',
    "local.account_id": f'"{ACCOUNT}"',
    "local.name_prefix": '"adp-dev"',
    "var.agent_authority_enabled": "true",
    "var.engine_command_verifier_role_arn": f'"arn:aws:iam::{ACCOUNT}:role/adp-dev-gateway-orchestration-tick-role"',
    "aws_iam_role.lambda_execution.arn": f'"arn:aws:iam::{ACCOUNT}:role/adp-dev-webhook-lambda-role"',
    "aws_iam_role.agent_scaledjob.arn": f'"arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-scaledjob-role"',
    "aws_iam_role.agent_authority_worker[0].arn": f'"arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-authority-worker-role"',
    "aws_kms_key.engine_command_signing.arn": f'"arn:aws:kms:{REGION}:{ACCOUNT}:key/engine-command-signing-key"',
    "aws_secretsmanager_secret.engine_command_signing_key.arn": f'"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:adp/dev/webhook-ingress/engine-command-signing-key-AbCdEf"',
}

# The three policies under test, keyed by the resource that carries each one. Named
# rather than "every jsonencode in the file" so that adding a fourth policy resource
# without a test is a KeyError here, not a silent gap in coverage.
POLICIES = {
    "key": 'resource "aws_kms_key" "engine_command_signing"',
    "signer": 'resource "aws_iam_role_policy" "webhook_lambda_engine_command_signing"',
    "verifier": 'resource "aws_iam_role_policy" "engine_command_verifier"',
}


def _policy_expression(source: str, block_header: str) -> str:
    """The `jsonencode(...)` argument of the `policy =` inside one resource block.

    Brace-matched rather than regex-terminated: these policies contain nested objects
    and a non-greedy `\\}` match would stop at the first inner close brace, silently
    truncating the policy under test into something that still parses.
    """
    start = source.index(block_header)
    open_paren = source.index("policy = jsonencode(", start) + len("policy = jsonencode(")
    depth = 1
    index = open_paren
    while depth:
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                break
        index += 1
    return source[open_paren:index]


def render() -> dict[str, dict]:
    source = SOURCE.read_text(encoding="utf-8")
    # Comments first: a fixture ARN substituted inside a comment would be harmless, but
    # a `#`-commented `}` inside a policy body would break the brace match above.
    source = re.sub(r"^\s*#.*$", "", source, flags=re.M)

    expressions = {name: _policy_expression(source, header) for name, header in POLICIES.items()}

    # The policy expressions become locals verbatim — still HCL objects, not strings, so
    # `compact()` and the `? :` on the authority worker are evaluated by Terraform.
    body = "locals {\n" + "\n".join(f"  {name} = {expr}" for name, expr in expressions.items()) + "\n}\n"
    for ref, value in FIXTURES.items():
        body = body.replace(ref, value)

    unresolved = re.findall(r"\b(?:var|local|aws_[a-z_]+)\.[A-Za-z0-9_.\[\]]+", body)
    if unresolved:
        raise AssertionError(f"unsubstituted references, add them to FIXTURES: {sorted(set(unresolved))}")

    with tempfile.TemporaryDirectory(prefix="adp-engine-cmd-render-") as td:
        Path(td, "main.tf").write_text(body)
        # ONE expression, not one per policy: a piped `terraform console` evaluates every
        # line but prints only the last result, so a query per policy silently returns a
        # single value and the count assertion below would be the only thing catching it.
        query = "jsonencode({" + ", ".join(f"{name} = local.{name}" for name in expressions) + "})\n"
        result = subprocess.run(
            ["terraform", "console", "-no-color"],
            input=query,
            cwd=td,
            capture_output=True,
            text=True,
            check=True,
        )
    # Console prints the string result as a quoted HCL string, hence the double decode.
    rendered = json.loads(json.loads(result.stdout.strip()))
    assert set(rendered) == set(expressions), f"expected {sorted(expressions)}, got {sorted(rendered)}"
    return rendered


if __name__ == "__main__":
    print(json.dumps(render()))
