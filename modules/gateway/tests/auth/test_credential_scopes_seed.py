"""Seed lockstep test — credential_scopes must be granted in BOTH Terraform roots.

Issue #4131 (grant step): the scaledjob-worker registry row is written by two
independent Terraform roots that both target the same DynamoDB item:

  * modules/gateway/infra/modules/lambda-authorizer/main.tf
    (aws_dynamodb_table_item.scaledjob_worker — the canonical copy, applies in the
    pipeline deploy path)
  * modules/agent-factory/infra/agent-registry-seed.tf
    (aws_dynamodb_table_item.scaledjob_worker_agent — gated behind
    seed_agent_registry, for self-managed deploys that only run agent-factory infra)

Both write the same item and both carry ignore_changes = [item], so whichever
applies first wins and the other never corrects it. If credential_scopes is added
to only one root, the deploy path that runs the *other* root seeds a row with no
grant — and once enforcement lands (follow-on PR) every internal credential call
from that plane 403s. That failure mode is invisible to a Terraform plan and to
every runtime unit test, because it depends on which root applied.

Hence a static-HCL assertion: read both files, confirm each grants
credential:raw-read to scaledjob-worker. This is deliberately textual — parsing
HCL properly would need a dependency, and the thing being guarded is exactly that
one attribute's presence in one item in each file.

If this test fails, add the attribute to the missing root. Do not relax the test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]

_GATEWAY_SEED = _REPO_ROOT / "modules" / "gateway" / "infra" / "modules" / "lambda-authorizer" / "main.tf"
_AGENT_FACTORY_SEED = _REPO_ROOT / "modules" / "agent-factory" / "infra" / "agent-registry-seed.tf"

# The two resource blocks that write the scaledjob-worker item, one per root.
_SEEDS = [
    (_GATEWAY_SEED, "scaledjob_worker"),
    (_AGENT_FACTORY_SEED, "scaledjob_worker_agent"),
]

_GRANTED_SCOPE = "credential:raw-read"


def _resource_block(path: Path, resource_name: str) -> str:
    """Return the text of the aws_dynamodb_table_item.<resource_name> block.

    Slices from the resource header to the next top-level `resource`/`data`/
    `module` declaration (column 0), which is enough to isolate one block without
    a real HCL parser.

    Comment lines are stripped. Both seeds carry a `# ... credential_scopes ...`
    explanatory comment, so a substring check against the raw text would pass even
    if the actual attribute were deleted — the guard has to see only real HCL.
    """
    if not path.is_file():
        pytest.fail(
            f"Terraform seed file not found at {path}. This lockstep test's path "
            "arithmetic is stale — fix the path rather than deleting the test, or "
            "the two seed locations stop being compared at all."
        )

    text = path.read_text()
    header = f'resource "aws_dynamodb_table_item" "{resource_name}"'
    start = text.find(header)
    if start == -1:
        pytest.fail(
            f'{path.name} no longer declares aws_dynamodb_table_item."{resource_name}". '
            "If the seed was renamed or moved, update this test to point at the new "
            "resource — the credential_scopes grant still has to exist in both roots."
        )

    rest = text[start + len(header) :]
    next_block = re.search(r"^(resource|data|module)\s", rest, re.M)
    end = len(rest) if next_block is None else next_block.start()
    block = rest[:end]

    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))


@pytest.mark.parametrize(("path", "resource_name"), _SEEDS, ids=["gateway-infra", "agent-factory-infra"])
def test_seed_grants_credential_scopes(path: Path, resource_name: str) -> None:
    """Each root's scaledjob-worker item must carry a credential_scopes string set."""
    block = _resource_block(path, resource_name)

    assert "credential_scopes" in block, (
        f"{path.name}: aws_dynamodb_table_item.{resource_name} does not seed "
        "credential_scopes. Both Terraform roots write this same DynamoDB item with "
        "ignore_changes=[item], so a grant missing from one root means whichever "
        "deploy path applies that root seeds a row with no credential grant."
    )


@pytest.mark.parametrize(("path", "resource_name"), _SEEDS, ids=["gateway-infra", "agent-factory-infra"])
def test_seed_grants_exactly_raw_read(path: Path, resource_name: str) -> None:
    """The grant must be exactly credential:raw-read — no broader, no narrower.

    credential:raw-read is the only scope any caller asserts today
    (agent-worker-image/lib/gateway_credential_client.py). Seeding
    credential:materialize as well would grant an unexercised capability; seeding
    something else would leave the real caller unauthorized once enforcement lands.
    """
    block = _resource_block(path, resource_name)

    match = re.search(r"credential_scopes\s*=\s*\{\s*SS\s*=\s*\[(?P<items>[^\]]*)\]", block)
    assert match is not None, (
        f"{path.name}: credential_scopes on {resource_name} is not a DynamoDB string "
        "set of the form `credential_scopes = { SS = [...] }`. The gateway parses it "
        'via .get("SS", []), so any other attribute type silently reads as no grant.'
    )

    scopes = [s.strip().strip('"') for s in match.group("items").split(",") if s.strip()]
    assert scopes == [_GRANTED_SCOPE], f"{path.name}: {resource_name} grants {scopes!r}, expected [{_GRANTED_SCOPE!r}]."


def test_both_seeds_grant_identical_scopes() -> None:
    """The two roots must grant the same set — they write the same item."""
    grants = {}
    for path, resource_name in _SEEDS:
        block = _resource_block(path, resource_name)
        match = re.search(r"credential_scopes\s*=\s*\{\s*SS\s*=\s*\[(?P<items>[^\]]*)\]", block)
        assert match is not None, f"{path.name}: no credential_scopes string set found"
        grants[path.name] = sorted(s.strip().strip('"') for s in match.group("items").split(",") if s.strip())

    distinct = {tuple(v) for v in grants.values()}
    assert len(distinct) == 1, (
        f"The two seed roots disagree on scaledjob-worker's credential scopes: {grants}. "
        "Both write the same DynamoDB item with ignore_changes=[item], so the grant a "
        "row ends up with depends on which root applied first — a non-deterministic "
        "authorization outcome. Make them identical."
    )
