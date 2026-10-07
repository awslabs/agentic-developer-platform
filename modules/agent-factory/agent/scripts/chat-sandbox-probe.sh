#!/usr/bin/env bash
set -euo pipefail

if [[ "${ADP_PROBE_AUTHORIZED:-}" != true || "${ADP_CHAT_DATA_ENABLED:-}" != true || "${ADP_WORKLOAD_TOKEN_FILE:-}" != /var/run/adp-model/token || ! -r /var/run/adp-model/token || ! -x /app/chat-sandbox-entrypoint ]]; then
  printf '%s\n' 'Sandbox probe refused: run only in an authorized, running chat sandbox' >&2
  exit 3
fi

failed=0
blocked=0
record() {
  local name="$1" outcome="$2"
  printf '%s %s\n' "$name" "$outcome"
  [[ "$outcome" != fail ]] || failed=1
  [[ "$outcome" != blocked ]] || blocked=1
}

if env | cut -d= -f1 | grep -Eq '^(AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN|ROLE_ARN|WEB_IDENTITY_TOKEN_FILE|PROFILE|DEFAULT_PROFILE|CONFIG_FILE|SHARED_CREDENTIALS_FILE|CONTAINER_CREDENTIALS_RELATIVE_URI|CONTAINER_CREDENTIALS_FULL_URI|CONTAINER_AUTHORIZATION_TOKEN_FILE)|GITHUB_(APP|TOKEN|PRIVATE_KEY)|GH_TOKEN|VAULT_INTERNAL_API_KEY)$'; then
  record environment fail
else
  record environment pass
fi

proc_access=pass
for environ in /proc/[0-9]*/environ; do
  if [[ ! -r "$environ" ]]; then
    proc_access=blocked
    continue
  fi
  if tr '\0' '\n' < "$environ" 2>/dev/null | cut -d= -f1 | grep -Eq '^(AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN|ROLE_ARN|WEB_IDENTITY_TOKEN_FILE)|GITHUB_(APP|TOKEN|PRIVATE_KEY)|GH_TOKEN|VAULT_INTERNAL_API_KEY)$'; then
    proc_access=fail
    break
  fi
done
record proc "$proc_access"

file_access=pass
for path in /var/run/secrets/kubernetes.io/serviceaccount/token /var/run/secrets/eks.amazonaws.com/serviceaccount/token /var/run/secrets/pods.eks.amazonaws.com/serviceaccount/eks-pod-identity-token /root/.aws/credentials /home/agent/.aws/credentials /app/.env; do
  if [[ -r "$path" ]]; then
    file_access=fail
  fi
done
record projected_files "$file_access"
if (( failed )); then exit 1; fi

cli_error="$(mktemp)"
sdk_report="$(mktemp)"
model_response="$(mktemp)"
trap 'rm -f "$cli_error" "$sdk_report" "$model_response"' EXIT
if command -v aws >/dev/null 2>&1; then
  for service in sts s3api dynamodb secretsmanager bedrock-runtime; do
    case "$service" in
      sts) arguments=(get-caller-identity) ;;
      s3api) arguments=(list-buckets) ;;
      dynamodb) arguments=(list-tables --max-items 1) ;;
      secretsmanager) arguments=(list-secrets --max-results 1) ;;
      bedrock-runtime) arguments=(invoke-model --model-id probe.invalid --body e30= "$model_response") ;;
    esac
    if timeout 8 aws --region "${AWS_REGION:-us-east-1}" "$service" "${arguments[@]}" >/dev/null 2>"$cli_error"; then
      record "cli_$service" fail
      exit 1
    elif [[ "$?" == 124 ]]; then
      record "cli_$service" blocked
    elif grep -Eqi 'Unable to locate credentials|AccessDenied|InvalidClientTokenId|UnrecognizedClient|ExpiredToken|NoCredentials' "$cli_error"; then
      record "cli_$service" pass
    else
      record "cli_$service" blocked
    fi
  done
else
  for service in sts s3api dynamodb secretsmanager bedrock-runtime; do record "cli_$service" blocked; done
fi

if timeout 60 node >"$sdk_report" 2>/dev/null <<'NODE'
const denied = error => /CredentialsProviderError|AccessDenied|UnrecognizedClient|InvalidClientToken|ExpiredToken|NoCredentials|Unauthorized/.test(error?.name || '') ? 'pass' : 'blocked';
const loadSdk = require('node:module').createRequire('/app/chat-sandbox-probe');
const modules = [
  ['sts', '@aws-sdk/client-sts', 'STSClient', 'GetCallerIdentityCommand', {}],
  ['s3', '@aws-sdk/client-s3', 'S3Client', 'ListBucketsCommand', {}],
  ['dynamodb', '@aws-sdk/client-dynamodb', 'DynamoDBClient', 'ListTablesCommand', { Limit: 1 }],
  ['secrets', '@aws-sdk/client-secrets-manager', 'SecretsManagerClient', 'ListSecretsCommand', { MaxResults: 1 }],
  ['bedrock', '@aws-sdk/client-bedrock-runtime', 'BedrockRuntimeClient', 'InvokeModelCommand', { modelId: 'probe.invalid', body: Buffer.from('{}') }],
];
(async () => {
  try {
    const credentials = loadSdk('@aws-sdk/credential-provider-node').defaultProvider();
    const result = await Promise.race([credentials().then(() => 'fail', denied), new Promise(resolve => setTimeout(() => resolve('blocked'), 8000))]);
    console.log('sdk_provider', result);
    if (result === 'fail') {
      for (const [name] of modules) console.log('sdk_' + name, 'blocked');
      return;
    }
  } catch {
    console.log('sdk_provider blocked');
  }
  for (const [name, moduleName, clientName, commandName, input] of modules) {
    try {
      const sdk = loadSdk(moduleName);
      const client = new sdk[clientName]({ region: process.env.AWS_REGION || 'us-east-1', maxAttempts: 1, requestHandler: undefined });
      const outcome = await Promise.race([
        client.send(new sdk[commandName](input), { abortSignal: AbortSignal.timeout(7000) }).then(() => 'fail', denied),
        new Promise(resolve => setTimeout(() => resolve('blocked'), 8000)),
      ]);
      console.log('sdk_' + name, outcome);
      client.destroy();
    } catch {
      console.log('sdk_' + name, 'blocked');
    }
  }
})().catch(() => { console.log('sdk_probe blocked'); process.exitCode = 2; });
NODE
then
  while read -r name outcome; do
    case "$name:$outcome" in
      sdk_*:pass|sdk_*:fail|sdk_*:blocked) record "$name" "$outcome" ;;
      *) record sdk_probe blocked ;;
    esac
  done < "$sdk_report"
else
  record sdk_probe blocked
fi

for destination in http://169.254.169.254/latest/meta-data/iam/security-credentials https://kubernetes.default.svc/version; do
  case "$destination" in
    http://169.254.169.254/*) name=metadata ;;
    *) name=kubernetes_api ;;
  esac
  if ! command -v curl >/dev/null 2>&1; then
    record "$name" blocked
  elif timeout 5 curl --noproxy '*' -ksS --connect-timeout 2 -o /dev/null "$destination" >/dev/null 2>&1; then
    record "$name" fail
  elif [[ "$?" == 124 ]]; then
    record "$name" blocked
  else
    record "$name" pass
  fi
done

if [[ -z "${ADP_PROBE_DENIED_URL:-}" ]] || ! command -v curl >/dev/null 2>&1; then
  record denied_route blocked
elif timeout 5 curl --noproxy '*' -ksS --connect-timeout 2 -o /dev/null "$ADP_PROBE_DENIED_URL" >/dev/null 2>&1; then
  record denied_route fail
elif [[ "$?" == 124 ]]; then
  record denied_route blocked
else
  record denied_route pass
fi

if (( failed )); then exit 1; fi
if (( blocked )); then exit 2; fi
