# Agent Factory test targets

.PHONY: test test-unit test-e2e test-e2e-regression test-e2e-memory test-e2e-artifacts test-e2e-wholesome cleanup-e2e

# Default: run unit tests only
test: test-unit

test-unit:
	python -m pytest tests/ -m "not live_only and not workflow" -v

# Run all live E2E tests (auth, roundtrip, KEDA + behavior)
test-e2e:
	TEST_ENV=dev RUN_COSTLY_TESTS=yes \
		python -m pytest tests/e2e/ -v --tb=short \
		--junitxml=test-results/e2e-junit.xml

# Run behavior regression tests only (guards fixes from #68/#85/#87/#88/#89/#92)
# Produces JUnit XML + latency JSON summary
test-e2e-regression:
	TEST_ENV=dev RUN_COSTLY_TESTS=yes \
		python -m pytest tests/e2e/test_chat_agent_behavior.py -v --tb=short \
		--junitxml=test-results/e2e-regression-junit.xml \
		-m "live_only and costs_money"

# Run only fast regression tests (skip @slow)
test-e2e-regression-fast:
	TEST_ENV=dev RUN_COSTLY_TESTS=yes \
		python -m pytest tests/e2e/test_chat_agent_behavior.py -v --tb=short \
		--junitxml=test-results/e2e-regression-fast-junit.xml \
		-m "live_only and costs_money and not slow"

# --- Wave 1: Memory + Artifacts ---

# Run memory E2E tests (save_preference, recall_memory, save_learning)
test-e2e-memory:
	TEST_ENV=dev RUN_COSTLY_TESTS=yes \
		python -m pytest tests/e2e/test_memory_e2e.py -v --tb=short \
		--junitxml=test-results/e2e-memory-junit.xml \
		-o "addopts=" -m "live_only and costs_money"

# Run artifact E2E tests (publish, fetch-edit-publish with supersedes)
test-e2e-artifacts:
	TEST_ENV=dev RUN_COSTLY_TESTS=yes \
		python -m pytest tests/e2e/test_artifacts_e2e.py -v --tb=short \
		--junitxml=test-results/e2e-artifacts-junit.xml \
		-o "addopts=" -m "live_only and costs_money"

# Run the full "wholesome" suite: regression + memory + artifacts
# (~15 min, costs money via Bedrock)
test-e2e-wholesome:
	TEST_ENV=dev RUN_COSTLY_TESTS=yes \
		python -m pytest \
		tests/e2e/test_chat_agent_behavior.py \
		tests/e2e/test_memory_e2e.py \
		tests/e2e/test_artifacts_e2e.py \
		-v --tb=short \
		--junitxml=test-results/e2e-wholesome-junit.xml \
		-o "addopts=" -m "live_only and costs_money"

# Cleanup helper: close any leftover E2E test issues in GitHub
cleanup-e2e:
	@echo "Closing E2E test issues..."
	gh issue list --repo aws-e/adp --label e2e-test --state open --json number \
		--jq '.[].number' | xargs -I{} gh issue close {} --repo aws-e/adp 2>/dev/null || true
	@echo "Done."
