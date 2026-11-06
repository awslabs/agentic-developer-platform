"""Offline design link, fixture and story consistency checks; no provider calls."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[2]


def anchors(text):
    result = set()
    counts = {}
    for title in re.findall(r"^#{1,6} (.+)$", text, re.M):
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        result.add(slug + (f"-{count}" if count else ""))
        counts[slug] = count + 1
    return result


def check_links(path):
    failures = []
    text = path.read_text()
    for target in re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", text):
        parsed = urlsplit(target)
        content = None
        if parsed.scheme:
            match = re.fullmatch(r"/aws-e/adp/blob/([0-9a-f]{40})/(.+)", parsed.path)
            if parsed.netloc == "github.com" and match:
                source = subprocess.run(["git", "show", f"{match[1]}:{unquote(match[2])}"], cwd=ROOT, capture_output=True)
                if source.returncode:
                    failures.append(f"{path.name}: missing pinned Git source {target}")
                    continue
                content = source.stdout.decode()
            else:
                continue
        else:
            local = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            if not local.exists():
                failures.append(f"{path.name}: missing local target {target}")
                continue
            if local.is_file():
                content = local.read_text()
        if content is not None and parsed.fragment:
            fragment = unquote(parsed.fragment)
            line = re.fullmatch(r"L([0-9]+)(?:-L([0-9]+))?", fragment)
            if line:
                if int(line[1]) > len(content.splitlines()):
                    failures.append(f"{path.name}: line outside source {target}")
            elif fragment not in anchors(content):
                failures.append(f"{path.name}: missing anchor {target}")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stories", action="store_true")
    arguments = parser.parse_args()
    failures = []
    documents = sorted(HOME.rglob("*.md"))
    for path in documents:
        failures.extend(check_links(path))
    architecture = (HOME / "architecture.md").read_text()
    examples = re.findall(r"```json\n(.*?)\n```", architecture, re.S)
    for raw in examples:
        example = json.loads(raw)
        assert example["service_id"] == "transform"
        assert len(example["sources"]) == 1
        reference = example["sources"][0]
        assert set(reference) == {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
        assert re.fullmatch(r"[0-9a-f]{40}", reference["record_revision"])
        assert example["capability"] == example["transform_input"]["goal"]["kind"]
    if arguments.stories:
        backlog = (HOME / "backlog.md").read_text()
        counts = {"TF01": 3, "TF02": 4, "TF03": 4, "TF04": 4, "TF05": 3, "TF06": 3, "TF07": 8}
        for criterion in range(1, 9):
            if len(re.findall(rf"^\| EC-{criterion:02} \|", backlog, re.M)) != 1:
                failures.append(f"EC-{criterion:02}: missing/duplicate backlog mapping")
        for alias, first, rest in re.findall(r"(TF[0-9]{2}) AC-([0-9]{2})((?:/[0-9]{2})*)", backlog):
            if any(int(value) > counts[alias] for value in [first, *rest.strip("/").split("/")] if value):
                failures.append(f"{alias}: backlog refers to an unknown AC")
        for number in range(1, 8):
            story = HOME / "stories" / f"TF{number:02}.md"
            if not story.exists():
                failures.append(f"Missing {story.name}")
                continue
            if len(story.read_bytes()) > 8192:
                failures.append(f"{story.name} exceeds 8 KiB")
            acceptance = re.findall(r"^\| (AC-[0-9]{2}) \|", story.read_text(), re.M)
            expected = [f"AC-{index:02}" for index in range(1, counts[story.stem] + 1)]
            if acceptance != expected:
                failures.append(f"{story.name}: acceptance IDs changed")
            result = subprocess.run([sys.executable, str(ROOT / "modules/insights/authoring/validate_issue.py"), str(story), "--stage", "draft"], cwd=ROOT, capture_output=True, text=True)
            if result.returncode:
                failures.append(result.stdout + result.stderr)
            if "**Approved design:** Accepted" not in story.read_text() or "#pullrequestreview-" not in story.read_text():
                failures.append(f"{story.name}: approval state is inconsistent")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Validated {len(documents)} Markdown documents and {len(examples)} proposed JSON examples; story checks={arguments.stories}")
    print("External non-source URLs, live routes, full proposed schemas and approval are not validated by this check.")


if __name__ == "__main__":
    main()
