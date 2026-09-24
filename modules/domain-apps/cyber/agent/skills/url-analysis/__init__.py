"""
URL investigation skill — model-directed exploration through AgentCore Browser.

The existing cyber agent reviews evidence, chooses a bounded browser action,
examines the resulting view in the same context, and revises its assessment.

Key modules:
- evidence_schema: Pydantic models defining the evidence contract
- denylist: Destination policy decision (canonicalises the address, fails closed)
- browser_client: Unprivileged entry point to the trusted browser broker
- domain_investigation: Stateful investigation, review, and validated finish tools
- investigation_browser: Persistent browser contexts and observed choices
- browser_broker: Trusted action executor with exclusive browser IAM
- browser_guard: Broker-internal destination and pinned-transport enforcement
- enrichment: WHOIS, VT, URLhaus, MISP lookups (pure HTTP, no AgentCore)
- live_evaluation: AWS-only model/browser feedback-loop acceptance
- benchmark: Secondary snapshot assessment, without live browser actions
- verdict: Legacy deterministic scoring and classification
- report: Markdown/JSON/HTML report rendering
"""
