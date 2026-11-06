#!/usr/bin/env bash
# Kept inside atlassian_* so the workflow's source/definition diff binds this driver.
set -euo pipefail

case "${1:-}" in
  install)
    python -m pip install pytest==9.0.3 pytest-asyncio==1.4.0 PyYAML==6.0.3 ruff==0.9.6
    python -m pip install -e 'modules/gateway[dev]' -e 'libs/python/adp-common[l5-consumers]' -e modules/tools -e modules/tools/facade moto==5.0.27 'aiosqlite>=0.20.0'
    ;;
  run)
    mkdir -p "$RUNNER_TEMP/atlassian-identity-proof"
    python -m pytest -q -o junit_family=xunit1 --junitxml="$RUNNER_TEMP/atlassian-identity-proof/junit.xml" \
      tests/e2e/tools/test_atlassian_provider_setup.py \
      tests/e2e/tools/test_atlassian_handoff_procedure.py \
      tests/e2e/tools/test_atlassian_capability_procedure.py \
      tests/e2e/tools/test_atlassian_lifecycle_procedure.py \
      tests/e2e/tools/test_atlassian_catalogue.py \
      tests/e2e/tools/test_atlassian_contract.py \
      tests/e2e/tools/test_atlassian_handoff.py \
      tests/e2e/tools/test_atlassian_oauth_probe.py \
      tests/e2e/tools/test_atlassian_listener.py \
      tests/e2e/tools/test_atlassian_probe_ledger.py
    python -m tests.e2e.tools.atlassian_phase report --output "$RUNNER_TEMP/atlassian-identity-proof"
    ;;
  *)
    echo 'expected install or run' >&2
    exit 2
    ;;
esac
