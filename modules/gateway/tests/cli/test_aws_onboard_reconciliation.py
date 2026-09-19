"""Where upstream AWS onboarding's responsibilities went (Issue #5039).

The reviewer's third finding: the story claimed `account`/`aws_onboard` were
"reused" because commands with those names exist, while the bodies had quietly
become something else — `aws_onboard` called a `GET /aws/onboarding-plan` and a
`POST /aws/accounts` that exist neither upstream nor in the design's endpoint
table, and no test established where the original responsibilities moved.

Upstream `account onboard --provider aws` did two separable things
(reference `src/superplane-cli/superplane/commands/aws_onboard.py`):

1. **Created AWS resources** under the user's own profile with boto3 — a
   cross-account `SuperplaneAccess` role with a locally generated ExternalId, a
   `SuperplaneIngestAccess` SQS role, five IRSA roles with placeholder trust, and
   provider secrets in the user's own Secrets Manager.
2. **Registered the resulting identifiers** with `POST /accounts`, carrying
   `role_arn`, `external_id`, `ingest_role_arn`, `secret_arns`, `irsa_role_arns`.

Only (2) belongs in this CLI. (1) is either owned by ADP already or has no stated
owner at all — and those are different situations, so this suite pins each one
separately. The mapping:

| Upstream responsibility | Owner now | Pinned by |
|---|---|---|
| `SuperplaneAccess` role + ExternalId | ADP `adp aws connect`: CloudFormation, server-side ExternalId | `test_no_role_is_created_or_minted_here` |
| Provider secrets in the user's Secrets Manager | ADP vault, id-only metadata | `test_no_secret_arn_is_registered` |
| Registering identifiers with `POST /accounts` | reused here, as a reference | `test_registration_sends_a_reference_not_an_arn` |
| IRSA roles, SQS ingest role, role width | **no stated owner** | `test_the_unresolved_contract_work_is_reported` |

The last row is the honest part. Nothing in the design note or the accepted
requirements says who creates those, and nothing in ADP creates them today, so
the verb reports them rather than letting "ok" imply a complete onboarding.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).parents[4]
SCRIPT = REPO / "modules/gateway/cli/adp-superplane.py"
DESIGN = REPO / "docs/design-notes/4904-ai-superplane-on-adp.md"

spec = importlib.util.spec_from_file_location("adp_superplane_reconcile", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def executable_source() -> str:
    """The helper's code with docstrings and comments stripped.

    These tests must forbid the retired *behaviour*, not the words. Scanning raw
    text would make it impossible to document what moved and why — the comments
    naming `boto3` and `SuperplaneAccess` are exactly the explanation a later
    reader needs. So compare against the code only: string literals and
    identifiers survive, prose does not.
    """
    tree = ast.parse(SCRIPT.read_text())
    holders = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            node.body.pop(0)  # drop the docstring
    return ast.unparse(tree)


class RecordingApi:
    """Records requests; returns a plausible domain response."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []

    def request(self, method, path, body=None, **kwargs):
        self.sent.append((method, path, body))
        return {"account_id": "123456789012", "registered": True}


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli.common, "access_token", lambda: "synthetic-session-token")
    return tmp_path


def _register(api):
    return cli.run(
        cli.parser().parse_args(["aws-onboard", "register", "--account-id", "123456789012", "--credential-id", "cred-42"]),
        api,
    )


# --- the invented routes are gone --------------------------------------------


def test_the_invented_aws_routes_are_not_called() -> None:
    """`/aws/onboarding-plan` and `/aws/accounts` exist nowhere.

    Not upstream (the reference CLI and API have no `/aws/*` route at all), not in
    the design's §7 endpoint table, and not in this repo's gateway. Calling them
    could only ever 404.
    """
    source = executable_source()
    assert "/aws/onboarding-plan" not in source
    assert "/aws/accounts" not in source


def test_there_is_no_plan_subcommand() -> None:
    """The `plan` verb only existed to call the invented plan endpoint."""
    with pytest.raises(cli.CliError) as raised:
        cli.parser().parse_args(["aws-onboard", "plan", "--account-id", "123456789012"])
    assert raised.value.code == "usage_error"


def test_the_api_base_matches_the_design() -> None:
    """Design §7: "Proposed base: /api/superplane/v1 on the existing ADP origin".

    `adp_common.gateway_url()` already appends `/api`, so the helper's base is the
    remainder. The previous `/domain-apps/superplane` matched neither the design
    nor anything mounted in this repo.
    """
    assert cli.API_BASE == "/superplane/v1"
    assert "/api/superplane/v1" in DESIGN.read_text()


# --- responsibility 1: AWS resource creation is NOT done here ----------------


def test_no_role_is_created_or_minted_here() -> None:
    """Role creation moved to ADP's connect flow; this CLI must not fork it.

    ADP creates `ADP-Agent-{Nickname}` from a CloudFormation template with a
    server-generated ExternalId (src/auth/aws_connect_routes.py,
    src/auth/cfn_templates/aws_role_v1.yaml). So no boto3, no IAM call, and no
    locally generated ExternalId may appear in this helper.
    """
    source = executable_source()
    for forbidden in ("boto3", "create_role", "put_role_policy", "assume_role", "SuperplaneAccess"):
        assert forbidden not in source, f"{forbidden} belongs to the retired local-onboarding path"
    # An ExternalId minted client-side would be an authority this CLI must not hold.
    assert "external_id" not in source
    assert "uuid" not in source


def test_registration_sends_a_reference_not_an_arn() -> None:
    """Design §7: the domain API accepts references only, keyed on the vault id.

    The upstream payload carried `role_arn`, `external_id`, `ingest_role_arn`,
    `secret_arns` and `irsa_role_arns`. None of those may be minted here.
    """
    api = RecordingApi()
    _register(api)

    (method, path, body) = api.sent[0]
    assert (method, path) == ("POST", cli.API_BASE + "/accounts")
    assert body["vault_credential_id"] == "cred-42"
    assert body["account_id"] == "123456789012"
    # No ARN of any kind, and no ExternalId, crosses over from the CLI.
    serialized = json.dumps(body)
    assert "arn:aws:" not in serialized
    for retired in ("role_arn", "external_id", "ingest_role_arn", "secret_arns", "irsa_role_arns"):
        assert retired not in body, f"{retired} is not the CLI's to supply"


def test_no_secret_arn_is_registered() -> None:
    """Secret ownership moved to ADP's vault; only ids are domain metadata.

    Upstream stored provider secrets in the user's own Secrets Manager and
    registered the ARNs. Design §3: "Move secret ownership to ADP; retain provider
    metadata and explicit workspace bindings only."
    """
    api = RecordingApi()
    _register(api)

    assert "secretsmanager" not in json.dumps(api.sent).lower()


def test_a_credential_reference_is_required() -> None:
    """Registering an account with no reference to a connection is unverifiable.

    Without the vault id the server cannot check that the caller actually owns a
    connection to that AWS account — which is what made the previous
    `--label`-only payload impossible to authorize.
    """
    with pytest.raises(cli.CliError) as raised:
        cli.parser().parse_args(["aws-onboard", "register", "--account-id", "123456789012"])
    assert raised.value.code == "usage_error"


# --- responsibility 2: what has NO owner is reported, not implied done -------


def test_the_unresolved_contract_work_is_reported() -> None:
    """ "ok" must not imply a complete onboarding.

    The IRSA roles and the SQS ingest role have no successor stated anywhere, so
    the verb names them. An operator who reads only the status would otherwise
    discover the gap when a workload cannot assume its service-account role.
    """
    result = _register(RecordingApi())

    unresolved = result["detail"]["unresolved"]
    assert unresolved, "the gaps must be reported, not silently dropped"
    text = " ".join(unresolved).lower()
    assert "irsa" in text
    assert "sqs" in text or "ingest" in text
    assert "readonlyaccess" in text or "read-only" in text


def test_the_unresolved_list_is_not_a_failure() -> None:
    """The registration itself succeeded; the gaps are separate contract work.

    Reporting them must not turn a working registration into an error — that would
    make the verb unusable — but they must be visible in the payload.
    """
    result = _register(RecordingApi())

    assert result["status"] == "ok"
    assert result["detail"]["registered"] is True


# --- the design's own limits stay visible ------------------------------------


def test_the_domain_api_is_documented_as_proposed_not_existing() -> None:
    """No route serves this base yet, and the source must say so.

    `modules/domain-apps/superplane/` deliberately has no `api/` directory, so
    every path here is a proposed contract. Recording that in the source is what
    stops a later reader from assuming these calls were verified live.
    """
    assert not (REPO / "modules/domain-apps/superplane/api").exists()
    # Deliberately the RAW source here: this one is about the documentation.
    assert "PROPOSED" in SCRIPT.read_text()
