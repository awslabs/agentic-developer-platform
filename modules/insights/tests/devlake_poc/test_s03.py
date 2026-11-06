import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
BUILD = ROOT / "modules/connectors/devlake/scripts/build.py"
WORKER = ROOT / "modules/insights/poc/devlake/cmd/collector-worker"


def test_normalization():
    backend_path = os.environ.get("DEVLAKE_S03_BACKEND")
    go_binary = os.environ.get("DEVLAKE_S03_GO_BINARY")
    if not backend_path and not go_binary:
        raise unittest.SkipTest("local selector requires DEVLAKE_S03_BACKEND and DEVLAKE_S03_GO_BINARY")
    if not backend_path or not go_binary:
        raise AssertionError("both pinned backend and Go 1.26 binary are required")
    backend = Path(backend_path).resolve()
    go = Path(go_binary).resolve()
    lock = json.loads((BUILD.parent.parent / "source-lock.json").read_text())
    manifest = json.loads((backend / "adp/source-manifest.json").read_text())
    if manifest["upstream"] != lock["upstream"] or manifest["toolchain"] != lock["toolchain"]:
        raise AssertionError("backend does not match the pinned DevLake source lock")
    for name in ("domain_query.go", "domain_query_test.go", "scenario_normalization_test.go"):
        staged = backend / "cmd/adp-collector-worker/internal/collection" / name
        if staged.read_bytes() != (WORKER / "internal/collection" / name).read_bytes():
            raise AssertionError(f"pinned worker has stale {name}")
    version = subprocess.run([str(go), "version"], check=True, capture_output=True, text=True, timeout=15)
    if f"go{lock['toolchain'][2:]} " not in version.stdout:
        raise AssertionError("Go binary differs from the source lock")
    result = subprocess.run(
        [str(go), "test", "-mod=readonly", "./cmd/adp-collector-worker/internal/collection",
         "-run", "^TestSCN02Normalization$", "-count=1"],
        cwd=backend, env={**os.environ, "GOTOOLCHAIN": "local"},
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError((result.stdout + result.stderr)[-2000:])


class WorkerPackagingTest(unittest.TestCase):
    def test_normalization(self):
        test_normalization()

    def test_worker_build_stages_admission_helper(self):
        spec = importlib.util.spec_from_file_location("devlake_build", BUILD)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "backend").mkdir()
            for name in ("LICENSE", "NOTICE"):
                (destination / name).write_text("test-only\n")
            builder.stage(json.loads((BUILD.parent.parent / "source-lock.json").read_text()),
                          destination, "a" * 40, "worker")
            package = destination / "backend/cmd/adp-collector-worker"
            self.assertEqual((package / "internal/worker/runtime.go").read_bytes(),
                             (WORKER / "internal/worker/runtime.go").read_bytes())
            self.assertEqual((package / "internal/worker/gateway.go").read_bytes(),
                             (WORKER / "internal/worker/gateway.go").read_bytes())
            self.assertEqual((package / "internal/sourceapi/records.go").read_bytes(),
                             (BUILD.parent.parent / "sourceapi/records.go").read_bytes())
            self.assertEqual((package / "internal/collection/admission.go").read_bytes(),
                             (WORKER / "internal/collection/admission.go").read_bytes())
            self.assertEqual((package / "internal/collection/generation.go").read_bytes(),
                             (WORKER / "internal/collection/generation.go").read_bytes())
            self.assertEqual((package / "internal/collection/batch.go").read_bytes(),
                             (WORKER / "internal/collection/batch.go").read_bytes())
            self.assertEqual((package / "internal/collection/incremental.go").read_bytes(),
                             (WORKER / "internal/collection/incremental.go").read_bytes())
            self.assertEqual((package / "internal/collection/sealed_readback.go").read_bytes(),
                             (WORKER / "internal/collection/sealed_readback.go").read_bytes())
            self.assertEqual((package / "internal/collection/domain_query.go").read_bytes(),
                             (WORKER / "internal/collection/domain_query.go").read_bytes())
            self.assertEqual((package / "internal/collection/incremental_checkpoint.go").read_bytes(),
                             (WORKER / "internal/collection/incremental_checkpoint.go").read_bytes())
            self.assertEqual((package / "internal/collection/scenario_normalization_test.go").read_bytes(),
                             (WORKER / "internal/collection/scenario_normalization_test.go").read_bytes())
            self.assertIn('"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"',
                          (package / "main.go").read_text())
