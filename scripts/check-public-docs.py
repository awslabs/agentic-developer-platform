#!/usr/bin/env python3
"""Check public documentation without echoing deployment identifiers."""

from pathlib import Path
import re
import subprocess
import sys


EXAMPLE_ACCOUNTS = {
    "123456789012",
    "111122223333",
    "444455556666",
    "210987654321",
    "987654321098",
    "111111111111",
    "222222222222",
    "333333333333",
    "444444444444",
    "300000000000",
}
ACCOUNT = re.compile(r"(?<![A-Za-z0-9])\d{12}(?![A-Za-z0-9])")
RESOURCE = re.compile(
    r"\b(?:vpc|subnet|sg|i|vol|eni|ami|igw|nat|rtb|vpce|snap|eipalloc|acl)"
    r"-([a-f0-9]{8,17})\b"
)
POOL = re.compile(r"\b(?:us|eu|ap|ca|sa|me|af|il)-(?:[a-z]+-)*\d_([A-Za-z0-9]{6,})\b")
HOST = re.compile(
    r"\b[A-Za-z0-9_.-]+\.(?:cloudfront\.net|execute-api\.[a-z0-9-]+\.amazonaws\.com"
    r"|elb\.amazonaws\.com|eks\.amazonaws\.com|rds\.amazonaws\.com)\b",
    re.I,
)


def findings(text):
    # Public GitHub job IDs can also be twelve digits. Preserve source links.
    public_job_ids = set(re.findall(r"https://github\.com/[^\s\"]+/job/(\d+)\b", text))
    for job_id in public_job_ids:
        text = text.replace('"id": ' + job_id, '"id": PUBLIC_JOB_ID')
    text = re.sub(r"https://github\.com/[^\s<>`\"\)]+", "", text)
    for line_number, line in enumerate(text.splitlines(), 1):
        if any(
            m[0] not in EXAMPLE_ACCOUNTS and not m[0].startswith("000000000")
            for m in ACCOUNT.finditer(line)
        ):
            yield line_number, "non-example account-like identifier"
        if any(not m[1].startswith("000000") for m in RESOURCE.finditer(line)):
            yield line_number, "non-example network/resource identifier"
        if any(
            not re.match(r"(?:example|x+|abc123)", m[1], re.I)
            for m in POOL.finditer(line)
        ):
            yield line_number, "non-example Cognito pool identifier"
        if any(
            m[0] != "truststore.pki.rds.amazonaws.com"
            and not any(
                token in m[0].lower()
                for token in (
                    "example",
                    "xxxx",
                    "your-api",
                    "abc123",
                    "api.execute-api.region",
                )
            )
            for m in HOST.finditer(line)
        ):
            yield line_number, "deployment endpoint"


def main():
    root = Path(__file__).resolve().parents[1]
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    count = 0
    errors = []
    for name in tracked:
        if not (
            name.startswith("docs/")
            or name in {"README.md", "AGENTS.md", "CLAUDE.md"}
            or (name.startswith("tests/e2e/cli_uplift/") and name.endswith(".json"))
        ):
            continue
        try:
            content = (root / name).read_text()
        except (UnicodeError, FileNotFoundError):
            continue
        count += 1
        errors.extend(
            f"{name}:{line}: {category}" for line, category in findings(content)
        )
    for error in errors:
        print(error)
    print(f"Checked {count} public documentation files; {len(errors)} findings.")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
