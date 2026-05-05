"""
URL analysis skill — agent-orchestrated browsing + IOC extraction via AgentCore Browser.

This skill uses a playbook-driven approach: the agent writes orchestration scripts
at runtime from the contract extract (agentcore-browser-contract.md), collects
Evidence, then passes it through deterministic verdict + report logic.

Key modules:
- evidence_schema: Pydantic models defining the evidence contract
- denylist: Pre-flight URL safety validation
- enrichment: WHOIS, VT, URLhaus, MISP lookups (pure HTTP, no AgentCore)
- verdict: Deterministic scoring and classification
- report: Markdown/JSON/HTML report rendering
"""
