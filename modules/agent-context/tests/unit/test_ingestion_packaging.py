"""Unit tests for the ingestion image packaging + refresh fail-fast (Issue #2800).

Regression coverage for the silent daily-refresh breakage:

- ``registry_reader.py`` was shipped in the ingestion source tree but never
  added to the Dockerfile ``COPY`` list, so ``publish-ingestion.py --from-registry``
  crashed with ``ModuleNotFoundError: No module named 'registry_reader'`` at
  Step 1 of the daily ``ingestion-refresh`` CronJob.
- The crash was swallowed: ``refresh-repos.py`` logged the failure and returned
  exit 0, so the Job reported success and ``lastSuccessfulTime`` updated even
  though no repo change detection ran.

These tests prove (a) the module is packaged, and (b) a publisher failure now
fails the refresh Job loudly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ingestion source tree (…/modules/agent-context/images/ingestion)
_INGESTION_DIR = Path(__file__).resolve().parents[2] / "images" / "ingestion"
_DOCKERFILE = _INGESTION_DIR / "Dockerfile"

# Modules that publish-ingestion.py imports lazily on the --from-registry path
# (or that sqs-worker/ingest-repo import) and that MUST be COPYed into /app.
# Each one is a top-level module in the image, not an installed package.
_REQUIRED_APP_MODULES = ["registry_reader", "scope", "status_callback"]


# ---------------------------------------------------------------------------
# Dockerfile packaging assertions
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return _DOCKERFILE.read_text()


class TestDockerfilePackagesModules:
    """The Dockerfile must COPY every sibling module publish-ingestion.py imports."""

    @pytest.mark.parametrize("module", _REQUIRED_APP_MODULES)
    def test_module_source_exists(self, module: str):
        """The module source file exists in the ingestion tree (sanity)."""
        assert (_INGESTION_DIR / f"{module}.py").is_file(), (
            f"{module}.py missing from ingestion source tree"
        )

    @pytest.mark.parametrize("module", _REQUIRED_APP_MODULES)
    def test_module_is_copied_into_image(self, dockerfile_text: str, module: str):
        """Each required module appears in a COPY ... /app/ instruction (#2800)."""
        copied = {
            token
            for line in dockerfile_text.splitlines()
            if line.strip().startswith("COPY")
            for token in line.split()
            if token.endswith(".py")
        }
        assert f"{module}.py" in copied, (
            f"{module}.py is not in any Dockerfile COPY — it will be missing at "
            f"/app/ and imports will fail at runtime (regression of #2800)"
        )

    def test_publish_ingestion_registry_import_target_is_packaged(self, dockerfile_text: str):
        """The exact module publish_from_registry() imports is packaged.

        publish-ingestion.py does `from registry_reader import ...` — assert the
        Dockerfile ships it so the daily refresh Step 1 cannot crash on import.
        """
        publisher = (_INGESTION_DIR / "publish-ingestion.py").read_text()
        assert "from registry_reader import" in publisher, (
            "expected publish-ingestion.py to import registry_reader (test premise)"
        )
        assert "registry_reader.py" in dockerfile_text, (
            "registry_reader.py must be COPYed into the image (#2800)"
        )


class TestRequiredModulesImportCleanly:
    """The build-time smoke check imports these modules; they must be import-safe.

    Mirrors the Dockerfile `RUN python -c "import registry_reader, scope,
    status_callback"` step — proves those modules have no import-time
    dependency on runtime env/DB, so the build assertion is meaningful and
    won't false-fail.
    """

    @pytest.mark.parametrize("module", _REQUIRED_APP_MODULES)
    def test_imports_without_runtime_env(self, module: str):
        if str(_INGESTION_DIR) not in sys.path:
            sys.path.insert(0, str(_INGESTION_DIR))
        # Import via importlib so a failure surfaces as a clear assertion.
        spec = importlib.util.find_spec(module)
        assert spec is not None, f"{module} not importable from ingestion dir"
        importlib.import_module(module)


# ---------------------------------------------------------------------------
# refresh-repos.py Step-1 fail-fast (SQS publisher path)
# ---------------------------------------------------------------------------


def _load_refresh_module():
    """Load refresh-repos.py as a module (handles the hyphenated filename)."""
    if str(_INGESTION_DIR) not in sys.path:
        sys.path.insert(0, str(_INGESTION_DIR))
    path = str(_INGESTION_DIR / "refresh-repos.py")
    spec = importlib.util.spec_from_file_location("refresh_repos", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRefreshFailFast:
    """When the SQS publisher fails, main() must exit non-zero (#2800)."""

    def test_publisher_failure_exits_nonzero(self, monkeypatch):
        mod = _load_refresh_module()
        monkeypatch.setattr(mod, "SQS_QUEUE_URL", "https://sqs.example.com/q")
        monkeypatch.setattr(sys, "argv", ["refresh-repos.py"])

        with (
            patch.object(mod, "mint_github_token", return_value=True),
            patch.object(
                mod,
                "run_publisher",
                return_value={"status": "failed", "error": "ModuleNotFoundError"},
            ),
        ):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 1, "refresh must fail the Job when Step 1 publisher fails"

    def test_publisher_success_exits_zero(self, monkeypatch):
        mod = _load_refresh_module()
        monkeypatch.setattr(mod, "SQS_QUEUE_URL", "https://sqs.example.com/q")
        monkeypatch.setattr(sys, "argv", ["refresh-repos.py"])

        with (
            patch.object(mod, "mint_github_token", return_value=True),
            patch.object(
                mod,
                "run_publisher",
                return_value={"total": 15, "enqueued": 3, "skipped": 12, "errors": 0},
            ),
        ):
            # main() returns normally (no SystemExit) on success.
            mod.main()
