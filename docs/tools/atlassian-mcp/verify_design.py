"""Offline link/source checks for the versioned Atlassian design documents."""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[3]
DOCUMENTS = [
    ROOT / "docs/tools/atlassian-mcp-contract.md",
    *sorted(Path(__file__).parent.glob("*.md")),
]
SOURCE = re.compile(
    r"https://github.com/aws-e/adp/blob/([a-f0-9]{40})/([^#]+)(?:#L([0-9]+))?$"
)


def anchors(text):
    result, seen = set(), {}
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug + (f"-{count}" if count else ""))
    result.update(re.findall(r'<a\s+(?:name|id)="([^"]+)"', text))
    return result


def main():
    checked = set()
    for document in DOCUMENTS:
        text = document.read_text()
        definitions = dict(
            re.findall(r"^\[([^\]]+)\]:\s+(\S+)\s*$", text, re.MULTILINE)
        )
        references = re.findall(r"\[[^\]]+\]\[([^\]]+)\]", text)
        for reference in references:
            if reference not in definitions:
                raise SystemExit(
                    f"Undefined reference {reference} in {document.relative_to(ROOT)}"
                )
        targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text) + list(
            definitions.values()
        )
        for target in targets:
            parsed = urlparse(target)
            source = SOURCE.fullmatch(target)
            if source:
                revision, path, line = source.groups()
                data = subprocess.check_output(
                    ["git", "show", f"{revision}:{path}"], cwd=ROOT
                ).decode()
                if line and not 1 <= int(line) <= len(data.splitlines()):
                    raise SystemExit(f"Invalid source line: {target}")
            elif parsed.scheme in {"https", "http"}:
                continue
            elif parsed.scheme:
                raise SystemExit(f"Unsupported link scheme: {target}")
            else:
                path = (
                    (document.parent / unquote(parsed.path)).resolve()
                    if parsed.path
                    else document
                )
                if not path.is_relative_to(ROOT) or not path.exists():
                    raise SystemExit(
                        f"Missing local target: {document.relative_to(ROOT)} -> {target}"
                    )
                if (
                    parsed.fragment
                    and path.suffix == ".md"
                    and unquote(parsed.fragment) not in anchors(path.read_text())
                ):
                    raise SystemExit(f"Missing local anchor: {target}")
            checked.add((str(document.relative_to(ROOT)), target))
    print(
        f"Verified {len(DOCUMENTS)} Markdown documents and {len(checked)} local/source links; external HTTP and semantic claims require separate review."
    )


if __name__ == "__main__":
    main()
