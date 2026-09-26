#!/usr/bin/env python3
"""The single entry point every SSM command invokes on the instance.

`python3 dispatcher.py <purpose> <payload.json>` runs one journey and prints one
JSON document. `--self-check` imports every registered module and exits 0, which
the install command runs immediately after extraction so a syntax error or a
missing module is caught at delivery time rather than three stages later.

PURPOSES is the contract between the orchestrator and this directory, and it is
deliberately explicit rather than derived from a directory listing. `bundle.py`
imports it to publish exactly what the shipped code can execute, so a stage that
asks for something unregistered fails naming the module a developer must write.
That replaces the resolver which used to return None for nine of the fifteen
cases while the mapping still looked complete.

Purpose names are keys, not paths: several purposes share a module and differ
only by mode (personal_aws_provision and personal_aws_handoff are one script run
two ways), and one module can be renamed without changing the orchestrator.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# purpose -> (module, extra payload the orchestrator does not have to supply)
PURPOSES = {
    "story_capabilities": ("story_reads", {"mode": "capabilities"}),
    "story_usage": ("story_reads", {"mode": "usage"}),
    "story_budget": ("story_reads", {"mode": "budget"}),
    "story_activity": ("story_reads", {"mode": "activity"}),
    "tenant_smoke": ("tenant_isolation", {"mode": "smoke"}),
    "tenant_isolation": ("tenant_isolation", {"mode": "isolation"}),
    "story_hierarchy": ("story_reads", {"mode": "hierarchy"}),
    "story_vault": ("story_reads", {"mode": "vault"}),
    "story_github_maintenance": ("story_reads", {"mode": "github_maintenance"}),
    # E01/E02/E03 — install the served release, then a real Cognito login.
    "install_auth": ("install_auth", {}),
    # E04/E05 — `adp aws connect`, provisioned and handoff variants.
    "personal_aws_provision": ("personal_aws", {"mode": "provision"}),
    "personal_aws_handoff": ("personal_aws", {"mode": "handoff"}),
    # E06 — `adp admin bedrock connect` direct/reuse/download/resume.
    "bedrock_routing": ("bedrock_routing", {}),
    # E08 — real Claude and Codex inference through ADP.
    "personal_inference": ("personal_inference", {}),
    # E13 — the live API run through its own CLI consumers and checked against
    # the browser's declared wire types.
    "api_parity": ("api_parity", {}),
    # E14 — `adp update`, `adp update --rollback`, an interrupted install, and
    # the `codex setup`/`claude setup` verbs plus both launchers.
    "update_rollback": ("update_rollback", {}),
    # E16/E17 (#5413) — one installed CLI against three real deployments. Two
    # purposes sharing one module, in the `personal_aws` pattern: `overlap` proves
    # three concurrent tool sessions do not cross, `lifecycle` proves a default
    # switch, a refresh and one logout leave the other two correctly routed.
    "multi_deployment_concurrency": ("multi_deployment", {"mode": "overlap"}),
    "multi_deployment_lifecycle": ("multi_deployment", {"mode": "lifecycle"}),
    # E19 — served capability contrast and read-only diagnostics.
    "capability_contrast": ("capability_contrast", {}),
    # E18 (#5637) — real Superplane workspace, deployment and two-store
    # credential lifecycle through the served CLI.
    "superplane_domain": ("superplane_domain", {}),
    # Diagnostic checkpoint only; deliberately not mapped to an acceptance case.
    "multi_deployment_sessions": ("multi_deployment_sessions", {}),
    "usage_readback": ("usage_readback", {}),
    # #5629 terminal diagnostic only; not proof of active-run acceptance.
    "agent_terminal_controls": ("agent_terminal_controls", {}),
}


def load(purpose):
    """Import the module implementing a purpose, or say what is missing."""
    if purpose not in PURPOSES:
        raise SystemExit(
            json.dumps(
                {
                    "success": False,
                    "error": f"No remote script implements {purpose!r}",
                    "error_type": "UnknownPurpose",
                    "available": sorted(PURPOSES),
                }
            )
        )
    name, defaults = PURPOSES[purpose]
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        raise SystemExit(
            json.dumps(
                {
                    "success": False,
                    "error": f"remote/{name}.py is registered for {purpose!r} but did not import",
                    "error_type": type(exc).__name__,
                }
            )
        ) from None
    return module, defaults


def self_check():
    """Import every registered module. Used at install time.

    Prints the purposes it proved importable, so the install evidence records
    what the instance can actually run rather than what we hoped we shipped.
    """
    problems = {}
    for purpose, (name, _defaults) in sorted(PURPOSES.items()):
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report, do not abort the loop
            problems[purpose] = f"{type(exc).__name__} importing remote/{name}.py"
            continue
        if not hasattr(module, "execute"):
            problems[purpose] = f"remote/{name}.py has no execute(config, evidence)"
    print(
        json.dumps(
            {
                "success": not problems,
                "purposes": sorted(PURPOSES),
                "problems": problems,
            },
            sort_keys=True,
        )
    )
    return int(bool(problems))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] == "--self-check":
        return self_check()
    purpose, rest = argv[0], argv[1:]
    module, defaults = load(purpose)
    import common

    def execute(config, evidence):
        evidence["purpose"] = purpose
        # Defaults are merged UNDER the payload so the orchestrator stays
        # authoritative; they only supply what a purpose implies.
        module.execute({**defaults, **config}, evidence)

    return common.run_script(execute, rest)


if __name__ == "__main__":
    sys.exit(main())
