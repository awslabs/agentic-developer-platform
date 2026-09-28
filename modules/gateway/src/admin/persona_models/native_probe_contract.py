"""SDK-generated native persona probe hashes; never register these as Task personas."""

import json
from pathlib import Path

_MANIFEST = json.loads(Path(__file__).with_name("native-probe-manifest.json").read_text())
if _MANIFEST.get("schema_version") != 1 or _MANIFEST.get("sdk") != "0.155.1" or _MANIFEST.get("normalization") != "codex-native-probe-v1":
    raise RuntimeError("Unsupported native Codex probe manifest")
_REPORT_MANIFEST = json.loads(Path(__file__).with_name("report-probe-manifest.json").read_text())
if (
    _REPORT_MANIFEST.get("schema_version") != 1
    or _REPORT_MANIFEST.get("sdk") != "0.155.1"
    or _REPORT_MANIFEST.get("normalization") != "codex-report-probe-v1"
):
    raise RuntimeError("Unsupported report Codex probe manifest")
# Both use actual SDK capture; Task profiles remain a separate registry.
_MANIFEST["personas"].update(_REPORT_MANIFEST["personas"])
NATIVE_PROBE_PERSONAS = frozenset(_MANIFEST["personas"])
NATIVE_PROBE_REVISION = _MANIFEST["sdk"]


def native_request_shape(persona: str, model: str) -> str | None:
    return _MANIFEST["personas"].get(persona, {}).get(model)
