"""
Integration tests package for BedrockGateway.

This package contains integration tests that verify cross-unit interactions:
- Auth → Token validation → Proxy request flow
- Budget check → Enforcement → Usage recording
- Rate limit check → Enforcement → Header responses
- Pool round-robin → Failover on throttle
- Complete middleware chain execution
"""
