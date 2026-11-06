"""All live checks sharing an environment must retain queued evidence jobs."""

from pathlib import Path
import re

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def _strip_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_every_live_verification_producer_preserves_pending_checks():
    found = set()
    for path in WORKFLOWS.glob("*.yml"):
        lines = _strip_comments(path.read_text(encoding="utf-8")).splitlines()
        for index, line in enumerate(lines):
            match = re.fullmatch(r"( *)concurrency:\s*", line)
            if not match:
                continue
            indent = len(match.group(1))
            fields = {}
            for child in lines[index + 1 :]:
                if not child.strip():
                    continue
                child_indent = len(child) - len(child.lstrip())
                if child_indent <= indent:
                    break
                if child_indent == indent + 2:
                    key, _, value = child.strip().partition(":")
                    fields[key] = value.strip()
            if "cli-live-eval-" not in fields.get("group", ""):
                continue
            found.add(path.name)
            # A non-cancelling running job still has only one pending slot by
            # default. Every producer must retain waiting deployment evidence.
            assert fields.get("queue") == "max", path.name
            assert fields.get("cancel-in-progress") == "false", path.name
    assert {
        "adp-environment-smoke.yml",
        "credential-binding-adversarial-e2e.yml",
        "e2e-chat-playwright.yml",
        "e2e-new-ui-playwright.yml",
        "eval-budget-ratelimit.yml",
        "eval-cli-onboarding.yml",
        "eval-cli-uplift.yml",
        "gateway-live-tests.yml",
        "gateway-product-recovery.yml",
        "gateway-smoke.yml",
        "gitlab-integration-tests.yml",
    } <= found
