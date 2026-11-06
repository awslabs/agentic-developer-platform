"""Do not publish a gateway/engine release with contradictory story readiness."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("gateway_engine_sync", ROOT / "modules/gateway/scripts/sync-gateway-engine.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class GatewayDispatchParityTests(unittest.TestCase):
    def test_upgrade_quiesce_checks_desired_input_before_pausing_and_allows_missing_old_gateway_flag(self):
        for desired, live, accepted in ((True, 'true', True), (False, 'true', False),
                                        ('true', 'true', False), (True, 'malformed', False)):
            with self.subTest(desired=desired, live=live), tempfile.TemporaryDirectory() as tmp:
                inputs = Path(tmp) / 'gateway.tfvars.json'
                inputs.write_text(json.dumps({'orchestration_product_story_dispatch_enabled': desired}))
                config = json.dumps({'Timeout': 1, 'Environment': {'Variables': {'ADP_PRODUCT_STORY_DISPATCH_ENABLED': live}}})
                with patch.object(sync, 'current_image_digest', return_value='sha256:' + 'a' * 64), \
                        patch.object(sync, 'command', side_effect=[config, '']) as command, \
                        patch.object(sync.time, 'sleep'):
                    if accepted:
                        sync.quiesce(account='123456789012', region='us-east-1', environment='dev', product_dispatch_inputs=inputs)
                    else:
                        with self.assertRaises(ValueError):
                            sync.quiesce(account='123456789012', region='us-east-1', environment='dev', product_dispatch_inputs=inputs)
                mutations = [call for call in command.call_args_list if 'disable-rule' in call.args[0]]
                self.assertEqual(len(mutations), int(accepted))

    def test_product_rollout_is_independent_and_defaults_off(self):
        self.assertFalse(sync.admission_settings({})["ADP_PRODUCT_STORY_DISPATCH_ENABLED"])
        observed = sync.admission_settings({
            "AGENT_AUTHORITY_ENABLED": "true", "ADP_WORK_CLAIMS_ENABLED": "true",
            "ADP_PRODUCT_STORY_DISPATCH_ENABLED": "false",
        })
        self.assertTrue(observed["AGENT_AUTHORITY_ENABLED"])
        self.assertFalse(observed["ADP_PRODUCT_STORY_DISPATCH_ENABLED"])

    def test_gateway_default_off_tick_on_refuses_before_any_tick_mutation(self):
        image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if args[:3] == ["aws", "sts", "get-caller-identity"]:
                return json.dumps({"Account": "123456789012"})
            if args[:3] == ["kubectl", "rollout", "status"]:
                return "success"
            if args[:3] == ["kubectl", "get", "deployment/bedrockgateway"]:
                return json.dumps({"spec": {
                    "template": {"spec": {"containers": [{"name": "bedrockgateway", "image": image}]}},
                    "selector": {"matchLabels": {"app": "gateway"}},
                }})
            if args[:3] == ["kubectl", "get", "pods"]:
                return json.dumps({"items": [{"metadata": {"name": "gateway-current"}, "status": {
                    "phase": "Running", "containerStatuses": [{
                        "name": "bedrockgateway", "ready": True, "imageID": image,
                    }],
                }}]})
            if args[:2] == ["kubectl", "exec"]:
                # This was the production regression: the gateway omitted the
                # switch and presented a false hold while the tick admitted work.
                self.assertIn("ADP_PRODUCT_STORY_DISPATCH_ENABLED", args[-1])
                return "{}"
            if args[:3] == ["aws", "lambda", "get-function"]:
                return json.dumps({"Configuration": {"Environment": {"Variables": {
                    "ADP_PRODUCT_STORY_DISPATCH_ENABLED": "true",
                }}}, "Code": {"ResolvedImageUri": image}})
            self.fail(f"unexpected command: {args}")

        with patch.object(sync, "command", command):
            with self.assertRaisesRegex(ValueError, "admission settings differ: ADP_PRODUCT_STORY_DISPATCH_ENABLED"):
                sync.synchronize(image=image, account="123456789012", region="us-east-1",
                                 environment="dev", namespace="adp-gateway")
        self.assertFalse(any("update-function-code" in call or "update-function-configuration" in call
                             or "disable-rule" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
