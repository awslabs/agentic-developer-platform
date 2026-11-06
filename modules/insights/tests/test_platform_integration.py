"""Keep the optional platform entrypoint connected to Insights ownership."""

import pathlib
import runpy
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]


class PlatformIntegrationTest(unittest.TestCase):
    def test_deploy_only_reaches_insights_script(self):
        source = (ROOT / "platform/scripts/deploy-all.sh").read_text()
        self.assertIn('--insights-only) INSIGHTS_ONLY=true', source)
        self.assertIn('exec bash "$ROOT_DIR/modules/insights/deploy.sh"', source)
        self.assertIn('if [ "$CI_MODE" = true ]; then args+=(--plan); fi', source)

    def test_teardown_orders_insights_before_platform_and_uses_isolated_state(self):
        engine = runpy.run_path(str(ROOT / "platform/scripts/teardown.py"))
        phases = engine["ORDER"]
        self.assertEqual(phases[0], "superplane")
        self.assertLess(phases.index("insights"), phases.index("platform"))
        self.assertEqual(engine["MODULES"]["insights"], ("modules/insights/platform", "modules/insights"))
        self.assertIn("insights", engine["DEPENDENTS"]["platform"])


if __name__ == "__main__":
    unittest.main()
