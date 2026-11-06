"""Validate this design bundle's local/source links and complete source inventory."""

import csv
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[3]
BUNDLE = Path(__file__).resolve().parent
SUMMARY = json.loads((BUNDLE / "inventory/summary.json").read_text())
REVISION = SUMMARY["source_revision"]


def rows(name):
    with (BUNDLE / "inventory" / name).open() as source:
        return list(csv.DictReader(source, delimiter="\t"))


def slug(text):
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def check_links():
    count = 0
    source_cache = {}
    for document in sorted(BUNDLE.glob("*.md")):
        text = document.read_text()
        references = dict(re.findall(r"^\[([^\]]+)\]:\s+(\S+)", text, re.M))
        targets = re.findall(r"\[[^\]]*\]\(([^)]+)\)", text) + list(references.values())
        for label in re.findall(r"\[[^\]]+\]\[([^\]]+)\]", text):
            assert label in references, (document.name, "undefined reference", label)
        for target in targets:
            if target.startswith("https://github.com/aws-e/adp/blob/"):
                commit, source_path = target.split("/blob/", 1)[1].split("/", 1)
                source_path, _, anchor = source_path.partition("#")
                key = (commit, source_path)
                if key not in source_cache:
                    source_cache[key] = subprocess.check_output(["git", "show", f"{commit}:{source_path}"], cwd=ROOT).decode()
                if anchor:
                    assert anchor.startswith("L") and int(anchor[1:]) <= len(source_cache[key].splitlines()), target
                count += 1
            elif "://" not in target:
                name, _, anchor = unquote(target).partition("#")
                path = (document.parent / name).resolve() if name else document
                assert path.exists(), (document.name, target, "missing file")
                if anchor:
                    content = path.read_text()
                    if re.fullmatch(r"L\d+", anchor):
                        assert int(anchor[1:]) <= len(content.splitlines()), target
                    else:
                        anchors = {slug(heading) for heading in re.findall(r"^#+ (.+)$", content, re.M)}
                        anchors.update(re.findall(r'id="([^"]+)"', content))
                        assert anchor in anchors, (document.name, target, "missing anchor")
                count += 1
    return count


def check_inventory():
    tree = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", REVISION], cwd=ROOT)
    expected = {}
    for item in tree.split(b"\0"):
        if item:
            metadata, name = item.split(b"\t", 1)
            expected[name.decode()] = metadata.decode().split()[2]
    artifacts = rows("artifacts.tsv")
    assert len(artifacts) == len(expected) == SUMMARY["artifacts"]
    assert {row["path"]: row["git_blob"] for row in artifacts} == expected
    assert all(row["component"] and row["disposition"] and row["scope"] in {"review", "support", "reference", "excluded"} for row in artifacts)
    entries = rows("entry-points.tsv")
    assert len(entries) == SUMMARY["entry_points"]
    assert {row["path"] for row in entries} == {row["path"] for row in artifacts if row["entry_kind"]}
    hits = rows("global-readers.tsv")
    assert len(hits) == SUMMARY["global_references"]
    assert len({row["path"] for row in hits}) == SUMMARY["global_files"]
    assert len(rows("migrations.tsv")) == SUMMARY["migration_files"]
    assert not SUMMARY["missing_migration_parents"]
    return len(artifacts)


if __name__ == "__main__":
    print(f"Validated {check_inventory()} tracked artifacts and {check_links()} local/pinned-source links.")
