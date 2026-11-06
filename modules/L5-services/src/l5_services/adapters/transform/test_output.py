"""Count tests from the selected runner's retained output, never provider prose."""

import re
from pathlib import PurePosixPath


def completed_test_count(argv, output):
    if not argv or not isinstance(output, bytes):
        return None
    executable = PurePosixPath(argv[0]).name
    if executable == "node" and "--test" in argv:
        tap = "--test-reporter=tap" in argv or any(
            argv[index:index + 2] == ["--test-reporter", "tap"] for index in range(len(argv) - 1)
        )
        if not tap:
            return None
        counts = {}
        for field in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
            matches = re.findall(rb"^# " + field.encode() + rb" (\d+)\r?$", output, re.MULTILINE)
            if len(matches) != 1:
                return None
            counts[field] = int(matches[0])
        if counts["tests"] != sum(counts[field] for field in ("pass", "fail", "cancelled", "skipped", "todo")):
            return None
        if (not re.search(rb"^1\.\.\d+\r?$", output, re.MULTILINE)
                or not re.search(rb"(?:^|\n)# duration_ms [\d.]+\r?\n?\Z", output)):
            return None
        return counts["pass"]
    pytest = executable == "pytest" or (
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable) and argv[1:3] == ["-m", "pytest"]
    )
    if pytest:
        counts = re.findall(rb"(?:^|[\s=,])(\d+) passed(?:[\s,=]|$)", output)
        return int(counts[-1]) if counts else None
    return None
