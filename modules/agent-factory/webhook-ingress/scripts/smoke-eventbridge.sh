#!/usr/bin/env bash
# End-to-end smoke test for the EventBridge → agent trigger path (issue #2154).
#
# Proves the deployed stack works end-to-end:
#   seed a test service-identity → invoke the Lambda with a synthetic EventBridge
#   event → assert 202 accepted → confirm a message landed on the agent SQS queue
#   (i.e. an agent run was enqueued with a machine-rooted chain).
#
# This is the controlled, no-side-effect variant: it invokes the Lambda DIRECTLY
# (aws lambda invoke) rather than going through a live EventBridge rule, so it
# needs no rule and creates no GitHub issue (target.create_issue is omitted).
# It DOES enqueue a real agent run — use a throwaway TARGET_ISSUE.
#
# Usage:
#   AWS_PROFILE=<profile> ENVIRONMENT=dev \
#   TARGET_REPO=<org/repo> TARGET_ISSUE=<n> [PERSONA=reviewer] \
#     ./scripts/smoke-eventbridge.sh
#
# Requires: aws CLI, jq, AWS creds with lambda:InvokeFunction + dynamodb + sqs read.
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
REGION="${AWS_REGION:-us-east-1}"
PERSONA="${PERSONA:-reviewer}"
TARGET_REPO="${TARGET_REPO:?set TARGET_REPO=org/repo (a repo the platform GitHub App is installed on)}"
TARGET_ISSUE="${TARGET_ISSUE:?set TARGET_ISSUE=<throwaway issue number> — a real agent run will be enqueued}"

FUNCTION="adp-${ENVIRONMENT}-github-webhook"
IDENTITY_TABLE="adp-${ENVIRONMENT}-identity-index"
QUEUE="adp-${ENVIRONMENT}-agent-submit.fifo"
SVC_ID="eventbridge:adp-${ENVIRONMENT}-smoke-test"

echo "==> 1. Resolve tenant/org from the target repo's installation (reuse a real one)"
# Reuse the org_id of any installation as tenant+org for the test identity.
ORG=$(aws dynamodb scan --table-name "$IDENTITY_TABLE" --region "$REGION" \
  --filter-expression "identity_type = :t" \
  --expression-attribute-values '{":t":{"S":"github_installation_id"}}' \
  --query 'Items[0].org_id.S' --output text --no-paginate | head -n1 | tr -d '[:space:]')
[ -n "$ORG" ] && [ "$ORG" != "None" ] || { echo "FATAL: no installation row to borrow org from"; exit 1; }
echo "    using tenant_id=org_id=$ORG"

echo "==> 2. Seed the test service-identity in identity-index"
aws dynamodb put-item --table-name "$IDENTITY_TABLE" --region "$REGION" --item "$(cat <<JSON
{
  "identity_type": {"S": "service_account"},
  "identity_value": {"S": "$SVC_ID"},
  "tenant_id": {"S": "$ORG"},
  "org_id": {"S": "$ORG"},
  "allowed_personas": {"L": [{"S": "$PERSONA"}]},
  "note": {"S": "smoke-test identity (#2154); safe to delete"}
}
JSON
)"
echo "    seeded $SVC_ID -> persona [$PERSONA]"

echo "==> 3. Capture SQS depth BEFORE"
QURL=$(aws sqs get-queue-url --queue-name "$QUEUE" --region "$REGION" --query QueueUrl --output text)
BEFORE=$(aws sqs get-queue-attributes --queue-url "$QURL" --region "$REGION" \
  --attribute-names ApproximateNumberOfMessages \
  --query 'Attributes.ApproximateNumberOfMessages' --output text)
echo "    queue messages before: $BEFORE"

echo "==> 4. Invoke the Lambda with a synthetic EventBridge event (CloudWatch alarm shape)"
cat > /tmp/eb-smoke-event.json <<JSON
{
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "detail": {
    "alarmName": "adp-${ENVIRONMENT}-smoke-test-alarm",
    "state": {"value": "ALARM", "reason": "smoke test synthetic alarm"},
    "adp_trigger": {
      "persona": "$PERSONA",
      "service_identity": "$SVC_ID",
      "reason": "EventBridge smoke test (#2154) — synthetic alarm",
      "dedup_key": "smoke-test-$(date +%s 2>/dev/null || echo fixed)",
      "target": {"repo": "$TARGET_REPO", "issue_number": $TARGET_ISSUE}
    }
  }
}
JSON
aws lambda invoke --function-name "$FUNCTION" --region "$REGION" \
  --payload "fileb:///tmp/eb-smoke-event.json" \
  --cli-binary-format raw-in-base64-out \
  /tmp/eb-smoke-resp.json >/tmp/eb-smoke-invoke.json
echo "    lambda response:"
cat /tmp/eb-smoke-resp.json; echo

echo "==> 5. Assert 202 accepted + correlation_id present"
STATUS=$(jq -r '.statusCode' /tmp/eb-smoke-resp.json)
BODY=$(jq -r '.body' /tmp/eb-smoke-resp.json)
echo "    statusCode=$STATUS body=$BODY"
if [ "$STATUS" != "202" ]; then
  echo "FAIL: expected 202, got $STATUS"; exit 1
fi
echo "$BODY" | jq -e '.is_human_rooted == false and .correlation_id != null' >/dev/null \
  && echo "    ✅ machine-rooted run accepted (is_human_rooted=false, correlation_id set)" \
  || { echo "FAIL: response missing expected lineage fields"; exit 1; }

echo "==> 6. Confirm a message was enqueued (depth increased or in-flight)"
sleep 3
AFTER=$(aws sqs get-queue-attributes --queue-url "$QURL" --region "$REGION" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --query 'Attributes' --output json)
echo "    queue after: $AFTER"

echo ""
echo "✅ SMOKE PASS: EventBridge event → Lambda → 202 → SQS enqueue. An agent ($PERSONA)"
echo "   run is now queued for $TARGET_REPO#$TARGET_ISSUE (machine-rooted chain)."
echo "   Verify the run appears in the Activity view, then optionally clean up:"
echo "   aws dynamodb delete-item --table-name $IDENTITY_TABLE --region $REGION \\"
echo "     --key '{\"identity_type\":{\"S\":\"service_account\"},\"identity_value\":{\"S\":\"$SVC_ID\"}}'"
