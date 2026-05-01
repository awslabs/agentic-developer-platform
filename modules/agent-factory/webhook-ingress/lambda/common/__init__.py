"""Common utilities for webhook ingress Lambdas.

Provides:
- webhook_events: DynamoDB audit logging for all incoming webhooks
- rate_limit: Sliding-window per-tenant rate limiting via DDB atomic counters
- metrics: CloudWatch custom metrics emission
"""
