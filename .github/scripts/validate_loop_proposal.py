#!/usr/bin/env python3
"""Validate a loop-proposal document. Advisory — exits non-zero on any violation.

Issue #4199. This is the **advisory** half of the loop-proposal contract. An
authoring skill runs it before posting a gate, and CI runs it over the shipped
fixtures. It is deliberately thin: parse a file, call `validate_proposal`, print
what is wrong, exit non-zero if anything is.

All rules live in `modules/gateway/src/orchestration/proposal.py`, which is the
same module `compile_proposal` imports. That is the point — this script holds no
validation logic of its own, so it cannot drift from the authoritative check.
Adding a rule here instead of there would create exactly the gap the double
validation exists to close: a document this script blesses that the engine then
refuses, or worse, one it blesses that the engine accepts for different reasons.

**This script is not a control.** It runs in the author's environment and an author
can skip it. The authoritative check is inside the transaction that creates
nodes (`compile_proposal`), which is the only code path that can write them.

Invocation is by path, with the exit code gating the build — the repo's
established shape for a Python CI tool. Precedent:
`.github/scripts/diff_security_findings.py`, invoked at `security-scan.yml:521`
with its exit code gating at `:544`. No `console_scripts` entrypoint is defined in
any of the repo's `pyproject.toml` files; every tool is path-invoked.

Usage:
    python3 .github/scripts/validate_loop_proposal.py <proposal.json> [more.json ...]

Exit codes:
    0  every document validated clean
    1  at least one document had a violation
    2  a document could not be read or parsed (bad path, malformed JSON, or a
       shape pydantic rejects outright — e.g. a missing required field)
"""

import argparse
import json
import sys
from pathlib import Path

# The model lives in the gateway module, which is not installed when this script
# runs from a bare checkout in CI. Add its `src` parent to the path so
# `src.orchestration...` imports resolve the same way they do under pytest.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATEWAY_ROOT = _REPO_ROOT / "modules" / "gateway"
if str(_GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_ROOT))

try:
    from pydantic import ValidationError

    from src.orchestration.proposal import LoopProposal, validate_proposal
except ImportError as exc:  # pragma: no cover - environment problem, not a document problem
    print(f"error: cannot import the loop-proposal model ({exc}).", file=sys.stderr)
    print(f"       expected it at {_GATEWAY_ROOT / 'src' / 'orchestration' / 'proposal.py'}", file=sys.stderr)
    print("       is pydantic installed? try: pip install -e 'modules/gateway[dev]'", file=sys.stderr)
    sys.exit(2)


# Distinguished from a violation: a document that will not parse has no rules to
# report against, so it exits 2 rather than 1. CI treats both as failure, but an
# author needs to know whether to fix their JSON or their plan.
EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_UNREADABLE = 2


def _load(path: Path) -> LoopProposal:
    """Read and parse one proposal document.

    Raises:
        ValueError: With a message written for the author, on any failure to get
            from a path to a `LoopProposal`. The caller turns it into exit 2.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object at the top level, got {type(payload).__name__}")

    try:
        return LoopProposal.model_validate(payload)
    except ValidationError as exc:
        # Flattened to one line per problem. Pydantic's default repr is several
        # lines per error with a docs URL, which buries the actual field names.
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors())
        raise ValueError(f"{path} does not match the loop-proposal schema: {problems}") from exc


def _report(path: Path, proposal: LoopProposal) -> int:
    """Validate one parsed proposal and print the outcome. Returns a violation count."""
    violations = validate_proposal(proposal)

    if not violations:
        print(f"OK   {path} — {len(proposal.nodes)} node(s), {len(proposal.edges)} edge(s), spec revision {proposal.spec_revision}")
        return 0

    print(f"FAIL {path} — {len(violations)} violation(s):")
    for violation in violations:
        location = f" [{violation.where}]" if violation.where else ""
        print(f"       {violation.rule}: {violation.message}{location}")
    return len(violations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate loop-proposal documents against the shared schema. Advisory: the authoritative check runs at approval.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="proposal JSON document(s) to validate")
    args = parser.parse_args(argv)

    # Every document is processed before exiting, rather than bailing on the
    # first bad one, so an author fixing a batch sees the whole picture in one run.
    unreadable = 0
    total_violations = 0

    for path in args.paths:
        try:
            proposal = _load(path)
        except ValueError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            unreadable += 1
            continue
        total_violations += _report(path, proposal)

    if unreadable:
        print(f"\n{unreadable} document(s) could not be read or parsed.", file=sys.stderr)
        return EXIT_UNREADABLE

    if total_violations:
        print(f"\n{total_violations} violation(s) across {len(args.paths)} document(s).", file=sys.stderr)
        return EXIT_VIOLATIONS

    print(f"\nAll {len(args.paths)} document(s) valid.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
