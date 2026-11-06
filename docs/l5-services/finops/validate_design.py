"""Validate FinOps design fixtures, local links and acceptance coverage offline."""

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker

HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[2]


def slug(value):
    return re.sub(r"[^\w\- ]", "", value.lower()).replace(" ", "-")


def main():
    schema = json.loads((HOME / "contract.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    examples = json.loads((HOME / "examples.json").read_text())
    for fixture in examples["valid"]:
        validator.validate(fixture["value"])
        query = fixture["value"]["query"]
        assert date.fromisoformat(query["start"]) < date.fromisoformat(query["end"])
        assert date.fromisoformat(query["end"]) <= date.fromisoformat(examples["clock"][:10])
    for fixture in examples["invalid"]:
        assert list(validator.iter_errors(fixture["value"])), fixture["name"]
    for fixture in examples["reconciliation_cases"]:
        assert sum(map(Decimal, fixture["groups"]), Decimal("0")) == Decimal(fixture["expected_total"])

    stories = json.loads((HOME / "stories.json").read_text())["stories"]
    assert [story["alias"] for story in stories] == [f"FO{index:02}" for index in range(1, 8)]
    covered = set()
    backlog = (HOME / "backlog.md").read_text()
    architecture = (HOME / "architecture.md").read_text()
    architecture_anchors = {slug(heading) for heading in re.findall(r"^#+ (.+)$", architecture, re.M)}
    for story in stories:
        assert story["issue_url"] == f'https://github.com/aws-e/adp/issues/{story["issue_number"]}'
        assert story["issue_url"] in backlog
        assert set(story["sections"]) <= architecture_anchors
        assert story["approval"] == "approved"
        assert re.fullmatch(r"[a-f0-9]{40}", story["accepted_revision"])
        assert "#pullrequestreview-" in story["review_url"]
        assert len({row["id"] for row in story["acceptance"]}) == len(story["acceptance"])
        for row in story["acceptance"]:
            covered.update(row["epic_criteria"])
            assert f'### {story["alias"]} {row["scenario_id"]}' in backlog
    assert covered == {f"EC-{index:02}" for index in range(1, 9)}

    checked = 0
    for source in [*HOME.glob("*.md"), HOME.parent / "README.md"]:
        text = source.read_text()
        targets = re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text)
        targets += re.findall(r"^\[[^\]]+\]:\s*(\S+)", text, re.M)
        for target in targets:
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            destination = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
            assert destination.is_relative_to(ROOT), (source, target)
            assert destination.exists(), (source, target)
            if parsed.fragment and destination.suffix == ".md":
                headings = re.findall(r"^#+ (.+)$", destination.read_text(), re.M)
                assert unquote(parsed.fragment) in {slug(heading) for heading in headings}, (source, target)
            checked += 1
    print(f"FinOps design: {len(examples['valid'])} valid / {len(examples['invalid'])} rejected schema fixtures, {len(examples['reconciliation_cases'])} Decimal fixture oracles, 7 stories / 8 ECs, {checked} local links verified")
    print("No runtime, native-provider, deployment, consumer or approval claim")


if __name__ == "__main__":
    main()
