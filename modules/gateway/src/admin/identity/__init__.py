"""Tenant + user onboarding admin API (Phase A.1).

Issue #387: Single authoritative writer for tenant and user records.
All other modules read from Postgres + DDB identity-index as cached projections.
"""
