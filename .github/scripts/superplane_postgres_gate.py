"""Require executed retirement PostgreSQL evidence from the offline domain lane."""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED_SUITES = frozenset(
    {
        "test_retirement_access_seal_postgres",
        "test_retirement_execution_postgres",
        "test_retirement_managed_access_postgres",
        "test_retirement_recovery_postgres",
    }
)


def require_retirement_postgres(report: Path) -> int:
    cases = ET.parse(report).getroot().findall(".//testcase")
    executed = 0
    for suite in sorted(REQUIRED_SUITES):
        expected = f"workspace_provisioning.tests.{suite}"
        selected = [
            case
            for case in cases
            if case.get("classname", "") == expected
            or case.get("classname", "").endswith("." + expected)
        ]
        if not selected:
            raise ValueError(f"Missing retirement PostgreSQL suite: {suite}")
        for case in selected:
            if any(
                case.find(status) is not None
                for status in ("skipped", "failure", "error")
            ):
                raise ValueError(
                    f"Retirement PostgreSQL test did not pass: {suite}::{case.get('name')}"
                )
        executed += len(selected)
    return executed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    arguments = parser.parse_args()
    count = require_retirement_postgres(arguments.report)
    print(f"Required retirement PostgreSQL evidence: {count} passed, zero skipped")


if __name__ == "__main__":
    main()
