# Legacy gateway desired-image remediation

The enabled `adp-gateway-agents/agent-gateway-worker` ScaledJob still references the old Python worker. Its historical workflow says Chat superseded it, but no live retirement is assumed. The desired old digest has 59 Critical /228 High raw matches.

The normal Dockerfile now reuses the authenticated curl package artifact and portable AWS CLI 2.37.4 built with Python 3.14.7, retaining their licenses and source provenance. It also requires patched AnyIO. The complete candidate build has 24 Critical /90 High raw matches, **0 Critical /60 High after exact curl binary/source review**. Raw scan and SBOM remain under `/workspaces/projects/security27-continuation/legacy-gateway-fixed-scan/`, bound by the committed receipt.

Native nonroot consumer import, SQS receive contract, empty session history, AWS CLI, curl/Git TLS and certificate-refusal checks passed. Candidate publication and deployment are separate; this evidence does not claim live closure or authorize retirement of the legacy consumer.
