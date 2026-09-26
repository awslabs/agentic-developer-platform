#!/usr/bin/env bash
set -euo pipefail
NATIVE_WORK=$(mktemp -d /tmp/superplane-native-lane.XXXXXX)
NATIVE_RECEIVED="$NATIVE_WORK/received"
NATIVE_RESULT="$NATIVE_WORK/result"
NATIVE_BUILD_COMPONENT=${CODEBUILD_BUILD_ID##*:}
[[ "$NATIVE_BUILD_COMPONENT" =~ ^[A-Za-z0-9-]+$ ]]
finish() {
  local outcome=$?
  trap - EXIT
  set +e
  # Upload whatever exists, including failed/unknown state. A killed host may not
  # execute this trap; dispatch/child.json remains the independent recovery handle.
  aws s3 cp "$NATIVE_WORK" "s3://${NATIVE_OUTPUT_BUCKET}/builds/${NATIVE_BUILD_COMPONENT}/" \
    --recursive --exclude 'received/download/*' --exclude 'received/source/*' \
    --region "$AWS_REGION"
  local upload_status=$?
  if [ "$upload_status" -ne 0 ]; then
    echo 'Native build evidence upload failed; use dispatcher receipt for reconciliation.' >&2
    exit "$upload_status"
  fi
  exit "$outcome"
}
trap finish EXIT
NATIVE_ENVELOPE=$(printf '%s' "$NATIVE_ENVELOPE_B64" | base64 --decode)
python3 -B modules/domain-apps/superplane/images/native-node/lane/transport.py receive \
  --output "$NATIVE_RECEIVED" --envelope "$NATIVE_ENVELOPE" --bucket "$NATIVE_INPUT_BUCKET" \
  --region "$AWS_REGION" --account-id "$NATIVE_ACCOUNT_ID" --constraints "$NATIVE_CONSTRAINTS"
export NATIVE_STATE_RECEIPT_URI="s3://${NATIVE_OUTPUT_BUCKET}/builds/${NATIVE_BUILD_COMPONENT}/native-start.json"
python3 -B "$NATIVE_RECEIVED/source/modules/domain-apps/superplane/images/native-node/producer.py" \
  --plan "$NATIVE_RECEIVED/download/plan.json" \
  --source-attestation "$NATIVE_RECEIVED/download/source-attestation.json" \
  --inputs "$NATIVE_RECEIVED/download/inputs" \
  --packer "$NATIVE_RECEIVED/download/packer" \
  --amazon-plugin "$NATIVE_RECEIVED/download/amazon_plugin" --output "$NATIVE_RESULT"
