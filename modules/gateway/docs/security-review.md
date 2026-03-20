# Bedrock Gateway Security Review

**Date:** 2026-02-17
**Reviewer:** AI Security Agent
**Version:** Issue #129 - Comprehensive Security Audit

## Executive Summary

This document presents a comprehensive security review of the Bedrock Gateway solution, covering infrastructure, application code, authentication, CI/CD, and operational configuration. The review identified **24 findings** ranging from critical authentication bypass issues to best practice recommendations.

## Summary Table

| Severity | Count | Primary Categories |
|----------|-------|-------------------|
| CRITICAL | 2 | Authentication |
| HIGH | 5 | Authentication, Infrastructure |
| MEDIUM | 8 | Application, Network, CI/CD |
| LOW | 6 | Operations, Infrastructure |
| INFO | 3 | Best Practices |

---

## CRITICAL Findings

### [CRITICAL] Admin Routes Authentication Bypass via Hardcoded Mock Context

**Category**: Authentication
**Risk**: Complete admin API bypass. Any unauthenticated user can access ALL admin endpoints with full platform admin privileges. This allows attackers to:
- Create/modify/delete organizations
- Access all user data across tenants
- Modify budget and rate limit configurations
- Access sensitive audit logs
- Manage the Bedrock pool accounts

**Current State**: The `get_current_user()` dependency in `src/admin/routes.py` (lines 62-77) returns a hardcoded mock `TokenContext` with `is_admin=True`:

```python
async def get_current_user() -> TokenContext:
    """Get the current authenticated user context.

    Note: In production, this would be populated by auth middleware.
    For now, we return a mock context for testing.
    """
    return TokenContext(
        user_id="system",
        org_id="system",
        team_id="system",
        department_id="system",
        account_type="service",
        is_admin=True,  # CRITICAL: Always admin!
        expires_at=datetime.now(),
    )
```

**Recommendation**:
1. Replace `get_current_user()` with `get_current_user_context` from `src/auth/middleware.py`
2. Apply proper Cognito JWT validation to all admin routes
3. Add integration tests that verify 401/403 for unauthenticated requests

**Priority**: P0 (fix immediately)
**Files**: `src/admin/routes.py:62-77`

---

### [CRITICAL] Deprecated /auth/exchange Endpoint Still Active

**Category**: Authentication
**Risk**: The legacy `/auth/exchange` endpoint accepts raw AWS credentials and returns gateway tokens. This:
- Bypasses Cognito authentication entirely
- Accepts potentially stolen AWS credentials
- Could be exploited for unauthorized access if AWS credentials are compromised

**Current State**: The endpoint exists at `src/auth/routes.py:36-108` and is marked as `deprecated=True` but is still fully functional and routed.

```python
@router.post(
    "/exchange",
    response_model=AuthExchangeResponse,
    deprecated=True,  # Only marks in OpenAPI docs, still accessible!
    ...
)
async def exchange_credentials(request: AuthExchangeRequest, db: AsyncSession = Depends(get_db)) -> AuthExchangeResponse:
```

**Recommendation**:
1. Add a feature flag to completely disable this endpoint in production
2. Return 410 Gone or 404 for this endpoint when Cognito auth is enabled
3. Log all attempts to use this endpoint for security monitoring
4. Plan complete removal in next major version

**Priority**: P0 (fix immediately)
**Files**: `src/auth/routes.py:36-108`

---

## HIGH Findings

### [HIGH] Token Secret Key Exposed in ConfigMap Template

**Category**: Infrastructure / Secrets Management
**Risk**: The `BG_TOKEN_SECRET_KEY` is set via ConfigMap templating in the deployment workflow. If the ConfigMap is logged, exposed in error messages, or accessible via kubectl, the secret key could be leaked, allowing token forgery.

**Current State**: In `k8s/configmap.yaml`:
```yaml
BG_TOKEN_SECRET_KEY: "__TOKEN_SECRET_KEY__"
```

The value is generated in `backend-deploy.yml` (line 236-240):
```yaml
if [ -z "$TOKEN_SECRET" ]; then
    TOKEN_SECRET="${ENV}-secret-key-$(date +%s | sha256sum | head -c 16)"
fi
```

**Recommendation**:
1. Store `BG_TOKEN_SECRET_KEY` in AWS Secrets Manager
2. Use external-secrets-operator or IRSA to fetch at runtime
3. Never pass secrets through sed replacement in CI/CD
4. Use Kubernetes Secrets (not ConfigMap) for sensitive values

**Priority**: P1 (fix before production)
**Files**: `k8s/configmap.yaml:49`, `.github/workflows/backend-deploy.yml:236-240`

---

### [HIGH] MFA Not Enforced for Cognito Users

**Category**: Authentication
**Risk**: Without MFA, compromised passwords lead directly to account takeover. Single-factor authentication is insufficient for a platform handling sensitive AI workloads.

**Current State**: In `infra/modules/cognito/main.tf`:
```hcl
mfa_configuration = "OPTIONAL"
```

**Recommendation**:
1. Set `mfa_configuration = "ON"` for production environments
2. Enable software token (TOTP) MFA: `software_token_mfa_configuration { enabled = true }`
3. Consider requiring MFA for admin roles at minimum

**Priority**: P1 (fix before production)
**Files**: `infra/modules/cognito/main.tf:33`

---

### [HIGH] Access Token Lifetime Too Long (24 Hours)

**Category**: Authentication
**Risk**: Long-lived tokens increase the window of opportunity for token theft and replay attacks. If a token is stolen, it remains valid for 24 hours.

**Current State**: In `infra/modules/cognito/main.tf`:
```hcl
access_token_validity  = 24  # hours
id_token_validity      = 24  # hours
```

**Recommendation**:
1. Reduce access token validity to 1 hour (3600 seconds)
2. Reduce ID token validity to 1 hour
3. Rely on refresh token flow for longer sessions
4. Implement token binding or fingerprinting for sensitive operations

**Priority**: P1 (fix before production)
**Files**: `infra/modules/cognito/main.tf:85-86`

---

### [HIGH] ALB Security Group Allows 0.0.0.0/0 Ingress

**Category**: Network Security
**Risk**: The ALB security group allows inbound traffic from any IP address. While this may be intentional for public-facing services, it increases attack surface.

**Current State**: In `infra/modules/networking/main.tf:141-155`:
```hcl
ingress {
    description = "HTTPS from Internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
}
```

**Recommendation**:
1. If using CloudFront VPC Origin (internal ALB), remove public access
2. If ALB must be public, use WAF with rate limiting and geo-restrictions
3. Consider IP allowlisting for admin endpoints
4. Enable CloudFront and use CloudFront-only origin access

**Priority**: P1 (fix before production)
**Files**: `infra/modules/networking/main.tf:136-170`

---

### [HIGH] IAM Policy Uses Resource: "*" for Bedrock

**Category**: Infrastructure / IAM
**Risk**: Overly permissive IAM policy allows access to all Bedrock resources and models, violating least privilege principle.

**Current State**: In `infra/modules/eks/main.tf:155-170`:
```hcl
Statement = [
    {
        Effect = "Allow"
        Action = [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
            ...
        ]
        Resource = "*"
    }
]
```

**Recommendation**:
1. Scope Bedrock permissions to specific model ARNs
2. Use resource patterns like `arn:aws:bedrock:*:*:inference-profile/*`
3. Consider per-organization IAM role scoping

**Priority**: P1 (fix before production)
**Files**: `infra/modules/eks/main.tf:151-171`

---

## MEDIUM Findings

### [MEDIUM] CloudFront Custom Error Responses May Leak Information

**Category**: Network Security
**Risk**: Custom error responses returning 200 with `/index.html` for 403/404 errors could mask security issues and leak application structure.

**Current State**: In `infra/modules/cloudfront/main.tf:234-246`:
```hcl
custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
}
```

**Recommendation**:
1. Return proper error pages for API paths (not index.html)
2. Add path-based error handling to distinguish frontend SPA from API
3. Log all 403/404 errors for security monitoring

**Priority**: P2 (fix soon)
**Files**: `infra/modules/cloudfront/main.tf:234-246`

---

### [MEDIUM] Container Running as Root User

**Category**: Container Security
**Risk**: Running as root inside containers increases the impact of container escapes and provides unnecessary privileges.

**Current State**: The `Dockerfile` does not specify a non-root user:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
# No USER directive - runs as root
```

**Recommendation**:
1. Add a non-root user to the Dockerfile:
```dockerfile
RUN useradd -r -u 1000 appuser
USER appuser
```
2. Set `runAsNonRoot: true` in Kubernetes deployment security context
3. Add `readOnlyRootFilesystem: true` where possible

**Priority**: P2 (fix soon)
**Files**: `Dockerfile`, `k8s/deployment.yaml`

---

### [MEDIUM] Missing Security Context in Kubernetes Deployment

**Category**: Container Security
**Risk**: Without proper security context, pods run with default privileges which may be excessive.

**Current State**: `k8s/deployment.yaml` lacks `securityContext` configuration.

**Recommendation**: Add security context:
```yaml
spec:
  containers:
    - name: bedrockgateway
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        readOnlyRootFilesystem: true
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
```

**Priority**: P2 (fix soon)
**Files**: `k8s/deployment.yaml`

---

### [MEDIUM] CORS Configuration May Be Too Permissive

**Category**: Application Security
**Risk**: If CORS origins are not properly validated, cross-origin attacks may be possible.

**Current State**: CORS origins are set via environment variable:
```yaml
CORS_ALLOWED_ORIGINS: "__CORS_ALLOWED_ORIGINS__"
```
In deployment, this includes localhost for development:
```bash
CORS_ORIGINS="https://${CF_DOMAIN},http://localhost:5173"
```

**Recommendation**:
1. Remove localhost from production CORS settings
2. Validate CORS origin against allowlist in backend
3. Consider stricter CORS for admin endpoints

**Priority**: P2 (fix soon)
**Files**: `k8s/configmap.yaml:45`, `.github/workflows/backend-deploy.yml:218-221`

---

### [MEDIUM] GitHub Actions Workflow Missing Dependabot/Version Pinning

**Category**: CI/CD Security
**Risk**: Unpinned GitHub Actions versions could be compromised through supply chain attacks.

**Current State**: Actions use major version tags:
```yaml
uses: actions/checkout@v4
uses: actions/setup-node@v4
```

**Recommendation**:
1. Pin actions to specific SHA commits: `uses: actions/checkout@abcdef123...`
2. Enable Dependabot for GitHub Actions updates
3. Review action permissions and use minimal required permissions

**Priority**: P2 (fix soon)
**Files**: `.github/workflows/*.yml`

---

### [MEDIUM] Frontend Stores Tokens in Session Storage

**Category**: Frontend Security
**Risk**: Session storage is accessible to any JavaScript running on the same origin. XSS attacks could steal tokens.

**Current State**: In `frontend/src/services/auth.ts`:
```typescript
sessionStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
sessionStorage.setItem(ID_TOKEN_KEY, tokens.id_token);
sessionStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
```

**Recommendation**:
1. Consider using httpOnly cookies for token storage
2. Implement Content Security Policy to mitigate XSS
3. Add token binding to prevent stolen token reuse
4. Use short-lived tokens with frequent refresh

**Priority**: P2 (fix soon)
**Files**: `frontend/src/services/auth.ts:254-264`

---

### [MEDIUM] Sensitive Configuration Logged in Deploy Workflow

**Category**: CI/CD Security
**Risk**: ConfigMap contents (potentially containing secrets) are printed to workflow logs.

**Current State**: In `.github/workflows/backend-deploy.yml:270-271`:
```bash
echo "--- ConfigMap Contents ---"
cat k8s/configmap.yaml
```

**Recommendation**:
1. Remove ConfigMap printing from logs
2. If needed, mask sensitive values before printing
3. Use GitHub Actions secret masking for sensitive values

**Priority**: P2 (fix soon)
**Files**: `.github/workflows/backend-deploy.yml:270-271`

---

### [MEDIUM] CLI Script Stores Credentials in Plaintext File

**Category**: CLI Security
**Risk**: AWS credentials stored in `~/.aws/credentials` and Cognito tokens in `~/.bedrock-gateway/tokens.json` could be read by other processes or users.

**Current State**: In `cli/bg-cognito-auth.sh`:
```bash
cat > "${TOKEN_FILE}" << EOF
{
    "id_token": "${id_token}",
    "access_token": "${access_token}",
    "refresh_token": "${refresh_token}",
    ...
}
EOF
chmod 600 "${TOKEN_FILE}"
```

**Recommendation**:
1. Use OS keychain integration (macOS Keychain, Linux Secret Service)
2. Encrypt tokens at rest with user-derived key
3. Warn users about file permissions in documentation

**Priority**: P2 (fix soon)
**Files**: `cli/bg-cognito-auth.sh:104-121`

---

## LOW Findings

### [LOW] EKS Public Endpoint Access Enabled

**Category**: Infrastructure
**Risk**: Public API endpoint increases attack surface, though access is protected by IAM.

**Current State**: In `infra/modules/eks/main.tf:40`:
```hcl
endpoint_public_access  = true
```

**Recommendation**:
1. For production, set `endpoint_public_access = false`
2. Use VPN or bastion host for cluster administration
3. If public access needed, restrict `public_access_cidrs`

**Priority**: P3 (nice to have)
**Files**: `infra/modules/eks/main.tf:37-42`

---

### [LOW] Missing Network Policies for Pod-to-Pod Communication

**Category**: Network Security
**Risk**: Without network policies, any pod can communicate with any other pod in the cluster.

**Current State**: No Kubernetes NetworkPolicy resources defined in `k8s/` directory.

**Recommendation**:
1. Implement default-deny network policies
2. Create explicit allow policies for required communication
3. Isolate the bedrockgw namespace from other namespaces

**Priority**: P3 (nice to have)
**Files**: `k8s/` (missing NetworkPolicy)

---

### [LOW] Password in URL During OAuth Flow (Standard Behavior)

**Category**: Authentication
**Risk**: Authorization code is passed in URL during OAuth callback. While standard OAuth behavior, codes should be single-use and short-lived.

**Current State**: Standard OAuth PKCE flow implementation.

**Recommendation**:
1. Ensure authorization codes are single-use
2. Verify short code expiration (default Cognito is 5 minutes)
3. Log and alert on code reuse attempts

**Priority**: P3 (nice to have)
**Files**: `frontend/src/services/auth.ts`

---

### [LOW] Exception Details May Leak Internal Information

**Category**: Application Security
**Risk**: Exception messages may contain internal implementation details that could help attackers.

**Current State**: In `src/auth/routes.py:104-107`:
```python
except Exception as e:
    logger.error(f"Unexpected error in credential exchange: {e}")
    raise HTTPException(status_code=500, detail={
        "error": "internal_error",
        "message": "An unexpected error occurred during authentication"
    })
```

**Recommendation**:
1. Ensure all 500 errors use generic messages (currently good)
2. Add structured logging with request IDs for debugging
3. Implement error correlation for support without exposing details

**Priority**: P3 (nice to have)
**Files**: Various `routes.py` files

---

### [LOW] CloudTrail Module Not Enabled by Default

**Category**: Audit / Compliance
**Risk**: Without CloudTrail, API activity is not logged for security forensics.

**Current State**: CloudTrail module exists but may not be enabled in all environments.

**Recommendation**:
1. Enable CloudTrail in all environments
2. Enable log file validation for tamper detection
3. Send logs to a separate security account
4. Set up CloudWatch alarms for suspicious activity

**Priority**: P3 (nice to have)
**Files**: `infra/modules/cloudtrail/main.tf`

---

### [LOW] RDS Master Password Generated at Apply Time

**Category**: Infrastructure
**Risk**: Random password is generated during Terraform apply. While IAM auth is primary, the master password should be managed securely.

**Current State**: In `infra/modules/rds/main.tf:7-10`:
```hcl
resource "random_password" "master" {
    length  = 32
    special = false
}
```

**Recommendation**:
1. Store master password in Secrets Manager after initial creation
2. Enable automatic rotation for master password
3. Document that IAM auth is the primary authentication method

**Priority**: P3 (nice to have)
**Files**: `infra/modules/rds/main.tf:7-10`

---

## INFO Observations

### [INFO] Good Security Practices Observed

The following positive security practices were identified:

1. **PKCE Implementation**: Frontend uses proper PKCE flow for OAuth
2. **IAM Database Authentication**: RDS uses IAM auth instead of passwords
3. **ElastiCache IAM Auth**: Redis uses IAM authentication
4. **KMS Encryption**: EKS secrets encrypted with KMS
5. **TLS Enforcement**: CloudFront enforces TLS 1.2+
6. **Security Headers**: Proper HSTS, X-Frame-Options, CSP headers
7. **JWT Validation**: Cognito JWT properly validated against JWKS
8. **Password Policy**: Strong Cognito password requirements
9. **Refresh Token Rotation**: Tokens rotated on refresh
10. **Storage Encryption**: RDS and ElastiCache have encryption at rest

---

### [INFO] OWASP Top 10 Coverage

| OWASP Category | Status | Notes |
|----------------|--------|-------|
| A01: Broken Access Control | CRITICAL | Admin routes bypass authentication |
| A02: Cryptographic Failures | MEDIUM | Token secret in ConfigMap |
| A03: Injection | LOW | ORM used, parameterized queries |
| A04: Insecure Design | HIGH | Legacy auth endpoint still active |
| A05: Security Misconfiguration | MEDIUM | Various infrastructure findings |
| A06: Vulnerable Components | LOW | Dependencies appear current |
| A07: Auth Failures | CRITICAL | Mock auth context, weak MFA |
| A08: Data Integrity | LOW | No code signing issues found |
| A09: Logging Failures | LOW | Comprehensive logging in place |
| A10: SSRF | LOW | Model IDs validated, limited external calls |

---

### [INFO] AWS Well-Architected Security Pillar Alignment

| Pillar Area | Rating | Notes |
|-------------|--------|-------|
| Identity and Access Management | Needs Improvement | Critical auth bypass |
| Detection | Good | CloudTrail, logging configured |
| Infrastructure Protection | Good | VPC, security groups proper |
| Data Protection | Good | Encryption at rest and transit |
| Incident Response | Needs Improvement | Missing runbooks |

---

## Recommendations Summary

### Immediate Actions (P0)
1. Fix admin routes authentication bypass
2. Disable or restrict legacy /auth/exchange endpoint

### Before Production (P1)
1. Move token secret to Secrets Manager
2. Enable MFA for Cognito users
3. Reduce access token lifetime
4. Restrict ALB security group or use internal ALB
5. Scope Bedrock IAM permissions

### Near Term (P2)
1. Add container security context
2. Pin GitHub Actions versions
3. Improve token storage security
4. Remove sensitive data from CI logs
5. Improve CLI credential storage

### Long Term (P3)
1. Disable EKS public endpoint
2. Implement network policies
3. Enable CloudTrail in all environments
4. Set up security alerting

---

## Appendix: Files Reviewed

### Infrastructure
- `infra/modules/cognito/*.tf`
- `infra/modules/networking/main.tf`
- `infra/modules/cloudfront/main.tf`
- `infra/modules/eks/main.tf`
- `infra/modules/alb/main.tf`
- `infra/modules/iam/main.tf`
- `infra/modules/rds/main.tf`
- `infra/modules/redis/main.tf`

### Application
- `src/auth/*.py`
- `src/admin/*.py`
- `src/proxy/*.py`
- `Dockerfile`

### Kubernetes
- `k8s/*.yaml`

### CI/CD
- `.github/workflows/*.yml`

### Frontend
- `frontend/src/services/auth.ts`
- `frontend/src/contexts/AuthContext.tsx`

### CLI
- `cli/bg-cognito-auth.sh`

### Dependencies
- `pyproject.toml`
- `frontend/package.json`

---

*This security review was conducted as part of Issue #129. All findings should be tracked and remediated according to priority.*
