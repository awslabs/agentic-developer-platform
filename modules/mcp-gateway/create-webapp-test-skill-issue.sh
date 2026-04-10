#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

gh issue create --repo "$REPO" \
  --title "Web Application Testing: BedrockGateway Admin UI (Skill Agent)" \
  --label "skill-agent" \
  --body "## Web Application Testing: BedrockGateway Admin UI

### Objective
Test the BedrockGateway Admin UI using the webapp-testing skill with Playwright.

**URL**: https://dp7n42m5j4pl6.cloudfront.net/

**Credentials** (available as env vars from GitHub Actions secrets):
- Username: \$WEBAPP_TEST_USERNAME
- Password: \$WEBAPP_TEST_PASSWORD

### Instructions

1. **Read the Epic** at https://github.com/PranavSharma1000/bedrock-gateway/issues/7 to understand Admin UI features

2. **Read the frontend source code** in \`frontend/src/\` to understand components, routes, forms, and expected behavior

3. **Use the webapp-testing skill** (\`.claude/skills/webapp-testing/SKILL.md\`) for Playwright patterns and best practices

4. **Install Playwright** if needed: \`pip install playwright && playwright install chromium\`

5. **Write and execute test scripts** covering:
   - Login flow (enter credentials, verify success)
   - Dashboard (verify sections load)
   - Navigation (all sidebar links)
   - Organization management
   - Budget and rate limit configuration
   - Usage/logs viewer
   - Error handling (invalid inputs)

6. **Capture screenshots** at each major step

7. **Create test report** at \`tests/e2e/webapp/test-report.md\` with results, screenshots, and findings

8. **Create PR** with test scripts, screenshots, and report in \`tests/e2e/webapp/\`"

echo "Created webapp testing issue with skill-agent label"
