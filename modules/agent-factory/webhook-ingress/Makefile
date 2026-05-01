# Webhook Ingress — test targets

.PHONY: test-integration-webhook test-unit lint

# Run the full integration test suite against the deployed environment
test-integration-webhook:
	TEST_ENV=$(TEST_ENV) \
		python -m pytest tests/test_e2e_webhook.py \
		-v --tb=short \
		-m "integration" \
		--junitxml=test-results/e2e-webhook-junit.xml

# Run unit tests only (no live infra required)
test-unit:
	python -m pytest tests/ -m "not integration" -v --tb=short

# Lint check
lint:
	ruff check tests/
	ruff format --check tests/
