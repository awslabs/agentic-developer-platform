# Webhook Ingress — dev targets

.PHONY: lint test test-unit test-integration test-integration-webhook package deploy destroy clean

lint:
	ruff check lambda/ && mypy lambda/ --strict

test:
	pytest lambda/ -m "not integration" -v

test-unit:
	python -m pytest tests/ -m "not integration" -v --tb=short

test-integration:
	pytest tests/ -m integration -v

# Full integration test suite against the deployed environment
test-integration-webhook:
	TEST_ENV=$(TEST_ENV) \
		python -m pytest tests/test_e2e_webhook.py \
		-v --tb=short \
		-m "integration" \
		--junitxml=test-results/e2e-webhook-junit.xml

package:
	./scripts/package-lambdas.sh

deploy:
	cd infra && terraform apply -auto-approve

destroy:
	cd infra && terraform destroy -auto-approve

clean:
	rm -rf dist/
