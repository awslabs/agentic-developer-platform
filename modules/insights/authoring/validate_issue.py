"""Offline format lint for opted-in epic/story issue bodies; never an acceptance gate."""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SECTIONS = [
    "The problem in plain terms",
    "Description",
    "Impact analysis",
    "Design",
    "Deployment",
    "Validation",
]
STORY_AC = [
    "ID",
    "Setup and action",
    "Expected result / failure condition",
    "Required evidence",
    "Phase and owner",
]
EPIC_AC = [
    "ID",
    "Outcome / acceptance rule",
    "Child stories / criteria",
    "Integration evidence",
    "Phase and owner",
]
SCENARIOS = [
    "Scenario ID",
    "Acceptance IDs",
    "Case contract",
    "Verification",
    "Test reference",
    "State",
]
LAYERS = {"automatic-unit", "automatic-integration", "automatic-e2e", "manual"}


def visible_lines(body):
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    fence = None
    for line in body.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            token = match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            yield line


def tables(lines):
    result, section, block = [], "", []
    for line in [*lines, ""]:
        if line.startswith("|"):
            block.append(
                [
                    cell.strip()
                    for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
                ]
            )
        elif block:
            if len(block) >= 2 and all(
                re.fullmatch(r":?-+:?", cell) for cell in block[1]
            ):
                result.append((section, block[0], block[2:]))
            block = []
        if line.startswith("## "):
            section = line[3:].strip()
    return result


def issue_url(value, criterion=False):
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and re.fullmatch(r"/[^/]+/[^/]+/issues/[1-9][0-9]*", parsed.path)
        and (
            re.fullmatch(r"AC-[0-9]{2,}", parsed.fragment)
            if criterion
            else not parsed.fragment
        )
    )


def unresolved(value):
    return bool(re.search(r"\b(?:unresolved|unmapped|TBD|TODO)\b|<[^>]+>", value, re.I))


def lint(body, *, stage="draft"):
    """Return errors and advisories; remote identity/authority and semantics are not checked."""
    errors, advisories = [], []
    if stage not in {"draft", "ready"}:
        return ["stage must be draft or ready"], []
    if len(body.encode()) > 8192:
        errors.append("issue body exceeds 8 KiB; link versioned supporting procedures")
    lines = list(visible_lines(body))
    headings = [line[3:].strip() for line in lines if line.startswith("## ")]
    if headings != SECTIONS:
        errors.append("use the six canonical sections once, in order")
    parsed = tables(lines)

    def table(section, header):
        matches = [
            rows for sec, cols, rows in parsed if sec == section and cols == header
        ]
        if len(matches) != 1 or not matches[0]:
            errors.append(
                f"{section}: exactly one nonempty {header[0]} table with the canonical columns is required"
            )
            return []
        good = []
        for row in matches[0]:
            if len(row) != len(header) or any(not cell for cell in row):
                errors.append(
                    f"{section}: empty/malformed table row; escape literal pipes"
                )
            else:
                good.append(row)
        return good

    lineage_rows = table("Description", ["Field", "Value"])
    lineage = dict(lineage_rows)
    if len(lineage) != len(lineage_rows):
        errors.append("duplicate lineage fields")
    for key in [
        "Kind",
        "Project reference",
        "Parent epic",
        "Module IDs",
        "Feature IDs",
    ]:
        if not lineage.get(key):
            errors.append(f"lineage requires {key}")
        elif stage == "ready" and unresolved(lineage[key]):
            errors.append(f"lineage {key} remains unresolved")
    kind = lineage.get("Kind")
    if kind not in {"story", "epic"}:
        errors.append(
            "Kind must be story or epic; this checker does not lint other issue types"
        )
        return errors, advisories
    parent = lineage.get("Parent epic", "")
    if stage == "ready" and parent != "none" and not issue_url(parent):
        errors.append("Parent epic must be a full issue URL or none")
    for label in ["Delivery owner", "Completion boundary"]:
        match = re.search(r"\*\*" + label + r":\*\*\s*([^\n]+)", "\n".join(lines))
        if not match or (stage == "ready" and unresolved(match[1])):
            errors.append(f"{label} must be explicit")
    rows = table("Validation", STORY_AC if kind == "story" else EPIC_AC)
    ids = [row[0] for row in rows]
    if len(set(ids)) != len(ids):
        errors.append("duplicate acceptance IDs")
    pattern = r"AC-[0-9]{2,}" if kind == "story" else r"EC-[0-9]{2,}"
    for row in rows:
        if not re.fullmatch(pattern, row[0]):
            errors.append(f"invalid acceptance ID: {row[0]}")
        if stage == "ready" and any(unresolved(cell) for cell in row):
            errors.append(f"{row[0]} has unresolved acceptance fields")
        if (
            kind == "story"
            and stage == "ready"
            and not all(word in row[1] for word in ["Given:", "When:"])
        ):
            errors.append(f"{row[0]} needs explicit Given: and When:")
        if kind == "epic" and stage == "ready":
            children = [part.strip() for part in row[2].split(",")]
            if row[2] != "direct" and not all(
                issue_url(child, criterion=True) for child in children
            ):
                errors.append(
                    f"{row[0]} requires full child issue URL#AC-ID references or direct integration proof"
                )
    if kind == "story":
        scenarios = table("Validation", SCENARIOS)
        covered, pairs, contracts = set(), set(), {}
        for sid, acs, case, layer, test, state in scenarios:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", sid):
                errors.append(f"invalid scenario ID: {sid}")
            links = [part.strip() for part in acs.split(",")]
            if any(key not in ids for key in links) or len(set(links)) != len(links):
                errors.append(f"{sid} has unknown/duplicate acceptance references")
            covered.update(links)
            if (sid, test) in pairs:
                errors.append(f"{sid} repeats a test mapping")
            pairs.add((sid, test))
            signature = (tuple(sorted(links)), case)
            if sid in contracts and contracts[sid] != signature:
                errors.append(f"{sid} has conflicting case or acceptance definitions")
            contracts[sid] = signature
            if layer not in LAYERS or state not in {"planned", "implemented", "manual"}:
                errors.append(f"{sid} has unknown verification/state")
            if (layer == "manual") != (state == "manual"):
                errors.append(f"{sid} manual verification and state must agree")
            if stage == "ready":
                if any(
                    unresolved(cell) for cell in (sid, acs, case, layer, test, state)
                ):
                    errors.append(f"{sid} has unresolved scenario fields")
                pinned = re.fullmatch(r"[^\s]+@[0-9a-f]{40}#[A-Za-z0-9._-]+", case)
                if not pinned and not all(
                    word in case for word in ["Given:", "When:", "Then:", "Controls:"]
                ):
                    errors.append(
                        f"{sid} needs Given/When/Then/Controls or a path@commit#anchor"
                    )
                if layer != "manual" and not re.fullmatch(r"[^\s:]+::\S.*", test):
                    errors.append(
                        f"{sid} automated Test reference needs path::selector"
                    )
                if layer == "manual" and not re.fullmatch(
                    r"[^\s]+@[0-9a-f]{40}#[A-Za-z0-9._-]+", test
                ):
                    errors.append(
                        f"{sid} manual Test reference needs path@commit#anchor"
                    )
            if state == "planned":
                advisories.append(
                    f"{sid}: planned test; format readiness is not executed coverage"
                )
        for missing in sorted(set(ids) - covered):
            errors.append(f"{missing} has no scenario or manual procedure mapping")
    advisories.append(
        "Offline format only: verify registered project, native hierarchy, selectors, fixtures, oracle and authority independently."
    )
    return errors, advisories


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("body", type=Path)
    parser.add_argument("--stage", choices=["draft", "ready"], default="draft")
    args = parser.parse_args(argv)
    try:
        errors, advisories = lint(args.body.read_text(), stage=args.stage)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Cannot validate issue body: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print("ERROR: " + error, file=sys.stderr)
    for advisory in advisories:
        print("NOTE: " + advisory)
    if not errors:
        print(f"{args.stage} format checks passed; no execution or acceptance claim")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
