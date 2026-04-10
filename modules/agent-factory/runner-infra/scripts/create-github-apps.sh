#!/bin/bash
# =============================================================================
# GitHub App Creation Script
# =============================================================================
# Creates multiple GitHub Apps using the App Manifest flow.
# Each app gets its own rate limit bucket (5000 req/hr).
#
# Usage:
#   ./create-github-apps.sh [--org ORG_NAME] [--apps APP_LIST]
#
# Examples:
#   ./create-github-apps.sh --org aws-innovate
#   ./create-github-apps.sh --org aws-innovate --apps "pm,dev"
#   ./create-github-apps.sh --org aws-innovate --apps "pm,dev,ops"
#
# After creation, credentials are stored in AWS Secrets Manager.
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
ORG_NAME="${ORG_NAME:-aws-innovate}"
APPS_TO_CREATE="pm,dev,ops"
CALLBACK_PORT=3456
AWS_REGION="${AWS_REGION:-us-west-2}"
SECRET_PREFIX="github-app"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --org)
            ORG_NAME="$2"
            shift 2
            ;;
        --apps)
            APPS_TO_CREATE="$2"
            shift 2
            ;;
        --port)
            CALLBACK_PORT="$2"
            shift 2
            ;;
        --region)
            AWS_REGION="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--org ORG_NAME] [--apps APP_LIST] [--port PORT] [--region REGION]"
            echo ""
            echo "Options:"
            echo "  --org      GitHub organization name (default: aws-innovate)"
            echo "  --apps     Comma-separated list of apps to create: pm,dev,ops (default: all)"
            echo "  --port     Local callback server port (default: 3456)"
            echo "  --region   AWS region for Secrets Manager (default: us-west-2)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

log() {
    local level=$1
    shift
    local color=""
    case $level in
        INFO) color=$BLUE ;;
        SUCCESS) color=$GREEN ;;
        WARN) color=$YELLOW ;;
        ERROR) color=$RED ;;
    esac
    echo -e "${color}[$level]${NC} $*"
}

# Check prerequisites
check_prerequisites() {
    log INFO "Checking prerequisites..."

    local missing=()

    command -v node >/dev/null 2>&1 || missing+=("node")
    command -v npm >/dev/null 2>&1 || missing+=("npm")
    command -v aws >/dev/null 2>&1 || missing+=("aws-cli")
    command -v jq >/dev/null 2>&1 || missing+=("jq")

    if [[ ${#missing[@]} -gt 0 ]]; then
        log ERROR "Missing required tools: ${missing[*]}"
        exit 1
    fi

    # Check AWS credentials
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        log ERROR "AWS credentials not configured. Run 'aws configure' first."
        exit 1
    fi

    log SUCCESS "All prerequisites met"
}

# Create the Node.js callback server
create_callback_server() {
    local server_dir=$(mktemp -d)
    log INFO "Creating callback server in $server_dir" >&2

    cat > "$server_dir/package.json" << 'PACKAGE_EOF'
{
  "name": "github-app-creator",
  "version": "1.0.0",
  "type": "module",
  "dependencies": {}
}
PACKAGE_EOF

    cat > "$server_dir/server.mjs" << 'SERVER_EOF'
import http from 'http';
import { URL } from 'url';
import { exec } from 'child_process';
import { promisify } from 'util';

const execAsync = promisify(exec);

const PORT = process.env.CALLBACK_PORT || 3456;
const ORG_NAME = process.env.ORG_NAME || 'aws-innovate';
const AWS_REGION = process.env.AWS_REGION || 'us-west-2';
const SECRET_PREFIX = process.env.SECRET_PREFIX || 'github-app';

// Track created apps
const createdApps = [];
let expectedApps = [];

// App configurations
const APP_CONFIGS = {
  pm: {
    name: 'ADP Agent PM',
    description: 'Project manager agent for AIDLC orchestration',
    permissions: {
      issues: 'write',
      pull_requests: 'write',
      contents: 'write',
      metadata: 'read',
      actions: 'read',
      repository_projects: 'write',
      organization_projects: 'write'
    },
    events: ['issues', 'issue_comment', 'pull_request']
  },
  dev: {
    name: 'ADP Agent Dev',
    description: 'Developer and architect agents',
    permissions: {
      issues: 'write',
      pull_requests: 'write',
      contents: 'write',
      metadata: 'read',
      actions: 'read'
    },
    events: ['issues', 'issue_comment', 'pull_request']
  },
  ops: {
    name: 'ADP Agent Ops',
    description: 'Reviewer and operations agents',
    permissions: {
      issues: 'write',
      pull_requests: 'write',
      contents: 'write',
      metadata: 'read',
      actions: 'write',
      deployments: 'write'
    },
    events: ['issues', 'issue_comment', 'pull_request', 'deployment']
  }
};

function buildManifest(appType) {
  const config = APP_CONFIGS[appType];
  if (!config) {
    throw new Error(`Unknown app type: ${appType}`);
  }

  return {
    name: config.name,
    description: config.description,
    url: `https://github.com/${ORG_NAME}`,
    hook_attributes: {
      url: 'https://example.com/webhooks',
      active: false
    },
    redirect_url: `http://localhost:${PORT}/callback`,
    public: false,
    default_permissions: config.permissions,
    default_events: config.events
  };
}

async function storeInSecretsManager(appType, appData) {
  const secretName = `${SECRET_PREFIX}-${appType}`;

  const secretValue = JSON.stringify({
    app_id: appData.id.toString(),
    app_slug: appData.slug,
    client_id: appData.client_id,
    client_secret: appData.client_secret,
    pem: appData.pem,
    webhook_secret: appData.webhook_secret,
    created_at: new Date().toISOString()
  });

  try {
    // Try to create the secret
    await execAsync(`aws secretsmanager create-secret \
      --name "${secretName}" \
      --description "GitHub App credentials for ${APP_CONFIGS[appType].name}" \
      --secret-string '${secretValue.replace(/'/g, "'\\''")}' \
      --region ${AWS_REGION}`);
    console.log(`[SUCCESS] Created secret: ${secretName}`);
  } catch (err) {
    if (err.message.includes('ResourceExistsException')) {
      // Update existing secret
      await execAsync(`aws secretsmanager put-secret-value \
        --secret-id "${secretName}" \
        --secret-string '${secretValue.replace(/'/g, "'\\''")}' \
        --region ${AWS_REGION}`);
      console.log(`[SUCCESS] Updated secret: ${secretName}`);
    } else {
      throw err;
    }
  }

  return secretName;
}

async function exchangeCodeForCredentials(code) {
  const response = await fetch(
    `https://api.github.com/app-manifests/${code}/conversions`,
    { method: 'POST' }
  );

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`GitHub API error: ${response.status} - ${text}`);
  }

  return response.json();
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Serve the creation page
  if (url.pathname === '/' || url.pathname === '/create') {
    const appsParam = url.searchParams.get('apps') || 'pm,dev,ops';
    expectedApps = appsParam.split(',').map(a => a.trim());

    const html = `
<!DOCTYPE html>
<html>
<head>
  <title>Create GitHub Apps</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
    h1 { color: #24292e; }
    .app-card { border: 1px solid #e1e4e8; border-radius: 6px; padding: 20px; margin: 20px 0; }
    .app-card h2 { margin-top: 0; color: #0366d6; }
    .app-card p { color: #586069; }
    .permissions { background: #f6f8fa; padding: 10px; border-radius: 4px; font-family: monospace; font-size: 12px; }
    button { background: #2ea44f; color: white; border: none; padding: 12px 24px; border-radius: 6px; cursor: pointer; font-size: 16px; margin-right: 10px; }
    button:hover { background: #22863a; }
    button:disabled { background: #94d3a2; cursor: not-allowed; }
    .created { border-color: #2ea44f; background: #f0fff4; }
    .created h2 { color: #22863a; }
    .status { margin-top: 20px; padding: 15px; border-radius: 6px; }
    .status.success { background: #dcffe4; border: 1px solid #34d058; }
    .status.pending { background: #fff5b1; border: 1px solid #f9c513; }
  </style>
</head>
<body>
  <h1>Create GitHub Apps for ADP</h1>
  <p>Organization: <strong>${ORG_NAME}</strong></p>
  <p>Each app gets its own API rate limit (5,000 requests/hour).</p>

  <div id="apps">
    ${expectedApps.map(appType => {
      const config = APP_CONFIGS[appType];
      const manifest = buildManifest(appType);
      return `
      <div class="app-card" id="card-${appType}">
        <h2>${config.name}</h2>
        <p>${config.description}</p>
        <div class="permissions">
          <strong>Permissions:</strong><br>
          ${Object.entries(config.permissions).map(([k, v]) => `${k}: ${v}`).join('<br>')}
        </div>
        <br>
        <form action="https://github.com/organizations/${ORG_NAME}/settings/apps/new" method="post" target="_blank">
          <input type="hidden" name="manifest" value='${JSON.stringify(manifest)}'>
          <button type="submit" id="btn-${appType}">Create ${config.name}</button>
        </form>
      </div>
      `;
    }).join('')}
  </div>

  <div id="status" class="status pending">
    <strong>Status:</strong> Waiting for apps to be created... (${createdApps.length}/${expectedApps.length})
  </div>

  <script>
    // Poll for status updates
    setInterval(async () => {
      const res = await fetch('/status');
      const data = await res.json();

      data.created.forEach(app => {
        const card = document.getElementById('card-' + app);
        if (card && !card.classList.contains('created')) {
          card.classList.add('created');
          document.getElementById('btn-' + app).disabled = true;
          document.getElementById('btn-' + app).textContent = '✓ Created';
        }
      });

      const status = document.getElementById('status');
      if (data.complete) {
        status.className = 'status success';
        status.innerHTML = '<strong>✓ Complete!</strong> All apps created and credentials stored in AWS Secrets Manager.<br><br>You can close this window and check the terminal for next steps.';
      } else {
        status.innerHTML = '<strong>Status:</strong> Waiting for apps to be created... (' + data.created.length + '/' + data.expected.length + ')';
      }
    }, 2000);
  </script>
</body>
</html>
    `;

    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(html);
    return;
  }

  // Status endpoint
  if (url.pathname === '/status') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      expected: expectedApps,
      created: createdApps,
      complete: expectedApps.length > 0 && createdApps.length === expectedApps.length
    }));
    return;
  }

  // Callback from GitHub after app creation
  if (url.pathname === '/callback') {
    const code = url.searchParams.get('code');

    if (!code) {
      res.writeHead(400, { 'Content-Type': 'text/html' });
      res.end('<h1>Error: No code provided</h1>');
      return;
    }

    try {
      console.log('[INFO] Received callback, exchanging code for credentials...');
      const appData = await exchangeCodeForCredentials(code);

      console.log(`[INFO] App created: ${appData.name} (ID: ${appData.id})`);

      // Determine app type from name
      let appType = 'unknown';
      if (appData.name.toLowerCase().includes('pm')) appType = 'pm';
      else if (appData.name.toLowerCase().includes('dev')) appType = 'dev';
      else if (appData.name.toLowerCase().includes('ops')) appType = 'ops';

      // Store in Secrets Manager
      const secretName = await storeInSecretsManager(appType, appData);

      createdApps.push(appType);

      const html = `
<!DOCTYPE html>
<html>
<head>
  <title>App Created Successfully</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; text-align: center; }
    .success { color: #22863a; }
    .info { background: #f6f8fa; padding: 20px; border-radius: 6px; text-align: left; margin: 20px 0; }
    code { background: #e1e4e8; padding: 2px 6px; border-radius: 3px; }
  </style>
</head>
<body>
  <h1 class="success">✓ ${appData.name} Created!</h1>

  <div class="info">
    <p><strong>App ID:</strong> <code>${appData.id}</code></p>
    <p><strong>App Slug:</strong> <code>${appData.slug}</code></p>
    <p><strong>Secret Name:</strong> <code>${secretName}</code></p>
    <p><strong>Region:</strong> <code>${AWS_REGION}</code></p>
  </div>

  <p>Credentials have been stored in AWS Secrets Manager.</p>
  <p>You can close this tab and continue with the next app.</p>

  <script>
    // Auto-close after 3 seconds
    setTimeout(() => {
      window.close();
    }, 3000);
  </script>
</body>
</html>
      `;

      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(html);

      // Check if all apps are created
      if (createdApps.length === expectedApps.length) {
        console.log('\n[SUCCESS] All apps created!');
        console.log('[INFO] Shutting down server in 5 seconds...');
        setTimeout(() => process.exit(0), 5000);
      }

    } catch (err) {
      console.error('[ERROR]', err.message);
      res.writeHead(500, { 'Content-Type': 'text/html' });
      res.end(`<h1>Error</h1><pre>${err.message}</pre>`);
    }
    return;
  }

  // 404
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('Not found');
});

server.listen(PORT, () => {
  console.log(`\n[INFO] Callback server running at http://localhost:${PORT}`);
  console.log(`[INFO] Organization: ${ORG_NAME}`);
  console.log(`[INFO] AWS Region: ${AWS_REGION}`);
  console.log('\n[ACTION] Open your browser to: http://localhost:' + PORT + '/create?apps=' + (process.env.APPS_TO_CREATE || 'pm,dev,ops'));
  console.log('\n[INFO] Click each "Create" button to create the GitHub Apps.');
  console.log('[INFO] Credentials will be automatically stored in AWS Secrets Manager.\n');
});
SERVER_EOF

    echo "$server_dir"
}

# Main execution
main() {
    log INFO "GitHub App Creation Script"
    log INFO "=========================="
    echo ""

    check_prerequisites

    log INFO "Configuration:"
    log INFO "  Organization: $ORG_NAME"
    log INFO "  Apps to create: $APPS_TO_CREATE"
    log INFO "  Callback port: $CALLBACK_PORT"
    log INFO "  AWS Region: $AWS_REGION"
    echo ""

    # Create and start the callback server
    SERVER_DIR=$(create_callback_server)

    log INFO "Starting callback server..."

    # Export environment variables for the server
    export CALLBACK_PORT
    export ORG_NAME
    export AWS_REGION
    export SECRET_PREFIX
    export APPS_TO_CREATE

    # Start the server
    cd "$SERVER_DIR"

    # Open browser automatically (macOS)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        (sleep 2 && open "http://localhost:$CALLBACK_PORT/create?apps=$APPS_TO_CREATE") &
    else
        log INFO "Open your browser to: http://localhost:$CALLBACK_PORT/create?apps=$APPS_TO_CREATE"
    fi

    # Run the server (this will block until all apps are created)
    node server.mjs

    # Cleanup
    rm -rf "$SERVER_DIR"

    echo ""
    log SUCCESS "All done!"
    log INFO ""
    log INFO "Next steps:"
    log INFO "1. Update your GitHub Actions workflows to use the new app credentials:"
    log INFO ""
    log INFO "   # For @agent-pm workflow:"
    log INFO "   - uses: actions/create-github-app-token@v1"
    log INFO "     with:"
    log INFO "       app-id: \${{ secrets.GH_APP_PM_ID }}"
    log INFO "       private-key: \${{ secrets.GH_APP_PM_PRIVATE_KEY }}"
    log INFO ""
    log INFO "2. Copy app IDs and private keys from Secrets Manager to GitHub:"
    log INFO ""
    log INFO "   # Get PM app credentials:"
    log INFO "   aws secretsmanager get-secret-value --secret-id ${SECRET_PREFIX}-pm --region $AWS_REGION | jq -r '.SecretString | fromjson'"
    log INFO ""
    log INFO "3. Add these as repository secrets in GitHub:"
    log INFO "   - GH_APP_PM_ID, GH_APP_PM_PRIVATE_KEY"
    log INFO "   - GH_APP_DEV_ID, GH_APP_DEV_PRIVATE_KEY"
    log INFO "   - GH_APP_OPS_ID, GH_APP_OPS_PRIVATE_KEY"
}

main "$@"
