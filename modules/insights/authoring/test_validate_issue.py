"""Behavior and repository contracts for opt-in, offline issue format lint."""

import unittest
from pathlib import Path

from validate_issue import lint, tables, visible_lines

ROOT = Path(__file__).resolve().parents[3]
RULES = ROOT / "modules/agent-factory/rules"
EXAMPLES = ROOT / "docs/engineering-insights/examples"


class IssueFormatTests(unittest.TestCase):
    def setUp(self):
        self.story = (EXAMPLES / "story-invitations.md").read_text()
        self.epic = (EXAMPLES / "epic-organizations.md").read_text()

    def assert_invalid(self, body, fragment):
        errors, _ = lint(body, stage="ready")
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_filled_examples_have_ready_format(self):
        for body in [self.story, self.epic]:
            with self.subTest(body=body[:80]):
                self.assertEqual(lint(body, stage="ready")[0], [])

    def test_templates_are_drafts_and_not_ready(self):
        for name in ["developer-issue.md", "epic-issue.md"]:
            body = (RULES / "templates" / name).read_text()
            with self.subTest(template=name):
                self.assertEqual(lint(body)[0], [])
                self.assert_invalid(body, "unresolved")

    def test_planned_tests_are_not_execution_claims(self):
        errors, notes = lint(self.story, stage="ready")
        self.assertEqual(errors, [])
        self.assertEqual(sum("planned test" in note for note in notes), 3)
        self.assertTrue(any("Offline format only" in note for note in notes))

    def test_remote_identity_and_nonexistent_selectors_are_not_resolved(self):
        body = self.story.replace("project-example", "unregistered-example")
        body = body.replace("tests/organizations/", "nonexistent/path/")
        self.assertEqual(lint(body, stage="ready")[0], [])

    def test_duplicate_criterion_ids(self):
        self.assert_invalid(
            self.story.replace("| AC-03 | Given:", "| AC-01 | Given:"),
            "duplicate acceptance",
        )

    def test_unknown_acceptance_reference(self):
        self.assert_invalid(
            self.story.replace("| SCN-01 | AC-01 |", "| SCN-01 | AC-99 |"),
            "unknown/duplicate acceptance",
        )

    def test_unmapped_criterion(self):
        body = "\n".join(
            line
            for line in self.story.splitlines()
            if not line.startswith("| SCN-03 |")
        )
        self.assert_invalid(body, "AC-03 has no scenario")

    def test_conflicting_scenario_contract(self):
        self.assert_invalid(
            self.story.replace("| SCN-02 |", "| SCN-01 |"), "conflicting case"
        )

    def test_multiple_tests_can_share_one_scenario(self):
        row = next(
            line for line in self.story.splitlines() if line.startswith("| SCN-01 |")
        )
        extra = row.replace("::test_admin_invite", "::test_admin_invite_persistence")
        self.assertEqual(
            lint(self.story.replace(row, row + "\n" + extra), stage="ready")[0], []
        )
        self.assert_invalid(
            self.story.replace(row, row + "\n" + row), "repeats a test mapping"
        )

    def test_scenario_requires_controls(self):
        self.assert_invalid(
            self.story.replace("Controls:", "Notes:", 1), "Given/When/Then/Controls"
        )

    def test_unknown_verification_layer(self):
        self.assert_invalid(
            self.story.replace("automatic-integration", "automatic-magic", 1),
            "unknown verification/state",
        )

    def test_test_reference_needs_path_and_selector(self):
        reference = "tests/organizations/test_invites.py::test_admin_invite"
        self.assertEqual(
            lint(
                self.story.replace(
                    reference, "tests/invites.spec.ts::admin can invite a member"
                ),
                stage="ready",
            )[0],
            [],
        )
        for invalid in [
            "tests/organizations/test_invites.py",
            "::test_admin_invite",
            "tests/test_invites.py::",
        ]:
            with self.subTest(reference=invalid):
                self.assert_invalid(
                    self.story.replace(reference, invalid), "path::selector"
                )

    def test_manual_mapping_requires_versioned_procedure(self):
        body = self.story.replace("automatic-integration", "manual").replace(
            "| planned |", "| manual |"
        )
        self.assert_invalid(body, "path@commit#anchor")
        for selector in [
            "test_admin_invite",
            "test_denied_actor",
            "test_identical_retry",
        ]:
            body = body.replace(
                "tests/organizations/test_invites.py::" + selector,
                "docs/procedure.md@" + "a" * 40 + "#" + selector,
            )
        self.assertEqual(lint(body, stage="ready")[0], [])
        self.assert_invalid(
            body.replace("| manual |", "| planned |", 1),
            "manual verification and state must agree",
        )

    def test_case_can_link_immutable_procedure(self):
        row = next(
            line for line in self.story.splitlines() if line.startswith("| SCN-01 |")
        )
        cells = row.split("|")
        cells[3] = " docs/cases.md@" + "b" * 40 + "#admin "
        self.assertEqual(
            lint(self.story.replace(row, "|".join(cells)), stage="ready")[0], []
        )

    def test_ready_lineage_requires_resolved_fields(self):
        self.assert_invalid(
            self.story.replace("project-example", "unresolved"),
            "Project reference remains unresolved",
        )
        self.assert_invalid(
            self.story.replace(
                "| Parent epic | https://github.example.com/team/product/issues/40 |",
                "| Parent epic | #40 |",
            ),
            "full issue URL",
        )
        self.assert_invalid(
            self.story.replace(
                "| Parent epic | https://github.example.com/team/product/issues/40 |",
                "| Parent epic | https://[broken |",
            ),
            "full issue URL",
        )

    def test_epic_child_references_require_issue_and_criterion(self):
        self.assert_invalid(
            self.epic.replace("issues/42#AC-01", "issues/42"),
            "full child issue URL#AC-ID",
        )

    def test_existing_acceptance_padding_is_preserved(self):
        body = self.story.replace("AC-01", "AC-001")
        self.assertEqual(lint(body, stage="ready")[0], [])

    def test_code_and_comments_cannot_supply_required_structure(self):
        visible_story = "\n".join(visible_lines(self.story))
        for hidden in [
            "<!--\n" + visible_story + "\n-->",
            "```markdown\n" + visible_story + "\n```",
        ]:
            self.assert_invalid(hidden, "six canonical sections")
        self.assertEqual(
            lint(self.story + "\n<!--\n## Validation\n-->\n```\n## Description\n```")[
                0
            ],
            [],
        )

    def test_table_before_adjacent_heading_retains_section(self):
        body = "## Description\n| Field | Value |\n|---|---|\n| Kind | story |\n## Validation\n"
        self.assertEqual(
            tables(list(visible_lines(body))),
            [("Description", ["Field", "Value"], [["Kind", "story"]])],
        )

    def test_escaped_pipe_does_not_add_column(self):
        body = self.story.replace(
            "HTTP 201, pending status", r"HTTP 201, pending status (pending\|queued)"
        )
        self.assertEqual(lint(body, stage="ready")[0], [])
        self.assert_invalid(
            body.replace(r"pending\|queued", "pending|queued"), "malformed table row"
        )

    def test_unique_lineage_fields_and_required_owner(self):
        self.assert_invalid(
            self.story.replace(
                "| Kind | story |", "| Kind | story |\n| Kind | story |"
            ),
            "duplicate lineage",
        )
        self.assert_invalid(
            self.story.replace("**Delivery owner:**", "**Contact:**"),
            "Delivery owner must be explicit",
        )

    def test_byte_limit_includes_multibyte_text(self):
        self.assert_invalid(self.story + "\n" + "é" * 4096, "exceeds 8 KiB")


class TemplateDistributionTests(unittest.TestCase):
    def test_github_and_canonical_bodies_match(self):
        for canonical, github in [
            ("developer-issue.md", "developer-task.md"),
            ("epic-issue.md", "engineering-epic.md"),
        ]:
            with self.subTest(template=canonical):
                expected = (RULES / "templates" / canonical).read_text()
                actual = (
                    (ROOT / ".github/ISSUE_TEMPLATE" / github)
                    .read_text()
                    .split("---", 2)[2]
                    .lstrip("\n")
                )
                actual = actual.replace(
                    "../../modules/agent-factory/rules/agents/issue-authoring.md",
                    "../agents/issue-authoring.md",
                )
                self.assertEqual(actual, expected)
                self.assertLessEqual(len(expected.encode()), 8192)

    def test_delivery_review_reference_is_packaged(self):
        for dockerfile, source in [
            ("agent/Dockerfile", "rules/agents/issue-delivery-review.md"),
            (
                "agent-worker-image/Dockerfile",
                "modules/agent-factory/rules/agents/issue-delivery-review.md",
            ),
        ]:
            body = (ROOT / "modules/agent-factory" / dockerfile).read_text()
            self.assertIn("COPY --chown=agent:agent " + source, body)
        allowed = (
            (ROOT / "modules/agent-factory/agent/Dockerfile.dockerignore")
            .read_text()
            .splitlines()
        )
        self.assertIn("!rules/agents/issue-delivery-review.md", allowed)
        self.assertTrue((RULES / "agents/issue-delivery-review.md").is_file())

    def test_both_agent_images_include_templates(self):
        runtime = (ROOT / "modules/agent-factory/agent/Dockerfile").read_text()
        worker = (
            ROOT / "modules/agent-factory/agent-worker-image/Dockerfile"
        ).read_text()
        allowed = (
            (ROOT / "modules/agent-factory/agent/Dockerfile.dockerignore")
            .read_text()
            .splitlines()
        )
        for name in ["developer-issue.md", "epic-issue.md"]:
            with self.subTest(template=name):
                self.assertIn(
                    f"COPY --chown=agent:agent rules/templates/{name} ./rules/templates/{name}",
                    runtime,
                )
                self.assertIn(
                    f"COPY --chown=agent:agent modules/agent-factory/rules/templates/{name} /app/rules/templates/{name}",
                    worker,
                )
                self.assertIn(f"!rules/templates/{name}", allowed)


if __name__ == "__main__":
    unittest.main()
