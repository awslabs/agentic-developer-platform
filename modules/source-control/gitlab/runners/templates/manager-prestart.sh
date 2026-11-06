#!/bin/bash
set -euo pipefail
umask 077
/entrypoint verify --url= >/dev/null 2>&1
nonce=$(cat /proc/sys/kernel/random/uuid)
printf '%s\n' "$nonce" > /runner-guard/request.next
mv /runner-guard/request.next /runner-guard/request
deadline=$(($(date +%s) + 300))
while (( $(date +%s) < deadline )); do
  if [[ -f /runner-guard/ack ]] && [[ "$(cat /runner-guard/ack)" == "$nonce" ]]; then
    manager_pid=$PPID
    (
      trap 'kill -TERM "$manager_pid" 2>/dev/null || true' EXIT
      while true; do
        touch /runner-guard/alive.next
        mv /runner-guard/alive.next /runner-guard/alive
        if [[ ! -f /runner-guard/lease ]] || (( $(date +%s) - $(stat -c %Y /runner-guard/lease) > 60 )); then
          kill -TERM "$manager_pid"
          exit 1
        fi
        sleep 10
      done
    ) </dev/null >/dev/null 2>&1 &
    exit 0
  fi
  sleep 1
done
exit 1
