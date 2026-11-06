import ast
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path


GITLAB = Path(__file__).resolve().parents[3]
ACCEPTANCE = GITLAB / "tests/acceptance"
RUNBOOK = GITLAB / "docs/runners.md"


def command_blocks():
    return re.findall(r"```bash\n(.*?)\n\s*```", RUNBOOK.read_text(), re.DOTALL)


def required_arguments(script):
    required = set()
    tree = ast.parse((ACCEPTANCE / script).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "add_argument":
            continue
        if any(keyword.arg == "required" and isinstance(keyword.value, ast.Constant)
               and keyword.value.value is True for keyword in node.keywords):
            required.add(node.args[0].value)
    return required


def test_operator_examples_supply_every_required_private_collection_argument():
    blocks = command_blocks()
    for script, count in (("dispatch.py", 2), ("poll_pending.py", 1),
                          ("poll_cleanup.py", 1), ("collect_host.py", 1), ("verify.py", 1)):
        examples = [block for block in blocks if f"tests/acceptance/{script}" in block]
        assert len(examples) == count
        for example in examples:
            flags = set(re.findall(r"--[a-z][a-z0-9-]+", example))
            assert required_arguments(script) <= flags
    dispatch = [block for block in blocks if "tests/acceptance/dispatch.py" in block]
    assert "--prepare" in dispatch[0] and "--dispatch" in dispatch[1]
    assert "--job-id" in next(block for block in blocks if "tests/acceptance/poll_pending.py" in block)
    assert "--stage pre-teardown" in next(block for block in blocks if "tests/acceptance/verify.py" in block)
    example = next(block for block in blocks if "tests/acceptance/verify.py" in block)
    assert set(re.findall(r'--host-history "\$(HOST_[A-Z_0-9]+)"', example)) == {
        "HOST_COLD_1", "HOST_COLD_2", "HOST_COLD_3", "HOST_FAILED", "HOST_RESTART", "HOST_NETWORK"}
    assert "`targets_file`" in RUNBOOK.read_text()
    assert "`run_id`/`targets` JSON" in RUNBOOK.read_text()


def test_documented_private_fixture_check_accepts_valid_scope_and_hides_invalid_values(tmp_path):
    snippet = next(block for block in command_blocks() if "Draft202012Validator" in block)
    spec = importlib.util.spec_from_file_location("synthetic_qualification", ACCEPTANCE / "tests/test_verify.py")
    synthetic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synthetic)
    fixtures, _ = synthetic.sample.__wrapped__()
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    environment = {**os.environ, "FIXTURES": str(path)}
    valid = subprocess.run(["bash", "-c", snippet], cwd=GITLAB.parents[2], env=environment,
                           text=True, capture_output=True, timeout=5)
    assert valid.returncode == 0 and valid.stdout == valid.stderr == ""
    canary = "glrt-private-schema-canary-for-runbook"
    fixtures["account_id"] = canary
    path.write_text(json.dumps(fixtures))
    invalid = subprocess.run(["bash", "-c", snippet], cwd=GITLAB.parents[2], env=environment,
                             text=True, capture_output=True, timeout=5)
    assert invalid.returncode != 0 and "Private runner fixture schema invalid" in invalid.stderr
    assert canary not in invalid.stdout + invalid.stderr


def test_network_scope_and_refusal_instructions_match_checked_source():
    spec = importlib.util.spec_from_file_location("acceptance_verify", ACCEPTANCE / "verify.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    contents = RUNBOOK.read_text()
    for address, port, kind in verifier.METADATA_TARGETS.values():
        assert f"`{address}:{port}`" in contents and f"both `{kind}`" in contents
    assert "another address in the same IP family is not" in contents
    assert "only 0 allowed" in contents and "services_limit=0" not in contents
    assert "runner_system_failure" in contents and "no build container" in contents
    assert "disabled_by_provider" in contents and "exact launch-template-version" in contents
    assert "no IAM profile" in contents and "a job report cannot" in contents.replace("\n", " ")


def test_bounded_private_evidence_and_lifecycle_stay_explicit():
    contents = RUNBOOK.read_text()
    assert contents.index("## Registration and private CA") < contents.index("## Apply and activate")
    assert contents.index("## Apply and activate") < contents.index("## Acceptance and evidence")
    assert contents.index("## Acceptance and evidence") < contents.index("## Rotation and recovery")
    assert contents.index("## Rotation and recovery") < contents.index("## Reconciliation, revocation and teardown")
    for expected in ("mode-0600 JSONL", "330-second", "10 minutes", "five minutes",
                     "three seconds", "three fresh EC2", "15 minutes", "--stage final",
                     "including when the poll fails", "separate run-owned project in the verified B group", "full logs", "mode-0700 audit directory",
                     "do not set `PrivateNetwork=yes`", "AF_UNIX and AF_NETLINK", "AF_INET/AF_INET6",
                     "shared CA", "existing GitLab instance", "pre-teardown verdict is **not** final acceptance"):
        assert expected in contents
