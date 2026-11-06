#!/usr/bin/env bash
set -euo pipefail

: "${BOX_NAME:?Set BOX_NAME to the name or ID of your own running Dev Box.}"

for tool in adp ssh ssh-keygen aws session-manager-plugin; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Missing laptop prerequisite: %s\n' "$tool" >&2
    exit 2
  fi
done

printf 'This helper uses your existing ADP login and does not create a box.\n'
printf 'Verify the SSH server fingerprint with your operator before continuing.\n'
configured="$(adp devbox ssh-config "$BOX_NAME")"
alias="${configured#SSH host configured: }"
if [[ "$configured" == "$alias" || ! "$alias" =~ ^adp-devbox-[a-z0-9-]+$ ]]; then
  printf 'Could not determine a valid SSH host alias.\n' >&2
  exit 1
fi

printf 'Checking remote user over the pinned alias: %s\n' "$alias"
ssh -o BatchMode=yes "$alias" 'test "$(id -un)" = ec2-user && printf "Dev Box SSH owner shell OK\n"'
printf 'Now opening the interactive shell. Check the box identity, then exit.\n'
adp devbox ssh "$BOX_NAME"
printf 'Interactive SSH exited. Operator: check CloudTrail, Session Manager history,\n'
printf 'the guest SG, non-owner refusal, VS Code, Kiro IDE and Kiro Crew remote mode.\n'
