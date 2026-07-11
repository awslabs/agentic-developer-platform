"""Feature flags module — Issue #3566.

Exposes deployment-level feature gates via GET /api/features.
Flags default to enabled (fail-open); operators disable modules
by setting FEATURE_*_ENABLED=false on the gateway Deployment.
"""
