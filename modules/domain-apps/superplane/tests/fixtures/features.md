# U1 feature-response fixture

`features.json` supplies the backend-derived fixture named by Wave 1 evaluation
[#5067](https://github.com/aws-e/adp/issues/5067), tracked in
[#5288](https://github.com/aws-e/adp/issues/5288). It represents the response with
feature configuration absent. Its values are defaults, not a captured deployment
or an instruction to enable features.

Provenance at ADP commit `b5b0092e53f6c1ab2416845b322583f04c63471a`:

- Producer: [`src/features/routes.py`](https://github.com/aws-e/adp/blob/b5b0092e53f6c1ab2416845b322583f04c63471a/modules/gateway/src/features/routes.py),
  `get_features`, `_is_enabled`, and `_is_enabled_strict`.
- Producer file SHA-256:
  `f131255e1dc73b862fa183f351815506167e780dcfa6ce5222cc8d16b6e44679`.
- Consumer: [`FeatureFlags`](https://github.com/aws-e/adp/blob/b5b0092e53f6c1ab2416845b322583f04c63471a/modules/gateway/frontend/src/services/features.ts).
  All 13 declared boolean fields, including `new_ui` and `superplane`, are present.

The fixture was obtained by executing the producer with an empty environment.
Only its unused authentication dependency was replaced for offline import; the
response function and both flag readers were executed unchanged. This does not
exercise authentication. With the gateway's Python dependencies installed, the
following command reproduces the fixture from the checked-out source. Run it
from the repository root and review any difference before updating the fixture:

```bash
python - <<'PY'
import asyncio
import importlib.util
import json
import os
import sys
import types
from unittest.mock import patch

dependency = types.ModuleType("src.auth.dependencies")
dependency.get_current_user = lambda: None
spec = importlib.util.spec_from_file_location(
    "fixture_features", "modules/gateway/src/features/routes.py"
)
producer = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"src.auth.dependencies": dependency}):
    spec.loader.exec_module(producer)
with patch.dict(os.environ, {}, clear=True):
    response = asyncio.run(producer.get_features(None))
print(json.dumps(response, indent=2))
PY
```

For an authorized live response already captured as `/tmp/sp-features.json`, the
evaluation's fixture-subset check is now runnable:

```bash
jq -e --slurpfile live /tmp/sp-features.json \
  '(.features | keys) - ($live[0].features | keys) | length == 0' \
  modules/domain-apps/superplane/tests/fixtures/features.json
```

That command checks field presence only. Run #5067's boolean-type and
`superplane == false` checks separately. Other live flag values may legitimately
differ from these defaults; do not compare the entire live response to this
fixture. Adding this file supplies no live observation, browser-gating proof,
deployment/undeploy evidence, or resource-cleanup acceptance. The U1 live checker
and its teardown criterion remain outstanding under #5288.
