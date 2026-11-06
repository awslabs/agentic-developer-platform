import shutil
import subprocess

import pytest

from l5_services.adapters.transform.test_output import completed_test_count


@pytest.mark.parametrize("failure", [False, True])
def test_actual_node_runner_counts_and_exit_status(tmp_path, failure):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the JavaScript portability fixture")
    source = tmp_path / "example.test.cjs"
    source.write_text("const test = require('node:test'); const assert = require('node:assert/strict');\n"
                      "test('first', () => assert.equal(1, 1));\n"
                      "test('second', () => { console.log('999 passed'); assert.equal(1, "
                      + ("2" if failure else "1") + "); });\n")
    argv = [node, "--test", "--test-reporter=tap", str(source)]
    result = subprocess.run(argv, capture_output=True, timeout=15, check=False)
    output = result.stdout + result.stderr
    assert result.returncode == int(failure)
    assert completed_test_count(argv, output) == (1 if failure else 2)
    assert completed_test_count(argv, output + b"# pass 999\n") is None
    assert completed_test_count(argv, output.replace(b"# tests 2", b"# tests 999")) is None
    assert completed_test_count(argv, output.split(b"# duration_ms")[0]) is None
    assert completed_test_count([node, "--test"], output) is None
    assert completed_test_count([node, "script.js"], output) is None


@pytest.mark.parametrize("argv", [["pytest", "-q"], ["python", "-m", "pytest"], ["python3.12", "-m", "pytest"]])
def test_existing_pytest_runner(argv):
    assert completed_test_count(argv, b"2 passed in 0.1s\n") == 2
    assert completed_test_count(argv, b"no tests ran in 0.1s\n") is None


def test_unrecognized_runner_cannot_supply_a_pytest_count():
    assert completed_test_count(["sh", "-c", "echo '2 passed'"], b"2 passed\n") is None
