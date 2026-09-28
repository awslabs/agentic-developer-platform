"""adp-review CLI entry point (issue #5350).

Usage:
  adp-review submit --repo OWNER/NAME --pr N --event APPROVE --body-file FILE
  adp-review submit --repo OWNER/NAME --pr N --event REQUEST_CHANGES --body "text"
  adp-review identity --repo OWNER/NAME

Flag parsing is hand-rolled to match adp-cred and adp-trigger, the two CLIs
already in this image.
"""

from __future__ import annotations

import json
import logging
import sys

from adp_review.client import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PENDING_APPROVAL,
    EXIT_USAGE,
    VALID_EVENTS,
    ReviewError,
    mint_review_token,
    submit_review,
)


def _usage() -> None:
    print(
        "Usage:\n"
        "  adp-review submit --repo OWNER/NAME --pr N --event EVENT "
        "(--body TEXT | --body-file FILE) [--commit SHA]\n"
        "  adp-review identity --repo OWNER/NAME\n"
        f"\nEVENT is one of: {', '.join(VALID_EVENTS)}\n"
        "\nExit codes:\n"
        "  0  formal review submitted, verdict recorded\n"
        "  1  the review could not be published\n"
        "  2  usage or environment error\n"
        "  3  verdict published as a comment only; a human approval is pending\n",
        file=sys.stderr,
    )
    sys.exit(EXIT_USAGE)


def _parse_flags(args: list[str], allowed: set[str]) -> dict[str, str]:
    """Parse ``--flag value`` pairs, rejecting anything unrecognised.

    Strict like adp-trigger's parser: an unknown flag or a missing value is a usage
    error rather than a silently dropped argument, because a silently dropped
    --event would change the verdict.
    """
    parsed: dict[str, str] = {}
    i = 0
    while i < len(args):
        flag = args[i]
        if not flag.startswith("--"):
            print(f"error: unexpected argument: {flag}", file=sys.stderr)
            _usage()
        name = flag[2:]
        if name not in allowed:
            print(f"error: unknown flag: {flag}", file=sys.stderr)
            _usage()
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            print(f"error: {flag} requires a value", file=sys.stderr)
            _usage()
        parsed[name] = args[i + 1]
        i += 2
    return parsed


def cmd_submit(args: list[str]) -> None:
    flags = _parse_flags(args, {"repo", "pr", "event", "body", "body-file", "commit"})

    repo = flags.get("repo")
    raw_pr = flags.get("pr")
    event = (flags.get("event") or "").upper()

    if not repo or not raw_pr or not event:
        print("error: --repo, --pr and --event are required", file=sys.stderr)
        _usage()
    try:
        pr_number = int(raw_pr)
    except ValueError:
        print(f"error: --pr must be a number, got {raw_pr!r}", file=sys.stderr)
        _usage()
        return

    if "body" in flags and "body-file" in flags:
        print("error: pass either --body or --body-file, not both", file=sys.stderr)
        _usage()
    if "body-file" in flags:
        try:
            with open(flags["body-file"], encoding="utf-8") as handle:
                body = handle.read()
        except OSError as exc:
            print(f"error: cannot read --body-file: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
    elif "body" in flags:
        body = flags["body"]
    else:
        print("error: --body or --body-file is required", file=sys.stderr)
        _usage()
        return

    # Prefer the distinct reviewer identity. A fallback is reported, never hidden:
    # the caller needs to know a formal verdict was impossible BEFORE reading the
    # outcome, so the reason is on stderr even on the success path.
    try:
        token, identity = mint_review_token(repo=repo)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILED)

    if identity != "review" and event in ("APPROVE", "REQUEST_CHANGES"):
        print(
            "note: using the run's default GitHub identity. Whether it can record a verdict "
            "depends on the PR author and repository permissions; GitHub's response will "
            "determine the publication outcome.",
            file=sys.stderr,
        )

    try:
        result = submit_review(
            repo=repo,
            pr_number=pr_number,
            event=event,
            body=body,
            commit_id=flags.get("commit"),
            token=token,
        )
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILED)

    result["identity"] = identity
    print(json.dumps(result, indent=2))

    # The exit code is the machine-readable half of the same honesty. A caller that
    # checks only "did it exit 0" must not conclude a verdict was recorded.
    sys.exit(EXIT_OK if result.get("verdict_recorded") else EXIT_PENDING_APPROVAL)


def cmd_identity(args: list[str]) -> None:
    """Report whether a formal verdict is possible, without publishing anything."""
    flags = _parse_flags(args, {"repo"})
    repo = flags.get("repo")
    if not repo:
        print("error: --repo is required", file=sys.stderr)
        _usage()
        return

    try:
        _, identity = mint_review_token(repo=repo)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAILED)

    print(
        json.dumps(
            {
                "identity": identity,
                # The default App can review a human-authored PR. Without a PR
                # and a submission response, its capability is unknown.
                "can_record_verdict": True if identity == "review" else None,
            },
            indent=2,
        )
    )
    sys.exit(EXIT_OK if identity == "review" else EXIT_PENDING_APPROVAL)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 2:
        _usage()

    command = sys.argv[1]
    rest = sys.argv[2:]

    if command == "submit":
        cmd_submit(rest)
    elif command == "identity":
        cmd_identity(rest)
    else:
        print(f"error: unknown command: {command}", file=sys.stderr)
        _usage()


if __name__ == "__main__":
    main()
