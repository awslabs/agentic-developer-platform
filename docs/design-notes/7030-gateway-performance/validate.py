"""Validate this proposal's local links, line references and inventory."""

import csv
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[2]


def anchors(text):
    result = set()
    for heading in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        result.add(slug)
    return result


def main():
    subprocess.run(
        [sys.executable, str(DIRECTORY / "audit.py"), "--check"], cwd=ROOT, check=True
    )
    checked = 0
    failures = []
    with (DIRECTORY / "inventory.tsv").open() as inventory:
        baseline = {row["path"] for row in csv.DictReader(inventory, delimiter="\t")}
    with (DIRECTORY / "design-artifacts.tsv").open() as inventory:
        artifacts = list(csv.DictReader(inventory, delimiter="\t"))
    package_paths = [row["path"] for row in artifacts]
    if len(package_paths) != len(set(package_paths)):
        failures.append("Duplicate design artifact mapping")
    actual_package = {path.name for path in DIRECTORY.iterdir() if path.is_file()}
    if actual_package != set(package_paths):
        failures.append("Design artifact ledger does not match package files")
    package = {str((DIRECTORY / path).relative_to(ROOT)) for path in package_paths}
    tracked = set(
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .strip("\0")
        .split("\0")
    )
    if tracked | package != baseline | package:
        failures.append(
            "Tracked tree differs from baseline plus design artifact ledger"
        )
    print(f"Mapped {len(baseline)} baseline and {len(package)} design artifacts")
    stories = (DIRECTORY / "stories.md").read_text()
    sections = re.split(r"^## #(703[1-6]) .*?$", stories, flags=re.MULTILINE)
    expected_stories = {str(number) for number in range(7031, 7037)}
    if set(sections[1::2]) != expected_stories:
        failures.append("Missing or unexpected story section")
    for number, section in zip(sections[1::2], sections[2::2], strict=True):
        expected = [
            f"AC-{index:02}" for index in range(1, 5 if number == "7036" else 4)
        ]
        if re.findall(r"^\| (AC-\d+) \|", section, re.MULTILINE) != expected:
            failures.append(f"Acceptance IDs changed for #{number}")
    campaign = (DIRECTORY / "campaigns.md").read_text()
    if re.findall(r"^\| (AC-\d+) \|", campaign, re.MULTILINE) != [
        f"AC-{index:02}" for index in range(1, 6)
    ]:
        failures.append("Epic acceptance IDs changed")
    print("Checked 19 child and 5 epic acceptance IDs")
    for document in sorted(DIRECTORY.glob("*.md")):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            destination = (
                (document.parent / unquote(parsed.path)).resolve()
                if parsed.path
                else document
            )
            if not destination.is_file() or not destination.is_relative_to(ROOT):
                failures.append(f"{document.name}: missing/invalid path {target}")
                continue
            checked += 1
            fragment = unquote(parsed.fragment)
            if not fragment:
                continue
            content = destination.read_text()
            line = re.fullmatch(r"L([0-9]+)", fragment)
            if line:
                if not 1 <= int(line.group(1)) <= len(content.splitlines()):
                    failures.append(f"{document.name}: invalid source line {target}")
            elif destination.suffix == ".md" and fragment not in anchors(content):
                failures.append(f"{document.name}: missing heading {target}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Validated {checked} local file/heading/line links")


if __name__ == "__main__":
    main()
