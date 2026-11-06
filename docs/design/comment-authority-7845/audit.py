"""Reproduce the source-only issue 7845 inventory and validate its documents."""

import argparse
import collections
import io
from pathlib import Path
import re
import subprocess
import tarfile


BASE = "c958000f0829b1462c2f4ce23bfb743676b97306"
ROOT = Path(__file__).resolve().parents[3]
DESTINATION = Path(__file__).resolve().parent
INFLUENCE = re.compile(
    r"issue_comment|pull_request_review|listComments|getComments|fetchIssueComments|"
    r"/comments(?:[?`'\"]|\b)|comments\(first:|--json[^\n]*comments|"
    r"parseUserCommands|parsePlanApproval|hasRepositoryWritePermission|"
    r"pollForApproval|min_author_association|extractGateAnswerComment|"
    r"/approve\b|/lgtm\b|/monitor\b|/instruct\b|/queryPM\b|/retry\b"
)
AUTHORITY = re.compile(
    r"spawn_persona|is_human_rooted|authorized_user_id|credential_chain_depth|"
    r"VerifiedHumanEvent|authorize_command|PLAN_APPROVE|wrapUntrusted"
)
BUILD = re.compile(
    r"\b(?:terraform|tofu|kubectl|helm|docker|podman|buildah|codebuild)\b|"
    r"\baws\s+(?:lambda|cloudformation|eks|ecr)\b"
)


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments])


def component(path):
    prefixes = (
        ("modules/agent-factory/webhook-ingress/", "github-ingress-and-dispatch"),
        ("modules/agent-factory/agent/src/control", "run-control"),
        ("modules/agent-factory/agent/", "typescript-runtime"),
        ("modules/agent-factory/agent-worker-image/", "hosted-worker"),
        ("modules/agent-factory/codex-", "native-codex"),
        ("modules/agent-factory/", "agent-factory-sibling"),
        ("modules/gateway/src/activity/", "gateway-run-control"),
        ("modules/gateway/src/agentauth/", "gateway-protected-authority"),
        ("modules/gateway/src/orchestration/", "gateway-engine"),
        ("modules/gateway/src/admin/", "gateway-identity-and-connections"),
        ("modules/gateway/", "gateway-sibling"),
        ("platform/", "platform-build-and-deployment"),
        ("codebuild/", "image-build"),
        (".github/", "repository-automation"),
        ("scripts/", "repository-scripts"),
        ("environments/", "deployment-configuration"),
        ("contracts/", "shared-contracts"),
    )
    for prefix, name in prefixes:
        if path.startswith(prefix):
            return name
    parts = path.split("/")
    if parts[0] == "modules":
        return "/".join(parts[:3])
    return parts[0] if len(parts) > 1 else "repository-root"


def disposition(path, signals):
    if path.startswith(("docs/", "doc/", "aidlc/", "aidlc-docs/", "agent_learning/", ".adp/")) or path.endswith(".md"):
        return "reference-only;not-executable-comment-authority"
    if "/tests/" in path or "/test/" in path or path.startswith("tests/") or ".test." in path or Path(path).name.startswith("test_") or path.endswith(".tftest.hcl"):
        return "test-or-fixture;not-production-entry"
    if signals:
        return "source-candidate;see-channel-audit-for-effect-boundary"
    if path.startswith(("modules/agent-factory/", "modules/gateway/", ".github/", "platform/", "codebuild/", "scripts/", "contracts/", "environments/")) or path in {"deploy.sh", "remove.sh", "status.sh"}:
        return "component-support;no-matched-comment-authority-syntax"
    return "excluded-other-component;no-matched-comment-authority-syntax"


def surfaces(path, text):
    name = Path(path).name
    kinds = []
    if path.endswith((".yaml", ".yml")) and ("/workflows/" in path or re.search(r"^on:", text, re.M)):
        kinds.append("workflow-or-template")
    if name in {"action.yml", "action.yaml"}:
        kinds.append("action")
    if "Dockerfile" in name or name.startswith("Containerfile"):
        kinds.append("image")
    if "buildspec" in name or path.startswith("codebuild/"):
        kinds.append("buildspec-or-helper")
    if name in {"package.json", "pyproject.toml", "Makefile", "makefile", "Justfile", "Taskfile.yml", "Cargo.toml", "go.mod", "setup.py", "setup.cfg"}:
        kinds.append("package-build")
    if path.endswith((".tf", ".tf.json")):
        kinds.append("infrastructure")
    if path.endswith((".yml", ".yaml")) and re.search(r"^kind:\s*\S", text, re.M):
        kinds.append("manifest")
    if path.endswith((".sh", ".bash", ".ps1")) or text.startswith("#!") or re.search(r"if __name__\s*==", text):
        kinds.append("script-entry")
    if not path.endswith((".md", ".json", ".lock")) and BUILD.search(text):
        kinds.append("build-deploy-reference")
    return ",".join(kinds) or "-"


def table(header, rows):
    return "\n".join("\t".join(str(value).replace("\t", " ").replace("\n", " ") or "-" for value in row) for row in [header, *rows]) + "\n"


def inventory():
    tracked = {}
    for record in git("ls-tree", "-rz", BASE).decode().split("\0"):
        if record:
            metadata, path = record.split("\t", 1)
            mode, kind, digest = metadata.split()
            tracked[path] = (mode, kind, digest)
    content = {}
    with tarfile.open(fileobj=io.BytesIO(git("archive", BASE))) as archive:
        for member in archive:
            if member.isfile():
                raw = archive.extractfile(member).read()
                content[member.name] = None if b"\0" in raw else raw.decode("utf-8", errors="replace")
    artifacts, entries, influences, authorities = [], [], [], []
    for path, (mode, kind, digest) in sorted(tracked.items()):
        text = content.get(path)
        lines = (text or "").splitlines()
        influence_lines = [str(number) for number, line in enumerate(lines, 1) if INFLUENCE.search(line)]
        authority_lines = [str(number) for number, line in enumerate(lines, 1) if AUTHORITY.search(line)]
        owner = component(path)
        decision = disposition(path, influence_lines or authority_lines)
        surface = surfaces(path, text or "")
        encoding = "text" if text is not None else "non-text-or-non-file;path-inventoried"
        artifacts.append((path, digest, owner, decision, encoding, surface))
        if surface != "-":
            entries.append((path, owner, surface, decision))
        if influence_lines:
            influences.append((path, owner, decision, ",".join(influence_lines)))
        if authority_lines:
            authorities.append((path, owner, decision, ",".join(authority_lines)))
    counts = collections.Counter(row[2] for row in artifacts)
    summary = [f"Source: {BASE}", f"Tracked artifacts: {len(artifacts)}", f"Build/deployment/package/script candidates: {len(entries)}", f"Comment/influence syntax candidates: {len(influences)}", f"Authority/lineage syntax candidates: {len(authorities)}", f"Unclassified tracked paths: {len(tracked) - len(artifacts)}", "", "Component\tTracked artifacts"]
    summary.extend(f"{owner}\t{count}" for owner, count in sorted(counts.items()))
    return {
        "tracked-artifacts.tsv": table(("path", "git_blob", "component", "disposition", "content", "entrypoint_kinds"), artifacts),
        "entrypoints.tsv": table(("path", "component", "kinds", "disposition"), entries),
        "influence-sources.tsv": table(("path", "component", "disposition", "matching_lines"), influences),
        "authority-sources.tsv": table(("path", "component", "disposition", "matching_lines"), authorities),
        "inventory-summary.txt": "\n".join(summary) + "\n",
    }, content


def check_links(content):
    checked = 0
    for document in sorted(DESTINATION.glob("*.md")):
        text = document.read_text()
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            source = re.fullmatch(r"https://github.com/aws-e/adp/blob/([a-f0-9]{40})/(.+?)(?:#L(\d+)(?:-L(\d+))?)?", target)
            if source:
                revision, path, first, last = source.groups()
                if revision != BASE or path not in content:
                    raise SystemExit(f"Invalid source link in {document.name}: {target}")
                length = len((content[path] or "").splitlines())
                if first and not 1 <= int(first) <= int(last or first) <= length:
                    raise SystemExit(f"Invalid source lines in {document.name}: {path}")
                checked += 1
            elif not target.startswith(("https://", "http://", "#")):
                path = target.split("#", 1)[0]
                if not (document.parent / path).exists():
                    raise SystemExit(f"Broken local link in {document.name}: {path}")
                checked += 1
    issue = (DESTINATION / "issue-body.md").read_text()
    backlog = (DESTINATION / "backlog.md").read_text()
    acceptance = re.findall(r"^\| (AC-\d+) \|", issue, re.M)
    scenarios = re.findall(r"^\| (SCN-\d+) \|", backlog, re.M)
    if acceptance != [f"AC-{number:02d}" for number in range(1, 11)]:
        raise SystemExit("Acceptance IDs missing, duplicated or reordered")
    if scenarios != [f"SCN-{number:02d}" for number in range(1, 13)]:
        raise SystemExit("Scenario IDs missing, duplicated or reordered")
    if len(issue.encode()) > 8192:
        raise SystemExit("Issue body exceeds the 8 KiB authoring limit")
    print(f"Validated {checked} immutable-source/local document links, 10 ACs, 12 scenarios and issue size")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Regenerate only the adjacent static inventory artifacts")
    args = parser.parse_args()
    outputs, content = inventory()
    for name, expected in outputs.items():
        path = DESTINATION / name
        if args.write:
            path.write_text(expected)
        elif not path.exists() or path.read_text() != expected:
            raise SystemExit(f"Inventory differs: {name}; regenerate at the pinned source")
    check_links(content)
    print(outputs["inventory-summary.txt"].split("\n\n", 1)[0])


if __name__ == "__main__":
    main()
