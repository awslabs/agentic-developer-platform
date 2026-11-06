"""Validate this design's local Markdown links without network or credentials."""

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


HERE = Path(__file__).resolve().parent
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def anchors(text):
    found = set()
    counts = {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if fenced or not re.match(r"^#{1,6} ", line):
            continue
        heading = re.sub(r"^#+ ", "", line).lower()
        slug = re.sub(r"[^\w\- ]", "", heading).replace(" ", "-")
        occurrence = counts.get(slug, 0)
        counts[slug] = occurrence + 1
        found.add(f"{slug}-{occurrence}" if occurrence else slug)
    return found


def main():
    checked = 0
    errors = []
    for document in sorted(HERE.glob("*.md")):
        for link in LINK.findall(document.read_text()):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            target = (document.parent / unquote(parsed.path)).resolve() if parsed.path else document
            checked += 1
            if not target.is_file():
                errors.append(f"{document.name}: missing target {link}")
                continue
            if not parsed.fragment:
                continue
            fragment = unquote(parsed.fragment)
            lines = re.fullmatch(r"L([0-9]+)(?:-L([0-9]+))?", fragment)
            text = target.read_text()
            if lines:
                end = int(lines.group(2) or lines.group(1))
                if not 1 <= int(lines.group(1)) <= end <= len(text.splitlines()):
                    errors.append(f"{document.name}: invalid source line {link}")
            elif fragment not in anchors(text):
                errors.append(f"{document.name}: missing heading {link}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Validated {checked} local Markdown links and source/heading anchors.")


if __name__ == "__main__":
    main()
