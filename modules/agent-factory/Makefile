# Agent Factory test targets

.PHONY: test test-unit test-e2e test-e2e-regression

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
