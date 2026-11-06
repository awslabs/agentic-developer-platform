"""Offline checks for the independent Insights CI routing contract."""

import fnmatch
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


def pull_request_paths(workflow):
    source = (ROOT / ".github" / "workflows" / workflow).read_text()
    match = re.search(r"(?m)^  pull_request:\s*\n(?P<body>(?:    .*\n|\n)*)", source)
    if not match:
        raise AssertionError(f"{workflow} has no pull_request filter")
    return re.findall(r"(?m)^      - ['\"]([^'\"]+)['\"]", match.group("body"))


class InsightsCiContractTest(unittest.TestCase):
    def test_insights_only_change_triggers_insights_ci(self):
        paths = pull_request_paths("insights-ci.yml")
        self.assertIn("modules/insights/**", paths)
        self.assertTrue(any(fnmatch.fnmatch("modules/insights/emitter/service.py", path) for path in paths))
        self.assertFalse(any(fnmatch.fnmatch("modules/gateway/src/app.py", path) for path in paths))

    def test_insights_only_change_does_not_trigger_gateway_ci(self):
        paths = pull_request_paths("gateway-ci.yml")
        self.assertFalse(any(fnmatch.fnmatch("modules/insights/emitter/service.py", path) for path in paths))

    def test_optional_only_changes_do_not_deploy_ordinary_insights(self):
        source = (ROOT / ".github/workflows/insights-deploy.yml").read_text()
        match = re.search(r"(?m)^  push:\s*\n(?P<body>(?:    .*\n|\n)*)", source)
        self.assertIsNotNone(match)
        paths = re.findall(r"(?m)^      - ['\"]([^'\"]+)['\"]", match.group("body"))

        def triggers(path):
            selected = False
            for pattern in paths:
                if fnmatch.fnmatchcase(path, pattern.lstrip("!")):
                    selected = not pattern.startswith("!")
            return selected

        self.assertTrue(triggers("modules/insights/platform/main.tf"))
        self.assertTrue(triggers("modules/insights/emitter/service.py"))
        self.assertTrue(triggers(".github/workflows/insights-deploy.yml"))
        self.assertFalse(triggers("modules/insights/poc/devlake/mode.py"))
        self.assertFalse(triggers("modules/insights/tests/devlake_poc/test_s02_intent.py"))

    def test_schema_design_changes_trigger_insights_ci(self):
        paths = pull_request_paths("insights-ci.yml")
        self.assertIn("docs/engineering-insights/architecture.md", paths)
        self.assertIn("docs/engineering-insights/lineage-schema.md", paths)

    def test_ci_keeps_module_and_schema_validation_jobs(self):
        source = (ROOT / ".github/workflows/insights-ci.yml").read_text()
        self.assertNotIn("<<<<<<<", source)
        for job, commands in {
            "verify": ("unittest discover -s modules/insights/tests", "bash -n modules/insights/deploy.sh",
                       "terraform validate", "terraform test"),
            "validate-lineage": ("pip install -r modules/insights/schemas/requirements.txt",
                                 "unittest discover -s modules/insights/schemas",
                                 "python modules/insights/schemas/validate.py",
                                 "python modules/insights/schemas/check_traceability.py"),
        }.items():
            with self.subTest(job=job):
                match = re.search(rf"(?ms)^  {job}:\n(?P<body>.*?)(?=^  [a-z][a-z0-9-]*:|\Z)", source)
                self.assertIsNotNone(match)
                for command in commands:
                    self.assertIn(command, match.group("body"))


if __name__ == "__main__":
    unittest.main()
