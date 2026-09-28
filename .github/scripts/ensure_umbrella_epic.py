#!/usr/bin/env python3
"""Idempotently ensure the nightly-security preconditions exist.

Two GitHub objects the nightly security pipeline files work against:

1. The ``story`` label, used to mark an implementable work item.
2. The long-lived **runtime umbrella EPIC** that each night's dated EPIC becomes
   a child of, linked natively under the remediation parent (#615).

Both create paths are safely repeatable: the nightly run happens every night and
retries on failure, so anything created carelessly here gets created repeatedly.

The runtime umbrella is matched by **exact title**. That is deliberate: the
*delivery* EPIC for intent #4290 is also a child of #615 and also reads as "the
umbrella EPIC under #615" in prose. A substring or relevance match would find it
and make this script silently no-op, leaving the nightly runs with no home.
"""

import argparse
import json
import re
import subprocess  # nosec B404
import sys

# The runtime umbrella EPIC. Matched by exact equality, never by substring.
UMBRELLA_TITLE = "Security scan of the day — pen test + code scanning"

# Remediation tree the umbrella hangs under. Nightly findings must stay inside
# this tree — a second intake path is one nobody watches.
DEFAULT_PARENT = 615

# Bare labels only; no `type:`-prefixed scheme (decision D-20).
UMBRELLA_LABEL = "epic"
STORY_LABEL = "story"

# Native parent/child linking. Precedent: .github/workflows/spawn-deploy-instance.yml
ADD_SUB_ISSUE_MUTATION = (
    "mutation($p:ID!,$c:ID!)"
    "{addSubIssue(input:{issueId:$p,subIssueId:$c}){subIssue{number}}}"
)

UMBRELLA_BODY = """\
Permanent umbrella for the nightly security agent. Each night's dated EPIC is
linked here as a sub-issue, so one night's findings read as a single unit and a
month's read as a trend.

Created and maintained by `.github/scripts/ensure_umbrella_epic.py`. Do not
close or rename this issue: the script matches it by exact title, and a rename
makes the next nightly run file a second umbrella.

This is **not** the delivery EPIC for the build work that produced the pipeline
(that one is scoped to its units and closes when they merge). This issue has no
end date.
"""


def _gh(args: list[str]) -> tuple[int, str, str]:
    """Run a `gh` command. Single seam for every GitHub call, so tests can
    intercept all of them — including asserting that a create was *not* issued.
    """
    proc = subprocess.run(  # nosec: B603, B607
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,  # callers inspect the return code and decide
    )
    return proc.returncode, proc.stdout, proc.stderr


def label_exists(repo: str, name: str) -> bool:
    """True if the label already exists (404 from the labels endpoint = absent)."""
    rc, _, _ = _gh(["api", f"repos/{repo}/labels/{name}"])
    return rc == 0


def ensure_label(repo: str, name: str, color: str, description: str) -> bool:
    """Create the label if absent. Returns True if this call created it.

    Idempotent: when the label already exists no create request is issued at all,
    so re-running cannot clobber a hand-tuned colour or description.
    """
    if label_exists(repo, name):
        print(f"label '{name}' already exists — no create issued")
        return False

    rc, _, err = _gh(
        [
            "api",
            f"repos/{repo}/labels",
            "--method",
            "POST",
            "--field",
            f"name={name}",
            "--field",
            f"color={color}",
            "--field",
            f"description={description}",
        ]
    )
    if rc != 0:
        # Lost a race with a concurrent run: GitHub reports "already_exists".
        # That is the desired end state, not a failure.
        if "already_exists" in err or "already exists" in err:
            print(f"label '{name}' created concurrently — treating as success")
            return False
        raise RuntimeError(f"failed to create label '{name}': {err.strip()}")

    print(f"created label '{name}'")
    return True


def find_issue_by_exact_title(repo: str, title: str) -> int | None:
    """Find an issue whose title is *exactly* `title`, or None.

    The search API is a relevance match, so it happily returns near-identical
    EPICs. Everything it returns is filtered down to exact equality before use.
    """
    rc, out, err = _gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--search",
            f'in:title "{title}"',
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "number,title",
        ]
    )
    if rc != 0:
        raise RuntimeError(f"failed to search issues in {repo}: {err.strip()}")

    try:
        candidates = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unparseable issue search output: {exc}") from exc

    matches = sorted(c["number"] for c in candidates if c.get("title") == title)
    if not matches:
        return None
    if len(matches) > 1:
        print(
            f"WARNING: {len(matches)} issues share the exact title "
            f"{title!r}: {matches}. Using the lowest (#{matches[0]}); "
            "close the duplicates.",
            file=sys.stderr,
        )
    return matches[0]


def _node_id(repo: str, issue: int) -> str:
    rc, out, err = _gh(["api", f"repos/{repo}/issues/{issue}", "--jq", ".node_id"])
    if rc != 0:
        raise RuntimeError(f"failed to read node_id of #{issue}: {err.strip()}")
    return out.strip()


def current_parent(repo: str, issue: int) -> int | None:
    """Native sub-issue parent of `issue`, or None if it has no parent."""
    rc, out, _ = _gh(["api", f"repos/{repo}/issues/{issue}/parent", "--jq", ".number"])
    if rc != 0:
        return None
    out = out.strip()
    return int(out) if out.isdigit() else None


def link_sub_issue(repo: str, parent: int, child: int) -> bool:
    """Link `child` under `parent` natively. Returns True if a link was created.

    No-ops when the parent already resolves, so re-runs issue no mutation.
    """
    existing = current_parent(repo, child)
    if existing == parent:
        print(f"#{child} is already a sub-issue of #{parent} — no mutation issued")
        return False
    if existing is not None:
        raise RuntimeError(
            f"#{child} is a sub-issue of #{existing}, not #{parent}; "
            "refusing to re-parent it"
        )

    rc, _, err = _gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={ADD_SUB_ISSUE_MUTATION}",
            "-F",
            f"p={_node_id(repo, parent)}",
            "-F",
            f"c={_node_id(repo, child)}",
        ]
    )
    if rc != 0:
        raise RuntimeError(f"failed to link #{child} under #{parent}: {err.strip()}")

    print(f"linked #{child} as a sub-issue of #{parent}")
    return True


def _create_umbrella(repo: str, title: str) -> int:
    rc, out, err = _gh(
        [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            UMBRELLA_BODY,
            "--label",
            UMBRELLA_LABEL,
        ]
    )
    if rc != 0:
        raise RuntimeError(f"failed to create umbrella EPIC: {err.strip()}")

    match = re.search(r"/issues/(\d+)", out.strip())
    if not match:
        raise RuntimeError(f"could not parse issue number from: {out.strip()!r}")
    return int(match.group(1))


def ensure_umbrella_epic(
    repo: str, parent: int = DEFAULT_PARENT, title: str = UMBRELLA_TITLE
) -> dict:
    """Ensure exactly one runtime umbrella EPIC exists, linked under `parent`.

    Returns ``{"number": int, "created": bool, "linked": bool}``. Safe to call
    repeatedly: the second call finds the umbrella by exact title and issues no
    create mutation.
    """
    existing = find_issue_by_exact_title(repo, title)
    if existing is not None:
        print(f"runtime umbrella EPIC already exists: #{existing}")
        # Repair a half-finished earlier run that created but never linked.
        return {
            "number": existing,
            "created": False,
            "linked": link_sub_issue(repo, parent, existing),
        }

    number = _create_umbrella(repo, title)
    print(f"created runtime umbrella EPIC #{number}")
    return {
        "number": number,
        "created": True,
        "linked": link_sub_issue(repo, parent, number),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently ensure the story label and the runtime "
        "umbrella EPIC for nightly security runs exist."
    )
    parser.add_argument("--repo", required=True, help="Repository (owner/name)")
    parser.add_argument(
        "--parent",
        type=int,
        default=DEFAULT_PARENT,
        help=f"Parent issue for the umbrella (default: {DEFAULT_PARENT})",
    )
    parser.add_argument(
        "--title", default=UMBRELLA_TITLE, help="Runtime umbrella EPIC title"
    )
    args = parser.parse_args(argv)

    ensure_label(
        args.repo,
        STORY_LABEL,
        color="0e8a16",
        description="An implementable unit of work",
    )
    result = ensure_umbrella_epic(args.repo, args.parent, args.title)
    print(f"umbrella_issue={result['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
